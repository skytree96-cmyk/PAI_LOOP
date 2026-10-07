"""Per-notice answers a person gives to an eligibility question the profile cannot settle (2026-10-07).

Today the only question is whether the notice's nonprofit exception covers the
company (``NONPROFIT_EXCEPTION_APPLIES``). An answer is bound to the PPS metadata
version it was given against: an amended notice asks again. Answers never call
a model; recording one re-runs the free analysis pipeline for that notice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .accounts import Identity, authenticated_account, enabled
from .auth import require_api_key
from .eligibility_policy import NONPROFIT_CONFIRMATION_QUESTION_KEY
from .models import EligibilityConfirmation, Notice, NoticeVersion
from .notice_freshness import _latest_pps_metadata_version

router = APIRouter(prefix="/api/v1/notices", tags=["eligibility confirmations"])
QUESTION_KEYS = frozenset({NONPROFIT_CONFIRMATION_QUESTION_KEY})


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


class ConfirmationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_key: Literal["NONPROFIT_EXCEPTION_APPLIES"]
    answer: Literal["YES", "NO"]


def _basis_version_id(notice: Notice) -> str | None:
    metadata = _latest_pps_metadata_version(notice)
    return metadata.id if metadata is not None else None


def current_confirmations(session: Session, notice: Notice) -> dict[str, str]:
    """Answers still valid for the notice's current PPS metadata version."""
    basis = _basis_version_id(notice)
    rows = session.scalars(
        select(EligibilityConfirmation)
        .where(EligibilityConfirmation.notice_id == notice.id, EligibilityConfirmation.revoked_at.is_(None))
        .order_by(EligibilityConfirmation.created_at)
    ).all()
    answers: dict[str, str] = {}
    for row in rows:
        if row.basis_version_id == basis and row.question_key in QUESTION_KEYS:
            answers[row.question_key] = row.answer
    return answers


def confirmation_records(session: Session, notice: Notice) -> list[dict]:
    basis = _basis_version_id(notice)
    rows = session.scalars(
        select(EligibilityConfirmation)
        .where(EligibilityConfirmation.notice_id == notice.id, EligibilityConfirmation.revoked_at.is_(None))
        .order_by(EligibilityConfirmation.created_at)
    ).all()
    return [
        {"question_key": row.question_key, "answer": row.answer, "confirmed_by": row.actor_label,
         "confirmed_at": row.created_at, "current": row.basis_version_id == basis}
        for row in rows
    ]


def _access(request: Request) -> Identity | None:
    if request.headers.get("x-pai-loop-api-key"):
        require_api_key(request)
        return None
    if enabled(request):
        return authenticated_account(request, mutation=True, department_write=True)
    require_api_key(request)
    return None


def _load(session: Session, notice_key: str) -> Notice:
    notice = session.scalar(
        select(Notice).where(Notice.notice_key == notice_key).options(selectinload(Notice.versions))
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return notice


def _rerun(request: Request, notice_id: str) -> str | None:
    from .analysis_pipeline import run_analysis_pipeline

    with request.app.state.session_factory() as fresh:
        result = run_analysis_pipeline(fresh, notice_id=notice_id)
        return result.eligibility


@router.post("/{notice_key}/eligibility-confirmations", status_code=status.HTTP_201_CREATED)
def record_confirmation(notice_key: str, payload: ConfirmationCreate, request: Request, session: DbSession) -> dict:
    identity = _access(request)
    notice = _load(session, notice_key)
    if notice.status != "OPEN":
        raise HTTPException(status_code=409, detail="진행 중인 공고만 확인할 수 있습니다.")
    now = datetime.now(timezone.utc)
    for row in session.scalars(select(EligibilityConfirmation).where(
            EligibilityConfirmation.notice_id == notice.id,
            EligibilityConfirmation.question_key == payload.question_key,
            EligibilityConfirmation.revoked_at.is_(None))):
        row.revoked_at = now
    session.add(EligibilityConfirmation(
        notice_id=notice.id, notice_key=notice.notice_key, question_key=payload.question_key,
        answer=payload.answer, basis_version_id=_basis_version_id(notice),
        actor_account_id=identity.id if identity else None,
        actor_label=f"{identity.actor_label} {identity.username}" if identity else "서버",
    ))
    notice_id = notice.id
    session.commit()
    eligibility = _rerun(request, notice_id)
    return {"question_key": payload.question_key, "answer": payload.answer, "eligibility": eligibility}


@router.delete("/{notice_key}/eligibility-confirmations/{question_key}")
def revoke_confirmation(notice_key: str, question_key: str, request: Request, session: DbSession) -> dict:
    _access(request)
    if question_key not in QUESTION_KEYS:
        raise HTTPException(status_code=422, detail="알 수 없는 확인 항목입니다.")
    notice = _load(session, notice_key)
    now = datetime.now(timezone.utc)
    revoked = 0
    for row in session.scalars(select(EligibilityConfirmation).where(
            EligibilityConfirmation.notice_id == notice.id,
            EligibilityConfirmation.question_key == question_key,
            EligibilityConfirmation.revoked_at.is_(None))):
        row.revoked_at = now
        revoked += 1
    notice_id = notice.id
    session.commit()
    eligibility = _rerun(request, notice_id) if revoked else None
    return {"question_key": question_key, "revoked": revoked, "eligibility": eligibility}
