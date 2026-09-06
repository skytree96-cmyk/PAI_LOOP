from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_private_evidence_access
from .models import (
    CompanyPerformanceRecord,
    Evidence,
    PerformanceNormalizationBatch,
    PerformanceNormalizationRevision,
)
from .performance_records import (
    MAX_PRIVATE_SOURCE_ROW,
    PRIVATE_IMPORT_ATTESTATION,
    PerformanceRecordFields,
    _private_import_serialization,
    _private_workbook_uri,
)

ALGORITHM_VERSION = "explicit-performance-period-v1"
_REVISION_SOURCE = "PRIVATE_PERIOD_NORMALIZATION"


def parse_explicit_performance_period(value: str) -> tuple[date, date] | None:
    """Accept only two exact date boundaries; never infer a year crossing.

    Two-digit years follow the source register's explicit 2000-based convention.
    The caller's authenticated local-source binding is trusted separately; a
    workbook SHA and a cell name do not prove a supplied literal's membership.
    """
    text = " ".join(value.replace("\u00a0", " ").split())
    full = r"(?:20\d{2}|\d{2})\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}\.?|20\d{6}"
    end = rf"(?:{full}|\d{{1,2}}\s*[./-]\s*\d{{1,2}}\.?)"
    match = re.fullmatch(
        rf"(?P<start>{full})(?:\s*[~〜∼～–—-]\s*|\s+)(?P<end>{end})", text
    )
    if match is None:
        return None

    def boundary(token: str, *, year: int | None = None) -> date | None:
        compact = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", token)
        parts = compact.groups() if compact else tuple(
            part for part in re.split(r"[./-]", token.rstrip(".")) if part.strip()
        )
        try:
            numbers = tuple(int(part.strip()) for part in parts)
            if len(numbers) == 2 and year is not None:
                return date(year, *numbers)
            if len(numbers) == 3:
                y, month, day = numbers
                return date(y + 2000 if y < 100 else y, month, day)
        except ValueError:
            return None
        return None

    start = boundary(match.group("start"))
    end_date = boundary(match.group("end"), year=start.year if start else None)
    if start is None or end_date is None or end_date < start:
        return None
    return start, end_date


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PerformancePeriodNormalizationItem(_PrivateModel):
    record_id: UUID
    row_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_row: int = Field(ge=2, le=MAX_PRIVATE_SOURCE_ROW)
    expected_revision: int = Field(ge=1)
    expected_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_period: str = Field(min_length=1, max_length=180)


class PerformancePeriodNormalizationRequest(_PrivateModel):
    idempotency_key: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sheet_name: Literal["프로젝트DB"]
    algorithm_version: Literal["explicit-performance-period-v1"]
    # A machine source-match assertion, distinct from a new human attestation.
    source_binding_basis: Literal["AUTHENTICATED_LOCAL_SOURCE_MATCH"]
    records: list[PerformancePeriodNormalizationItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_rows(self) -> "PerformancePeriodNormalizationRequest":
        for attribute in ("record_id", "row_key", "source_row"):
            values = [getattr(item, attribute) for item in self.records]
            if len(values) != len(set(values)):
                raise ValueError("정규화 대상 행은 중복될 수 없습니다.")
        return self


class PerformancePeriodNormalizationOut(_PrivateModel):
    status: Literal["APPLIED", "UNCHANGED"]
    normalized: int
    activated: int
    unchanged: int


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()


def performance_normalization_state(record: CompanyPerformanceRecord | Mapping) -> dict:
    """Canonical private before-state, also usable with an authenticated API row."""
    def field(name: str):
        value = record.get(name) if isinstance(record, Mapping) else getattr(record, name)
        return value.isoformat() if isinstance(value, (date, datetime)) else value

    names = (*PerformanceRecordFields.model_fields, "id", "record_key", "source",
             "revision", "created_by", "updated_by")
    return {name: field(name) for name in names}


def performance_normalization_state_sha256(record: CompanyPerformanceRecord | Mapping) -> str:
    return _canonical_hash(performance_normalization_state(record))


def _conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="현재 비공개 원본·행·수정 이력이 정규화 요청과 일치하지 않습니다.")


def _can_validate(values: dict) -> bool:
    # An invalid share was previously represented by 0 in a DRAFT. Without its
    # original reason code, zero is not sufficient proof for status promotion.
    if not (
        values["completed"] is True
        and values["vat_basis"] == "INCLUDED"
        and values["certificate_status"] == "ISSUED"
        and values["recognized_amount_is_net_of_share"] is True
        and values["gross_contract_amount_krw"] is not None
        and values["recognized_performance_amount_krw"] is not None
        and 0 < values["share_pct"] <= 100
    ):
        return False
    try:
        PerformanceRecordFields.model_validate({**values, "record_status": "VALIDATED"})
    except ValidationError:
        return False
    return True


def _get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


router = APIRouter(dependencies=[Depends(require_private_evidence_access)])


@router.post("/performance-period-normalizations", response_model=PerformancePeriodNormalizationOut)
def normalize_private_performance_periods(
    payload: PerformancePeriodNormalizationRequest,
    session: Annotated[Session, Depends(_get_session)],
) -> PerformancePeriodNormalizationOut:
    # Shares the import/replacement lock across workers. All checks and all rows
    # are committed atomically; a rejected request cannot partly change records.
    with _private_import_serialization(session):
        try:
            result = _apply_normalization(payload, session)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise


def _apply_normalization(payload: PerformancePeriodNormalizationRequest, session: Session):
    request_sha256 = _canonical_hash(payload.model_dump(mode="json"))
    previous = session.scalar(select(PerformanceNormalizationBatch).where(
        PerformanceNormalizationBatch.idempotency_key == str(payload.idempotency_key)
    ))
    if previous is not None:
        if previous.request_sha256 != request_sha256:
            raise _conflict()
        # A receipt replay never reactivates a subsequently superseded source.
        return PerformancePeriodNormalizationOut(
            status="UNCHANGED", normalized=0, activated=0, unchanged=previous.normalized_count
        )

    evidence = session.scalar(select(Evidence).where(
        Evidence.sha256 == payload.source_sha256,
        Evidence.evidence_type == "PRIVATE_PERFORMANCE_WORKBOOK",
    ).with_for_update())
    metadata = dict(evidence.metadata_json or {}) if evidence is not None else {}
    base_uri = _private_workbook_uri(payload.source_sha256)
    if (
        evidence is None or evidence.status != "VERIFIED"
        or evidence.source_location != base_uri
        or metadata.get("schema_version") != "kma-private-performance-v1"
        or metadata.get("sheet_name") != payload.sheet_name
        or metadata.get("verification_attestation") != PRIVATE_IMPORT_ATTESTATION
    ):
        raise _conflict()

    plans = []
    for supplied in payload.records:
        record = session.scalar(select(CompanyPerformanceRecord).where(
            CompanyPerformanceRecord.id == str(supplied.record_id)
        ).with_for_update())
        if (
            record is None
            or record.source != "PRIVATE_IMPORT"
            or record.record_status != "DRAFT"
            or record.start_date is not None or record.end_date is not None
            or record.record_key != "private-" + supplied.row_key[:40]
            or record.evidence_reference != (
                f"{base_uri}#{payload.sheet_name}!A{supplied.source_row}:Q{supplied.source_row}"
            )
            or record.revision != supplied.expected_revision
            or performance_normalization_state_sha256(record) != supplied.expected_state_sha256
        ):
            raise _conflict()
        interval = parse_explicit_performance_period(supplied.source_period)
        if interval is None:
            raise HTTPException(status_code=422, detail="원문에서 명확한 수행 시작일과 종료일을 확인할 수 없습니다.")
        before = performance_normalization_state(record)
        fields = {name: getattr(record, name) for name in PerformanceRecordFields.model_fields}
        fields.update(start_date=interval[0], end_date=interval[1])
        promoted = _can_validate(fields)
        if promoted:
            fields["record_status"] = "VALIDATED"
        plans.append((supplied, record, before, fields, promoted))

    receipt = PerformanceNormalizationBatch(
        idempotency_key=str(payload.idempotency_key), request_sha256=request_sha256,
        evidence_id=evidence.id, source_sha256=payload.source_sha256,
        algorithm_version=payload.algorithm_version, source_binding_basis=payload.source_binding_basis,
        normalized_count=len(plans),
        activated_count=sum(plan[4] for plan in plans),
    )
    session.add(receipt)
    session.flush()
    now = datetime.now(timezone.utc)
    for supplied, record, before, fields, promoted in plans:
        record.start_date = fields["start_date"]
        record.end_date = fields["end_date"]
        record.record_status = fields["record_status"]
        record.revision += 1
        record.updated_by = _REVISION_SOURCE
        record.updated_at = now
        session.add(PerformanceNormalizationRevision(
            batch_id=receipt.id, record_id=record.id,
            from_revision=before["revision"], to_revision=record.revision,
            source_cell=f"{payload.sheet_name}!G{supplied.source_row}",
            source_period_sha256=hashlib.sha256(supplied.source_period.encode("utf-8")).hexdigest(),
            before_state=before, after_state=performance_normalization_state(record),
        ))
    session.flush()
    return PerformancePeriodNormalizationOut(
        status="APPLIED", normalized=receipt.normalized_count,
        activated=receipt.activated_count, unchanged=0,
    )
