from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from .models import Evaluation, Notice, UserDecision
from .notice_freshness import (
    authoritative_pps_notice_is_cancelled,
    latest_current_evaluation,
)
from .schemas import DecisionCreate


def _begin_current_evaluation_snapshot(session: Session) -> None:
    """Prevent an evaluation insert from crossing decision validation.

    PostgreSQL's SHARE table lock is compatible with other decision readers,
    but conflicts with the ROW EXCLUSIVE lock taken by every evaluation
    insert. SQLite's IMMEDIATE transaction reserves the database writer before
    the first read. In both supported deployments, an evaluation either
    commits before this snapshot and is observed, or waits until the decision
    commits.
    """

    if session.in_transaction():
        raise RuntimeError(
            "current-evaluation decision persistence requires a clean Session"
        )
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("LOCK TABLE evaluations IN SHARE MODE"))
        return
    if dialect == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
        return
    raise RuntimeError(f"unsupported decision transaction dialect: {dialect}")


def _load_notice(
    session: Session,
    notice_key: str,
    *,
    refresh: bool = False,
) -> Notice:
    statement = (
        select(Notice)
        .where(Notice.notice_key == notice_key)
        .options(
            selectinload(Notice.versions),
            selectinload(Notice.evaluations),
            selectinload(Notice.decisions),
        )
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    notice = session.scalar(statement)
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return notice


def _require_same_current_evaluation(
    notice: Notice,
    expected_evaluation_id: str,
) -> Evaluation:
    latest = latest_current_evaluation(notice)
    if latest is None:
        raise HTTPException(status_code=422, detail="먼저 공고 평가를 실행해야 합니다.")
    if latest.id != expected_evaluation_id:
        raise HTTPException(
            status_code=409,
            detail="공고 평가가 갱신되었습니다. 최신 분석을 다시 확인한 뒤 판단해 주세요.",
        )
    return latest


def persist_current_evaluation_decision(
    session: Session,
    *,
    notice_key: str,
    payload: DecisionCreate,
    require_explicit_evaluation_id: bool,
) -> UserDecision:
    """Validate and insert one decision against an atomic current snapshot."""

    _begin_current_evaluation_snapshot(session)
    try:
        notice = _load_notice(session, notice_key)
        if authoritative_pps_notice_is_cancelled(session, notice):
            raise HTTPException(
                status_code=409,
                detail="조달청에서 취소된 공고이므로 새 담당자 결정을 기록할 수 없습니다.",
            )

        latest = latest_current_evaluation(notice)
        if latest is None:
            raise HTTPException(status_code=422, detail="먼저 공고 평가를 실행해야 합니다.")
        if require_explicit_evaluation_id and not payload.evaluation_id:
            raise HTTPException(
                status_code=422,
                detail="화면에서 확인한 evaluation_id가 필요합니다.",
            )

        evaluation = latest
        if payload.evaluation_id:
            evaluation = session.get(Evaluation, payload.evaluation_id)
            if evaluation is None or evaluation.notice_id != notice.id:
                raise HTTPException(
                    status_code=422,
                    detail="이 공고의 evaluation_id가 아닙니다.",
                )
            _require_same_current_evaluation(notice, evaluation.id)

        decision = UserDecision(
            notice_id=notice.id,
            evaluation_id=evaluation.id,
            **payload.model_dump(exclude={"evaluation_id"}),
        )
        session.add(decision)
        session.flush()

        # Keep an explicit final guard next to the INSERT. The database lock
        # makes the snapshot atomic; the refresh also fails closed if a future
        # writer is introduced without taking the expected evaluation lock.
        refreshed_notice = _load_notice(session, notice_key, refresh=True)
        _require_same_current_evaluation(refreshed_notice, evaluation.id)

        session.commit()
        session.refresh(decision)
        return decision
    except Exception:
        session.rollback()
        raise
