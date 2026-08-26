from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from .api import (
    _authoritative_pps_events,
    _mark_pps_job_failed,
    _persist_pps_ingestion_result,
    _pps_notice_key,
    _publication_safe_source_url,
)
from .integrations.pps import PpsApiError, PpsClient
from .manual_analysis import (
    _manual_execution_slot,
    _manual_feature_enabled,
    _require_manual_operator,
    _same_origin_request,
)
from .models import IngestionJob, Notice, NoticeAnalysisPolicy
from .schemas import PpsIngestionRequest


_SEARCH_ROWS_PER_PAGE = 50
_SEARCH_MAX_PAGES = 3
_SEARCH_DEADLINE_SECONDS = 25
_SELECTION_KEY = re.compile(r"^PPS-[A-Za-z0-9_-]{1,140}$")


class PpsDiscoverySearchRequest(BaseModel):
    """Bounded operator-approved title search against the PPS service feed."""

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
    def validate_window(self) -> "PpsDiscoverySearchRequest":
        if self.to_date < self.from_date:
            raise ValueError("to_date must be on or after from_date")
        if (self.to_date - self.from_date).days > 30:
            raise ValueError("나라장터 전체 검색은 한 번에 최대 31일입니다.")
        return self


class PpsDiscoverySaveRequest(BaseModel):
    """Bind a save click to a prior result without accepting provider metadata."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=60)
    from_date: date
    to_date: date
    bid_notice_no: str = Field(min_length=1, max_length=80)
    selection_key: str = Field(min_length=5, max_length=160)

    @field_validator("query", "bid_notice_no", "selection_key")
    @classmethod
    def normalise_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if len(value) < 2:
            raise ValueError("검색어는 공백을 제외하고 2자 이상이어야 합니다.")
        return value

    @field_validator("bid_notice_no")
    @classmethod
    def validate_notice_no(cls, value: str) -> str:
        if not value:
            raise ValueError("bid_notice_no가 필요합니다.")
        return value

    @field_validator("selection_key")
    @classmethod
    def validate_selection_key(cls, value: str) -> str:
        if not _SELECTION_KEY.fullmatch(value):
            raise ValueError("selection_key 형식이 올바르지 않습니다.")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "PpsDiscoverySaveRequest":
        if self.to_date < self.from_date:
            raise ValueError("to_date must be on or after from_date")
        if (self.to_date - self.from_date).days > 30:
            raise ValueError("나라장터 전체 검색은 한 번에 최대 31일입니다.")
        return self


class PpsDiscoveryCandidate(BaseModel):
    selection_key: str | None
    bid_notice_no: str
    revision_no: str
    title: str
    agency: str
    published_at: datetime | None
    deadline: datetime | None
    estimated_amount: float | None
    notice_kind: str
    direct_contract_signal: bool
    source_url: str | None
    already_stored: bool
    stored_notice_key: str | None
    related_revision_stored: bool
    saveable: bool
    save_block_reason: str | None


class PpsDiscoverySearchResponse(BaseModel):
    query: str
    window: dict[str, str]
    api_calls: int
    result_count: int
    truncated: bool
    candidates: list[PpsDiscoveryCandidate]
    message: str


class PpsDiscoverySaveResponse(BaseModel):
    notice_key: str
    bid_notice_no: str
    outcome: Literal["CREATED", "UPDATED", "ALREADY_STORED"]
    status: str
    attachments_discovered: int = 0
    manifest_created: bool = False
    manifest_reused: bool = False
    analysis_started: Literal[False] = False
    openai_calls: Literal[0] = 0
    message: str


router = APIRouter(prefix="/api/v1/pps-discovery", tags=["PPS notice discovery"])


def _require_discovery_operator(request: Request) -> None:
    """Reuse the deliberately narrow public manual-operator boundary."""

    if not _manual_feature_enabled(request):
        raise HTTPException(status_code=404, detail="나라장터 전체 검색 기능이 비활성화되어 있습니다.")
    if not _same_origin_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="홈페이지와 동일한 출처에서만 나라장터 검색을 요청할 수 있습니다.",
        )
    _require_manual_operator(request)


def _fetch_candidates(
    request: Request,
    *,
    query: str,
    from_date: date,
    to_date: date,
) -> tuple[list[dict[str, Any]], int, bool]:
    settings = request.app.state.settings
    if not settings.pps_api_key:
        raise HTTPException(status_code=503, detail="조달청 검색 서비스 설정을 확인해 주세요.")

    try:
        deadline = time.monotonic() + _SEARCH_DEADLINE_SECONDS
        with PpsClient(
            service_key=settings.pps_api_key,
            base_url=settings.pps_base_url,
            timeout_seconds=12,
            max_retries=1,
        ) as client:
            fetched = list(
                client.iter_notices(
                    operation_path=settings.pps_notice_operation,
                    start=from_date,
                    end=to_date,
                    rows=_SEARCH_ROWS_PER_PAGE,
                    max_pages=_SEARCH_MAX_PAGES,
                    extra_params={"bidNtceNm": query},
                    deadline_monotonic=deadline,
                )
            )
            api_calls = client.request_count
            truncated = bool(client.hit_page_limit or client.hit_time_limit)
    except PpsApiError as exc:
        raise HTTPException(status_code=502, detail="조달청 API 검색에 실패했습니다.") from exc

    authoritative, _superseded = _authoritative_pps_events(
        fetched,
        now=datetime.now(timezone.utc),
    )
    authoritative.sort(
        key=lambda item: (
            item.get("provider_changed_at")
            or item.get("published_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            str(item.get("bid_notice_no") or ""),
        ),
        reverse=True,
    )
    return authoritative, api_calls, truncated


def _candidate_selection_key(item: dict[str, Any]) -> str | None:
    if not item.get("bid_notice_no") or not item.get("title") or item.get("deadline") is None:
        return None
    if str(item.get("notice_kind") or "").strip() == "취소공고":
        return None
    return _pps_notice_key(item)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _candidate_block_reason(item: dict[str, Any]) -> str | None:
    if str(item.get("notice_kind") or "").strip() == "취소공고":
        return "취소공고는 새 분석 대상으로 저장할 수 없습니다."
    deadline = item.get("deadline")
    if not item.get("title") or not isinstance(deadline, datetime):
        return "공고명 또는 마감일시가 없어 저장할 수 없습니다."
    if _as_utc(deadline) < datetime.now(timezone.utc):
        return "입찰 마감일시가 지난 공고는 저장할 수 없습니다."
    return None


@router.post("/search", response_model=PpsDiscoverySearchResponse)
def search_pps_notices(
    payload: PpsDiscoverySearchRequest,
    request: Request,
) -> PpsDiscoverySearchResponse:
    """Search PPS without writing notices, jobs, manifests, or model inputs."""

    _require_discovery_operator(request)
    with _manual_execution_slot(request) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="다른 수동 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": "15"},
            )
        rows, api_calls, provider_truncated = _fetch_candidates(
            request,
            query=payload.query,
            from_date=payload.from_date,
            to_date=payload.to_date,
        )

    visible_rows = rows[: payload.limit]
    selection_keys = {
        key
        for item in visible_rows
        if (key := _candidate_selection_key(item)) is not None
    }
    notice_nos = {
        str(item.get("bid_notice_no") or "").strip()
        for item in visible_rows
        if str(item.get("bid_notice_no") or "").strip()
    }
    stored_by_key: dict[str, Notice] = {}
    related_notice_nos: set[str] = set()
    if notice_nos:
        with request.app.state.session_factory() as session:
            stored_rows = list(
                session.scalars(
                    select(Notice).where(Notice.bid_notice_no.in_(notice_nos))
                ).all()
            )
        stored_by_key = {
            notice.notice_key: notice
            for notice in stored_rows
            if notice.notice_key in selection_keys
        }
        related_notice_nos = {notice.bid_notice_no for notice in stored_rows}

    candidates: list[PpsDiscoveryCandidate] = []
    for item in visible_rows:
        selection_key = _candidate_selection_key(item)
        stored = stored_by_key.get(selection_key or "")
        block_reason = _candidate_block_reason(item)
        source_url = item.get("source_url")
        candidates.append(
            PpsDiscoveryCandidate(
                selection_key=selection_key,
                bid_notice_no=str(item.get("bid_notice_no") or ""),
                revision_no=str(item.get("revision_no") or "00"),
                title=str(item.get("title") or ""),
                agency=str(item.get("agency") or ""),
                published_at=item.get("published_at"),
                deadline=item.get("deadline"),
                estimated_amount=item.get("estimated_amount"),
                notice_kind=str(item.get("notice_kind") or ""),
                direct_contract_signal=bool(item.get("direct_contract_signal")),
                source_url=_publication_safe_source_url(
                    str(source_url) if source_url else None
                ),
                already_stored=stored is not None,
                stored_notice_key=stored.notice_key if stored is not None else None,
                related_revision_stored=(
                    str(item.get("bid_notice_no") or "") in related_notice_nos
                ),
                saveable=block_reason is None,
                save_block_reason=block_reason,
            )
        )

    truncated = provider_truncated or len(rows) > payload.limit
    return PpsDiscoverySearchResponse(
        query=payload.query,
        window={"from": payload.from_date.isoformat(), "to": payload.to_date.isoformat()},
        api_calls=api_calls,
        result_count=len(candidates),
        truncated=truncated,
        candidates=candidates,
        message=(
            "나라장터 검색 결과입니다. 저장 전에는 현재 수집 공고나 분석 대상에 포함되지 않습니다."
        ),
    )


@router.post("/save", response_model=PpsDiscoverySaveResponse)
def save_pps_notice(
    payload: PpsDiscoverySaveRequest,
    request: Request,
) -> PpsDiscoverySaveResponse:
    """Re-fetch and idempotently save exactly one operator-selected notice."""

    _require_discovery_operator(request)
    with _manual_execution_slot(request) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="다른 수동 요청을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": "15"},
            )
        rows, api_calls, _truncated = _fetch_candidates(
            request,
            query=payload.query,
            from_date=payload.from_date,
            to_date=payload.to_date,
        )
        selected = next(
            (
                item
                for item in rows
                if str(item.get("bid_notice_no") or "") == payload.bid_notice_no
            ),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=404,
                detail="선택한 공고를 현재 나라장터 검색 결과에서 찾지 못했습니다.",
            )
        block_reason = _candidate_block_reason(selected)
        if block_reason is not None:
            raise HTTPException(status_code=409, detail=block_reason)
        current_selection_key = _candidate_selection_key(selected)
        if current_selection_key != payload.selection_key:
            raise HTTPException(
                status_code=409,
                detail="공고가 정정 또는 연장되었습니다. 최신 결과를 다시 검색해 선택해 주세요.",
            )

        selected = {**selected, "_search_keywords": [payload.query]}
        ingestion_payload = PpsIngestionRequest(
            from_date=payload.from_date,
            to_date=payload.to_date,
            keyword=payload.query,
            page_size=_SEARCH_ROWS_PER_PAGE,
            max_pages=_SEARCH_MAX_PAGES,
            dry_run=False,
        )
        with request.app.state.session_factory() as session:
            # Discovery-saved notices now participate in automatic analysis.
            # Re-saving also clears a marker left by the legacy MANUAL_ONLY
            # behavior so the notice can enter the next daily/backfill plan.
            policy = session.get(NoticeAnalysisPolicy, payload.selection_key)
            if policy is not None:
                session.delete(policy)
                session.commit()

            job = IngestionJob(
                source="PPS_MANUAL_SAVE",
                mode="LIVE",
                status="RUNNING",
                window_json={
                    "from": payload.from_date.isoformat(),
                    "to": payload.to_date.isoformat(),
                },
                keyword=payload.query,
                request_json={
                    "scope": "ONE_OPERATOR_SELECTED_SERVICE_NOTICE",
                    "selection_key": payload.selection_key,
                    "bid_notice_no": payload.bid_notice_no,
                    "credential_exposed": False,
                    "analysis_requested": False,
                },
                notice_keys=[],
                warnings=[],
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            try:
                result = _persist_pps_ingestion_result(
                    payload=ingestion_payload,
                    session=session,
                    job=job,
                    fetched_rows=[selected],
                    api_calls=api_calls,
                    hit_page_limit=False,
                    hit_time_limit=False,
                    profile_truncated=False,
                    keywords_used=[payload.query],
                    provider_query_count=1,
                    department_coverage_count=0,
                )
            except Exception:
                _mark_pps_job_failed(
                    session,
                    job_id=job.id,
                    error_code="PPS_MANUAL_SAVE_ERROR",
                    warning="선택한 공고 저장 중 오류가 발생했습니다.",
                )
                raise

            stored = session.scalar(
                select(Notice).where(Notice.notice_key == payload.selection_key)
            )
            if stored is None:
                raise HTTPException(
                    status_code=409,
                    detail="저장된 최신 공고 상태와 충돌했습니다. 다시 검색해 주세요.",
                )
            stored_status = stored.status

    if result.created:
        outcome: Literal["CREATED", "UPDATED", "ALREADY_STORED"] = "CREATED"
        message = "선택한 공고와 현재 첨부 목록을 저장했습니다. 판단은 아직 실행하지 않았습니다."
    elif result.updated or result.manifests_created:
        outcome = "UPDATED"
        message = "선택한 공고를 나라장터 최신 정보로 갱신했습니다. 판단은 아직 실행하지 않았습니다."
    else:
        outcome = "ALREADY_STORED"
        message = "이미 저장된 동일 공고를 재사용했습니다. 판단은 별도 버튼으로 실행할 수 있습니다."
    return PpsDiscoverySaveResponse(
        notice_key=payload.selection_key,
        bid_notice_no=payload.bid_notice_no,
        outcome=outcome,
        status=stored_status,
        attachments_discovered=result.attachments_discovered,
        manifest_created=bool(result.manifests_created),
        manifest_reused=bool(result.manifests_reused),
        message=message,
    )
