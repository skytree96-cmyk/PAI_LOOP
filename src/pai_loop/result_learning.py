from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload

from .auth import require_api_key
from .accounts import Identity, authenticated_account, audit, enabled, serial_transaction
from .outcome_write_lock import lock_outcome_notice
from .notice_freshness import authoritative_pps_notice_is_cancelled
from .models import BidOutcome, Notice
from .outcome_identity import PpsOpeningIdentity, normalise_opening_identity
from .outcome_participation import PARTICIPATION_KIND, provider_participation_verified


OutcomeStatus = Literal["NO_BID", "SUBMITTED", "WON", "LOST", "CANCELLED"]
WorkflowStatus = Literal["DRAFT", "VALIDATED", "ARCHIVED"]
LearningScope = Literal["ENDED", "ALL", "WITH_OUTCOME"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class SubmittedRateCalculation(ApiModel):
    mode: Literal["MANUAL", "AUTO"] = "MANUAL"
    basis_kind: Literal["PLANNED_PRICE", "BASE_AMOUNT"] | None = None
    basis_amount: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    basis_reference: str | None = Field(default=None, max_length=1000)

    @field_validator("basis_reference")
    @classmethod
    def normalise_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @model_validator(mode="after")
    def validate_basis(self) -> "SubmittedRateCalculation":
        if self.mode == "AUTO":
            if self.basis_kind is None or self.basis_amount is None or not self.basis_reference:
                raise ValueError("자동 계산에는 기준가격의 종류·금액·출처가 모두 필요합니다.")
        elif any(value is not None for value in (self.basis_kind, self.basis_amount, self.basis_reference)):
            raise ValueError("수기 모드의 계산 기준은 비워 주세요.")
        return self


class ResultLearningFields(ApiModel):
    record_status: WorkflowStatus = "DRAFT"
    status: OutcomeStatus
    submitted_bid_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    submitted_bid_rate: float | None = Field(default=None, ge=0, le=200, allow_inf_nan=False)
    submitted_rate_calculation: SubmittedRateCalculation = Field(default_factory=SubmittedRateCalculation)
    winning_bid_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    winning_bid_rate: float | None = Field(default=None, ge=0, le=200, allow_inf_nan=False)
    winning_rate_calculation: SubmittedRateCalculation = Field(default_factory=SubmittedRateCalculation)
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
    opening_identity: PpsOpeningIdentity | None = None

    @field_validator("winner_name", "reason_code", "loss_reason", "source_reference", "operator_note")
    @classmethod
    def normalise_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ResultLearningFields":
        for prefix, label in (("submitted", "우리 투찰금액"), ("winning", "낙찰금액")):
            calculation = getattr(self, f"{prefix}_rate_calculation")
            if calculation.mode != "AUTO":
                continue
            amount = getattr(self, f"{prefix}_bid_amount")
            if amount is None:
                raise ValueError(f"자동 계산에는 {label}이 필요합니다.")
            with localcontext() as context:
                context.prec = 40
                rate = Decimal(str(amount)) / Decimal(str(calculation.basis_amount)) * 100
                if rate > 200:
                    raise ValueError("기준가격 대비 비율은 200%를 초과할 수 없습니다. 금액과 기준을 확인해 주세요.")
                # The server owns the result, including when an old client sends a stale rate.
                setattr(self, f"{prefix}_bid_rate", float(rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)))
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
    expected_outcome_id: str | None = Field(default=None, max_length=36)

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
    submitted_bid_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    submitted_bid_rate: float | None = Field(default=None, ge=0, le=200, allow_inf_nan=False)
    submitted_rate_calculation: SubmittedRateCalculation = Field(default_factory=SubmittedRateCalculation)
    winning_bid_amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    winning_bid_rate: float | None = Field(default=None, ge=0, le=200, allow_inf_nan=False)
    winning_rate_calculation: SubmittedRateCalculation = Field(default_factory=SubmittedRateCalculation)
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
    opening_identity: PpsOpeningIdentity | None = None

    @field_validator("winner_name", "reason_code", "loss_reason", "source_reference", "operator_note")
    @classmethod
    def normalise_optional_text(cls, value: str | None) -> str | None:
        return ResultLearningFields.normalise_optional_text(value)


class ResultLearningOutcomeOut(ApiModel):
    id: str
    department_revision: int | None = None
    account_id: str | None = None
    department_id: str | None = None
    department_name: str | None = None
    outcome_key: str
    record_status: WorkflowStatus
    revision: int
    status: OutcomeStatus
    submitted_bid_amount: float | None
    submitted_bid_rate: float | None
    submitted_rate_calculation: SubmittedRateCalculation
    winning_bid_amount: float | None
    winning_bid_rate: float | None
    winning_rate_calculation: SubmittedRateCalculation
    technical_score: float | None
    price_score: float | None
    total_score: float | None
    rank: int | None
    winner_name: str | None
    reason_code: str | None
    loss_reason: str | None
    source: str
    source_reference: str | None
    opening_identity: PpsOpeningIdentity | None = None
    participation_verified: bool = False
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
    revision_no: str
    title: str
    agency: str
    deadline: datetime
    notice_status: str
    latest_outcome: ResultLearningOutcomeOut | None
    outcomes: list[ResultLearningOutcomeOut] = Field(default_factory=list)


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


def _operator_access(request: Request, *, mutation: bool) -> Identity | None:
    if request.headers.get("x-pai-loop-api-key"):
        require_api_key(request)
        return None
    if enabled(request):
        return authenticated_account(request, mutation=mutation, department_write=mutation)
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
    source = item.source.upper()
    exact_match = evidence.get("exact_match")
    participation = evidence.get("participation_basis")
    opening_identity = normalise_opening_identity(evidence.get("opening_identity"))
    provider_confirmed = provider_participation_verified(source, item.status, evidence)
    confirmed_loss = provider_confirmed or bool(
        isinstance(exact_match, dict) and exact_match.get("verified") is True
        and opening_identity is not None
        and isinstance(participation, dict)
        and participation.get("record_status") == "VALIDATED"
        and participation.get("human_reviewed") is True
        and normalise_opening_identity(participation.get("opening_identity")) == opening_identity
    )
    if source == "PPS_AUTO_FEEDBACK" and item.status == "LOST" and not confirmed_loss:
        # Preserve the observation, but a legacy workflow flag cannot supply
        # the opening identity missing from its participation evidence.
        record_status = "ARCHIVED" if record_status == "ARCHIVED" else "DRAFT"
    if (source == "PPS_AUTO_FEEDBACK" and isinstance(participation, dict)
        and participation.get("kind") == PARTICIPATION_KIND and not provider_confirmed):
        record_status = "ARCHIVED" if record_status == "ARCHIVED" else "DRAFT"
    if record_status not in {"DRAFT", "VALIDATED", "ARCHIVED"}:
        automatic_validated = bool(
            source == "PPS_AUTO_FEEDBACK"
            and isinstance(exact_match, dict)
            and exact_match.get("verified") is True
            and (
                item.status == "WON"
                or item.status == "LOST" and confirmed_loss
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


def _rate_calculation(item: BidOutcome, prefix: str = "submitted") -> SubmittedRateCalculation:
    evidence = item.evidence_json if isinstance(item.evidence_json, dict) else {}
    stored = evidence.get(f"_{prefix}_bid_rate")
    if item.source == "MANUAL_UI" and isinstance(stored, dict):
        try:
            return SubmittedRateCalculation.model_validate(stored.get("calculation"))
        except ValidationError:
            pass
    return SubmittedRateCalculation()


def _out(item: BidOutcome) -> ResultLearningOutcomeOut:
    workflow = _workflow(item)
    return ResultLearningOutcomeOut(
        id=item.id,
        account_id=item.account_id,
        department_id=item.department_id,
        department_name=item.department_name,
        department_revision=item.department_revision,
        outcome_key=item.outcome_key,
        record_status=workflow["record_status"],
        revision=workflow["revision"],
        status=item.status,
        submitted_bid_amount=item.submitted_bid_amount,
        submitted_bid_rate=item.submitted_bid_rate,
        submitted_rate_calculation=_rate_calculation(item),
        winning_bid_amount=item.winning_bid_amount,
        winning_bid_rate=item.winning_bid_rate,
        winning_rate_calculation=_rate_calculation(item, "winning"),
        technical_score=item.technical_score,
        price_score=item.price_score,
        total_score=item.total_score,
        rank=item.rank,
        winner_name=item.winner_name,
        reason_code=item.reason_code,
        loss_reason=item.loss_reason,
        source=item.source,
        source_reference=item.source_reference,
        opening_identity=_stored_opening_identity(item),
        participation_verified=provider_participation_verified(item.source, item.status, item.evidence_json),
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


def _stored_opening_identity(item: BidOutcome) -> dict[str, str] | None:
    evidence = item.evidence_json if isinstance(item.evidence_json, dict) else {}
    return normalise_opening_identity(evidence.get("opening_identity"))


def _validated_opening_identity(
    identity: PpsOpeningIdentity | None, notice: Notice,
) -> dict[str, str] | None:
    if identity is None:
        return None
    supplied = identity.model_dump()
    expected = normalise_opening_identity({
        **supplied, "bid_notice_no": notice.bid_notice_no, "revision_no": notice.revision_no,
    })
    if supplied != expected:
        raise HTTPException(status_code=422, detail="개찰 식별자가 이 공고·차수와 일치하지 않습니다.")
    return supplied


def _fields(payload: ResultLearningFields) -> dict[str, object]:
    return payload.model_dump(exclude={"record_status", "operator_note", "notice_key", "idempotency_key", "submitted_rate_calculation", "winning_rate_calculation", "opening_identity"})


def _rate_snapshot(fields: dict[str, Any], prefix: str = "submitted") -> dict[str, Any]:
    calculation = fields[f"{prefix}_rate_calculation"]
    return {
        f"{prefix}_bid_amount": fields[f"{prefix}_bid_amount"],
        f"{prefix}_bid_rate": fields[f"{prefix}_bid_rate"],
        "calculation": calculation,
        "rounding_policy": "DECIMAL_HALF_UP_4" if calculation["mode"] == "AUTO" else None,
    }


def _evidence(
    existing: dict[str, Any] | None,
    *,
    record_status: str,
    revision: int,
    operator_note: str | None,
    created: bool,
    opening_identity: dict[str, str] | None,
    basis: BidOutcome | None = None,
    actor_label: str | None = None,
    actor_id: str | None = None,
    rate_fields: ResultLearningFields | None = None,
    previous_rate_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(existing) if isinstance(existing, dict) else {}
    previous_opening_identity = result.get("opening_identity")
    if opening_identity is None:
        result.pop("opening_identity", None)
    else:
        result["opening_identity"] = opening_identity
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
            "opening_identity": _stored_opening_identity(basis),
            "participation_verified": provider_participation_verified(basis.source, basis.status, basis.evidence_json),
        }
    elif isinstance(previous_basis, dict):
        result["_workflow"]["basis_outcome"] = dict(previous_basis)
    if created:
        result["_workflow"]["created_by"] = "KMA 입찰팀"
    if actor_label:
        result["_workflow"]["updated_by"] = actor_label
        if created:
            result["_workflow"]["created_by"] = actor_label
    if operator_note:
        result["operator_note"] = operator_note
    else:
        result.pop("operator_note", None)
    if rate_fields is not None:
        for prefix in ("submitted", "winning"):
            before = _rate_snapshot(previous_rate_fields, prefix) if previous_rate_fields is not None else None
            after = _rate_snapshot(rate_fields.model_dump(), prefix)
            previous_rate = result.get(f"_{prefix}_bid_rate")
            previous_rate = previous_rate if isinstance(previous_rate, dict) else {}
            history = previous_rate.get("history")
            history = list(history) if isinstance(history, list) else []
            if before != after:
                history.append({
                    "revision": revision,
                    "changed_at": datetime.now(timezone.utc).isoformat(),
                    "actor_id": actor_id,
                    "actor_label": actor_label or "KMA 입찰팀",
                    "before": before,
                    "after": after,
                })
            result[f"_{prefix}_bid_rate"] = {
                "calculation": after["calculation"],
                "history": history,
            }
    if previous_opening_identity != opening_identity:
        history = result.get("_opening_identity_history")
        history = list(history) if isinstance(history, list) else []
        history.append({
            "revision": revision,
            "changed_at": datetime.now(timezone.utc).isoformat(),
            "actor": result["_workflow"]["updated_by"],
            "before": previous_opening_identity,
            "after": opening_identity,
        })
        result["_opening_identity_history"] = history
    return result


def _current_fields(item: BidOutcome) -> dict[str, object]:
    workflow = _workflow(item)
    return {
        "record_status": workflow["record_status"],
        "opening_identity": _stored_opening_identity(item),
        "status": item.status,
        "submitted_bid_amount": item.submitted_bid_amount,
        "submitted_bid_rate": item.submitted_bid_rate,
        "submitted_rate_calculation": _rate_calculation(item).model_dump(),
        "winning_bid_amount": item.winning_bid_amount,
        "winning_bid_rate": item.winning_bid_rate,
        "winning_rate_calculation": _rate_calculation(item, "winning").model_dump(),
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


def _notice_out(notice: Notice, *, include_outcomes: bool) -> ResultLearningNoticeOut:
    latest = _latest_outcome(notice)
    return ResultLearningNoticeOut(
        notice_key=notice.notice_key,
        bid_notice_no=notice.bid_notice_no,
        revision_no=notice.revision_no,
        title=notice.title,
        agency=notice.agency,
        deadline=notice.deadline,
        notice_status=_effective_notice_status(notice),
        latest_outcome=_out(latest) if latest else None,
        outcomes=[
            _out(item)
            for item in sorted(
                notice.bid_outcomes,
                key=lambda item: (_utc(item.observed_at), item.id),
                reverse=True,
            )
        ] if include_outcomes else [],
    )


@router.get("/notices/{notice_key}", response_model=ResultLearningNoticeOut)
def get_result_learning_notice(
    notice_key: str,
    request: Request,
    session: DbSession,
) -> ResultLearningNoticeOut:
    _operator_access(request, mutation=False)
    notice = session.scalar(
        select(Notice)
        .where(Notice.notice_key == notice_key)
        .options(selectinload(Notice.bid_outcomes))
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return _notice_out(notice, include_outcomes=enabled(request))


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
            _notice_out(notice, include_outcomes=enabled(request))
            for notice, _latest in page
        ],
    )


@router.post("", response_model=ResultLearningMutationOut, status_code=status.HTTP_201_CREATED)
def create_result_learning(
    payload: ResultLearningCreate,
    request: Request,
    session: DbSession,
) -> ResultLearningMutationOut:
    identity = _operator_access(request, mutation=True)
    if identity:
        serial_transaction(session, scope=f"result:{payload.notice_key}:{identity.department_id}")
        if "expected_outcome_id" not in payload.model_fields_set:
            raise HTTPException(422, "화면에서 확인한 자기 부서의 최신 결과 식별자가 필요합니다.")
    lock_outcome_notice(session, payload.notice_key)
    notice = _notice(session, payload.notice_key)
    if identity and (notice.status == "CANCELLED" or authoritative_pps_notice_is_cancelled(session, notice)):
        raise HTTPException(409, "취소된 공고의 결과는 변경할 수 없습니다.")
    opening_identity = _validated_opening_identity(payload.opening_identity, notice)
    basis: BidOutcome | None = None
    if payload.basis_outcome_id:
        basis = session.get(BidOutcome, payload.basis_outcome_id)
        if basis is None or basis.notice_id != notice.id:
            raise HTTPException(
                status_code=422,
                detail="이 공고의 검토 기준 결과가 아닙니다.",
            )
        if "opening_identity" not in payload.model_fields_set:
            inherited_identity = _stored_opening_identity(basis)
            opening_identity = _validated_opening_identity(
                PpsOpeningIdentity.model_validate(inherited_identity) if inherited_identity else None, notice,
            )
    key_material = f"{identity.department_id}:{payload.idempotency_key}" if identity else payload.idempotency_key
    outcome_key = "manual-ui:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:40]
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
        if identity and existing.department_id != identity.department_id:
            raise HTTPException(403, "자기 부서의 결과만 변경할 수 있습니다.")
        expected = {
            **values,
            "submitted_rate_calculation": validated.submitted_rate_calculation.model_dump(),
            "winning_rate_calculation": validated.winning_rate_calculation.model_dump(),
            "record_status": validated.record_status,
            "operator_note": validated.operator_note,
            "opening_identity": opening_identity,
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
    if identity:
        previous = session.scalar(select(BidOutcome).where(BidOutcome.notice_id == notice.id, BidOutcome.department_id == identity.department_id).order_by(BidOutcome.department_revision.desc(), BidOutcome.id.desc()).limit(1))
        if (previous.id if previous else None) != payload.expected_outcome_id:
            raise HTTPException(409, "자기 부서의 결과가 갱신되었습니다. 다시 확인해 주세요.")
    item = BidOutcome(
        notice_id=notice.id,
        outcome_key=outcome_key,
        source="MANUAL_UI",
        account_id=identity.id if identity else None,
        department_id=identity.department_id if identity else None,
        department_name=identity.department_name if identity else None,
        department_revision=(previous.department_revision + 1 if previous else 1) if identity else None,
        observed_at=datetime.now(timezone.utc),
        evidence_json=_evidence(
            None,
            record_status=validated.record_status,
            revision=1,
            operator_note=validated.operator_note,
            created=True,
            opening_identity=opening_identity,
            basis=basis,
            actor_label=identity.actor_label if identity else None,
            actor_id=identity.id if identity else None,
            rate_fields=validated,
        ),
        **values,
    )
    session.add(item)
    if identity:
        session.flush()
        audit(session, "DEPARTMENT_RESULT_CREATED", actor=identity.id, target=item.id)
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
    identity = _operator_access(request, mutation=True)
    item = session.get(BidOutcome, outcome_id)
    if item is None:
        raise HTTPException(status_code=404, detail="결과 학습 기록을 찾을 수 없습니다.")
    notice_key = session.scalar(select(Notice.notice_key).where(Notice.id == item.notice_id))
    # Discard the pre-lock read snapshot before waiting. CAS and ownership must
    # be checked against the row committed by the preceding notice writer.
    session.rollback()
    lock_outcome_notice(session, notice_key)
    item = session.get(BidOutcome, outcome_id)
    if item is None:
        raise HTTPException(status_code=404, detail="결과 학습 기록을 찾을 수 없습니다.")
    if identity and item.department_id != identity.department_id:
        raise HTTPException(403, "자기 부서의 결과만 변경할 수 있습니다.")
    if identity is None and item.department_id is not None:
        raise HTTPException(403, "부서 소유 결과는 해당 부서 계정으로만 변경할 수 있습니다.")
    if identity:
        notice = session.get(Notice, item.notice_id)
        if notice.status == "CANCELLED" or authoritative_pps_notice_is_cancelled(session, notice):
            raise HTTPException(409, "취소된 공고의 결과는 변경할 수 없습니다.")
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
    opening_identity = _validated_opening_identity(validated.opening_identity, item.notice)
    next_evidence = _evidence(
        item.evidence_json,
        record_status=validated.record_status,
        revision=workflow["revision"] + 1,
        operator_note=validated.operator_note,
        created=False,
        actor_label=identity.actor_label if identity else None,
        actor_id=identity.id if identity else None,
        rate_fields=validated,
        previous_rate_fields=_current_fields(item),
        opening_identity=opening_identity,
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
            BidOutcome.department_id == identity.department_id if identity else BidOutcome.department_id.is_(None),
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
    if identity:
        audit(session, "DEPARTMENT_RESULT_UPDATED", actor=identity.id, target=item.id)
    session.commit()
    session.expire(item)
    session.refresh(item)
    notice = session.get(Notice, item.notice_id)
    return ResultLearningMutationOut(
        created=False,
        notice_key=notice.notice_key if notice else "",
        outcome=_out(item),
    )
