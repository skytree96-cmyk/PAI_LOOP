from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .auth import require_api_key
from .integrations.openai_extraction import OpenAIExtractionClient, OpenAITelemetry
from .integrations.prespec import matched_pre_specification_keywords
from .manual_analysis import (
    _manual_execution_slot,
    _manual_feature_enabled,
    _require_manual_operator,
    _same_origin_request,
)
from .models import IngestionJob
from .prespec_models import (
    PreSpecification,
    PreSpecificationAnalysisRun,
    PreSpecificationDocument,
    PreSpecificationVersion,
)
from .prespec_service import (
    PRESPEC_MAX_DOCUMENTS,
    PreSpecificationServiceError,
    analyse_pre_specification_documents,
    current_pre_specification_documents,
    fetch_pre_specifications,
    latest_pre_specification_analysis,
    persist_pre_specification,
    pre_specification_selection_token,
    pre_specification_status,
)


router = APIRouter(prefix="/api/v1", tags=["PPS pre-specifications"])

_REGISTRY_NO = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SELECTION_TOKEN = re.compile(r"^PRESPECSEL-[a-f0-9]{32}$")
_MANUAL_ANALYSIS_SOURCES = ("MANUAL_ANALYSIS", "MANUAL_PRESPEC_ANALYSIS")
PRESPEC_ANALYSIS_STALE_AFTER = timedelta(minutes=20)


def _get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(_get_session)]


class PreSpecificationSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=60)
    from_date: date
    to_date: date
    limit: int = Field(default=25, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def normalise_query(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 2:
            raise ValueError("검색어는 공백을 제외하고 2자 이상이어야 합니다.")
        return cleaned

    @model_validator(mode="after")
    def validate_window(self) -> "PreSpecificationSearchRequest":
        if self.to_date < self.from_date:
            raise ValueError("to_date must be on or after from_date")
        if (self.to_date - self.from_date).days > 30:
            raise ValueError("사전규격 검색은 한 번에 최대 31일입니다.")
        return self


class PreSpecificationSaveRequest(PreSpecificationSearchRequest):
    registry_no: str = Field(min_length=1, max_length=40)
    selection_token: str = Field(min_length=43, max_length=43)

    @field_validator("registry_no")
    @classmethod
    def validate_registry_no(cls, value: str) -> str:
        cleaned = value.strip()
        if not _REGISTRY_NO.fullmatch(cleaned):
            raise ValueError("registry_no 형식이 올바르지 않습니다.")
        return cleaned

    @field_validator("selection_token")
    @classmethod
    def validate_selection_token(cls, value: str) -> str:
        cleaned = value.strip()
        if not _SELECTION_TOKEN.fullmatch(cleaned):
            raise ValueError("selection_token 형식이 올바르지 않습니다.")
        return cleaned


class PreSpecificationCandidate(BaseModel):
    pre_specification_key: str
    registry_no: str
    selection_token: str
    title: str
    ordering_agency: str | None
    demand_agency: str | None
    business_division: str | None
    budget_amount: float | None
    registered_at: datetime | None
    changed_at: datetime | None
    opinion_deadline: datetime | None
    software_business: bool
    linked_bid_notice_nos: list[str]
    matched_keywords: list[str]
    document_count: int
    already_stored: bool
    status: str


class PreSpecificationSearchResponse(BaseModel):
    query: str
    window: dict[str, str]
    provider_status: Literal["COMPLETED", "PARTIAL"]
    api_calls: int
    openai_calls: Literal[0] = 0
    fetched: int
    quarantined: int
    matched: int
    result_count: int
    truncated: bool
    warnings: list[str]
    candidates: list[PreSpecificationCandidate]


class PreSpecificationSaveResponse(BaseModel):
    pre_specification_key: str
    registry_no: str
    outcome: Literal["CREATED", "UPDATED", "ALREADY_STORED"]
    version_created: bool
    linked_notice_count: int
    document_count: int
    provider_status: Literal["COMPLETED", "PARTIAL"]
    api_calls: int
    openai_calls: Literal[0] = 0
    analysis_started: Literal[False] = False
    warnings: list[str]


class PreSpecificationAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_extraction: bool = False


class PreSpecificationAnalysisResponse(BaseModel):
    registry_no: str
    pre_specification_key: str
    analysis_id: str | None
    outcome: Literal[
        "QUEUED",
        "COMPLETED",
        "PARTIAL",
        "REVIEW",
        "FAILED",
        "ALREADY_ANALYZED",
        "COOLDOWN",
    ]
    source_digest: str
    documents_total: int
    documents_processed: int
    openai_calls: int = Field(ge=0, le=10)
    openai_telemetry: OpenAITelemetry = Field(default_factory=OpenAITelemetry)
    warnings: list[str]
    message: str


def _require_pre_spec_operator(request: Request) -> None:
    if not _manual_feature_enabled(request):
        raise HTTPException(status_code=404, detail="사전규격 운영 기능이 비활성화되어 있습니다.")
    if not _same_origin_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="홈페이지와 동일한 출처에서만 사전규격 작업을 요청할 수 있습니다.",
        )
    _require_manual_operator(request)


def _candidate(
    record: dict[str, Any],
    *,
    already_stored: bool,
) -> PreSpecificationCandidate:
    return PreSpecificationCandidate(
        pre_specification_key=str(record["pre_specification_key"]),
        registry_no=str(record["registry_no"]),
        selection_token=pre_specification_selection_token(record),
        title=str(record["title"]),
        ordering_agency=record.get("ordering_agency"),
        demand_agency=record.get("demand_agency"),
        business_division=record.get("business_division"),
        budget_amount=record.get("budget_amount"),
        registered_at=record.get("registered_at"),
        changed_at=record.get("changed_at"),
        opinion_deadline=record.get("opinion_deadline"),
        software_business=bool(record.get("software_business")),
        linked_bid_notice_nos=list(record.get("linked_bid_notice_nos") or []),
        matched_keywords=list(record.get("matched_keywords") or []),
        document_count=len(list(record.get("document_urls") or [])),
        already_stored=already_stored,
        status=pre_specification_status(
            opinion_deadline=record.get("opinion_deadline"),
            linked_bid_notice_nos=list(record.get("linked_bid_notice_nos") or []),
        ),
    )


def _fetch(request: Request, payload: PreSpecificationSearchRequest):
    try:
        return fetch_pre_specifications(
            request.app.state.settings,
            query=payload.query,
            from_date=payload.from_date,
            to_date=payload.to_date,
            client_factory=getattr(request.app.state, "prespec_client_factory", None),
        )
    except PreSpecificationServiceError as exc:
        code = str(exc)
        if code == "PPS_NOT_CONFIGURED":
            raise HTTPException(status_code=503, detail="조달청 사전규격 검색 설정을 확인해 주세요.") from exc
        raise HTTPException(
            status_code=502,
            detail={
                "code": "PPS_PRESPEC_PROVIDER_ERROR",
                "message": "조달청 사전규격 검색에 실패했습니다.",
            },
        ) from exc


@router.post(
    "/prespec-discovery/search",
    response_model=PreSpecificationSearchResponse,
)
def search_pre_specifications(
    payload: PreSpecificationSearchRequest,
    request: Request,
) -> PreSpecificationSearchResponse:
    """Search PPS with no database writes and an invariant zero OpenAI count."""

    _require_pre_spec_operator(request)
    with _manual_execution_slot(request) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail="다른 수동 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": "15"},
            )
        result = _fetch(request, payload)

    visible = result.records[: payload.limit]
    registry_nos = [str(item["registry_no"]) for item in visible]
    stored: set[str] = set()
    if registry_nos:
        with request.app.state.session_factory() as session:
            stored = set(
                session.scalars(
                    select(PreSpecification.registry_no).where(
                        PreSpecification.registry_no.in_(registry_nos)
                    )
                ).all()
            )
    return PreSpecificationSearchResponse(
        query=payload.query,
        window={"from": payload.from_date.isoformat(), "to": payload.to_date.isoformat()},
        provider_status=result.status,  # type: ignore[arg-type]
        api_calls=result.api_calls,
        fetched=result.fetched,
        quarantined=result.quarantined,
        matched=len(result.records),
        result_count=len(visible),
        truncated=result.status == "PARTIAL" or len(result.records) > payload.limit,
        warnings=result.warnings,
        candidates=[
            _candidate(item, already_stored=str(item["registry_no"]) in stored)
            for item in visible
        ],
    )


@router.post(
    "/prespec-discovery/save",
    response_model=PreSpecificationSaveResponse,
)
def save_pre_specification(
    payload: PreSpecificationSaveRequest,
    request: Request,
) -> PreSpecificationSaveResponse:
    """Re-fetch and idempotently persist exactly one selected pre-specification."""

    _require_pre_spec_operator(request)
    with _manual_execution_slot(request) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail="다른 수동 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": "15"},
            )
        fetched = _fetch(request, payload)
        selected = next(
            (
                record
                for record in fetched.records
                if str(record.get("registry_no") or "") == payload.registry_no
            ),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=404,
                detail="선택한 사전규격을 현재 조달청 검색 결과에서 찾지 못했습니다.",
            )
        if pre_specification_selection_token(selected) != payload.selection_token:
            raise HTTPException(
                status_code=409,
                detail="사전규격이 변경되었습니다. 최신 결과를 다시 검색해 선택해 주세요.",
            )
        with request.app.state.session_factory() as session:
            try:
                upsert = persist_pre_specification(
                    session,
                    record=selected,
                    matched_keywords=[payload.query],
                )
                stored = upsert.pre_specification
                document_count = len(
                    current_pre_specification_documents(session, stored)
                )
                job = IngestionJob(
                    source="PPS_PRESPEC_MANUAL_SAVE",
                    mode="LIVE",
                    status="COMPLETED" if fetched.status == "COMPLETED" else "PARTIAL",
                    window_json={
                        "from": payload.from_date.isoformat(),
                        "to": payload.to_date.isoformat(),
                    },
                    keyword=payload.query,
                    request_json={
                        "scope": "ONE_OPERATOR_SELECTED_PRE_SPECIFICATION",
                        "registry_no": payload.registry_no,
                        "pre_specification_key": stored.pre_specification_key,
                        "source_digest": stored.source_digest,
                        "credential_exposed": False,
                        "analysis_requested": False,
                    },
                    api_calls=fetched.api_calls,
                    fetched=fetched.fetched,
                    matched=len(fetched.records),
                    created_count=int(upsert.outcome == "CREATED"),
                    updated_count=int(upsert.outcome == "UPDATED"),
                    duplicate_count=int(upsert.outcome == "ALREADY_STORED"),
                    quarantined_count=fetched.quarantined,
                    notice_keys=[],
                    warnings=fetched.warnings,
                    completed_at=datetime.now(timezone.utc),
                )
                session.add(job)
                session.commit()
            except Exception:
                session.rollback()
                raise
    return PreSpecificationSaveResponse(
        pre_specification_key=stored.pre_specification_key,
        registry_no=stored.registry_no,
        outcome=upsert.outcome,  # type: ignore[arg-type]
        version_created=upsert.version_created,
        linked_notice_count=upsert.linked_notice_count,
        document_count=document_count,
        provider_status=fetched.status,  # type: ignore[arg-type]
        api_calls=fetched.api_calls,
        warnings=fetched.warnings,
    )


def _parse_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    values: dict[str, str] = {}
    raw_values = value.split(",")
    if len(raw_values) > 10:
        raise HTTPException(status_code=422, detail="검색어는 최대 10개까지 지정할 수 있습니다.")
    for raw in raw_values:
        keyword = " ".join(raw.split())
        if len(keyword) > 60:
            raise HTTPException(status_code=422, detail="검색어 하나는 최대 60자입니다.")
        if keyword:
            values.setdefault(keyword.casefold(), keyword)
    return list(values.values())


def _latest_analysis_projection(
    session: Session,
    pre_specification: PreSpecification,
    *,
    include_result: bool,
) -> dict[str, Any] | None:
    analysis = latest_pre_specification_analysis(session, pre_specification)
    if analysis is None:
        return None
    projection: dict[str, Any] = {
        "analysis_id": analysis.id,
        "status": analysis.status,
        "source_digest": analysis.source_digest,
        "warnings": list(analysis.warnings or []),
        "completed_at": analysis.completed_at,
    }
    if include_result:
        projection["result"] = analysis.result_json
        projection["documents"] = list(analysis.document_results or [])
    return projection


def _public_projection(
    session: Session,
    pre_specification: PreSpecification,
    *,
    include_detail: bool,
) -> dict[str, Any]:
    effective_status = pre_specification_status(
        opinion_deadline=pre_specification.opinion_deadline,
        linked_bid_notice_nos=list(pre_specification.linked_bid_notice_nos or []),
    )
    projection: dict[str, Any] = {
        "source_kind": "PPS_PRESPEC",
        "pre_specification_key": pre_specification.pre_specification_key,
        "registry_no": pre_specification.registry_no,
        "title": pre_specification.title,
        "ordering_agency": pre_specification.ordering_agency,
        "demand_agency": pre_specification.demand_agency,
        "business_division": pre_specification.business_division,
        "reference_no": pre_specification.reference_no,
        "budget_amount": pre_specification.budget_amount,
        "registered_at": pre_specification.registered_at,
        "changed_at": pre_specification.changed_at,
        "opinion_deadline": pre_specification.opinion_deadline,
        "delivery_due": pre_specification.delivery_due,
        "software_business": pre_specification.software_business,
        "status": effective_status,
        "linked_bid_notice_nos": list(pre_specification.linked_bid_notice_nos or []),
        "matched_keywords": list(pre_specification.matched_keywords or []),
        "source_digest": pre_specification.source_digest,
        "first_seen_at": pre_specification.first_seen_at,
        "last_seen_at": pre_specification.last_seen_at,
        "analysis": _latest_analysis_projection(
            session,
            pre_specification,
            include_result=include_detail,
        ),
    }
    if include_detail:
        versions = list(
            session.scalars(
                select(PreSpecificationVersion)
                .where(
                    PreSpecificationVersion.pre_specification_id
                    == pre_specification.id
                )
                .order_by(PreSpecificationVersion.version_no.desc())
            ).all()
        )
        documents = current_pre_specification_documents(session, pre_specification)
        projection.update(
            {
                "version_count": len(versions),
                "current_version": versions[0].version_no if versions else None,
                "documents": [
                    {
                        "slot": item.slot,
                        "safe_url": item.safe_url,
                        "source_digest": item.source_digest,
                    }
                    for item in documents
                ],
            }
        )
    return projection


@router.get("/pre-specifications")
def list_pre_specifications(
    session: DbSession,
    _auth: None = Depends(require_api_key),
    status_filter: Literal[
        "OPEN_FOR_OPINION", "OPINION_CLOSED", "LINKED_TO_BID"
    ]
    | None = Query(default=None, alias="status"),
    search_keywords: str | None = Query(default=None, max_length=620),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """Read only stored pre-specifications; this route never calls PPS/OpenAI."""

    keywords = _parse_keywords(search_keywords)
    query = select(PreSpecification).order_by(
        PreSpecification.changed_at.desc(),
        PreSpecification.registry_no.desc(),
    )
    # Keyword/status matching currently includes derived status and the
    # provider-normalised agency fields, so it cannot be expressed as one
    # portable SQL predicate. Scan the complete stored table when filters are
    # present; an arbitrary newest-500 cap makes older saved records
    # undiscoverable and violates the stored-DB search contract.
    if keywords:
        keyword_conditions = []
        for keyword in keywords:
            pattern = f"%{keyword}%"
            keyword_conditions.extend(
                (
                    PreSpecification.title.ilike(pattern),
                    PreSpecification.ordering_agency.ilike(pattern),
                    PreSpecification.demand_agency.ilike(pattern),
                )
            )
        query = query.where(or_(*keyword_conditions))
    if not keywords and not status_filter:
        query = query.limit(limit + 1)
    rows = list(session.scalars(query).all())
    matched: list[PreSpecification] = []
    for item in rows:
        effective_status = pre_specification_status(
            opinion_deadline=item.opinion_deadline,
            linked_bid_notice_nos=list(item.linked_bid_notice_nos or []),
        )
        if status_filter and effective_status != status_filter:
            continue
        if keywords:
            record = {
                "title": item.title,
                "ordering_agency": item.ordering_agency,
                "demand_agency": item.demand_agency,
            }
            if not matched_pre_specification_keywords(record, keywords):
                continue
        matched.append(item)
    visible = matched[:limit]
    return {
        "items": [
            _public_projection(session, item, include_detail=False) for item in visible
        ],
        "result_count": len(visible),
        "truncated": len(matched) > limit,
        "source_calls": {"pps": 0, "openai": 0},
    }


@router.get("/pre-specifications/{registry_no}")
def get_pre_specification(
    registry_no: Annotated[str, Path(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")],
    session: DbSession,
    _auth: None = Depends(require_api_key),
) -> dict[str, Any]:
    stored = session.scalar(
        select(PreSpecification).where(PreSpecification.registry_no == registry_no)
    )
    if stored is None:
        raise HTTPException(status_code=404, detail="사전규격을 찾을 수 없습니다.")
    return {
        **_public_projection(session, stored, include_detail=True),
        "source_calls": {"pps": 0, "openai": 0},
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _fail_stale_analysis(
    session: Session,
    analysis: PreSpecificationAnalysisRun,
    *,
    now: datetime,
) -> set[str]:
    """Fail one abandoned in-process reservation and its audit job.

    Background tasks are process-local, so a restart can leave RUNNING rows
    behind.  A bounded age check lets a later request recover without treating
    a fresh, legitimately running analysis as abandoned.
    """

    if analysis.status != "RUNNING":
        return set()
    if _utc(analysis.created_at) > now - PRESPEC_ANALYSIS_STALE_AFTER:
        return set()
    analysis.status = "FAILED"
    analysis.openai_telemetry = OpenAITelemetry(
        accounting_complete=False
    ).model_dump(mode="json")
    analysis.warnings = sorted(
        set(list(analysis.warnings or []) + ["PRESPEC_ANALYSIS_STALE"])
    )
    analysis.completed_at = now

    stale_job_ids: set[str] = set()
    jobs = list(
        session.scalars(
            select(IngestionJob).where(
                IngestionJob.source == "MANUAL_PRESPEC_ANALYSIS",
                IngestionJob.status == "RUNNING",
            )
        ).all()
    )
    for job in jobs:
        request_json = job.request_json if isinstance(job.request_json, dict) else {}
        if request_json.get("analysis_id") != analysis.id:
            continue
        job.status = "FAILED"
        job.error_code = "PRESPEC_ANALYSIS_STALE"
        job.warnings = sorted(
            set(list(job.warnings or []) + ["PRESPEC_ANALYSIS_STALE"])
        )
        job.completed_at = now
        stale_job_ids.add(job.id)
    session.flush()
    return stale_job_ids


def _analysis_response(
    pre_specification: PreSpecification,
    analysis: PreSpecificationAnalysisRun | None,
    *,
    outcome: str,
    documents_total: int,
    message: str,
) -> PreSpecificationAnalysisResponse:
    telemetry = OpenAITelemetry()
    if analysis is not None:
        try:
            telemetry = OpenAITelemetry.model_validate(analysis.openai_telemetry or {})
        except Exception:
            telemetry = OpenAITelemetry(accounting_complete=False)
    return PreSpecificationAnalysisResponse(
        registry_no=pre_specification.registry_no,
        pre_specification_key=pre_specification.pre_specification_key,
        analysis_id=analysis.id if analysis is not None else None,
        outcome=outcome,  # type: ignore[arg-type]
        source_digest=(
            analysis.source_digest if analysis is not None else pre_specification.source_digest
        ),
        documents_total=documents_total,
        documents_processed=(
            len(list(analysis.document_results or [])) if analysis is not None else 0
        ),
        openai_calls=analysis.openai_calls if analysis is not None else 0,
        openai_telemetry=telemetry,
        warnings=list(analysis.warnings or []) if analysis is not None else [],
        message=message,
    )


@router.post(
    "/pre-specifications/{registry_no}/analysis",
    response_model=PreSpecificationAnalysisResponse,
)
def request_pre_specification_analysis(
    payload: PreSpecificationAnalysisRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    registry_no: Annotated[str, Path(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")],
) -> PreSpecificationAnalysisResponse:
    """Explicitly run bounded analysis under the shared manual spend boundary."""

    _require_pre_spec_operator(request)
    with _manual_execution_slot(request) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=409,
                detail="다른 수동 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": "15"},
            )
        now = datetime.now(timezone.utc)
        with request.app.state.session_factory() as session:
            stored = session.scalar(
                select(PreSpecification).where(
                    PreSpecification.registry_no == registry_no
                )
            )
            if stored is None:
                raise HTTPException(status_code=404, detail="사전규격을 찾을 수 없습니다.")
            documents = current_pre_specification_documents(session, stored)
            if len(documents) > PRESPEC_MAX_DOCUMENTS:
                raise HTTPException(status_code=409, detail="사전규격 문서 상한을 초과했습니다.")
            previous = latest_pre_specification_analysis(session, stored)
            if previous is not None and previous.status == "COMPLETED":
                return _analysis_response(
                    stored,
                    previous,
                    outcome="ALREADY_ANALYZED",
                    documents_total=len(documents),
                    message="현재 사전규격 버전의 분석 결과를 재사용했습니다.",
                )
            recovered_stale_job_ids: set[str] = set()
            recovered_stale = False
            if previous is not None and previous.status == "RUNNING":
                recovered_stale_job_ids = _fail_stale_analysis(
                    session,
                    previous,
                    now=now,
                )
                recovered_stale = previous.status == "FAILED"
                if not recovered_stale:
                    raise HTTPException(
                        status_code=409,
                        detail="현재 사전규격 분석이 이미 실행 중입니다.",
                    )
            if documents and not payload.run_extraction:
                raise HTTPException(
                    status_code=409,
                    detail="현재 사전규격은 모델 호출이 필요합니다. 비용 사용을 명시적으로 승인해 주세요.",
                )

            settings = request.app.state.settings
            cooldown_period = (
                timedelta(minutes=5)
                if previous is not None and previous.openai_calls == 0
                else timedelta(hours=settings.public_manual_analysis_cooldown_hours)
            )
            cooldown_cutoff = now - cooldown_period
            cooldown_jobs = list(
                session.scalars(
                    select(IngestionJob).where(
                        IngestionJob.source == "MANUAL_PRESPEC_ANALYSIS",
                        IngestionJob.created_at >= cooldown_cutoff,
                    )
                ).all()
            )
            recent_same = next(
                (
                    job
                    for job in sorted(
                        cooldown_jobs,
                        key=lambda item: item.created_at,
                        reverse=True,
                    )
                    if _utc(job.created_at) >= cooldown_cutoff
                    and isinstance(job.request_json, dict)
                    and job.request_json.get("registry_no") == registry_no
                    and job.status in {"COMPLETED", "PARTIAL", "REVIEW", "FAILED"}
                    and job.error_code != "PRESPEC_ANALYSIS_STALE"
                ),
                None,
            )
            if (
                recent_same is not None
                and previous is not None
                and not recovered_stale
            ):
                return _analysis_response(
                    stored,
                    previous,
                    outcome="COOLDOWN",
                    documents_total=len(documents),
                    message="최근 분석 시도 후 재시도 대기 시간이 지나지 않았습니다.",
                )
            quota_cutoff = now - timedelta(hours=1)
            recent_manual_jobs = list(
                session.scalars(
                    select(IngestionJob).where(
                        IngestionJob.source.in_(_MANUAL_ANALYSIS_SOURCES),
                        IngestionJob.created_at >= quota_cutoff,
                    )
                ).all()
            )
            counted_manual_jobs = [
                job
                for job in recent_manual_jobs
                if job.id not in recovered_stale_job_ids
                and job.error_code != "PRESPEC_ANALYSIS_STALE"
            ]
            if (
                settings.public_manual_analysis_hourly_limit > 0
                and len(counted_manual_jobs)
                >= settings.public_manual_analysis_hourly_limit
            ):
                raise HTTPException(
                    status_code=429,
                    detail="시간당 수동 분석 한도에 도달했습니다.",
                    headers={"Retry-After": "3600"},
                )
            if documents and not settings.extraction_configured:
                raise HTTPException(status_code=503, detail="모델 분석 설정을 확인해 주세요.")

            request_material = {
                "registry_no": registry_no,
                "source_digest": stored.source_digest,
                "requested_at": now.isoformat(),
            }
            idempotency_key = "PRESPECAN-" + hashlib.sha256(
                json.dumps(request_material, sort_keys=True).encode("utf-8")
            ).hexdigest()
            analysis = PreSpecificationAnalysisRun(
                pre_specification_id=stored.id,
                source_digest=stored.source_digest,
                idempotency_key=idempotency_key,
                status="RUNNING",
                result_json=None,
                document_results=[],
                openai_calls=0,
                openai_telemetry=OpenAITelemetry().model_dump(mode="json"),
                warnings=[],
            )
            session.add(analysis)
            session.flush()
            job = IngestionJob(
                source="MANUAL_PRESPEC_ANALYSIS",
                mode="LIVE",
                status="RUNNING",
                window_json={"scope": "ONE_SAVED_PRE_SPECIFICATION"},
                request_json={
                    "registry_no": registry_no,
                    "pre_specification_key": stored.pre_specification_key,
                    "source_digest": stored.source_digest,
                    "analysis_id": analysis.id,
                    "run_extraction": bool(payload.run_extraction),
                    "max_documents": PRESPEC_MAX_DOCUMENTS,
                    "credential_exposed": False,
                },
                notice_keys=[],
                warnings=[],
            )
            session.add(job)
            session.commit()
            analysis_id = analysis.id
            job_id = job.id
            stored_id = stored.id
            source_digest = stored.source_digest
            detached_documents = list(documents)
        background_tasks.add_task(
            _execute_reserved_pre_specification_analysis,
            request,
            registry_no=registry_no,
            stored_id=stored_id,
            source_digest=source_digest,
            analysis_id=analysis_id,
            job_id=job_id,
            documents=detached_documents,
        )
        return _analysis_response(
            stored,
            analysis,
            outcome="QUEUED",
            documents_total=len(detached_documents),
            message="사전규격 문서 분석을 요청했습니다. 상태 조회로 결과를 확인해 주세요.",
        )


def _execute_reserved_pre_specification_analysis(
    request: Request,
    *,
    registry_no: str,
    stored_id: str,
    source_digest: str,
    analysis_id: str,
    job_id: str,
    documents: list[PreSpecificationDocument],
) -> None:
    """Run the slow provider/model work after the request has returned."""

    with _manual_execution_slot(request, blocking=True):
        try:
            result = analyse_pre_specification_documents(
                registry_no=registry_no,
                source_digest=source_digest,
                documents=documents,
                openai_api_key=request.app.state.settings.extraction_api_key or "",
                openai_model=request.app.state.settings.extraction_model,
                llm_provider=request.app.state.settings.llm_provider,
                llm_gateway_base_url=request.app.state.settings.llm_gateway_base_url,
                document_fetcher=getattr(
                    request.app.state,
                    "prespec_document_fetcher",
                    None,
                ),
                openai_client_factory=getattr(
                    request.app.state,
                    "prespec_openai_client_factory",
                    None,
                )
                or OpenAIExtractionClient,
            )
        except Exception:
            with request.app.state.session_factory() as session:
                analysis = session.get(PreSpecificationAnalysisRun, analysis_id)
                job = session.get(IngestionJob, job_id)
                if analysis is not None:
                    analysis.status = "FAILED"
                    analysis.openai_telemetry = OpenAITelemetry(
                        accounting_complete=False
                    ).model_dump(mode="json")
                    analysis.warnings = ["PRESPEC_ANALYSIS_FAILED"]
                    analysis.completed_at = datetime.now(timezone.utc)
                if job is not None:
                    job.status = "FAILED"
                    job.error_code = "PRESPEC_ANALYSIS_FAILED"
                    job.warnings = ["PRESPEC_ANALYSIS_FAILED"]
                    job.completed_at = datetime.now(timezone.utc)
                session.commit()
            return

        with request.app.state.session_factory() as session:
            analysis = session.get(PreSpecificationAnalysisRun, analysis_id)
            job = session.get(IngestionJob, job_id)
            stored = session.get(PreSpecification, stored_id)
            if analysis is None or job is None or stored is None:
                return
            if stored.source_digest != source_digest:
                analysis.status = "FAILED"
                analysis.warnings = ["PRESPEC_SOURCE_CHANGED"]
                analysis.completed_at = datetime.now(timezone.utc)
                job.status = "FAILED"
                job.error_code = "PRESPEC_SOURCE_CHANGED"
                job.warnings = ["PRESPEC_SOURCE_CHANGED"]
                job.completed_at = datetime.now(timezone.utc)
                session.commit()
                return
            analysis.status = result.status
            analysis.result_json = result.result_json
            analysis.document_results = result.document_results
            analysis.openai_calls = result.openai_calls
            analysis.openai_telemetry = result.openai_telemetry.model_dump(mode="json")
            analysis.warnings = result.warnings
            analysis.completed_at = datetime.now(timezone.utc)
            job.status = result.status
            job.api_calls = result.openai_calls
            job.fetched = len(documents)
            job.matched = len(result.document_results)
            job.warnings = result.warnings
            job.request_json = {
                **dict(job.request_json or {}),
                "openai_telemetry": result.openai_telemetry.model_dump(mode="json"),
            }
            job.completed_at = datetime.now(timezone.utc)
            session.commit()


@router.get(
    "/pre-specifications/{registry_no}/analysis/{analysis_id}",
    response_model=PreSpecificationAnalysisResponse,
)
def get_pre_specification_analysis_request(
    request: Request,
    registry_no: Annotated[str, Path(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")],
    analysis_id: Annotated[str, Path(min_length=36, max_length=36)],
) -> PreSpecificationAnalysisResponse:
    _require_pre_spec_operator(request)
    with request.app.state.session_factory() as session:
        stored = session.scalar(
            select(PreSpecification).where(PreSpecification.registry_no == registry_no)
        )
        if stored is None:
            raise HTTPException(status_code=404, detail="사전규격을 찾을 수 없습니다.")
        analysis = session.get(PreSpecificationAnalysisRun, analysis_id)
        if analysis is None or analysis.pre_specification_id != stored.id:
            raise HTTPException(status_code=404, detail="사전규격 분석 요청을 찾을 수 없습니다.")
        documents = current_pre_specification_documents(session, stored)
        if analysis.status == "RUNNING":
            _fail_stale_analysis(
                session,
                analysis,
                now=datetime.now(timezone.utc),
            )
            if analysis.status == "FAILED":
                session.commit()
                outcome = "FAILED"
                message = "처리 프로세스 종료로 오래된 분석 요청을 실패 처리했습니다. 다시 요청할 수 있습니다."
            else:
                outcome = "QUEUED"
                message = "사전규격 문서 분석을 처리 중입니다."
        elif analysis.status == "COMPLETED":
            outcome = "COMPLETED"
            message = "사전규격 문서의 근거 구조화를 완료했습니다. 입찰 GO 판정은 실행하지 않았습니다."
        elif analysis.status in {"PARTIAL", "REVIEW"}:
            outcome = analysis.status
            message = "처리 가능한 문서를 저장했지만 일부 또는 전체는 사람 검토가 필요합니다."
        else:
            outcome = "FAILED"
            message = "사전규격 문서 분석을 완료하지 못했습니다."
        return _analysis_response(
            stored,
            analysis,
            outcome=outcome,
            documents_total=len(documents),
            message=message,
        )
