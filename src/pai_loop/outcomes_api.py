from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import public_read_allowed, require_api_key
from .models import BidOutcome, Evaluation, Notice, UserDecision


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BidOutcomeUpsert(ApiModel):
    outcome_key: str | None = Field(default=None, min_length=1, max_length=180)
    status: Literal["NO_BID", "SUBMITTED", "WON", "LOST", "CANCELLED"]
    evaluation_id: str | None = None
    decision_id: str | None = None
    submitted_bid_amount: float | None = Field(default=None, ge=0)
    submitted_bid_rate: float | None = Field(default=None, ge=0, le=200)
    winning_bid_amount: float | None = Field(default=None, ge=0)
    winning_bid_rate: float | None = Field(default=None, ge=0, le=200)
    technical_score: float | None = Field(default=None, ge=0, le=100)
    price_score: float | None = Field(default=None, ge=0, le=100)
    total_score: float | None = Field(default=None, ge=0, le=200)
    rank: int | None = Field(default=None, ge=1)
    winner_name: str | None = Field(default=None, max_length=255)
    reason_code: str | None = Field(default=None, max_length=80)
    loss_reason: str | None = Field(default=None, max_length=4000)
    risk_summary: dict[str, Any] | None = None
    source: str = Field(default="MANUAL", min_length=1, max_length=48)
    source_reference: str | None = Field(default=None, max_length=1000)
    evidence_json: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def validate_outcome_facts(self) -> "BidOutcomeUpsert":
        if self.status == "NO_BID" and any(
            value is not None
            for value in (
                self.submitted_bid_amount,
                self.submitted_bid_rate,
                self.technical_score,
                self.price_score,
                self.total_score,
                self.rank,
            )
        ):
            raise ValueError("NO_BID에는 투찰 금액·점수·순위를 기록할 수 없습니다.")
        return self


class BidOutcomeOut(ApiModel):
    id: str
    notice_key: str
    outcome_key: str
    status: str
    evaluation_id: str | None
    decision_id: str | None
    submitted_bid_amount: float | None
    submitted_bid_rate: float | None
    winning_bid_amount: float | None
    winning_bid_rate: float | None
    technical_score: float | None
    price_score: float | None
    total_score: float | None
    rank: int | None
    winner_name: str | None
    reason_code: str | None
    loss_reason: str | None
    risk_summary: dict[str, Any] | None
    source: str
    source_reference: str | None
    evidence_json: dict[str, Any]
    occurred_at: datetime | None
    observed_at: datetime
    created_at: datetime
    updated_at: datetime


router = APIRouter(
    prefix="/api/v1/notices/{notice_key}/outcomes",
    tags=["bid outcomes"],
    dependencies=[Depends(require_api_key)],
)


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _notice(session: Session, notice_key: str) -> Notice:
    notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return notice


def _out(item: BidOutcome, *, public_view: bool = False) -> BidOutcomeOut:
    return BidOutcomeOut(
        id=item.id,
        notice_key=item.notice.notice_key,
        outcome_key=item.outcome_key,
        status=item.status,
        evaluation_id=item.evaluation_id,
        decision_id=item.decision_id,
        submitted_bid_amount=item.submitted_bid_amount,
        submitted_bid_rate=item.submitted_bid_rate,
        winning_bid_amount=item.winning_bid_amount,
        winning_bid_rate=item.winning_bid_rate,
        technical_score=item.technical_score,
        price_score=item.price_score,
        total_score=item.total_score,
        rank=item.rank,
        winner_name=item.winner_name,
        reason_code=item.reason_code,
        loss_reason=None if public_view else item.loss_reason,
        risk_summary=None if public_view else item.risk_summary,
        source=item.source,
        source_reference=item.source_reference,
        evidence_json={} if public_view else item.evidence_json,
        occurred_at=item.occurred_at,
        observed_at=item.observed_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("", response_model=list[BidOutcomeOut])
def list_bid_outcomes(
    notice_key: str,
    request: Request,
    session: DbSession,
) -> list[BidOutcomeOut]:
    notice = _notice(session, notice_key)
    rows = list(
        session.scalars(
            select(BidOutcome)
            .where(BidOutcome.notice_id == notice.id)
            .order_by(BidOutcome.observed_at.desc())
        ).all()
    )
    return [_out(row, public_view=public_read_allowed(request)) for row in rows]


@router.post("", response_model=BidOutcomeOut, status_code=status.HTTP_201_CREATED)
def upsert_bid_outcome(
    notice_key: str,
    payload: BidOutcomeUpsert,
    request: Request,
    session: DbSession,
) -> BidOutcomeOut:
    notice = _notice(session, notice_key)
    if payload.evaluation_id:
        evaluation = session.get(Evaluation, payload.evaluation_id)
        if evaluation is None or evaluation.notice_id != notice.id:
            raise HTTPException(status_code=422, detail="이 공고의 evaluation_id가 아닙니다.")
    if payload.decision_id:
        decision = session.get(UserDecision, payload.decision_id)
        if decision is None or decision.notice_id != notice.id:
            raise HTTPException(status_code=422, detail="이 공고의 decision_id가 아닙니다.")

    values = payload.model_dump(exclude={"outcome_key"})
    key = payload.outcome_key or hashlib.sha256(
        "|".join(
            (
                notice.notice_key,
                payload.status,
                payload.source,
                payload.occurred_at.isoformat() if payload.occurred_at else "undated",
                payload.source_reference or "manual",
            )
        ).encode("utf-8")
    ).hexdigest()[:40]
    item = session.scalar(
        select(BidOutcome).where(
            BidOutcome.notice_id == notice.id,
            BidOutcome.outcome_key == key,
        )
    )
    if item is None:
        item = BidOutcome(notice_id=notice.id, outcome_key=key, **values)
        session.add(item)
    else:
        for field, value in values.items():
            setattr(item, field, value)
    session.commit()
    session.refresh(item)
    return _out(item, public_view=public_read_allowed(request))
