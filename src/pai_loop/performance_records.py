from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from .auth import require_api_key, require_private_evidence_access
from .models import CompanyPerformanceRecord, Evidence


RecordStatus = Literal["DRAFT", "VALIDATED", "ARCHIVED"]
VatBasis = Literal["INCLUDED", "EXCLUDED", "UNKNOWN"]
CertificateStatus = Literal["NOT_REQUESTED", "REQUESTED", "ISSUED", "NOT_AVAILABLE"]
MAX_PRIVATE_SOURCE_ROW = 20_000
PRIVATE_IMPORT_ATTESTATION = (
    "OPERATOR_CONFIRMED_CERTIFICATE_BACKED_COMPLETED_VAT_INCLUDED_"
    "RECOGNIZED_AMOUNT_NET_OF_SHARE"
)
_PRIVATE_IMPORT_SOURCE = "PRIVATE_IMPORT"
_PRIVATE_IMPORT_PENDING_VALIDATED = "PRIVATE_IMPORT_PENDING_VALIDATED"
_PRIVATE_IMPORT_PENDING_DRAFT = "PRIVATE_IMPORT_PENDING_DRAFT"
_PRIVATE_IMPORT_PROCESS_LOCK = threading.Lock()
_PRIVATE_IMPORT_ADVISORY_LOCK_KEY = 0x50414950  # "PAIP"


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
    gross_contract_amount_krw: int | None = Field(default=None, ge=0, le=10**15)
    recognized_performance_amount_krw: int | None = Field(default=None, ge=0, le=10**15)
    recognized_amount_is_net_of_share: bool | None = None
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
        if (
            self.recognized_amount_is_net_of_share is True
            and self.recognized_performance_amount_krw is None
        ):
            raise ValueError("지분 반영 완료 표시는 인정 실적금액과 함께 입력해 주세요.")
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
    gross_contract_amount_krw: int | None = Field(default=None, ge=0, le=10**15)
    recognized_performance_amount_krw: int | None = Field(default=None, ge=0, le=10**15)
    recognized_amount_is_net_of_share: bool | None = None
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
    gross_contract_amount_krw: int | None
    recognized_performance_amount_krw: int | None
    recognized_amount_is_net_of_share: bool | None
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


class PrivatePerformanceImportItem(ApiModel):
    source_row: int = Field(ge=2, le=MAX_PRIVATE_SOURCE_ROW)
    row_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    fields: PerformanceRecordFields


class PrivatePerformanceImportRequest(ApiModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sheet_name: Literal["프로젝트DB"]
    schema_version: Literal["kma-private-performance-v1"]
    source_row_count: int = Field(ge=1, le=MAX_PRIVATE_SOURCE_ROW)
    batch_index: int = Field(ge=0, le=MAX_PRIVATE_SOURCE_ROW - 1)
    batch_count: int = Field(ge=1, le=MAX_PRIVATE_SOURCE_ROW)
    verification_attestation: Literal[
        "OPERATOR_CONFIRMED_CERTIFICATE_BACKED_COMPLETED_VAT_INCLUDED_RECOGNIZED_AMOUNT_NET_OF_SHARE"
    ]
    records: list[PrivatePerformanceImportItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicate_rows(self) -> "PrivatePerformanceImportRequest":
        if self.batch_index >= self.batch_count:
            raise ValueError("배치 번호는 전체 배치 수보다 작아야 합니다.")
        rows = [item.source_row for item in self.records]
        keys = [item.row_key for item in self.records]
        if len(rows) != len(set(rows)) or len(keys) != len(set(keys)):
            raise ValueError("한 배치에서 원본 행과 행 식별자는 중복될 수 없습니다.")
        for item in self.records:
            fields = item.fields
            if (
                fields.vat_basis != "INCLUDED"
                or fields.completed is not True
                or fields.certificate_status != "ISSUED"
                or (
                    fields.recognized_performance_amount_krw is not None
                    and fields.recognized_amount_is_net_of_share is not True
                )
            ):
                raise ValueError(
                    "비공개 실적 증빙 확인 내용과 행별 VAT·완료·증명서·지분 기준이 일치해야 합니다."
                )
            if fields.record_status == "VALIDATED" and (
                fields.gross_contract_amount_krw is None
                or fields.recognized_performance_amount_krw is None
                or fields.recognized_amount_is_net_of_share is not True
            ):
                raise ValueError(
                    "VALIDATED 비공개 실적에는 총액과 지분 반영 인정금액이 모두 필요합니다."
                )
        return self


class PrivatePerformanceImportOut(ApiModel):
    evidence_registered: bool
    created: int
    updated: int
    unchanged: int
    archived: int
    activated: int
    import_complete: bool


router = APIRouter(prefix="/api/v1/performance-records", tags=["company performance records"])


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _operator_access(request: Request, *, mutation: bool) -> bool:
    """Return whether the request has private-evidence authority."""

    if request.headers.get("x-pai-loop-api-key") or request.headers.get(
        "x-pai-private-evidence-token"
    ):
        require_private_evidence_access(request)
        return True
    from .accounts import authenticated_account, enabled
    if enabled(request):
        authenticated_account(request, mutation=mutation)
        if mutation:
            raise HTTPException(403, "실적 원장 변경에는 별도 증빙 관리 권한이 필요합니다.")
        return False
    require_api_key(request)
    return False


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


def _private_workbook_uri(source_sha256: str) -> str:
    return f"private-evidence://company-performance/{source_sha256}/workbook"


def _register_private_workbook_evidence(
    session: Session,
    payload: PrivatePerformanceImportRequest,
) -> tuple[Evidence, bool]:
    evidence_key = "PRIVATE-PERFORMANCE-WORKBOOK-" + payload.source_sha256[:48]
    source_location = _private_workbook_uri(payload.source_sha256)
    static_metadata = {
        "classification": "PRIVATE_SOURCE_METADATA",
        "raw_document_stored": False,
        "schema_version": payload.schema_version,
        "sheet_name": payload.sheet_name,
        "row_count": payload.source_row_count,
        "verification_attestation": payload.verification_attestation,
    }
    existing = session.scalar(
        select(Evidence)
        .where(Evidence.evidence_key == evidence_key)
        .with_for_update()
    )
    if existing is not None:
        existing_metadata = dict(existing.metadata_json or {})
        matches = (
            existing.name == "Private company performance workbook"
            and existing.evidence_type == "PRIVATE_PERFORMANCE_WORKBOOK"
            and existing.status in {"PENDING", "VERIFIED"}
            and existing.source_location == source_location
            and existing.sha256 == payload.source_sha256
            and all(
                existing_metadata.get(key) == value
                for key, value in static_metadata.items()
            )
        )
        if not matches:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="동일한 비공개 증빙 식별자에 다른 메타데이터를 등록할 수 없습니다.",
            )
        if existing.status == "PENDING" and (
            existing_metadata.get("expected_batch_count") != payload.batch_count
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="진행 중인 비공개 증빙 적재의 배치 구성이 일치하지 않습니다.",
            )
        return existing, False
    if payload.batch_index != 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="비공개 증빙 적재는 0번 배치부터 시작해야 합니다.",
        )
    evidence = Evidence(
        evidence_key=evidence_key,
        name="Private company performance workbook",
        evidence_type="PRIVATE_PERFORMANCE_WORKBOOK",
        status="PENDING",
        source_location=source_location,
        sha256=payload.source_sha256,
        metadata_json={
            **static_metadata,
            "expected_batch_count": payload.batch_count,
            "received_batches": [],
        },
    )
    session.add(evidence)
    session.flush()
    return evidence, True


@router.get("", response_model=PerformanceRecordListOut)
def list_performance_records(
    request: Request,
    response: Response,
    session: DbSession,
    q: str | None = Query(default=None, max_length=200),
    record_status: RecordStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PerformanceRecordListOut:
    private_access = _operator_access(request, mutation=False)
    response.headers["Cache-Control"] = "no-store"
    if private_access and (
        q is not None
        or bool(
            getattr(
                request.state,
                "private_performance_query_blocked",
                False,
            )
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "비공개 실적 조회에서는 URL 검색어를 사용할 수 없습니다. "
                "검색이 필요하면 목록을 승인된 단말 안에서 필터링해 주세요."
            ),
        )
    conditions = []
    if not private_access:
        conditions.append(
            ~CompanyPerformanceRecord.source.like("PRIVATE_IMPORT%")
        )
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
    private_access = _operator_access(request, mutation=True)
    if (
        payload.record_status == "VALIDATED"
        and request.app.state.settings.environment.casefold() == "production"
        and not private_access
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="VALIDATED 실적은 비공개 증빙 전용 인증으로만 생성할 수 있습니다.",
        )
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


@contextmanager
def _private_import_serialization(session: Session):
    """Serialize source replacement across workers and local test threads."""

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _PRIVATE_IMPORT_ADVISORY_LOCK_KEY},
        )
        yield
        return
    with _PRIVATE_IMPORT_PROCESS_LOCK:
        yield


@router.post("/private-import", response_model=PrivatePerformanceImportOut)
def import_private_performance_records(
    payload: PrivatePerformanceImportRequest,
    request: Request,
    session: DbSession,
) -> PrivatePerformanceImportOut:
    require_private_evidence_access(request)
    with _private_import_serialization(session):
        return _apply_private_performance_import_batch(payload, session)


def _apply_private_performance_import_batch(
    payload: PrivatePerformanceImportRequest,
    session: Session,
) -> PrivatePerformanceImportOut:
    """Upsert one bounded private-workbook batch using scoped operator access.

    The raw workbook never crosses this boundary.  Contact/address columns are
    absent from the request model, and the source location is an opaque URI.
    """

    evidence, evidence_registered = _register_private_workbook_evidence(session, payload)
    created = 0
    updated = 0
    unchanged = 0
    archived = 0
    activated = 0
    base_uri = _private_workbook_uri(payload.source_sha256)
    now = datetime.now(timezone.utc)
    verified_replay = evidence.status == "VERIFIED"

    # A replacement is intentionally fail-closed. As soon as a new workbook
    # starts, the previous private register is archived. New rows stay DRAFT
    # until every declared batch and every declared row has arrived.
    if evidence_registered:
        session.execute(
            update(Evidence)
            .where(
                Evidence.evidence_type == "PRIVATE_PERFORMANCE_WORKBOOK",
                Evidence.id != evidence.id,
                Evidence.status.in_({"PENDING", "VERIFIED"}),
            )
            .values(status="SUPERSEDED")
        )
        archived_result = session.execute(
            update(CompanyPerformanceRecord)
            .where(
                CompanyPerformanceRecord.source.in_(
                    {
                        _PRIVATE_IMPORT_SOURCE,
                        _PRIVATE_IMPORT_PENDING_VALIDATED,
                        _PRIVATE_IMPORT_PENDING_DRAFT,
                    }
                ),
                CompanyPerformanceRecord.record_status != "ARCHIVED",
            )
            .values(
                record_status="ARCHIVED",
                revision=CompanyPerformanceRecord.revision + 1,
                updated_by=_PRIVATE_IMPORT_SOURCE,
                updated_at=now,
            )
        )
        archived = int(archived_result.rowcount or 0)

    for imported in payload.records:
        record_key = "private-" + imported.row_key[:40]
        values = _record_values(imported.fields)
        desired_status = str(values["record_status"])
        desired_source = _PRIVATE_IMPORT_SOURCE
        if not verified_replay:
            values["record_status"] = "DRAFT"
            desired_source = (
                _PRIVATE_IMPORT_PENDING_VALIDATED
                if desired_status == "VALIDATED"
                else _PRIVATE_IMPORT_PENDING_DRAFT
            )
        values["evidence_reference"] = (
            f"{base_uri}#{payload.sheet_name}!A{imported.source_row}:Q{imported.source_row}"
        )
        existing = session.scalar(
            select(CompanyPerformanceRecord).where(
                CompanyPerformanceRecord.record_key == record_key
            )
        )
        if existing is None:
            if verified_replay:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="검증 완료된 동일 원본에 새로운 실적 행을 추가할 수 없습니다.",
                )
            session.add(
                CompanyPerformanceRecord(
                    record_key=record_key,
                    source=desired_source,
                    created_by=_PRIVATE_IMPORT_SOURCE,
                    updated_by=_PRIVATE_IMPORT_SOURCE,
                    **values,
                )
            )
            created += 1
            continue
        if _record_matches(existing, values) and existing.source == desired_source:
            unchanged += 1
            continue
        if verified_replay:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="검증 완료된 동일 원본의 실적 내용을 변경할 수 없습니다.",
            )
        for field, value in values.items():
            setattr(existing, field, value)
        existing.source = desired_source
        existing.updated_by = _PRIVATE_IMPORT_SOURCE
        existing.revision += 1
        existing.updated_at = now
        updated += 1

    import_complete = verified_replay
    if not verified_replay:
        metadata = dict(evidence.metadata_json or {})
        received = {
            int(value)
            for value in (metadata.get("received_batches") or [])
            if isinstance(value, int) and not isinstance(value, bool)
        }
        received.add(payload.batch_index)
        metadata["received_batches"] = sorted(received)
        evidence.metadata_json = metadata
        all_batches_received = received == set(range(payload.batch_count))
        if all_batches_received:
            # The application session deliberately disables autoflush. Make
            # this batch visible to the completeness count before activation.
            session.flush()
            staged_sources = {
                _PRIVATE_IMPORT_PENDING_VALIDATED,
                _PRIVATE_IMPORT_PENDING_DRAFT,
            }
            staged_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(CompanyPerformanceRecord)
                    .where(
                        CompanyPerformanceRecord.source.in_(staged_sources),
                        CompanyPerformanceRecord.evidence_reference.like(
                            f"{base_uri}#%"
                        ),
                    )
                )
                or 0
            )
            distinct_anchor_count = int(
                session.scalar(
                    select(
                        func.count(
                            func.distinct(
                                CompanyPerformanceRecord.evidence_reference
                            )
                        )
                    ).where(
                        CompanyPerformanceRecord.source.in_(staged_sources),
                        CompanyPerformanceRecord.evidence_reference.like(
                            f"{base_uri}#%"
                        ),
                    )
                )
                or 0
            )
            if (
                staged_count != payload.source_row_count
                or distinct_anchor_count != payload.source_row_count
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "수신한 비공개 실적 수가 선언된 전체 건수와 일치하지 않아 "
                        "활성화하지 않았습니다."
                    ),
                )
            validated_result = session.execute(
                update(CompanyPerformanceRecord)
                .where(
                    CompanyPerformanceRecord.source
                    == _PRIVATE_IMPORT_PENDING_VALIDATED,
                    CompanyPerformanceRecord.evidence_reference.like(
                        f"{base_uri}#%"
                    ),
                )
                .values(
                    record_status="VALIDATED",
                    source=_PRIVATE_IMPORT_SOURCE,
                    revision=CompanyPerformanceRecord.revision + 1,
                    updated_by=_PRIVATE_IMPORT_SOURCE,
                    updated_at=now,
                )
            )
            activated = int(validated_result.rowcount or 0)
            session.execute(
                update(CompanyPerformanceRecord)
                .where(
                    CompanyPerformanceRecord.source == _PRIVATE_IMPORT_PENDING_DRAFT,
                    CompanyPerformanceRecord.evidence_reference.like(
                        f"{base_uri}#%"
                    ),
                )
                .values(
                    source=_PRIVATE_IMPORT_SOURCE,
                    revision=CompanyPerformanceRecord.revision + 1,
                    updated_by=_PRIVATE_IMPORT_SOURCE,
                    updated_at=now,
                )
            )
            evidence.status = "VERIFIED"
            import_complete = True
    session.commit()
    return PrivatePerformanceImportOut(
        evidence_registered=evidence_registered,
        created=created,
        updated=updated,
        unchanged=unchanged,
        archived=archived,
        activated=activated,
        import_complete=import_complete,
    )


@router.patch("/{record_id}", response_model=PerformanceRecordOut)
def update_performance_record(
    record_id: str,
    payload: PerformanceRecordUpdate,
    request: Request,
    session: DbSession,
) -> PerformanceRecordOut:
    private_access = _operator_access(request, mutation=True)
    item = session.get(CompanyPerformanceRecord, record_id)
    if item is None:
        raise HTTPException(status_code=404, detail="회사 실적을 찾을 수 없습니다.")
    if item.source.startswith("PRIVATE_IMPORT") and not private_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="비공개 실적은 전용 인증으로만 변경할 수 있습니다.",
        )
    if item.source.startswith("PRIVATE_IMPORT"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "원본 SHA에 결합된 비공개 실적은 직접 수정할 수 없습니다. "
                "수정된 원본을 다시 정규화해 교체 적재하세요."
            ),
        )
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
    if (
        request.app.state.settings.environment.casefold() == "production"
        and not private_access
        and (
            item.record_status == "VALIDATED"
            or current.get("record_status") == "VALIDATED"
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="VALIDATED 실적은 비공개 증빙 전용 인증으로만 변경할 수 있습니다.",
        )
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
