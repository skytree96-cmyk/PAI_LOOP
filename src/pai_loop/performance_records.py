from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .auth import require_api_key
from .manual_analysis import (
    _manual_feature_enabled,
    _require_manual_operator,
    _same_origin_request,
)
from .models import CompanyPerformanceRecord


RecordStatus = Literal["DRAFT", "VALIDATED", "ARCHIVED"]
VatBasis = Literal["INCLUDED", "EXCLUDED", "UNKNOWN"]
CertificateStatus = Literal["NOT_REQUESTED", "REQUESTED", "ISSUED", "NOT_AVAILABLE"]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class PerformanceRecordFields(ApiModel):
    record_status: RecordStatus = "DRAFT"
    project_name: str = Field(min_length=2, max_length=500)
    agency: str = Field(default="", max_length=255)
    division: str = Field(default="", max_length=255)
    overview: str | None = Field(default=None, max_length=4000)
    contract_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    contract_amount: int | None = Field(default=None, ge=0, le=10**15)
    vat_basis: VatBasis = "UNKNOWN"
    completed: bool = False
    share_pct: float = Field(default=100.0, ge=0, le=100)
    certificate_status: CertificateStatus = "NOT_REQUESTED"
    evidence_reference: str | None = Field(default=None, max_length=1000)
    keywords: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("project_name")
    @classmethod
    def normalise_project_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 2:
            raise ValueError("사업명은 공백을 제외하고 2자 이상 입력해 주세요.")
        return cleaned

    @field_validator("agency", "division")
    @classmethod
    def normalise_label_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("overview", "evidence_reference")
    @classmethod
    def normalise_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @field_validator("keywords")
    @classmethod
    def normalise_keywords(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = " ".join(str(value).split())
            if not cleaned or len(cleaned) > 80:
                raise ValueError("키워드는 1~80자로 입력해 주세요.")
            folded = cleaned.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            output.append(cleaned)
        return output

    @model_validator(mode="after")
    def validate_dates_and_evidence(self) -> "PerformanceRecordFields":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
        if self.record_status == "VALIDATED":
            missing: list[str] = []
            if not self.agency:
                missing.append("발주기관")
            if not self.division:
                missing.append("수행부서")
            if not self.contract_date:
                missing.append("계약일")
            if self.completed and not self.end_date:
                missing.append("수행 종료일")
            if not self.evidence_reference:
                missing.append("근거 참조")
            if missing:
                raise ValueError("검증 완료 전 필수 항목: " + ", ".join(missing))
        return self


class PerformanceRecordCreate(PerformanceRecordFields):
    idempotency_key: str = Field(min_length=8, max_length=180)

    @field_validator("idempotency_key")
    @classmethod
    def normalise_idempotency_key(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 8:
            raise ValueError("요청 식별자는 공백을 제외하고 8자 이상이어야 합니다.")
        return cleaned


class PerformanceRecordUpdate(ApiModel):
    expected_updated_at: datetime
    record_status: RecordStatus | None = None
    project_name: str | None = Field(default=None, min_length=2, max_length=500)
    agency: str | None = Field(default=None, max_length=255)
    division: str | None = Field(default=None, max_length=255)
    overview: str | None = Field(default=None, max_length=4000)
    contract_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    contract_amount: int | None = Field(default=None, ge=0, le=10**15)
    vat_basis: VatBasis | None = None
    completed: bool | None = None
    share_pct: float | None = Field(default=None, ge=0, le=100)
    certificate_status: CertificateStatus | None = None
    evidence_reference: str | None = Field(default=None, max_length=1000)
    keywords: list[str] | None = Field(default=None, max_length=30)

    @field_validator("project_name")
    @classmethod
    def normalise_project_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("사업명은 비울 수 없습니다.")
        cleaned = " ".join(value.split())
        if len(cleaned) < 2:
            raise ValueError("사업명은 공백을 제외하고 2자 이상 입력해 주세요.")
        return cleaned

    @field_validator("agency", "division")
    @classmethod
    def normalise_label_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else ""

    @field_validator("overview", "evidence_reference")
    @classmethod
    def normalise_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

    @field_validator("keywords")
    @classmethod
    def normalise_keywords(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return []
        return PerformanceRecordFields.normalise_keywords(values)


class PerformanceRecordOut(ApiModel):
    id: str
    record_key: str
    record_status: RecordStatus
    project_name: str
    agency: str
    division: str
    overview: str | None
    contract_date: date | None
    start_date: date | None
    end_date: date | None
    contract_amount: int | None
    vat_basis: VatBasis
    completed: bool
    share_pct: float
    certificate_status: CertificateStatus
    evidence_reference: str | None
    keywords: list[str]
    source: str
    revision: int
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class PerformanceRecordMutationOut(ApiModel):
    created: bool
    record: PerformanceRecordOut


class PerformanceRecordListOut(ApiModel):
    total: int
    offset: int
    limit: int
    records: list[PerformanceRecordOut]


router = APIRouter(prefix="/api/v1/performance-records", tags=["company performance records"])


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
                detail="홈페이지와 동일한 출처에서만 실적을 변경할 수 있습니다.",
            )
        fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="홈페이지와 동일한 출처에서만 실적을 조회할 수 있습니다.",
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


def _record_values(payload: PerformanceRecordFields) -> dict[str, object]:
    return payload.model_dump(exclude={"idempotency_key"})


def _validate_record_values(values: dict[str, object]) -> dict[str, object]:
    try:
        validated = PerformanceRecordFields.model_validate(values)
    except ValidationError as exc:
        detail = " · ".join(str(error.get("msg") or "입력값을 확인해 주세요.") for error in exc.errors())
        raise HTTPException(status_code=422, detail=detail) from exc
    return _record_values(validated)


def _record_matches(item: CompanyPerformanceRecord, values: dict[str, object]) -> bool:
    return all(getattr(item, field) == value for field, value in values.items())


@router.get("", response_model=PerformanceRecordListOut)
def list_performance_records(
    request: Request,
    session: DbSession,
    q: str | None = Query(default=None, max_length=200),
    record_status: RecordStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PerformanceRecordListOut:
    _operator_access(request, mutation=False)
    conditions = []
    query_text = " ".join((q or "").split())
    if query_text:
        pattern = f"%{query_text}%"
        conditions.append(
            or_(
                CompanyPerformanceRecord.project_name.ilike(pattern),
                CompanyPerformanceRecord.agency.ilike(pattern),
                CompanyPerformanceRecord.division.ilike(pattern),
                CompanyPerformanceRecord.overview.ilike(pattern),
            )
        )
    if record_status:
        conditions.append(CompanyPerformanceRecord.record_status == record_status)
    base = select(CompanyPerformanceRecord)
    count_query = select(func.count()).select_from(CompanyPerformanceRecord)
    if conditions:
        base = base.where(*conditions)
        count_query = count_query.where(*conditions)
    rows = list(
        session.scalars(
            base.order_by(
                CompanyPerformanceRecord.updated_at.desc(),
                CompanyPerformanceRecord.project_name,
            )
            .offset(offset)
            .limit(limit)
        ).all()
    )
    return PerformanceRecordListOut(
        total=int(session.scalar(count_query) or 0),
        offset=offset,
        limit=limit,
        records=[PerformanceRecordOut.model_validate(row) for row in rows],
    )


@router.post("", response_model=PerformanceRecordMutationOut, status_code=status.HTTP_201_CREATED)
def create_performance_record(
    payload: PerformanceRecordCreate,
    request: Request,
    session: DbSession,
) -> PerformanceRecordMutationOut:
    _operator_access(request, mutation=True)
    record_key = "manual-" + hashlib.sha256(payload.idempotency_key.encode("utf-8")).hexdigest()[:40]
    values = _record_values(payload)
    existing = session.scalar(
        select(CompanyPerformanceRecord).where(CompanyPerformanceRecord.record_key == record_key)
    )
    if existing is not None:
        if not _record_matches(existing, values):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="같은 요청 식별자로 다른 실적을 저장할 수 없습니다.",
            )
        return PerformanceRecordMutationOut(
            created=False,
            record=PerformanceRecordOut.model_validate(existing),
        )
    item = CompanyPerformanceRecord(record_key=record_key, **values)
    session.add(item)
    session.commit()
    session.refresh(item)
    return PerformanceRecordMutationOut(
        created=True,
        record=PerformanceRecordOut.model_validate(item),
    )


@router.patch("/{record_id}", response_model=PerformanceRecordOut)
def update_performance_record(
    record_id: str,
    payload: PerformanceRecordUpdate,
    request: Request,
    session: DbSession,
) -> PerformanceRecordOut:
    _operator_access(request, mutation=True)
    item = session.get(CompanyPerformanceRecord, record_id)
    if item is None:
        raise HTTPException(status_code=404, detail="회사 실적을 찾을 수 없습니다.")
    if not _same_version(item.updated_at, payload.expected_updated_at):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="다른 사용자가 먼저 수정했습니다. 목록을 새로고침한 뒤 다시 시도해 주세요.",
        )
    current = {
        field: getattr(item, field)
        for field in PerformanceRecordFields.model_fields
    }
    changes = payload.model_dump(exclude={"expected_updated_at"}, exclude_unset=True)
    current.update(changes)
    validated = _validate_record_values(current)
    next_updated_at = datetime.now(timezone.utc)
    version_lower = item.updated_at - timedelta(microseconds=1)
    version_upper = item.updated_at + timedelta(microseconds=1)
    result = session.execute(
        update(CompanyPerformanceRecord)
        .where(
            CompanyPerformanceRecord.id == item.id,
            CompanyPerformanceRecord.updated_at >= version_lower,
            CompanyPerformanceRecord.updated_at <= version_upper,
        )
        .values(
            **validated,
            revision=item.revision + 1,
            updated_by="KMA 입찰팀",
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
    return PerformanceRecordOut.model_validate(item)
