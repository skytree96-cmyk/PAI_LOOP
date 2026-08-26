from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import _mark_pps_job_failed, _persist_pps_ingestion_result
from .integrations.pps import KST, PpsApiError, PpsClient
from .models import IngestionJob, Notice, NoticeVersion
from .notice_freshness import authoritative_pps_cancelled_notice_keys
from .pps_enrichment import PPS_METADATA_KIND, PPS_METADATA_SCHEMA
from .schemas import PpsIngestionRequest


RefreshStatus = Literal[
    "CURRENT",
    "REFRESHED",
    "NOT_APPLICABLE",
    "DRY_RUN",
    "INACTIVE",
    "CANCELLED",
    "FAILED",
]


@dataclass(frozen=True, slots=True)
class PpsMetadataRefreshResult:
    status: RefreshStatus
    warnings: tuple[str, ...] = ()
    provider_calls: int = 0

    @property
    def ready(self) -> bool:
        return self.status in {"CURRENT", "REFRESHED", "NOT_APPLICABLE"}


@dataclass(frozen=True, slots=True)
class _NoticeRefreshSnapshot:
    notice_key: str
    bid_notice_no: str
    query_start: date
    query_end: date


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_pps_metadata(
    session: Session,
    *,
    notice_id: str,
) -> NoticeVersion | None:
    return next(
        (
            version
            for version in session.scalars(
                select(NoticeVersion)
                .where(NoticeVersion.notice_id == notice_id)
                .order_by(NoticeVersion.version_no.desc())
            ).all()
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == PPS_METADATA_KIND
        ),
        None,
    )


def _active_notice_status(session: Session, notice: Notice) -> RefreshStatus | None:
    if authoritative_pps_cancelled_notice_keys(session, [notice]):
        return "CANCELLED"
    if notice.status != "OPEN" or _utc(notice.deadline) < datetime.now(timezone.utc):
        return "INACTIVE"
    return None


def _create_refresh_job(
    request: Request,
    *,
    notice: _NoticeRefreshSnapshot,
) -> str:
    with request.app.state.session_factory() as session:
        job = IngestionJob(
            source="PPS_METADATA_REFRESH",
            mode="LIVE",
            status="RUNNING",
            window_json={
                "from": notice.query_start.isoformat(),
                "to": notice.query_end.isoformat(),
            },
            keyword=None,
            request_json={
                "scope": "ONE_ACTIVE_STALE_PPS_NOTICE",
                "notice_key": notice.notice_key,
                "bid_notice_no": notice.bid_notice_no,
                "credential_exposed": False,
                "analysis_requested": False,
            },
            notice_keys=[notice.notice_key],
            warnings=[],
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


def _fail_refresh_job(
    request: Request,
    *,
    job_id: str,
    error_code: str,
    warning: str,
) -> None:
    with request.app.state.session_factory() as session:
        _mark_pps_job_failed(
            session,
            job_id=job_id,
            error_code=error_code,
            warning=warning,
        )


def refresh_pps_metadata_for_analysis(
    request: Request,
    *,
    notice_id: str,
    dry_run: bool,
    deadline_monotonic: float,
) -> PpsMetadataRefreshResult:
    """Repair a stale PPS metadata basis before the potentially billable step.

    The function is deliberately a no-op for inactive/cancelled notices and for
    a current metadata schema. Only an active PPS notice with missing or stale
    metadata crosses the exact-number provider boundary.
    """

    with request.app.state.session_factory() as session:
        notice = session.get(Notice, notice_id)
        if notice is None:
            return PpsMetadataRefreshResult("INACTIVE", ("NOTICE_NOT_FOUND",))
        inactive = _active_notice_status(session, notice)
        if inactive is not None:
            warning = (
                "PPS_NOTICE_CANCELLED"
                if inactive == "CANCELLED"
                else "AUTOMATIC_NOTICE_NOT_ACTIVE"
            )
            return PpsMetadataRefreshResult(inactive, (warning,))
        if not notice.notice_key.upper().startswith("PPS-"):
            # Manual/private notices have no PPS metadata contract. Preserve
            # their existing analysis/enrichment path without touching PPS.
            return PpsMetadataRefreshResult("NOT_APPLICABLE")
        if not notice.bid_notice_no:
            return PpsMetadataRefreshResult(
                "FAILED",
                ("PPS_METADATA_REFRESH_REQUIRED", "PPS_NOTICE_IDENTITY_MISSING"),
            )
        metadata = _latest_pps_metadata(session, notice_id=notice.id)
        if (
            metadata is not None
            and isinstance(metadata.source_payload, dict)
            and metadata.source_payload.get("schema_version") == PPS_METADATA_SCHEMA
        ):
            return PpsMetadataRefreshResult("CURRENT")
        today = datetime.now(KST).date()
        if notice.published_at is not None:
            query_start = _utc(notice.published_at).astimezone(KST).date()
        else:
            # PPS rows normally include the posting timestamp. Retain a bounded
            # recovery window for legacy rows that predate that field.
            query_start = max(
                today - timedelta(days=90),
                _utc(notice.deadline).astimezone(KST).date()
                - timedelta(days=90),
            )
        # The PPS posted-time search applies the date window before the exact
        # notice-number filter. Querying every day from publication to today
        # can exhaust the per-item analysis deadline for older active notices.
        # The original posting date is the authoritative lookup key for this
        # stored revision, so keep the recovery call to one bounded day.
        query_date = min(query_start, today)
        notice_snapshot = _NoticeRefreshSnapshot(
            notice_key=notice.notice_key,
            bid_notice_no=notice.bid_notice_no,
            query_start=query_date,
            query_end=query_date,
        )

    if dry_run:
        return PpsMetadataRefreshResult(
            "DRY_RUN",
            ("PPS_METADATA_REFRESH_REQUIRED", "DRY_RUN_NO_EXTERNAL_CALLS"),
        )
    if time.monotonic() >= deadline_monotonic:
        return PpsMetadataRefreshResult(
            "FAILED",
            ("PPS_METADATA_REFRESH_REQUIRED", "ENRICHMENT_TOTAL_TIMEOUT"),
        )

    settings = request.app.state.settings
    if not settings.pps_api_key:
        return PpsMetadataRefreshResult(
            "FAILED",
            ("PPS_METADATA_REFRESH_REQUIRED", "PPS_METADATA_REFRESH_UNAVAILABLE"),
        )

    job_id = _create_refresh_job(
        request,
        notice=notice_snapshot,
    )
    api_calls = 0
    client: PpsClient | None = None
    try:
        with PpsClient(
            service_key=settings.pps_api_key,
            base_url=settings.pps_base_url,
            timeout_seconds=12,
            max_retries=1,
        ) as client:
            fetched_rows = list(
                client.iter_notices(
                    operation_path=settings.pps_notice_operation,
                    start=notice_snapshot.query_start,
                    end=notice_snapshot.query_end,
                    # The PPS date search applies the posting-day window before
                    # it considers bidNtceNo. Busy posting days can exceed the
                    # former 100-row first page even for an exact notice lookup.
                    # Keep the window to one day, but request the provider's
                    # supported maximum page size and allow a small bounded
                    # continuation so the exact row is not falsely reported as
                    # incomplete.
                    rows=999,
                    max_window_days=30,
                    inquiry_division="1",
                    max_pages=5,
                    extra_params={"bidNtceNo": notice_snapshot.bid_notice_no},
                    deadline_monotonic=deadline_monotonic,
                )
            )
            api_calls = client.request_count
            incomplete_provider_result = bool(
                client.hit_page_limit or getattr(client, "hit_time_limit", False)
            )
    except PpsApiError:
        api_calls = client.request_count if client is not None else api_calls
        _fail_refresh_job(
            request,
            job_id=job_id,
            error_code="PPS_METADATA_REFRESH_API_ERROR",
            warning="조달청 공고 메타데이터 갱신 호출이 실패했습니다.",
        )
        return PpsMetadataRefreshResult(
            "FAILED",
            ("PPS_METADATA_REFRESH_REQUIRED", "PPS_METADATA_REFRESH_API_ERROR"),
            api_calls,
        )
    except Exception:
        api_calls = client.request_count if client is not None else api_calls
        _fail_refresh_job(
            request,
            job_id=job_id,
            error_code="PPS_METADATA_REFRESH_CLIENT_ERROR",
            warning="조달청 공고 메타데이터 갱신 클라이언트가 실패했습니다.",
        )
        return PpsMetadataRefreshResult(
            "FAILED",
            ("PPS_METADATA_REFRESH_REQUIRED", "PPS_METADATA_REFRESH_CLIENT_ERROR"),
            api_calls,
        )

    exact_rows = [
        row
        for row in fetched_rows
        if str(row.get("bid_notice_no") or "").strip()
        == notice_snapshot.bid_notice_no
    ]
    if incomplete_provider_result or not exact_rows:
        code = (
            "PPS_METADATA_REFRESH_INCOMPLETE"
            if incomplete_provider_result
            else "PPS_METADATA_REFRESH_NOT_FOUND"
        )
        _fail_refresh_job(
            request,
            job_id=job_id,
            error_code=code,
            warning="조달청에서 현재 공고번호의 완전한 메타데이터를 확인하지 못했습니다.",
        )
        return PpsMetadataRefreshResult(
            "FAILED",
            ("PPS_METADATA_REFRESH_REQUIRED", code),
            api_calls,
        )

    ingestion_payload = PpsIngestionRequest(
        from_date=notice_snapshot.query_start,
        to_date=notice_snapshot.query_end,
        page_size=999,
        max_pages=5,
        dry_run=False,
    )
    try:
        with request.app.state.session_factory() as session:
            job = session.get(IngestionJob, job_id)
            if job is None:  # pragma: no cover - database invariant
                raise RuntimeError("PPS metadata refresh audit disappeared")
            _persist_pps_ingestion_result(
                payload=ingestion_payload,
                session=session,
                job=job,
                fetched_rows=exact_rows,
                api_calls=api_calls,
                hit_page_limit=False,
                hit_time_limit=False,
                profile_truncated=False,
                keywords_used=[],
                provider_query_count=1,
                department_coverage_count=0,
            )
    except Exception:
        _fail_refresh_job(
            request,
            job_id=job_id,
            error_code="PPS_METADATA_REFRESH_PERSISTENCE_ERROR",
            warning="조달청 공고 메타데이터 갱신 결과를 저장하지 못했습니다.",
        )
        return PpsMetadataRefreshResult(
            "FAILED",
            (
                "PPS_METADATA_REFRESH_REQUIRED",
                "PPS_METADATA_REFRESH_PERSISTENCE_ERROR",
            ),
            api_calls,
        )

    with request.app.state.session_factory() as session:
        refreshed_notice = session.get(Notice, notice_id)
        if refreshed_notice is None:
            return PpsMetadataRefreshResult(
                "INACTIVE",
                ("NOTICE_NOT_FOUND",),
                api_calls,
            )
        inactive = _active_notice_status(session, refreshed_notice)
        if inactive is not None:
            warning = (
                "PPS_NOTICE_CANCELLED"
                if inactive == "CANCELLED"
                else "AUTOMATIC_NOTICE_NOT_ACTIVE"
            )
            return PpsMetadataRefreshResult(inactive, (warning,), api_calls)
        metadata = _latest_pps_metadata(session, notice_id=notice_id)
        if not (
            metadata is not None
            and isinstance(metadata.source_payload, dict)
            and metadata.source_payload.get("schema_version") == PPS_METADATA_SCHEMA
        ):
            return PpsMetadataRefreshResult(
                "FAILED",
                ("PPS_METADATA_REFRESH_REQUIRED", "PPS_METADATA_SCHEMA_NOT_CURRENT"),
                api_calls,
            )

    return PpsMetadataRefreshResult(
        "REFRESHED",
        ("PPS_METADATA_REFRESHED",),
        api_calls,
    )
