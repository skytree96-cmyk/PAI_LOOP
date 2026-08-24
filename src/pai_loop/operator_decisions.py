from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .auth import require_api_key
from .manual_analysis import (
    _manual_feature_enabled,
    _require_manual_operator,
    _same_origin_request,
)
from .models import Evaluation, Notice, UserDecision
from .notice_freshness import (
    authoritative_pps_notice_is_cancelled,
    latest_current_evaluation,
)
from .schemas import DecisionCreate, DecisionOut


router = APIRouter(prefix="/api/v1/operator-decisions", tags=["operator decisions"])


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _operator_access(request: Request, *, mutation: bool) -> None:
    """Allow the server key or the existing, narrowly scoped demo operator PIN."""

    if request.headers.get("x-pai-loop-api-key"):
        require_api_key(request)
        return
    if _manual_feature_enabled(request):
        if mutation and not _same_origin_request(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="홈페이지와 동일한 출처에서만 최종 판단을 저장할 수 있습니다.",
            )
        fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="홈페이지와 동일한 출처에서만 최종 판단을 조회할 수 있습니다.",
            )
        _require_manual_operator(request)
        return
    require_api_key(request)


def _load_notice(session: Session, notice_key: str) -> Notice:
    notice = session.scalar(
        select(Notice)
        .where(Notice.notice_key == notice_key)
        .options(
            selectinload(Notice.versions),
            selectinload(Notice.evaluations),
            selectinload(Notice.decisions),
        )
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return notice


def _latest_evaluation(notice: Notice) -> Evaluation | None:
    return latest_current_evaluation(notice)


@router.get("/notices/{notice_key}", response_model=list[DecisionOut])
def list_operator_decisions(
    notice_key: str,
    request: Request,
    session: DbSession,
) -> list[UserDecision]:
    _operator_access(request, mutation=False)
    notice = _load_notice(session, notice_key)
    return sorted(notice.decisions, key=lambda item: item.created_at)


@router.post(
    "/notices/{notice_key}",
    response_model=DecisionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_operator_decision(
    notice_key: str,
    payload: DecisionCreate,
    request: Request,
    session: DbSession,
) -> UserDecision:
    _operator_access(request, mutation=True)
    notice = _load_notice(session, notice_key)
    if authoritative_pps_notice_is_cancelled(session, notice):
        raise HTTPException(
            status_code=409,
            detail="조달청에서 취소된 공고이므로 새 담당자 결정을 기록할 수 없습니다.",
        )
    latest_evaluation = _latest_evaluation(notice)
    if latest_evaluation is None:
        raise HTTPException(status_code=422, detail="먼저 공고 평가를 실행해야 합니다.")
    if not payload.evaluation_id:
        raise HTTPException(
            status_code=422,
            detail="화면에서 확인한 evaluation_id가 필요합니다.",
        )
    evaluation = session.get(Evaluation, payload.evaluation_id)
    if evaluation is None or evaluation.notice_id != notice.id:
        raise HTTPException(status_code=422, detail="이 공고의 evaluation_id가 아닙니다.")
    if evaluation.id != latest_evaluation.id:
        raise HTTPException(
            status_code=409,
            detail="공고 평가가 갱신되었습니다. 최신 분석을 다시 확인한 뒤 판단해 주세요.",
        )
    decision = UserDecision(
        notice_id=notice.id,
        evaluation_id=evaluation.id,
        **payload.model_dump(exclude={"evaluation_id"}),
    )
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision
