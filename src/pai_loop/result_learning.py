from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload

from .auth import require_api_key
from .manual_analysis import (
    _manual_feature_enabled,
    _require_manual_operator,
    _same_origin_request,
)
from .models import BidOutcome, Notice


OutcomeStatus = Literal["NO_BID", "SUBMITTED", "WON", "LOST", "CANCELLED"]
WorkflowStatus = Literal["DRAFT", "VALIDATED", "ARCHIVED"]
LearningScope = Literal["ENDED", "ALL", "WITH_OUTCOME"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ResultLearningFields(ApiModel):
    record_status: WorkflowStatus = "DRAFT"
    status: OutcomeStatus
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
    source_reference: str | None = Field(default=None, max_length=1000)
    operator_note: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime | None = None

    @field_validator("winner_name", "reason_code", "loss_reason", "source_reference", "operator_note")
    @classmethod
    def normalise_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ResultLearningFields":
        bid_facts = (
            self.submitted_bid_amount,
            self.submitted_bid_rate,
            self.winning_bid_amount,
            self.winning_bid_rate,
            self.technical_score,
            self.price_score,
            self.total_score,
            self.rank,
        )
        if self.status == "NO_BID" and (
            any(value is not None for value in bid_facts) or self.winner_name
        ):
            raise ValueError("미참여 결과에는 투찰·낙찰·점수·순위 정보를 입력할 수 없습니다.")
        if (
            self.technical_score is not None
            and self.price_score is not None
            and self.total_score is not None
            and abs(self.technical_score + self.price_score - self.total_score) > 0.11
        ):
            raise ValueError("기술점수와 가격점수의 합이 총점과 일치하지 않습니다.")
        if self.record_status == "VALIDATED":
            if not self.source_reference:
                raise ValueError("검증 완료 결과에는 출처 또는 근거 참조가 필요합니다.")
            if self.status == "SUBMITTED" and self.submitted_bid_amount is None and self.submitted_bid_rate is None:
                raise ValueError("제출 완료 결과에는 투찰금액 또는 투찰률이 필요합니다.")
            if self.status == "WON" and (
                not self.winner_name
                or (self.winning_bid_amount is None and self.winning_bid_rate is None)
            ):
                raise ValueError("낙찰 결과에는 낙찰자와 낙찰금액 또는 낙찰률이 필요합니다.")
            if self.status == "LOST" and not self.loss_reason:
                raise ValueError("실주 결과에는 실주 사유가 필요합니다.")
        return self


class ResultLearningCreate(ResultLearningFields):
    notice_key: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=180)
    basis_outcome_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("notice_key")
    @classmethod
    def normalise_notice_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("공고 식별자는 비어 있을 수 없습니다.")
        return cleaned

    @field_validator("idempotency_key")
    @classmethod
    def normalise_idempotency_key(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 8:
            raise ValueError("요청 식별자는 공백을 제외하고 8자 이상이어야 합니다.")
        return cleaned

    @field_validator("basis_outcome_id")
    @classmethod
    def normalise_basis_outcome_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("검토 기준 결과 식별자는 비어 있을 수 없습니다.")
        return cleaned


class ResultLearningUpdate(ApiModel):
    expected_updated_at: datetime
    record_status: WorkflowStatus | None = None
    status: OutcomeStatus | None = None
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
    source_reference: str | None = Field(default=None, max_length=1000)
    operator_note: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime | None = None

    @field_validator("winner_name", "reason_code", "loss_reason", "source_reference", "operator_note")
    @classmethod
    def normalise_optional_text(cls, value: str | None) -> str | None:
        return ResultLearningFields.normalise_optional_text(value)


class ResultLearningOutcomeOut(ApiModel):
    id: str
    outcome_key: str
    record_status: WorkflowStatus
    revision: int
    status: OutcomeStatus
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
    source: str
    source_reference: str | None
    basis_outcome_id: str | None
    basis_source: str | None
    operator_note: str | None
    occurred_at: datetime | None
    observed_at: datetime
    created_at: datetime
    updated_at: datetime


class ResultLearningNoticeOut(ApiModel):
    notice_key: str
    bid_notice_no: str
    title: str
    agency: str
    deadline: datetime
    notice_status: str
    latest_outcome: ResultLearningOutcomeOut | None


class ResultLearningListOut(ApiModel):
    total: int
    offset: int
    limit: int
    records: list[ResultLearningNoticeOut]


class ResultLearningMutationOut(ApiModel):
    created: bool
    notice_key: str
    outcome: ResultLearningOutcomeOut


router = APIRouter(prefix="/api/v1/result-learning", tags=["result learning"])


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _operator_access(request: Request, *, mutation: bool) -> None:
    if request.headers.get("x-pai-loop-api-key"):
        require_api_key(request)
        return
    if _manual_feature_enabled(request):
        if mutation and not _same_origin_request(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="홈페이지와 동일한 출처에서만 결과 학습을 변경할 수 있습니다.",
            )
        fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="홈페이지와 동일한 출처에서만 결과 학습을 조회할 수 있습니다.",
            )
        _require_manual_operator(request)
        return
    require_api_key(request)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_version(actual: datetime, expected: datetime) -> bool:
    return abs((_utc(actual) - _utc(expected)).total_seconds()) < 0.001


def _effective_notice_status(notice: Notice) -> str:
    current = str(notice.status or "").upper()
    if current == "OPEN" and _utc(notice.deadline) < datetime.now(timezone.utc):
        return "EXPIRED"
    return current


def _workflow(item: BidOutcome) -> dict[str, Any]:
    evidence = item.evidence_json if isinstance(item.evidence_json, dict) else {}
    workflow = evidence.get("_workflow")
    workflow = workflow if isinstance(workflow, dict) else {}
    record_status = str(workflow.get("record_status") or "").upper()
    if record_status not in {"DRAFT", "VALIDATED", "ARCHIVED"}:
        source = item.source.upper()
        exact_match = evidence.get("exact_match")
        participation = evidence.get("participation_basis")
        automatic_validated = bool(
            source == "PPS_AUTO_FEEDBACK"
            and isinstance(exact_match, dict)
            and exact_match.get("verified") is True
            and (
                item.status == "WON"
                or (
                    item.status == "LOST"
                    and isinstance(participation, dict)
                    and participation.get("record_status") == "VALIDATED"
                    and participation.get("human_reviewed") is True
                )
            )
        )
        record_status = (
            "VALIDATED"
            if automatic_validated or source.startswith("PPS") and source != "PPS_AUTO_FEEDBACK"
            else "DRAFT"
        )
    try:
        revision = max(int(workflow.get("revision") or 1), 1)
    except (TypeError, ValueError):
        revision = 1
    basis = workflow.get("basis_outcome")
    basis = basis if isinstance(basis, dict) else {}
    return {
        "record_status": record_status,
        "revision": revision,
        "operator_note": str(evidence.get("operator_note") or "").strip() or None,
        "basis_outcome_id": str(basis.get("id") or "").strip() or None,
        "basis_source": str(basis.get("source") or "").strip() or None,
    }


def _out(item: BidOutcome) -> ResultLearningOutcomeOut:
    workflow = _workflow(item)
    return ResultLearningOutcomeOut(
        id=item.id,
        outcome_key=item.outcome_key,
        record_status=workflow["record_status"],
        revision=workflow["revision"],
        status=item.status,
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
        loss_reason=item.loss_reason,
        source=item.source,
        source_reference=item.source_reference,
        basis_outcome_id=workflow["basis_outcome_id"],
        basis_source=workflow["basis_source"],
        operator_note=workflow["operator_note"],
        occurred_at=item.occurred_at,
        observed_at=item.observed_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _latest_outcome(notice: Notice) -> BidOutcome | None:
    return max(
        notice.bid_outcomes,
        key=lambda item: (_utc(item.observed_at), _utc(item.updated_at)),
        default=None,
    )


def _fields(payload: ResultLearningFields) -> dict[str, object]:
    return payload.model_dump(exclude={"record_status", "operator_note", "notice_key", "idempotency_key"})


def _evidence(
    existing: dict[str, Any] | None,
    *,
    record_status: str,
    revision: int,
    operator_note: str | None,
    created: bool,
    basis: BidOutcome | None = None,
) -> dict[str, Any]:
    result = dict(existing) if isinstance(existing, dict) else {}
    previous = result.get("_workflow")
    previous = previous if isinstance(previous, dict) else {}
    result["_workflow"] = {
        "record_status": record_status,
        "revision": revision,
        "created_by": previous.get("created_by") or "KMA 입찰팀",
        "updated_by": "KMA 입찰팀",
        "human_reviewed": True,
    }
    previous_basis = previous.get("basis_outcome")
    if basis is not None:
        result["_workflow"]["basis_outcome"] = {
            "id": basis.id,
            "outcome_key": basis.outcome_key,
            "source": basis.source,
            "source_reference": basis.source_reference,
        }
    elif isinstance(previous_basis, dict):
        result["_workflow"]["basis_outcome"] = dict(previous_basis)
    if created:
        result["_workflow"]["created_by"] = "KMA 입찰팀"
    if operator_note:
        result["operator_note"] = operator_note
    else:
        result.pop("operator_note", None)
    return result


def _current_fields(item: BidOutcome) -> dict[str, object]:
    workflow = _workflow(item)
    return {
        "record_status": workflow["record_status"],
        "status": item.status,
        "submitted_bid_amount": item.submitted_bid_amount,
        "submitted_bid_rate": item.submitted_bid_rate,
        "winning_bid_amount": item.winning_bid_amount,
        "winning_bid_rate": item.winning_bid_rate,
        "technical_score": item.technical_score,
        "price_score": item.price_score,
        "total_score": item.total_score,
        "rank": item.rank,
        "winner_name": item.winner_name,
        "reason_code": item.reason_code,
        "loss_reason": item.loss_reason,
        "source_reference": item.source_reference,
        "operator_note": workflow["operator_note"],
        "occurred_at": item.occurred_at,
    }


def _same_learning_values(
    current: dict[str, object],
    expected: dict[str, object],
) -> bool:
    if current.keys() != expected.keys():
        return False
    for field, actual in current.items():
        wanted = expected[field]
        if isinstance(actual, datetime) and isinstance(wanted, datetime):
            if actual.tzinfo is None or wanted.tzinfo is None:
                matches = actual.replace(tzinfo=None) == wanted.replace(tzinfo=None)
            else:
                matches = _utc(actual) == _utc(wanted)
            if not matches:
                return False
            continue
        if isinstance(actual, float) and isinstance(wanted, (int, float)):
            if abs(actual - float(wanted)) > 1e-9:
                return False
            continue
        if actual != wanted:
            return False
    return True


def _notice(session: Session, notice_key: str) -> Notice:
    notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return notice


@router.get("", response_model=ResultLearningListOut)
def list_result_learning(
    request: Request,
    session: DbSession,
    q: str | None = Query(default=None, max_length=200),
    scope: LearningScope = "ENDED",
    outcome_status: OutcomeStatus | None = None,
    record_status: WorkflowStatus | None = None,
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ResultLearningListOut:
    _operator_access(request, mutation=False)
    query = select(Notice).options(selectinload(Notice.bid_outcomes))
    query_text = " ".join((q or "").split())
    if query_text:
        pattern = f"%{query_text}%"
        query = query.where(
            or_(
                Notice.title.ilike(pattern),
                Notice.agency.ilike(pattern),
                Notice.bid_notice_no.ilike(pattern),
                Notice.notice_key.ilike(pattern),
            )
        )
    notices = list(session.scalars(query.order_by(Notice.deadline.desc())).unique().all())
    selected: list[tuple[Notice, BidOutcome | None]] = []
    for notice in notices:
        latest = _latest_outcome(notice)
        lifecycle = _effective_notice_status(notice)
        if scope == "ENDED" and lifecycle == "OPEN" and latest is None:
            continue
        if scope == "WITH_OUTCOME" and latest is None:
            continue
        if outcome_status and (latest is None or latest.status != outcome_status):
            continue
        if record_status and (latest is None or _workflow(latest)["record_status"] != record_status):
            continue
        selected.append((notice, latest))
    page = selected[offset : offset + limit]
    return ResultLearningListOut(
        total=len(selected),
        offset=offset,
        limit=limit,
        records=[
            ResultLearningNoticeOut(
                notice_key=notice.notice_key,
                bid_notice_no=notice.bid_notice_no,
                title=notice.title,
                agency=notice.agency,
                deadline=notice.deadline,
                notice_status=_effective_notice_status(notice),
                latest_outcome=_out(latest) if latest else None,
            )
            for notice, latest in page
        ],
    )


@router.post("", response_model=ResultLearningMutationOut, status_code=status.HTTP_201_CREATED)
def create_result_learning(
    payload: ResultLearningCreate,
    request: Request,
    session: DbSession,
) -> ResultLearningMutationOut:
    _operator_access(request, mutation=True)
    notice = _notice(session, payload.notice_key)
    basis: BidOutcome | None = None
    if payload.basis_outcome_id:
        basis = session.get(BidOutcome, payload.basis_outcome_id)
        if basis is None or basis.notice_id != notice.id:
            raise HTTPException(
                status_code=422,
                detail="이 공고의 검토 기준 결과가 아닙니다.",
            )
    outcome_key = "manual-ui:" + hashlib.sha256(payload.idempotency_key.encode("utf-8")).hexdigest()[:40]
    validated = ResultLearningFields.model_validate(
        payload.model_dump(include=set(ResultLearningFields.model_fields))
    )
    values = _fields(validated)
    existing = session.scalar(
        select(BidOutcome).where(
            BidOutcome.notice_id == notice.id,
            BidOutcome.outcome_key == outcome_key,
        )
    )
    if existing is not None:
        expected = {
            **values,
            "record_status": validated.record_status,
            "operator_note": validated.operator_note,
        }
        if not _same_learning_values(_current_fields(existing), expected):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="같은 요청 식별자로 다른 결과를 저장할 수 없습니다.",
            )
        if _workflow(existing)["basis_outcome_id"] != payload.basis_outcome_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="같은 요청 식별자로 다른 검토 기준 결과를 저장할 수 없습니다.",
            )
        return ResultLearningMutationOut(
            created=False,
            notice_key=notice.notice_key,
            outcome=_out(existing),
        )
    item = BidOutcome(
        notice_id=notice.id,
        outcome_key=outcome_key,
        source="MANUAL_UI",
        observed_at=datetime.now(timezone.utc),
        evidence_json=_evidence(
            None,
            record_status=validated.record_status,
            revision=1,
            operator_note=validated.operator_note,
            created=True,
            basis=basis,
        ),
        **values,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return ResultLearningMutationOut(
        created=True,
        notice_key=notice.notice_key,
        outcome=_out(item),
    )


@router.patch("/{outcome_id}", response_model=ResultLearningMutationOut)
def update_result_learning(
    outcome_id: str,
    payload: ResultLearningUpdate,
    request: Request,
    session: DbSession,
) -> ResultLearningMutationOut:
    _operator_access(request, mutation=True)
    item = session.get(BidOutcome, outcome_id)
    if item is None:
        raise HTTPException(status_code=404, detail="결과 학습 기록을 찾을 수 없습니다.")
    if item.source != "MANUAL_UI":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="자동 환류·외부 원천 결과는 직접 수정할 수 없습니다. 해당 결과를 기준으로 담당자 검토본을 새로 저장해 주세요.",
        )
    if not _same_version(item.updated_at, payload.expected_updated_at):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="다른 사용자가 먼저 수정했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요.",
        )
    current = _current_fields(item)
    current.update(payload.model_dump(exclude={"expected_updated_at"}, exclude_unset=True))
    try:
        validated = ResultLearningFields.model_validate(current)
    except ValidationError as exc:
        detail = " · ".join(str(error.get("msg") or "입력값을 확인해 주세요.") for error in exc.errors())
        raise HTTPException(status_code=422, detail=detail) from exc
    values = _fields(validated)
    workflow = _workflow(item)
    next_evidence = _evidence(
        item.evidence_json,
        record_status=validated.record_status,
        revision=workflow["revision"] + 1,
        operator_note=validated.operator_note,
        created=False,
    )
    next_updated_at = datetime.now(timezone.utc)
    version_lower = item.updated_at - timedelta(microseconds=1)
    version_upper = item.updated_at + timedelta(microseconds=1)
    result = session.execute(
        update(BidOutcome)
        .where(
            BidOutcome.id == item.id,
            BidOutcome.updated_at >= version_lower,
            BidOutcome.updated_at <= version_upper,
        )
        .values(
            **values,
            evidence_json=next_evidence,
            updated_at=next_updated_at,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="다른 사용자가 먼저 수정했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요.",
        )
    session.commit()
    session.expire(item)
    session.refresh(item)
    notice = session.get(Notice, item.notice_id)
    return ResultLearningMutationOut(
        created=False,
        notice_key=notice.notice_key if notice else "",
        outcome=_out(item),
    )
