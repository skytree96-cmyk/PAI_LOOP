from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from .manual_analysis import _source_kind
from .models import Evaluation, Notice, UserDecision
from .notice_freshness import (
    authoritative_pps_notice_is_cancelled,
    latest_current_evaluation,
)
from .pps_enrichment import public_analysis_reason
from .schemas import DecisionCreate


DECISION_SNAPSHOT_VERSION = "operator-decision-snapshot-v1"
EVALUATED_SNAPSHOT_STATE = "EVALUATED"
NOT_EVALUATED_SNAPSHOT_STATE = "NOT_EVALUATED"


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


def _comparable_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _analysis_snapshot(
    notice: Notice,
    evaluation: Evaluation | None,
    *,
    captured_at: datetime,
) -> tuple[str, dict[str, Any]]:
    """Record what the server itself observed when this decision was stored.

    Nothing here comes from the client. The analysis state reuses the existing
    public reason classifier, so a decision saved before analysis, after an
    incomplete extraction, or after the deadline carries the same truthful
    state the notice screen shows, and never a fabricated evaluation. Only
    fields already published by ``EvaluationOut`` are copied, so the snapshot
    adds no raw payload, digest, provider metadata or private evidence.
    """

    reason = public_analysis_reason(
        notice.versions,
        evaluated=bool(notice.evaluations),
        source_kind=_source_kind(notice),
    )
    deadline = _comparable_utc(notice.deadline)
    state = (
        EVALUATED_SNAPSHOT_STATE if evaluation is not None else NOT_EVALUATED_SNAPSHOT_STATE
    )
    snapshot: dict[str, Any] = {
        "snapshot_version": DECISION_SNAPSHOT_VERSION,
        "captured_at": captured_at.isoformat(),
        "analysis_state": state,
        "analysis_reason_state": reason.state,
        "analysis_reason_code": reason.reason_code,
        "notice_status": str(notice.status or "").upper(),
        "deadline": deadline.isoformat() if deadline else None,
        "deadline_passed": bool(deadline and deadline < captured_at),
        "evaluation": None,
    }
    if evaluation is not None:
        evaluated_at = _comparable_utc(evaluation.evaluated_at)
        snapshot["evaluation"] = {
            "id": evaluation.id,
            "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
            "eligibility": evaluation.eligibility,
            "reason_code": evaluation.reason_code,
            "readiness_status": evaluation.readiness_status,
            "readiness_score": evaluation.readiness_score,
            "risk_band": evaluation.risk_band,
            "ruleset_version": evaluation.ruleset_version,
        }
    return state, snapshot


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
    """Insert one human decision against an atomic view of the current analysis.

    The decision belongs to the person, not to the analysis, so an absent
    evaluation is a recordable state rather than a rejection: a notice may not
    be analysed yet, its extraction may have failed, or its deadline may have
    passed, and the operator still has to answer for participating or not.

    The two defenses that matter are unchanged. A submitted ``evaluation_id``
    must belong to this notice and must still be the current one, and the
    identifier stays mandatory whenever a current evaluation exists, so a
    client cannot skip the stale-evaluation check by omitting it.
    """

    _begin_current_evaluation_snapshot(session)
    try:
        notice = _load_notice(session, notice_key)
        if authoritative_pps_notice_is_cancelled(session, notice):
            raise HTTPException(
                status_code=409,
                detail="조달청에서 취소된 공고이므로 새 담당자 결정을 기록할 수 없습니다.",
            )

        latest = latest_current_evaluation(notice)
        evaluation: Evaluation | None
        if payload.evaluation_id:
            evaluation = session.get(Evaluation, payload.evaluation_id)
            if evaluation is None or evaluation.notice_id != notice.id:
                raise HTTPException(
                    status_code=422,
                    detail="이 공고의 evaluation_id가 아닙니다.",
                )
            _require_same_current_evaluation(notice, evaluation.id)
        elif latest is not None and require_explicit_evaluation_id:
            raise HTTPException(
                status_code=422,
                detail="화면에서 확인한 evaluation_id가 필요합니다.",
            )
        else:
            evaluation = latest

        if evaluation is None and not payload.rationale.strip():
            raise HTTPException(
                status_code=422,
                detail="분석이 완료되지 않은 상태의 담당자 판단은 판단 사유가 필요합니다.",
            )

        captured_at = datetime.now(timezone.utc)
        state, snapshot = _analysis_snapshot(notice, evaluation, captured_at=captured_at)
        decision = UserDecision(
            notice_id=notice.id,
            evaluation_id=evaluation.id if evaluation is not None else None,
            analysis_state_snapshot=state,
            analysis_snapshot=snapshot,
            **payload.model_dump(exclude={"evaluation_id"}),
        )
        session.add(decision)
        session.flush()

        # Keep an explicit final guard next to the INSERT. The database lock
        # makes the snapshot atomic; the refresh also fails closed if a future
        # writer is introduced without taking the expected evaluation lock.
        # A decision recorded with no current evaluation has nothing to pin,
        # so it is re-checked the other way: an evaluation that appears during
        # this transaction must not be silently adopted as its basis.
        refreshed_notice = _load_notice(session, notice_key, refresh=True)
        if evaluation is not None:
            _require_same_current_evaluation(refreshed_notice, evaluation.id)
        elif latest_current_evaluation(refreshed_notice) is not None:
            raise HTTPException(
                status_code=409,
                detail="공고 평가가 갱신되었습니다. 최신 분석을 다시 확인한 뒤 판단해 주세요.",
            )

        session.commit()
        session.refresh(decision)
        return decision
    except Exception:
        session.rollback()
        raise
