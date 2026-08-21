from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .analysis_api import _backfill_status
from .auth import require_api_key
from .daily_analysis_scope import (
    validated_material_scope,
    validated_source_material_scope,
)
from .daily_operations import daily_briefing
from .models import IngestionJob


# Korea Standard Time has no daylight-saving transitions. A fixed offset keeps
# this production contract portable on minimal Windows/Linux Python images
# that may not ship the optional IANA tzdata package.
KST = timezone(timedelta(hours=9), name="Asia/Seoul")
MAX_DAILY_NOTICES = 3_012
MAX_INGESTION_COUNT = 1_000_000


router = APIRouter(
    prefix="/api/v1/operations",
    tags=["operations"],
    dependencies=[Depends(require_api_key)],
)


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DailyIngestionReadiness(ApiModel):
    job_id: str | None = Field(default=None, max_length=36)
    status: str | None = Field(default=None, max_length=24)
    completed: bool = False
    created: int = Field(default=0, ge=0, le=MAX_INGESTION_COUNT)
    updated: int = Field(default=0, ge=0, le=MAX_INGESTION_COUNT)
    matched: int = Field(default=0, ge=0, le=MAX_INGESTION_COUNT)


class DailyAnalysisReadiness(ApiModel):
    parent_job_id: str | None = Field(default=None, max_length=36)
    parent_status: str | None = Field(default=None, max_length=24)
    terminal: bool = False
    planned: int = Field(default=0, ge=0, le=MAX_DAILY_NOTICES)
    attempted: int = Field(default=0, ge=0, le=MAX_DAILY_NOTICES)
    remaining: int = Field(default=0, ge=0, le=MAX_DAILY_NOTICES)
    in_flight: int = Field(default=0, ge=0, le=MAX_DAILY_NOTICES)
    completed: int = Field(default=0, ge=0, le=MAX_DAILY_NOTICES)
    partial: int = Field(default=0, ge=0, le=MAX_DAILY_NOTICES)
    failed: int = Field(default=0, ge=0, le=MAX_DAILY_NOTICES)
    queue_pending: int = Field(default=0, ge=0, le=MAX_INGESTION_COUNT)


class TeamsDailyReadinessResponse(ApiModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["READY", "RUNNING", "NOT_PLANNED", "FAILED"]
    ready: bool
    reason_code: str = Field(min_length=1, max_length=64)
    kst_date: date
    checked_at: datetime
    retry_after_seconds: int | None = Field(default=None, ge=60, le=900)
    ingestion: DailyIngestionReadiness
    analysis: DailyAnalysisReadiness
    source_calls: dict[Literal["pps", "openai", "teams"], int]


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _kst_day_bounds(now: datetime) -> tuple[date, datetime, datetime]:
    current = _utc(now).astimezone(KST)
    local_start = datetime.combine(current.date(), datetime.min.time(), tzinfo=KST)
    start = local_start.astimezone(timezone.utc)
    return current.date(), start, start + timedelta(days=1)


def _is_in_day(value: datetime | None, start: datetime, end: datetime) -> bool:
    return value is not None and start <= _utc(value) < end


def _bounded(value: object, *, upper: int) -> tuple[int, bool]:
    valid = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= upper
    if valid:
        return int(value), True
    try:
        numeric = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        numeric = 0
    return min(upper, max(0, numeric)), False


def _analysis_counts(
    session: Session,
    parent: IngestionJob,
) -> tuple[DailyAnalysisReadiness, bool]:
    config = parent.request_json if isinstance(parent.request_json, dict) else {}
    chunk_size, chunk_ok = _bounded(config.get("chunk_size", 3), upper=3)
    ttl_hours, ttl_ok = _bounded(config.get("reservation_ttl_hours", 6), upper=24)
    execution_limit, execution_ok = _bounded(config.get("execution_limit", 30), upper=30)
    max_continuations, continuation_ok = _bounded(
        config.get("max_continuations", 128), upper=128
    )
    bounds_ok = all(
        (
            chunk_ok and chunk_size >= 1,
            ttl_ok and ttl_hours >= 1,
            execution_ok and execution_limit >= 1,
            continuation_ok and max_continuations >= 1,
        )
    )
    status = _backfill_status(
        session,
        parent,
        chunk_size=max(1, chunk_size),
        stale_after_hours=max(1, ttl_hours),
        execution_limit=max(1, execution_limit),
        max_continuations=max(1, max_continuations),
        segment_id=(
            str(config["lease_id"])
            if isinstance(config.get("lease_id"), str)
            else None
        ),
    )
    raw_counts = {
        "planned": status.planned,
        "attempted": status.attempted,
        "remaining": status.remaining,
        "in_flight": status.in_flight,
        "completed": status.completed,
        "partial": status.partial,
        "failed": status.failed,
    }
    counts: dict[str, int] = {}
    for key, value in raw_counts.items():
        counts[key], valid = _bounded(value, upper=MAX_DAILY_NOTICES)
        bounds_ok = bounds_ok and valid
    invariants_ok = (
        counts["planned"] == counts["attempted"] + counts["remaining"]
        and counts["attempted"]
        == counts["completed"] + counts["partial"] + counts["failed"]
        and counts["in_flight"] <= counts["remaining"]
    )
    return (
        DailyAnalysisReadiness(
            parent_job_id=parent.id,
            parent_status=parent.status,
            terminal=parent.completed_at is not None,
            **counts,
        ),
        bounds_ok and invariants_ok,
    )


def _ingestion_view(job: IngestionJob | None) -> tuple[DailyIngestionReadiness, bool]:
    if job is None:
        return DailyIngestionReadiness(), True
    values: dict[str, int] = {}
    valid = True
    for output, source in (
        ("created", job.created_count),
        ("updated", job.updated_count),
        ("matched", job.matched),
    ):
        values[output], count_valid = _bounded(source, upper=MAX_INGESTION_COUNT)
        valid = valid and count_valid
    return (
        DailyIngestionReadiness(
            job_id=job.id,
            status=job.status,
            completed=job.status == "COMPLETED" and job.completed_at is not None,
            **values,
        ),
        valid,
    )


def _response(
    *,
    status: Literal["READY", "RUNNING", "NOT_PLANNED", "FAILED"],
    reason_code: str,
    kst_date: date,
    checked_at: datetime,
    ingestion: DailyIngestionReadiness,
    analysis: DailyAnalysisReadiness | None = None,
) -> TeamsDailyReadinessResponse:
    return TeamsDailyReadinessResponse(
        status=status,
        ready=status == "READY",
        reason_code=reason_code,
        kst_date=kst_date,
        checked_at=checked_at,
        retry_after_seconds=None if status == "READY" else 900,
        ingestion=ingestion,
        analysis=analysis or DailyAnalysisReadiness(),
        source_calls={"pps": 0, "openai": 0, "teams": 0},
    )


@router.get("/teams-daily-readiness", response_model=TeamsDailyReadinessResponse)
def teams_daily_readiness(session: DbSession) -> TeamsDailyReadinessResponse:
    """Fail-closed readiness for the scheduled Teams briefing.

    This endpoint only reads durable ingestion/analysis audits and the stored
    daily queue. It never calls PPS/OpenAI/Teams and never mutates a lease.
    """

    now = datetime.now(timezone.utc)
    kst_date, day_start, day_end = _kst_day_bounds(now)

    ingestion_candidates = list(
        session.scalars(
            select(IngestionJob)
            .where(
                IngestionJob.source == "PPS",
                IngestionJob.mode == "LIVE",
                IngestionJob.created_at >= day_start - timedelta(hours=12),
                IngestionJob.created_at < day_end + timedelta(hours=12),
            )
            .order_by(IngestionJob.created_at.desc())
        ).all()
    )
    today_ingestions = [
        job
        for job in ingestion_candidates
        if _is_in_day(job.created_at, day_start, day_end)
        and isinstance(job.window_json, dict)
        and job.window_json.get("to") == kst_date.isoformat()
    ]
    ingestion_job = max(today_ingestions, key=lambda item: _utc(item.created_at), default=None)
    ingestion, ingestion_counts_valid = _ingestion_view(ingestion_job)
    if ingestion_job is None:
        return _response(
            status="NOT_PLANNED",
            reason_code="TODAY_PPS_NOT_PLANNED",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )
    if not ingestion_counts_valid:
        return _response(
            status="FAILED",
            reason_code="TODAY_PPS_COUNT_INVALID",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )
    if ingestion_job.status == "RUNNING" or ingestion_job.completed_at is None:
        return _response(
            status="RUNNING",
            reason_code="TODAY_PPS_RUNNING",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )
    if ingestion_job.status != "COMPLETED":
        return _response(
            status="FAILED",
            reason_code="TODAY_PPS_FAILED",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )
    ingestion_config = (
        ingestion_job.request_json
        if isinstance(ingestion_job.request_json, dict)
        else {}
    )
    ingestion_material_keys = validated_material_scope(ingestion_config)
    if ingestion_material_keys is None:
        return _response(
            status="FAILED",
            reason_code="TODAY_PPS_SCOPE_INVALID",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )

    parent_candidates = list(
        session.scalars(
            select(IngestionJob)
            .where(
                IngestionJob.source == "ANALYSIS_BACKFILL",
                IngestionJob.mode == "LIVE",
                or_(
                    IngestionJob.created_at >= day_start - timedelta(days=3),
                    and_(
                        IngestionJob.completed_at.is_(None),
                        IngestionJob.status.in_(["RUNNING", "PARTIAL"]),
                    ),
                ),
            )
            .order_by(IngestionJob.created_at.desc())
        ).all()
    )
    daily_parents = [
        parent
        for parent in parent_candidates
        if isinstance(parent.request_json, dict)
        and parent.request_json.get("queue_name") == "DAILY"
    ]
    bound_daily_parents = [
        parent
        for parent in daily_parents
        if isinstance(parent.request_json, dict)
        and parent.request_json.get("source_ingestion_job_id") == ingestion_job.id
    ]
    active_parents = [
        parent
        for parent in daily_parents
        if parent.completed_at is None and parent.status in {"RUNNING", "PARTIAL"}
    ]
    if len(active_parents) > 1:
        return _response(
            status="FAILED",
            reason_code="MULTIPLE_ACTIVE_DAILY_PARENTS",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )
    if len(bound_daily_parents) > 1:
        return _response(
            status="FAILED",
            reason_code="MULTIPLE_DAILY_PARENTS_FOR_INGESTION",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )

    parent: IngestionJob | None = active_parents[0] if active_parents else None
    if parent is None:
        parent = bound_daily_parents[0] if bound_daily_parents else None

    if parent is not None:
        analysis, analysis_valid = _analysis_counts(session, parent)
        if not analysis_valid:
            return _response(
                status="FAILED",
                reason_code="DAILY_ANALYSIS_COUNT_INVALID",
                kst_date=kst_date,
                checked_at=now,
                ingestion=ingestion,
                analysis=analysis,
            )
        parent_config = parent.request_json if isinstance(parent.request_json, dict) else {}
        if parent_config.get("source_ingestion_job_id") != ingestion_job.id:
            return _response(
                status="RUNNING",
                reason_code="OTHER_DAILY_ANALYSIS_RUNNING",
                kst_date=kst_date,
                checked_at=now,
                ingestion=ingestion,
                analysis=analysis,
            )
        parent_material_keys = validated_source_material_scope(parent_config)
        scope_covered = (
            parent_material_keys == ingestion_material_keys
            and set(ingestion_material_keys).issubset(set(parent.notice_keys or []))
            and analysis.planned >= len(ingestion_material_keys)
        )
        if not scope_covered:
            return _response(
                status="FAILED",
                reason_code="DAILY_ANALYSIS_SCOPE_INVALID",
                kst_date=kst_date,
                checked_at=now,
                ingestion=ingestion,
                analysis=analysis,
            )
        if parent.completed_at is None:
            return _response(
                status="RUNNING",
                reason_code="DAILY_ANALYSIS_RUNNING",
                kst_date=kst_date,
                checked_at=now,
                ingestion=ingestion,
                analysis=analysis,
            )
        ready = (
            parent.status == "COMPLETED"
            and analysis.terminal
            and analysis.remaining == 0
            and analysis.in_flight == 0
            and analysis.failed == 0
            and analysis.partial == 0
            and analysis.attempted == analysis.planned
        )
        return _response(
            status="READY" if ready else "FAILED",
            reason_code=(
                "DAILY_ANALYSIS_COMPLETE"
                if ready
                else "DAILY_ANALYSIS_TERMINAL_FAILURE"
            ),
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
            analysis=analysis,
        )

    # A zero-work morning may have no durable DAILY parent. It is safe only
    # when today's live ingestion is complete, reported no material changes,
    # there is no active DAILY parent, and the exact stored analysis queue is
    # empty. Every other no-parent state remains fail-closed.
    try:
        queue = daily_briefing(session=session, days=7, limit=1, as_of=now).get(
            "analysis_queue", {}
        )
        queue_pending, queue_valid = _bounded(
            queue.get("pending_total"), upper=MAX_INGESTION_COUNT
        )
    except Exception:  # pragma: no cover - defensive operational boundary
        logging.exception("Teams readiness stored analysis queue check failed")
        return _response(
            status="FAILED",
            reason_code="DAILY_QUEUE_CHECK_FAILED",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
        )
    analysis = DailyAnalysisReadiness(queue_pending=queue_pending)
    if not queue_valid:
        return _response(
            status="FAILED",
            reason_code="DAILY_QUEUE_COUNT_INVALID",
            kst_date=kst_date,
            checked_at=now,
            ingestion=ingestion,
            analysis=analysis,
        )
    ready_empty = (
        ingestion.created == 0
        and ingestion.updated == 0
        and not ingestion_material_keys
        and queue_pending == 0
    )
    return _response(
        status="READY" if ready_empty else "NOT_PLANNED",
        reason_code=("READY_EMPTY" if ready_empty else "DAILY_ANALYSIS_NOT_PLANNED"),
        kst_date=kst_date,
        checked_at=now,
        ingestion=ingestion,
        analysis=analysis,
    )
