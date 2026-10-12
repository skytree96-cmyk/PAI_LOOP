"""Operator-kept award candidates, separate from verified company performance.

The company-award lookup itself stays read-only.  An operator may keep one
award row as a *performance candidate* so the bid team can later request a
performance certificate.  These rows live in their own table and are never read
by quantitative scoring or the company performance screen.
"""

from __future__ import annotations

import math
import unicodedata
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .company_awards import CompanyAwardScope
from .integrations.company_awards import DEFAULT_COMPANY_NAME
from .integrations.pps import KST
from .manual_analysis import _manual_feature_enabled, _same_origin_request
from .models import AwardPerformanceCandidate

router = APIRouter(prefix="/api/v1", tags=["award performance candidates"])

_MAX_LIST = 500
_STATUS = Literal["CANDIDATE", "DISMISSED"]


class AwardCandidateCreateRequest(BaseModel):
    """One row from the company-award search response, as shown on screen."""

    model_config = ConfigDict(extra="ignore")

    scope: CompanyAwardScope
    bid_notice_no: str = Field(min_length=1, max_length=64)
    revision_no: str = Field(default="", max_length=16)
    classification_no: str = Field(default="", max_length=16)
    rebid_no: str = Field(default="", max_length=16)
    title: str = Field(min_length=1, max_length=500)
    agency: str = Field(default="", max_length=300)
    winner_name: str = Field(min_length=1, max_length=300)
    award_amount: float | None = Field(default=None, ge=0)
    award_rate: float | None = Field(default=None, ge=0, le=1000)
    participant_count: int | None = Field(default=None, ge=0)
    opened_at: datetime | None = None
    awarded_at: datetime | None = None


class AwardCandidateStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: _STATUS


class AwardCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_key: str
    candidate_status: str
    scope: str
    bid_notice_no: str
    revision_no: str
    classification_no: str
    rebid_no: str
    title: str
    agency: str
    winner_name: str
    award_amount_krw: int | None
    award_rate: float | None
    participant_count: int | None
    opened_at: datetime | None
    awarded_at: datetime | None
    source: str
    revision: int
    created_by: str
    updated_by: str
    created_at: datetime | None
    updated_at: datetime | None


class AwardCandidateMutationOut(BaseModel):
    created: bool
    candidate: AwardCandidateOut


class AwardCandidateListOut(BaseModel):
    items: list[AwardCandidateOut]
    count: int
    counts: dict[str, int]


def _actor(request: Request, *, mutation: bool) -> tuple[str | None, str]:
    if not _manual_feature_enabled(request):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "낙찰 실적 후보 기능이 비활성화되어 있습니다.")
    if mutation and not _same_origin_request(request):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "홈페이지와 동일한 출처에서만 낙찰 실적 후보를 저장할 수 있습니다.",
        )
    settings = request.app.state.settings
    if request.headers.get("x-pai-manual-token"):
        raise HTTPException(401, "부서 계정으로 다시 로그인해 주세요.")
    if settings.department_accounts_enabled:
        from .accounts import authenticated_account

        # Saving copies an already shown public award; no external API or
        # paid analysis runs, so the paid-analysis permission is not required.
        identity = authenticated_account(request, mutation=mutation)
        return identity.id, identity.actor_label
    if settings.environment.casefold() != "production":
        return None, "로컬 개발"
    raise HTTPException(401, "부서 계정 로그인이 필요합니다.")


def _compact(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def _is_default_company(winner_name: str) -> bool:
    name = _compact(winner_name)
    return bool(name) and (name == _compact(DEFAULT_COMPANY_NAME) or name == "한국능률협회")


def _as_kst(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # PPS timestamps are local (KST) wall-clock values without an offset.
    return value.replace(tzinfo=KST) if value.tzinfo is None else value


def _candidate_key(payload: AwardCandidateCreateRequest) -> str:
    parts = (
        payload.scope.value,
        payload.bid_notice_no.strip(),
        payload.revision_no.strip(),
        payload.classification_no.strip(),
        payload.rebid_no.strip(),
    )
    return "award:" + "|".join(parts)


def _counts(session) -> dict[str, int]:
    rows = session.execute(select(AwardPerformanceCandidate.candidate_status)).scalars().all()
    counts = {"CANDIDATE": 0, "DISMISSED": 0}
    for value in rows:
        counts[value] = counts.get(value, 0) + 1
    return counts


@router.get("/award-performance-candidates", response_model=AwardCandidateListOut)
def list_award_candidates(
    request: Request,
    candidate_status: _STATUS | None = Query(default="CANDIDATE", alias="status"),
) -> AwardCandidateListOut:
    _actor(request, mutation=False)
    with request.app.state.session_factory() as session:
        query = select(AwardPerformanceCandidate)
        if candidate_status is not None:
            query = query.where(AwardPerformanceCandidate.candidate_status == candidate_status)
        query = query.order_by(
            AwardPerformanceCandidate.opened_at.desc(),
            AwardPerformanceCandidate.bid_notice_no,
        ).limit(_MAX_LIST)
        items = [AwardCandidateOut.model_validate(row) for row in session.scalars(query)]
        return AwardCandidateListOut(items=items, count=len(items), counts=_counts(session))


@router.post(
    "/award-performance-candidates",
    response_model=AwardCandidateMutationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_award_candidate(
    payload: AwardCandidateCreateRequest,
    request: Request,
    response: Response,
) -> AwardCandidateMutationOut:
    account_id, actor = _actor(request, mutation=True)
    if not _is_default_company(payload.winner_name):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "한국능률협회 낙찰 건만 실적 후보로 저장할 수 있습니다.",
        )
    amount = payload.award_amount
    if amount is not None and not math.isfinite(amount):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "낙찰금액을 확인해 주세요.")
    key = _candidate_key(payload)
    values = {
        "scope": payload.scope.value,
        "bid_notice_no": payload.bid_notice_no.strip(),
        "revision_no": payload.revision_no.strip(),
        "classification_no": payload.classification_no.strip(),
        "rebid_no": payload.rebid_no.strip(),
        "title": payload.title.strip(),
        "agency": payload.agency.strip(),
        "winner_name": payload.winner_name.strip(),
        "award_amount_krw": None if amount is None else int(round(amount)),
        "award_rate": payload.award_rate,
        "participant_count": payload.participant_count,
        "opened_at": _as_kst(payload.opened_at),
        "awarded_at": _as_kst(payload.awarded_at),
    }
    with request.app.state.session_factory() as session:
        existing = session.scalar(
            select(AwardPerformanceCandidate).where(AwardPerformanceCandidate.candidate_key == key)
        )
        if existing is not None:
            # Saving the same award again is idempotent; a dismissed row is
            # restored instead of duplicated.
            if existing.candidate_status != "CANDIDATE":
                existing.candidate_status = "CANDIDATE"
                existing.updated_by = actor
                existing.revision += 1
                session.commit()
                session.refresh(existing)
            response.status_code = status.HTTP_200_OK
            return AwardCandidateMutationOut(
                created=False, candidate=AwardCandidateOut.model_validate(existing)
            )
        item = AwardPerformanceCandidate(
            candidate_key=key,
            created_by_account_id=account_id,
            created_by=actor,
            updated_by=actor,
            **values,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return AwardCandidateMutationOut(created=True, candidate=AwardCandidateOut.model_validate(item))


@router.patch("/award-performance-candidates/{candidate_id}", response_model=AwardCandidateOut)
def update_award_candidate_status(
    candidate_id: str,
    payload: AwardCandidateStatusRequest,
    request: Request,
) -> AwardCandidateOut:
    _account_id, actor = _actor(request, mutation=True)
    with request.app.state.session_factory() as session:
        item = session.get(AwardPerformanceCandidate, candidate_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "낙찰 실적 후보를 찾지 못했습니다.")
        if item.candidate_status != payload.status:
            item.candidate_status = payload.status
            item.updated_by = actor
            item.revision += 1
            session.commit()
            session.refresh(item)
        return AwardCandidateOut.model_validate(item)
