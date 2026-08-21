from __future__ import annotations

import uuid
import time
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from .analysis_pipeline import AnalysisPipelineError, run_analysis_pipeline
from .auth import require_api_key
from .models import AnalysisRun, IngestionJob, Notice, NoticeVersion
from .pps_enrichment import (
    MAX_ATTACHMENTS_IN_MANIFEST,
    PpsEnrichmentResult,
    current_pps_attachment_coverage,
    enrich_notice_from_pps,
    has_current_accepted_pps_extraction,
    public_analysis_reason,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AnalysisBatchRequest(ApiModel):
    notice_keys: list[str] = Field(min_length=1, max_length=20)
    dry_run: bool = False
    force: Literal[False] = False
    enrich_missing: bool = False
    max_notices: int = Field(default=3, ge=1, le=3)
    # This is an exact provider-manifest contract, not an operator sampling
    # knob. A smaller value would silently restore the former one-file bug.
    max_attachments_per_notice: Literal[10] = MAX_ATTACHMENTS_IN_MANIFEST
    # Optional parent operation identity used by n8n backfill/daily chunk
    # orchestration. It never changes analysis semantics; it only links the
    # sanitised child audit to a resumable parent run.
    operation_id: str | None = Field(default=None, min_length=36, max_length=36)
    segment_id: str | None = Field(default=None, min_length=36, max_length=36)
    chunk_index: int | None = Field(default=None, ge=0, le=9999)

    @field_validator("notice_keys")
    @classmethod
    def validate_notice_keys(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 160 for item in cleaned):
            raise ValueError("notice_keys must contain 1..160 character values")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("notice_keys must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_operation_claim(self) -> "AnalysisBatchRequest":
        operation_fields = (self.operation_id, self.segment_id, self.chunk_index)
        if any(value is None for value in operation_fields) and any(
            value is not None for value in operation_fields
        ):
            raise ValueError(
                "operation_id, segment_id, and chunk_index must be provided together"
            )
        if self.operation_id is not None:
            if len(self.notice_keys) != 1 or self.max_notices != 1:
                raise ValueError("operation chunks must contain exactly one notice")
        return self


class AnalysisBatchItemOut(ApiModel):
    notice_key: str
    status: Literal["COMPLETED", "SKIPPED", "FAILED"]
    document_status: str
    evaluation_status: str
    snapshot_status: str
    analysis_run_id: str | None = None
    evaluation_id: str | None = None
    notice_version_id: str | None = None
    input_sha256: str | None = None
    reused: bool = False
    materialized_requirements: int = 0
    requirement_snapshots: int = 0
    score_snapshots: int = 0
    recommendation_snapshots: int = 0
    analysis_state: Literal["ANALYZED", "REVIEW", "PENDING"] | None = None
    analysis_reason_code: Literal[
        "ANALYZED",
        "ATTACHMENT_NONE",
        "ATTACHMENT_COVERAGE_INCOMPLETE",
        "HWP_ONLY_UNSUPPORTED",
        "UNSUPPORTED_ATTACHMENT",
        "HWPX_EXTRACT_FAILED",
        "PDF_EXTRACT_FAILED",
        "DOCUMENT_EXTRACT_FAILED",
        "OPENAI_REVIEW",
        "QUOTE_UNVERIFIED",
        "NOT_SELECTED",
    ] | None = None
    analysis_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    attachments_discovered: int = 0
    attachments_audited: int = 0
    attachments_supported: int = 0
    attachments_accepted: int = 0
    attachment_coverage_complete: bool = False
    all_supported_attachments_accepted: bool = False


class AnalysisAttachmentEnrichmentOut(ApiModel):
    attachment_id: str
    media_type: str
    status: Literal["COMPLETED", "REUSED", "REVIEW", "PLANNED"]
    reason_code: str
    attempted: bool = False
    content_extracted: bool = False
    source_read_complete: bool = False
    analysis_input_complete: bool = False
    source_characters: int = Field(default=0, ge=0, le=2_000_000)
    analysis_input_characters: int = Field(default=0, ge=0, le=120_000)
    members_discovered: int = Field(default=0, ge=0, le=1_024)
    members_processed: int = Field(default=0, ge=0, le=1_024)
    openai_calls: int = Field(default=0, ge=0, le=2)
    version_id: str | None = None


class AnalysisEnrichmentOut(ApiModel):
    requested: int = 0
    attempted: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    attachments_discovered: int = 0
    attachments_attempted: int = 0
    attachments_processed: int = 0
    downloaded_bytes: int = Field(default=0, ge=0, le=80 * 1024 * 1024)
    source_characters: int = Field(default=0, ge=0, le=20_000_000)
    analysis_input_characters: int = Field(default=0, ge=0, le=1_200_000)
    source_read_complete: bool = False
    analysis_input_complete: bool = False
    members_discovered: int = Field(default=0, ge=0, le=10_240)
    members_processed: int = Field(default=0, ge=0, le=10_240)
    openai_calls: int = 0
    warnings: list[str] = Field(default_factory=list)
    attachment_results: list[AnalysisAttachmentEnrichmentOut] = Field(default_factory=list)


class AnalysisBatchResponse(ApiModel):
    job_id: str
    status: Literal["COMPLETED", "PARTIAL"]
    dry_run: bool
    requested: int
    processed: int
    completed: int
    skipped: int
    failed: int
    document_materialized: int
    evaluations_created: int
    snapshots_refreshed: int
    openai_calls: int
    results: list[AnalysisBatchItemOut]
    warnings: list[str]
    enrichment: AnalysisEnrichmentOut


class AnalysisBackfillPlanRequest(ApiModel):
    queue_name: Literal["BACKFILL", "DAILY", "ANY"] = "BACKFILL"
    # 3,000 is the upstream daily ingestion hard bound. A daily operation may
    # append up to twelve cooled backlog keys behind that exact created+updated
    # union, so the durable parent bound is intentionally 3,012.
    notice_keys: list[str] = Field(default_factory=list, max_length=3012)
    # DAILY callers identify the exact updated/attachment-changed partition.
    # A stable notice_key can then be reopened only when its persisted work
    # token changed, while a retried 08:00 request remains idempotent.
    refresh_notice_keys: list[str] = Field(default_factory=list, max_length=3000)
    retry_notice_keys: list[str] = Field(default_factory=list, max_length=12)
    retry_epoch: str | None = Field(
        default=None,
        min_length=10,
        max_length=10,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    # Stable for one n8n execution and therefore also for an HTTP node retry.
    # The active lease stores this owner token so a lost plan response can be
    # replayed exactly without exposing chunks to a different execution.
    request_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,119}$",
    )
    dry_run: bool = False
    # Multi-attachment notices can use the full provider manifest (up to ten
    # files), so a durable operation lease is intentionally one notice. This
    # prevents an older workflow from recreating an unbounded three-notice HTTP
    # request while retaining the generic protected batch API for diagnostics.
    chunk_size: Literal[1] = 1
    max_total: int = Field(default=300, ge=1, le=3012)
    execution_limit: int = Field(default=30, ge=1, le=30)
    # 3,012 keys / 30 per execution requires 101 segments. 128 leaves bounded
    # recovery headroom without permitting an unbounded continuation loop.
    max_continuations: int = Field(default=128, ge=1, le=128)
    include_retryable: bool = False
    retry_cooldown_hours: int = Field(default=24, ge=1, le=168)
    reservation_ttl_hours: int = Field(default=6, ge=1, le=24)
    resume_active: bool = True
    resume_only: bool = False
    resume_job_id: str | None = Field(default=None, min_length=36, max_length=36)

    @field_validator("notice_keys", "refresh_notice_keys", "retry_notice_keys")
    @classmethod
    def validate_explicit_notice_keys(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 160 for item in cleaned):
            raise ValueError("notice_keys must contain 1..160 character values")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("notice_keys must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_resume_mode(self) -> "AnalysisBackfillPlanRequest":
        if self.queue_name == "ANY" and (
            self.notice_keys
            or self.refresh_notice_keys
            or self.retry_notice_keys
            or self.retry_epoch is not None
            or not self.resume_only
        ):
            raise ValueError("ANY queue is allowed only for resume_only polling")
        if self.refresh_notice_keys:
            if self.queue_name != "DAILY":
                raise ValueError("refresh_notice_keys are allowed only for DAILY")
            if not set(self.refresh_notice_keys).issubset(self.notice_keys):
                raise ValueError("refresh_notice_keys must be a subset of notice_keys")
        if self.retry_notice_keys:
            if self.queue_name != "DAILY":
                raise ValueError("retry_notice_keys are allowed only for DAILY")
            if not set(self.retry_notice_keys).issubset(self.notice_keys):
                raise ValueError("retry_notice_keys must be a subset of notice_keys")
            if self.retry_epoch is None:
                raise ValueError("retry_epoch is required with retry_notice_keys")
        elif self.retry_epoch is not None:
            raise ValueError("retry_epoch requires retry_notice_keys")
        return self


class AnalysisBackfillPlanResponse(ApiModel):
    job_id: str | None
    segment_id: str | None
    status: Literal["RUNNING", "COMPLETED", "PARTIAL", "DEAD_LETTER", "NO_ACTIVE"]
    queue_name: Literal["BACKFILL", "DAILY", "ANY"]
    dry_run: bool
    policy: Literal["OPEN_NOT_SELECTED_THEN_COOLED_RETRY"]
    chunk_size: int
    planned: int
    attempted: int
    remaining: int
    in_flight: int
    offered: int
    continuation_required: bool
    continuation_round: int
    max_continuations: int
    completed: int
    partial: int
    failed: int
    child_jobs: int
    openai_calls: int
    notice_keys: list[str]
    chunks: list[list[str]]
    chunk_indices: list[int]
    warnings: list[str]
    note: str


class AnalysisBackfillCompleteRequest(ApiModel):
    """Acknowledge exactly one durably leased execution segment."""

    segment_id: str = Field(min_length=36, max_length=36)


router = APIRouter(
    prefix="/api/v1",
    tags=["analysis persistence"],
    dependencies=[Depends(require_api_key)],
)


# A single fixed namespace lock serialises the match-or-create arbitration for
# every DAILY/BACKFILL planner. Queue-specific locks are insufficient because a
# 08:00 DAILY request and a manual BACKFILL request can contain the same key.
# PostgreSQL provides cross-process safety; SQLite and other test dialects use
# an in-process fallback so concurrent TestClient requests exercise the same
# contract. 0x5041494C is the stable ASCII namespace "PAIL".
_PLANNER_ADVISORY_LOCK_KEY = 0x5041494C
_PLANNER_PROCESS_LOCK = threading.RLock()


def _serialize_analysis_planner(function):
    @wraps(function)
    def wrapped(payload: AnalysisBackfillPlanRequest, session: DbSession):
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _PLANNER_ADVISORY_LOCK_KEY},
            )
            return function(payload, session)
        with _PLANNER_PROCESS_LOCK:
            return function(payload, session)

    return wrapped


def _serialize_analysis_completion(function):
    """Use the planner arbitration order for segment completion too.

    Both paths acquire the fixed advisory/process lock before the parent row.
    This prevents a 08:00 append from racing a 15-minute completion between
    active-parent selection and durable lease persistence.
    """

    @wraps(function)
    def wrapped(
        job_id: str,
        payload: AnalysisBackfillCompleteRequest,
        session: DbSession,
    ):
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _PLANNER_ADVISORY_LOCK_KEY},
            )
            return function(job_id, payload, session)
        with _PLANNER_PROCESS_LOCK:
            return function(job_id, payload, session)

    return wrapped


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _create_batch_job(
    request: Request,
    payload: AnalysisBatchRequest,
) -> tuple[str, AnalysisBatchResponse | None]:
    job_id = str(uuid.uuid4())
    claim_generations: dict[str, int] = {}
    with request.app.state.session_factory() as session:
        if payload.operation_id is not None:
            parent = session.scalar(
                select(IngestionJob)
                .where(IngestionJob.id == payload.operation_id)
                .with_for_update()
            )
            if parent is None or parent.source != "ANALYSIS_BACKFILL":
                raise HTTPException(status_code=404, detail="analysis operation not found")
            if parent.status not in {"RUNNING", "PARTIAL"} or parent.completed_at is not None:
                raise HTTPException(status_code=409, detail="analysis operation is not active")
            if any(key not in set(parent.notice_keys or []) for key in payload.notice_keys):
                raise HTTPException(status_code=409, detail="chunk contains an unplanned notice key")
            parent_config = (
                parent.request_json if isinstance(parent.request_json, dict) else {}
            )
            parent_generations = _parent_work_generations(parent)
            claim_generations = {
                key: parent_generations.get(key, 0) for key in payload.notice_keys
            }
            if parent_config.get("lease_id") != payload.segment_id:
                raise HTTPException(
                    status_code=409,
                    detail="segment_id is not the active analysis lease",
                )
            lease_started_raw = parent_config.get("lease_started_at")
            try:
                lease_started = _utc(datetime.fromisoformat(str(lease_started_raw)))
            except (TypeError, ValueError):
                lease_started = None
            stale_cutoff = datetime.now(timezone.utc) - timedelta(
                hours=max(
                    1,
                    min(24, int(parent_config.get("reservation_ttl_hours", 6))),
                )
            )
            if lease_started is None or lease_started < stale_cutoff:
                raise HTTPException(
                    status_code=409,
                    detail="analysis segment lease expired; request a continuation plan",
                )
            leased_chunks = parent_config.get("leased_chunks")
            if not isinstance(leased_chunks, list):
                raise HTTPException(status_code=409, detail="analysis segment has no chunk map")
            expected_chunk = next(
                (
                    entry
                    for entry in leased_chunks
                    if isinstance(entry, dict)
                    and entry.get("chunk_index") == payload.chunk_index
                ),
                None,
            )
            if expected_chunk is None or expected_chunk.get("notice_keys") != payload.notice_keys:
                raise HTTPException(
                    status_code=409,
                    detail="chunk_index and notice_keys do not match the active segment",
                )
            requested_keys = set(payload.notice_keys)
            for child in _backfill_children(session, parent.id):
                child_config = (
                    child.request_json if isinstance(child.request_json, dict) else {}
                )
                child_keys = set(child.notice_keys or [])
                same_index = child_config.get("chunk_index") == payload.chunk_index
                overlap = any(
                    key in requested_keys
                    and _child_key_is_effective(
                        child,
                        key,
                        parent_generations.get(key, 0),
                    )
                    for key in child_keys
                )
                if _terminalize_stale_analysis_child(
                    child,
                    parent_generations=parent_generations,
                    stale_cutoff=stale_cutoff,
                    now=datetime.now(timezone.utc),
                ):
                    continue
                requeue_keys = child_config.get("requeue_notice_keys")
                if isinstance(requeue_keys, list) and any(
                    key in requeue_keys for key in child_keys
                ):
                    # A terminalized stale claim is retained for audit only.
                    # It must never shadow the stored response of the newer
                    # effective child during an exact HTTP retry.
                    continue
                if not same_index and not overlap:
                    continue
                if same_index and list(child.notice_keys or []) != payload.notice_keys:
                    raise HTTPException(
                        status_code=409,
                        detail="chunk_index is already bound to different notice keys",
                    )
                if child.status == "RUNNING":
                    raise HTTPException(
                        status_code=409,
                        detail="analysis chunk is already in flight",
                    )
                if same_index:
                    stored = child_config.get("result_json")
                    if isinstance(stored, dict):
                        return child.id, AnalysisBatchResponse.model_validate(stored)
                    raise HTTPException(
                        status_code=409,
                        detail="analysis chunk was already processed",
                    )
                if overlap:
                    raise HTTPException(
                        status_code=409,
                        detail="notice key was already claimed by another chunk",
                    )
        request_json: dict[str, Any] = {
            "notice_count": len(payload.notice_keys),
            "dry_run": payload.dry_run,
            "force": False,
            "enrich_missing": payload.enrich_missing,
            "max_notices": payload.max_notices,
            "max_attachments_per_notice": payload.max_attachments_per_notice,
        }
        if payload.operation_id is not None:
            request_json["parent_job_id"] = payload.operation_id
            request_json["segment_id"] = payload.segment_id
            request_json["chunk_index"] = payload.chunk_index
            request_json["work_generations"] = claim_generations
        session.add(
            IngestionJob(
                id=job_id,
                source="ANALYSIS",
                mode="DRY_RUN" if payload.dry_run else "LIVE",
                status="RUNNING",
                window_json={"scope": "NOTICE_KEYS"},
                request_json=request_json,
                matched=len(payload.notice_keys),
                notice_keys=list(payload.notice_keys),
            )
        )
        session.commit()
    return job_id, None


def _store_batch_response(
    request: Request,
    *,
    job_id: str,
    response: AnalysisBatchResponse,
) -> None:
    with request.app.state.session_factory() as session:
        job = session.get(IngestionJob, job_id)
        if job is None:  # pragma: no cover - database invariant
            raise RuntimeError("analysis batch audit job disappeared")
        request_json = dict(job.request_json or {})
        request_json["result_json"] = response.model_dump(mode="json")
        if "ATTACHMENT_CONTINUATION_REQUIRED" in response.enrichment.warnings:
            # A bounded request persisted all completed attachment attempts but
            # intentionally stopped before the next worst-case unit. Retain the
            # child as an audit row while making its notice key non-terminal so
            # the parent planner leases it again with a new chunk index.
            request_json["requeue_notice_keys"] = [
                item.notice_key for item in response.results
            ]
        job.request_json = request_json
        session.commit()


_RETRYABLE_ANALYSIS_CODES = {
    # Legacy/manual records can have a valid Evaluation but no AnalysisRun
    # snapshot.  Re-running the pipeline reuses their accepted extraction and
    # materialises the missing snapshot without an OpenAI call.
    "ANALYZED",
    "HWPX_EXTRACT_FAILED",
    "PDF_EXTRACT_FAILED",
    "DOCUMENT_EXTRACT_FAILED",
    "ATTACHMENT_COVERAGE_INCOMPLETE",
    "HWP_ONLY_UNSUPPORTED",
    "UNSUPPORTED_ATTACHMENT",
    "OPENAI_REVIEW",
    "QUOTE_UNVERIFIED",
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _backfill_children(session: Session, job_id: str) -> list[IngestionJob]:
    children = list(
        session.scalars(
            select(IngestionJob)
            .where(IngestionJob.source == "ANALYSIS")
            .order_by(IngestionJob.created_at)
        ).all()
    )
    return [
        child
        for child in children
        if isinstance(child.request_json, dict)
        and child.request_json.get("parent_job_id") == job_id
    ]


def _parent_work_generations(parent: IngestionJob) -> dict[str, int]:
    config = parent.request_json if isinstance(parent.request_json, dict) else {}
    raw = config.get("work_generations")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, int) and value >= 0
    }


def _child_work_generation(child: IngestionJob, key: str) -> int:
    config = child.request_json if isinstance(child.request_json, dict) else {}
    raw = config.get("work_generations")
    if isinstance(raw, dict):
        value = raw.get(key, 0)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _child_key_is_effective(
    child: IngestionJob,
    key: str,
    current_generation: int,
) -> bool:
    config = child.request_json if isinstance(child.request_json, dict) else {}
    requeue = config.get("requeue_notice_keys")
    return (
        _child_work_generation(child, key) == current_generation
        and not (isinstance(requeue, list) and key in requeue)
    )


def _terminalize_stale_analysis_child(
    child: IngestionJob,
    *,
    parent_generations: dict[str, int],
    stale_cutoff: datetime,
    now: datetime,
) -> bool:
    if child.status != "RUNNING" or _utc(child.created_at) >= stale_cutoff:
        return False
    child_keys = list(child.notice_keys or [])
    superseded = any(
        _child_work_generation(child, key) != parent_generations.get(key, 0)
        for key in child_keys
    )
    code = (
        "SUPERSEDED_STALE_ANALYSIS_CLAIM"
        if superseded
        else "STALE_ANALYSIS_CLAIM"
    )
    config = dict(child.request_json or {})
    config["requeue_notice_keys"] = child_keys
    child.request_json = config
    child.status = "FAILED"
    child.error_code = code
    child.warnings = sorted(set([*(child.warnings or []), code]))
    child.completed_at = now
    return True


def _effective_terminal_keys(
    parent: IngestionJob,
    children: list[IngestionJob],
) -> set[str]:
    generations = _parent_work_generations(parent)
    return {
        key
        for child in children
        if child.status != "RUNNING"
        for key in (child.notice_keys or [])
        if _child_key_is_effective(child, key, generations.get(key, 0))
    }


def _notice_work_tokens(session: Session, keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    notices = list(
        session.scalars(
            select(Notice)
            .where(Notice.notice_key.in_(keys))
            .options(selectinload(Notice.versions))
        ).all()
    )
    tokens: dict[str, str] = {}
    for notice in notices:
        # Only provider-authored notice metadata is an authoritative input
        # token. Analysis itself appends OPENAI_REQUIREMENT_EXTRACTION output
        # versions; including those would make a successful child falsely
        # supersede itself on an idempotent 08:00 retry.
        latest = max(
            (
                version
                for version in notice.versions
                if isinstance(version.source_payload, dict)
                and version.source_payload.get("kind") == "PPS_NOTICE_METADATA"
            ),
            key=lambda item: item.version_no,
            default=None,
        )
        updated = _utc(notice.updated_at or notice.created_at).isoformat()
        version_identity = (
            f"{latest.id}:{latest.file_sha256}" if latest is not None else "NO_VERSION"
        )
        tokens[notice.notice_key] = f"{updated}:{version_identity}"
    return tokens


def _eligible_retry_notice_keys(
    session: Session,
    keys: list[str],
    *,
    now: datetime,
    cooldown_hours: int,
) -> set[str]:
    if not keys:
        return set()
    notices = list(
        session.scalars(
            select(Notice)
            .where(Notice.notice_key.in_(keys))
            .options(
                selectinload(Notice.versions),
                selectinload(Notice.evaluations),
            )
        ).all()
    )
    cutoff = now - timedelta(hours=cooldown_hours)
    eligible: set[str] = set()
    for notice in notices:
        source_kind = (
            "PPS" if notice.notice_key.upper().startswith("PPS-") else "MANUAL"
        )
        reason = public_analysis_reason(
            notice.versions,
            evaluated=bool(notice.evaluations),
            source_kind=source_kind,
        )
        attempt_at = max(
            (_utc(version.created_at) for version in notice.versions),
            default=_utc(notice.published_at or notice.created_at),
        )
        if reason.reason_code in _RETRYABLE_ANALYSIS_CODES and attempt_at <= cutoff:
            eligible.add(notice.notice_key)
    return eligible


def _never_attempted_notice_keys(
    session: Session,
    keys: list[str],
) -> set[str]:
    """Return mislabeled queue keys that are ordinary first-attempt work.

    The daily briefing exposes an explicitly partitioned queue, but this
    defensive classification keeps older workflow payloads from dropping a
    ``NOT_SELECTED`` key merely because they labeled every backlog item as a
    retry.  Such keys remain generation-zero work and never receive a retry
    epoch token.
    """

    if not keys:
        return set()
    notices = list(
        session.scalars(
            select(Notice)
            .where(Notice.notice_key.in_(keys))
            .options(
                selectinload(Notice.versions),
                selectinload(Notice.evaluations),
            )
        ).all()
    )
    never_attempted: set[str] = set()
    for notice in notices:
        source_kind = (
            "PPS" if notice.notice_key.upper().startswith("PPS-") else "MANUAL"
        )
        reason = public_analysis_reason(
            notice.versions,
            evaluated=bool(notice.evaluations),
            source_kind=source_kind,
        )
        if reason.reason_code == "NOT_SELECTED":
            never_attempted.add(notice.notice_key)
    return never_attempted


def _completed_retry_epoch_keys(
    session: Session,
    keys: list[str],
    *,
    retry_epoch: str | None,
) -> set[str]:
    """Find retry keys already executed by a terminal DAILY operation.

    A downstream n8n failure can cause the whole 08:00 workflow to be run
    again after its analysis parent has completed.  The active-parent token is
    therefore insufficient: terminal parent+child audits form the durable
    same-epoch dedupe ledger.  A token is consumed only when the key has an
    effective terminal child, so a parent that died before dispatch does not
    suppress legitimate recovery work.
    """

    if not keys or retry_epoch is None:
        return set()
    requested = set(keys)
    consumed: set[str] = set()
    parents = list(
        session.scalars(
            select(IngestionJob).where(
                IngestionJob.source == "ANALYSIS_BACKFILL",
                IngestionJob.completed_at.is_not(None),
            )
        ).all()
    )
    parent_ids = {parent.id for parent in parents}
    children_by_parent: dict[str, list[IngestionJob]] = {
        parent_id: [] for parent_id in parent_ids
    }
    if parent_ids:
        for child in session.scalars(
            select(IngestionJob)
            .where(IngestionJob.source == "ANALYSIS")
            .order_by(IngestionJob.created_at)
        ).all():
            child_config = (
                child.request_json if isinstance(child.request_json, dict) else {}
            )
            parent_id = child_config.get("parent_job_id")
            if parent_id in children_by_parent:
                children_by_parent[parent_id].append(child)
    for parent in parents:
        config = parent.request_json if isinstance(parent.request_json, dict) else {}
        if config.get("queue_name") != "DAILY":
            continue
        raw_tokens = config.get("retry_tokens")
        if not isinstance(raw_tokens, dict):
            continue
        candidate_keys = {
            key
            for key in requested
            if raw_tokens.get(key) == retry_epoch
        }
        if not candidate_keys:
            continue
        terminal_keys = _effective_terminal_keys(
            parent,
            children_by_parent[parent.id],
        )
        consumed.update(candidate_keys & terminal_keys)
    return consumed


def _matching_active_backfill(
    session: Session,
    payload: AnalysisBackfillPlanRequest,
    *,
    now: datetime,
) -> IngestionJob | None:
    if not payload.resume_active:
        return None
    candidates = list(
        session.scalars(
            select(IngestionJob)
            .where(
                IngestionJob.source == "ANALYSIS_BACKFILL",
                IngestionJob.status.in_(["RUNNING", "PARTIAL"]),
                IngestionJob.completed_at.is_(None),
            )
            .order_by(IngestionJob.created_at)
            .with_for_update()
        ).all()
    )
    compatible: list[IngestionJob] = []
    for candidate in candidates:
        # Re-check after acquiring the row lock. Completion may have won the
        # race before this SELECT; a terminal row must never receive new keys.
        if candidate.status not in {"RUNNING", "PARTIAL"} or candidate.completed_at is not None:
            continue
        config = candidate.request_json if isinstance(candidate.request_json, dict) else {}
        if (
            (
                payload.queue_name == "ANY"
                or str(config.get("queue_name", "BACKFILL")) == payload.queue_name
            )
            and
            bool(config.get("dry_run")) == payload.dry_run
            and int(config.get("chunk_size", 0)) == payload.chunk_size
            and bool(config.get("include_retryable")) == payload.include_retryable
            and int(config.get("retry_cooldown_hours", 0))
            == payload.retry_cooldown_hours
        ):
            compatible.append(candidate)
    compatible.sort(
        key=lambda candidate: (
            # Exact response-loss recovery owns its lease regardless of ANY's
            # normal DAILY-first queue priority. Otherwise a new DAILY parent
            # could hide the BACKFILL segment created by the same W11 request.
            0
            if payload.request_token is not None
            and isinstance(candidate.request_json, dict)
            and candidate.request_json.get("lease_request_token")
            == payload.request_token
            else 1,
            0
            if payload.queue_name == "ANY"
            and isinstance(candidate.request_json, dict)
            and candidate.request_json.get("queue_name") == "DAILY"
            else 1,
            _utc(candidate.created_at),
        )
    )
    return compatible[0] if compatible else None


def _reserved_backfill_keys(
    session: Session,
    *,
    now: datetime,
    ttl_hours: int,
) -> set[str]:
    parents = list(
        session.scalars(
            select(IngestionJob).where(
                IngestionJob.source == "ANALYSIS_BACKFILL",
                IngestionJob.status.in_(["RUNNING", "PARTIAL"]),
                IngestionJob.completed_at.is_(None),
            )
        ).all()
    )
    reserved: set[str] = set()
    for parent in parents:
        terminal_keys = _effective_terminal_keys(
            parent,
            _backfill_children(session, parent.id),
        )
        reserved.update(
            key for key in (parent.notice_keys or []) if key not in terminal_keys
        )
    return reserved


def _select_backfill_notice_keys(
    session: Session,
    payload: AnalysisBackfillPlanRequest,
    *,
    now: datetime,
) -> list[str]:
    notices = list(
        session.scalars(
            select(Notice)
            .where(
                Notice.status == "OPEN",
                Notice.deadline >= now,
            )
            .options(
                selectinload(Notice.versions),
                selectinload(Notice.evaluations),
            )
        ).all()
    )
    reserved = _reserved_backfill_keys(
        session,
        now=now,
        ttl_hours=payload.reservation_ttl_hours,
    )
    never_attempted: list[tuple[datetime, str]] = []
    retryable: list[tuple[datetime, str]] = []
    retry_cutoff = now - timedelta(hours=payload.retry_cooldown_hours)
    for notice in notices:
        if notice.notice_key in reserved:
            continue
        source_kind = "PPS" if notice.notice_key.upper().startswith("PPS-") else "MANUAL"
        reason = public_analysis_reason(
            notice.versions,
            evaluated=bool(notice.evaluations),
            source_kind=source_kind,
        )
        observed_at = _utc(notice.published_at or notice.created_at)
        attempt_at = max(
            (_utc(version.created_at) for version in notice.versions),
            default=observed_at,
        )
        if reason.reason_code == "NOT_SELECTED":
            never_attempted.append((observed_at, notice.notice_key))
        elif (
            payload.include_retryable
            and reason.reason_code in _RETRYABLE_ANALYSIS_CODES
            and attempt_at <= retry_cutoff
        ):
            retryable.append((attempt_at, notice.notice_key))
    # New notices first, then cooled retry work oldest-attempt-first. This
    # guarantees that a persistent provider/document failure cannot starve a
    # notice that has never received an analysis attempt.
    never_attempted.sort(key=lambda row: (row[0], row[1]), reverse=True)
    retryable.sort(key=lambda row: (row[0], row[1]))
    return [key for _, key in [*never_attempted, *retryable]]


def _backfill_status(
    session: Session,
    parent: IngestionJob,
    *,
    chunk_size: int,
    stale_after_hours: int,
    execution_limit: int,
    max_continuations: int,
    offer_next: bool = False,
    segment_id: str | None = None,
    chunk_start_index: int = 0,
) -> AnalysisBackfillPlanResponse:
    now = datetime.now(timezone.utc)
    config = parent.request_json if isinstance(parent.request_json, dict) else {}
    current_generations = _parent_work_generations(parent)
    planned_keys = list(parent.notice_keys or [])
    planned_set = set(planned_keys)
    children = _backfill_children(session, parent.id)
    stale_cutoff = now - timedelta(hours=stale_after_hours)
    terminal_outcomes: dict[str, str] = {}
    in_flight_keys: set[str] = set()
    warnings = list(parent.warnings or [])
    openai_calls = 0
    for child in children:
        child_config = child.request_json if isinstance(child.request_json, dict) else {}
        stored_result = child_config.get("result_json")
        if isinstance(stored_result, dict):
            stored_calls = stored_result.get("openai_calls", 0)
            if isinstance(stored_calls, int) and stored_calls >= 0:
                openai_calls += stored_calls
        warnings.extend(str(value) for value in (child.warnings or []) if value)
        is_stale_running = (
            child.status == "RUNNING" and _utc(child.created_at) < stale_cutoff
        )
        if is_stale_running:
            warnings.append("STALE_CHILD_REQUEUED")
            continue
        if child.status == "RUNNING":
            # Even a superseded generation must finish (or become stale)
            # before the same stable key is offered again. This avoids old
            # and new document versions writing snapshots concurrently.
            in_flight_keys.update(
                key for key in (child.notice_keys or []) if key in planned_set
            )
        else:
            child_keys = list(child.notice_keys or [])
            result_outcomes: dict[str, str] = {}
            if isinstance(stored_result, dict) and isinstance(
                stored_result.get("results"), list
            ):
                for item in stored_result["results"]:
                    if not isinstance(item, dict):
                        continue
                    key = item.get("notice_key")
                    status_value = item.get("status")
                    if isinstance(key, str) and status_value in {
                        "COMPLETED",
                        "SKIPPED",
                        "FAILED",
                    }:
                        result_outcomes[key] = str(status_value)
            if not result_outcomes:
                # Legacy/synthetic terminal rows may predate stored result
                # details. Preserve an exact attempted partition using their
                # audited counters; current rows always use result_json.
                fallback = [
                    *(["COMPLETED"] * max(0, int(child.created_count or 0))),
                    *(["SKIPPED"] * max(0, int(child.duplicate_count or 0))),
                    *(["FAILED"] * max(0, int(child.quarantined_count or 0))),
                ]
                for index, key in enumerate(child_keys):
                    result_outcomes[key] = (
                        fallback[index] if index < len(fallback) else "PARTIAL"
                    )
            for key in child_keys:
                if (
                    key in planned_set
                    and _child_key_is_effective(
                        child,
                        key,
                        current_generations.get(key, 0),
                    )
                ):
                    # Children are ordered oldest to newest; a stale claim
                    # followed by a successful retry resolves to the latest
                    # effective outcome for this key and never double-counts.
                    terminal_outcomes[key] = result_outcomes.get(key, "PARTIAL")
    # A newer in-flight retry is authoritative over an older terminal claim
    # of the same generation until it reaches a terminal audit itself.
    for key in in_flight_keys:
        terminal_outcomes.pop(key, None)
    terminal_keys = set(terminal_outcomes)
    completed = sum(value == "COMPLETED" for value in terminal_outcomes.values())
    failed = sum(value == "FAILED" for value in terminal_outcomes.values())
    partial = len(terminal_outcomes) - completed - failed
    remaining_keys = [key for key in planned_keys if key not in terminal_keys]
    claimable_keys = [key for key in remaining_keys if key not in in_flight_keys]
    # Never open a second segment while any non-stale child is still running.
    # This server-side gate is independent of n8n workflow concurrency and
    # prevents overlapping 15-minute schedules from multiplying OpenAI calls.
    offered_keys = (
        claimable_keys[:execution_limit]
        if offer_next and not in_flight_keys
        else []
    )
    chunks = [
        offered_keys[index : index + chunk_size]
        for index in range(0, len(offered_keys), chunk_size)
    ]
    chunk_indices = list(
        range(chunk_start_index, chunk_start_index + len(chunks))
    )
    response_status = parent.status
    if response_status not in {"RUNNING", "COMPLETED", "PARTIAL", "DEAD_LETTER"}:
        response_status = "PARTIAL"
    continuation_round = max(0, int(config.get("continuation_round", 0)))
    if (
        remaining_keys
        and not in_flight_keys
        and segment_id is None
        and continuation_round >= max_continuations
    ):
        response_status = "DEAD_LETTER"
        offered_keys = []
        chunks = []
        chunk_indices = []
        warnings.append("MAX_CONTINUATIONS_EXCEEDED")
    queue_name = str(config.get("queue_name", "BACKFILL"))
    if queue_name not in {"BACKFILL", "DAILY"}:  # pragma: no cover - DB invariant
        queue_name = "BACKFILL"
    return AnalysisBackfillPlanResponse(
        job_id=parent.id,
        segment_id=segment_id,
        status=response_status,
        queue_name=queue_name,
        dry_run=parent.mode == "DRY_RUN",
        policy="OPEN_NOT_SELECTED_THEN_COOLED_RETRY",
        chunk_size=chunk_size,
        planned=len(planned_keys),
        attempted=len(terminal_keys),
        remaining=len(remaining_keys),
        in_flight=len(in_flight_keys),
        offered=len(offered_keys),
        continuation_required=bool(remaining_keys),
        continuation_round=continuation_round,
        max_continuations=max_continuations,
        completed=completed,
        partial=partial,
        failed=failed,
        child_jobs=len(children),
        openai_calls=openai_calls,
        notice_keys=offered_keys,
        chunks=chunks,
        chunk_indices=chunk_indices,
        warnings=sorted(set(warnings)),
        note=(
            "신규·정정 OPEN 공고를 우선 예약하고, 선택된 경우에만 cooldown이 지난 재시도 대상을 뒤에 배치합니다. 각 chunk는 최대 3건, 실행당 제공량은 고정되며 child audit로 다음 continuation을 계산합니다."
        ),
    )


@router.post(
    "/operations/analysis-backfills/plan",
    response_model=AnalysisBackfillPlanResponse,
)
@_serialize_analysis_planner
def plan_analysis_backfill(
    payload: AnalysisBackfillPlanRequest,
    session: DbSession,
) -> AnalysisBackfillPlanResponse:
    """Reserve a resumable snapshot of OPEN analysis work.

    This endpoint does not call PPS/OpenAI and does not analyse documents. n8n
    expands the returned list into independent three-notice calls to the
    existing analysis batch endpoint. An active reservation is reused so a
    retried workflow cannot create a second concurrent sweep of the same keys.
    """

    now = datetime.now(timezone.utc)
    parent: IngestionJob | None = None
    planner_mutated = False
    if payload.resume_job_id is not None:
        parent = session.scalar(
            select(IngestionJob)
            .where(IngestionJob.id == payload.resume_job_id)
            .with_for_update()
        )
        if parent is None or parent.source != "ANALYSIS_BACKFILL":
            raise HTTPException(status_code=404, detail="analysis backfill not found")
        if parent.status not in {"RUNNING", "PARTIAL", "COMPLETED"}:
            raise HTTPException(status_code=409, detail="analysis backfill cannot be resumed")
    else:
        parent = _matching_active_backfill(session, payload, now=now)

    if parent is None and payload.resume_only:
        return AnalysisBackfillPlanResponse(
            job_id=None,
            segment_id=None,
            status="NO_ACTIVE",
            queue_name=payload.queue_name,
            dry_run=payload.dry_run,
            policy="OPEN_NOT_SELECTED_THEN_COOLED_RETRY",
            chunk_size=payload.chunk_size,
            planned=0,
            attempted=0,
            remaining=0,
            in_flight=0,
            offered=0,
            continuation_required=False,
            continuation_round=0,
            max_continuations=payload.max_continuations,
            completed=0,
            partial=0,
            failed=0,
            child_jobs=0,
            openai_calls=0,
            notice_keys=[],
            chunks=[],
            chunk_indices=[],
            warnings=[],
            note="재개할 활성 분석 operation이 없습니다. audit row를 생성하지 않았습니다.",
        )

    if parent is None:
        initial_retry_eligible: set[str] = set()
        initial_rejected_retry_count = 0
        initial_consumed_retry_count = 0
        if payload.notice_keys:
            reserved = _reserved_backfill_keys(
                session,
                now=now,
                ttl_hours=payload.reservation_ttl_hours,
            )
            eligible = set(
                session.scalars(
                    select(Notice.notice_key).where(
                        Notice.notice_key.in_(payload.notice_keys),
                        Notice.status == "OPEN",
                        Notice.deadline >= now,
                    )
                ).all()
            )
            consumed_retry_keys = _completed_retry_epoch_keys(
                session,
                payload.retry_notice_keys,
                retry_epoch=payload.retry_epoch,
            )
            initial_consumed_retry_count = len(consumed_retry_keys)
            retry_candidates = [
                key
                for key in payload.retry_notice_keys
                if key not in consumed_retry_keys
            ]
            initial_retry_eligible = _eligible_retry_notice_keys(
                session,
                retry_candidates,
                now=now,
                cooldown_hours=payload.retry_cooldown_hours,
            )
            initial_never_attempted = _never_attempted_notice_keys(
                session,
                retry_candidates,
            )
            rejected_retry = (
                set(retry_candidates)
                - initial_retry_eligible
                - initial_never_attempted
            )
            initial_rejected_retry_count = len(rejected_retry)
            notice_keys = [
                key
                for key in payload.notice_keys
                if key in eligible
                and key not in reserved
                and (
                    key not in consumed_retry_keys
                    or key in set(payload.refresh_notice_keys)
                )
                and (
                    key not in rejected_retry
                    or key in set(payload.refresh_notice_keys)
                )
            ]
        else:
            notice_keys = _select_backfill_notice_keys(session, payload, now=now)
        if len(notice_keys) > payload.max_total:
            raise HTTPException(
                status_code=409,
                detail="analysis scope exceeds max_total; increase the explicit durable plan bound",
            )
        work_tokens = _notice_work_tokens(session, notice_keys)
        parent = IngestionJob(
            source="ANALYSIS_BACKFILL",
            mode="DRY_RUN" if payload.dry_run else "LIVE",
            status="RUNNING" if notice_keys else "COMPLETED",
            window_json={
                "scope": "OPEN_NOT_SELECTED",
                "as_of": now.isoformat(),
            },
            request_json={
                "queue_name": payload.queue_name,
                "dry_run": payload.dry_run,
                "chunk_size": payload.chunk_size,
                "max_total": payload.max_total,
                "execution_limit": payload.execution_limit,
                "max_continuations": payload.max_continuations,
                "include_retryable": payload.include_retryable,
                "retry_cooldown_hours": payload.retry_cooldown_hours,
                "reservation_ttl_hours": payload.reservation_ttl_hours,
                "policy": "OPEN_NOT_SELECTED_THEN_COOLED_RETRY",
                "continuation_round": 0,
                "work_tokens": work_tokens,
                "work_generations": {key: 0 for key in notice_keys},
                "retry_tokens": {
                    key: payload.retry_epoch
                    for key in initial_retry_eligible
                    if payload.retry_epoch is not None
                },
            },
            matched=len(notice_keys),
            notice_keys=notice_keys,
            warnings=[
                *(
                    [f"RETRY_KEYS_NOT_ELIGIBLE:{initial_rejected_retry_count}"]
                    if initial_rejected_retry_count
                    else []
                ),
                *(
                    [f"RETRY_EPOCH_ALREADY_CONSUMED:{initial_consumed_retry_count}"]
                    if initial_consumed_retry_count
                    else []
                ),
            ],
            completed_at=now if not notice_keys else None,
        )
        session.add(parent)
        # Keep the advisory lock and any matched parent row lock until the
        # lease/no-work response is durably committed at the function exit.
        # An intermediate commit would reopen the complete-vs-plan race.
        session.flush()
        planner_mutated = True
    elif payload.notice_keys and parent.completed_at is None:
        # A 08:00 daily run may discover new/updated keys while a prior day's
        # continuation is still active. Append them ahead of the old remaining
        # queue but never re-add child-audited keys.
        children = _backfill_children(session, parent.id)
        attempted = _effective_terminal_keys(parent, children)
        old_remaining = [
            key for key in (parent.notice_keys or []) if key not in attempted
        ]
        eligible = set(
            session.scalars(
                select(Notice.notice_key).where(
                    Notice.notice_key.in_(payload.notice_keys),
                    Notice.status == "OPEN",
                    Notice.deadline >= now,
                )
            ).all()
        )
        incoming = [
            key
            for key in payload.notice_keys
            if key in eligible and key not in attempted
        ]
        parent_config = dict(parent.request_json or {})
        raw_tokens = parent_config.get("work_tokens")
        work_tokens = dict(raw_tokens) if isinstance(raw_tokens, dict) else {}
        work_generations = _parent_work_generations(parent)
        current_tokens = _notice_work_tokens(session, list(eligible))
        refresh_set = set(payload.refresh_notice_keys)
        retry_eligible = _eligible_retry_notice_keys(
            session,
            payload.retry_notice_keys,
            now=now,
            cooldown_hours=payload.retry_cooldown_hours,
        )
        retry_labeled_never_attempted = _never_attempted_notice_keys(
            session,
            payload.retry_notice_keys,
        )
        raw_retry_tokens = parent_config.get("retry_tokens")
        retry_tokens = (
            dict(raw_retry_tokens) if isinstance(raw_retry_tokens, dict) else {}
        )
        rejected_retry_count = len(
            set(payload.retry_notice_keys)
            - retry_eligible
            - retry_labeled_never_attempted
        )
        if rejected_retry_count:
            parent.warnings = sorted(
                set(
                    [
                        *(parent.warnings or []),
                        f"RETRY_KEYS_NOT_ELIGIBLE:{rejected_retry_count}",
                    ]
                )
            )
        for key in payload.notice_keys:
            if key not in eligible:
                continue
            current_token = current_tokens.get(key)
            previous_token = work_tokens.get(key)
            should_increment = False
            if previous_token is None:
                # Compatibility for an active operation created before the
                # version-aware contract: an explicit updated key that has
                # already completed must supersede generation zero once.
                if key in refresh_set and key in attempted:
                    should_increment = True
                else:
                    work_generations.setdefault(key, 0)
                if current_token is not None:
                    work_tokens[key] = current_token
            elif (
                key in refresh_set
                and current_token is not None
                and current_token != previous_token
            ):
                should_increment = True
                work_tokens[key] = current_token
            if key in retry_eligible and payload.retry_epoch is not None:
                if retry_tokens.get(key) != payload.retry_epoch:
                    if key in attempted:
                        should_increment = True
                    retry_tokens[key] = payload.retry_epoch
            if should_increment:
                work_generations[key] = work_generations.get(key, 0) + 1
        parent_config["work_tokens"] = work_tokens
        parent_config["work_generations"] = work_generations
        parent_config["retry_tokens"] = retry_tokens
        parent.request_json = parent_config
        incoming_set = set(incoming)
        attempted_order = [
            key for key in (parent.notice_keys or []) if key in attempted
        ]
        parent.notice_keys = [
            *attempted_order,
            *incoming,
            *(key for key in old_remaining if key not in incoming_set),
        ]
        parent.matched = len(parent.notice_keys)
        session.flush()
        planner_mutated = True

    # Acquire the durable segment lease while holding the parent row lock.
    # PostgreSQL serialises competing 08:00/15-minute planners here; SQLite
    # ignores FOR UPDATE but remains deterministic in single-process tests.
    parent = session.scalar(
        select(IngestionJob)
        .where(IngestionJob.id == parent.id)
        .with_for_update()
    )
    assert parent is not None  # database invariant
    config = dict(parent.request_json or {})
    chunk_size = int(config.get("chunk_size", payload.chunk_size))
    stale_after_hours = int(
        config.get("reservation_ttl_hours", payload.reservation_ttl_hours)
    )
    execution_limit = max(
        1, min(30, int(config.get("execution_limit", payload.execution_limit)))
    )
    config["execution_limit"] = execution_limit
    configured_continuations = int(
        config.get("max_continuations", payload.max_continuations)
    )
    # Upgrade only the shipped v0.8 pre-release bound. Explicit smaller bounds
    # remain meaningful for fail-closed tests and operator-created jobs.
    if configured_continuations == 96 and payload.max_continuations >= 128:
        configured_continuations = 128
    max_continuations = max(1, min(128, configured_continuations))
    config["max_continuations"] = max_continuations
    lease_id = config.get("lease_id")
    lease_started_raw = config.get("lease_started_at")
    lease_started: datetime | None = None
    if isinstance(lease_started_raw, str):
        try:
            lease_started = _utc(datetime.fromisoformat(lease_started_raw))
        except ValueError:
            lease_started = None
    lease_active = bool(
        isinstance(lease_id, str)
        and lease_started is not None
        and lease_started >= now - timedelta(hours=stale_after_hours)
    )
    parent_generations = _parent_work_generations(parent)
    stale_cutoff = now - timedelta(hours=stale_after_hours)
    stale_children_cleaned = False
    for child in _backfill_children(session, parent.id):
        stale_children_cleaned = _terminalize_stale_analysis_child(
            child,
            parent_generations=parent_generations,
            stale_cutoff=stale_cutoff,
            now=now,
        ) or stale_children_cleaned
    if lease_active:
        active_status = _backfill_status(
            session,
            parent,
            chunk_size=chunk_size,
            stale_after_hours=stale_after_hours,
            execution_limit=execution_limit,
            max_continuations=max_continuations,
            offer_next=False,
            segment_id=lease_id,
        )
        same_request_retry = bool(
            payload.request_token is not None
            and config.get("lease_request_token") == payload.request_token
        )
        if same_request_retry:
            leased_chunks_raw = config.get("leased_chunks")
            leased_chunks = [
                list(entry["notice_keys"])
                for entry in leased_chunks_raw
                if isinstance(entry, dict)
                and isinstance(entry.get("notice_keys"), list)
                and isinstance(entry.get("chunk_index"), int)
            ] if isinstance(leased_chunks_raw, list) else []
            leased_indices = [
                int(entry["chunk_index"])
                for entry in leased_chunks_raw
                if isinstance(entry, dict)
                and isinstance(entry.get("notice_keys"), list)
                and isinstance(entry.get("chunk_index"), int)
            ] if isinstance(leased_chunks_raw, list) else []
            leased_notice_keys = [key for chunk in leased_chunks for key in chunk]
            active_status = active_status.model_copy(
                update={
                    "offered": len(leased_notice_keys),
                    "notice_keys": leased_notice_keys,
                    "chunks": leased_chunks,
                    "chunk_indices": leased_indices,
                    "note": "동일 n8n execution의 plan 응답 재시도이므로 기존 segment lease와 exact chunks를 재반환했습니다.",
                }
            )
        if stale_children_cleaned or planner_mutated:
            session.commit()
        return active_status
    recovered_stale_lease = bool(lease_id)
    if lease_id:
        config.pop("lease_id", None)
        config.pop("lease_started_at", None)
        config.pop("leased_keys", None)
        config.pop("leased_chunks", None)
        config.pop("lease_request_token", None)
        config["lease_recovery_count"] = int(config.get("lease_recovery_count", 0)) + 1
        parent.warnings = sorted(set([*(parent.warnings or []), "STALE_LEASE_RECOVERED"]))

    chunk_start_index = int(config.get("next_chunk_index", 0))
    planned = _backfill_status(
        session,
        parent,
        chunk_size=chunk_size,
        stale_after_hours=stale_after_hours,
        execution_limit=execution_limit,
        max_continuations=max_continuations,
        offer_next=True,
        chunk_start_index=chunk_start_index,
    )
    if not planned.offered:
        parent.request_json = config
        if (
            planned.remaining == 0
            and planned.in_flight == 0
            and parent.completed_at is None
        ):
            # A chunk execution can finish durably and then lose the n8n
            # `/complete` request.  Once that lease is stale, terminal child
            # audits are authoritative: close the parent here instead of
            # letting an otherwise finished operation starve every ANY poll.
            parent.fetched = planned.attempted
            parent.created_count = planned.completed
            parent.quarantined_count = planned.failed
            parent.status = (
                "PARTIAL" if planned.partial or planned.failed else "COMPLETED"
            )
            parent.error_code = (
                "BACKFILL_CHILD_FAILURE" if planned.failed else None
            )
            parent.completed_at = now
            if planned.failed:
                parent.warnings = sorted(
                    set([*(parent.warnings or []), "BACKFILL_CHILD_FAILURE"])
                )
            if recovered_stale_lease:
                parent.warnings = sorted(
                    set([*(parent.warnings or []), "STALE_SEGMENT_AUTO_FINALIZED"])
                )
            config["last_auto_finalized_at"] = now.isoformat()
            parent.request_json = config
        elif planned.status == "DEAD_LETTER":
            parent.status = "DEAD_LETTER"
            parent.error_code = "MAX_CONTINUATIONS_EXCEEDED"
            parent.completed_at = now
            parent.warnings = sorted(
                set([*(parent.warnings or []), "MAX_CONTINUATIONS_EXCEEDED"])
            )
        session.commit()
        session.refresh(parent)
        return _backfill_status(
            session,
            parent,
            chunk_size=chunk_size,
            stale_after_hours=stale_after_hours,
            execution_limit=execution_limit,
            max_continuations=max_continuations,
        )
    new_lease_id = str(uuid.uuid4())
    next_round = int(config.get("continuation_round", 0)) + 1
    config["lease_id"] = new_lease_id
    config["lease_started_at"] = now.isoformat()
    config["leased_keys"] = list(planned.notice_keys)
    config["leased_chunks"] = [
        {"chunk_index": index, "notice_keys": keys}
        for index, keys in zip(planned.chunk_indices, planned.chunks, strict=True)
    ]
    config["lease_request_token"] = payload.request_token
    config["next_chunk_index"] = chunk_start_index + len(planned.chunks)
    config["continuation_round"] = next_round
    parent.request_json = config
    session.commit()
    return planned.model_copy(
        update={"segment_id": new_lease_id, "continuation_round": next_round}
    )


@router.get(
    "/operations/analysis-backfills/{job_id}",
    response_model=AnalysisBackfillPlanResponse,
)
def get_analysis_backfill(
    job_id: str,
    session: DbSession,
) -> AnalysisBackfillPlanResponse:
    parent = session.get(IngestionJob, job_id)
    if parent is None or parent.source != "ANALYSIS_BACKFILL":
        raise HTTPException(status_code=404, detail="analysis backfill not found")
    config = parent.request_json if isinstance(parent.request_json, dict) else {}
    return _backfill_status(
        session,
        parent,
        chunk_size=max(1, min(3, int(config.get("chunk_size", 3)))),
        stale_after_hours=max(
            1,
            min(24, int(config.get("reservation_ttl_hours", 6))),
        ),
        execution_limit=max(1, min(30, int(config.get("execution_limit", 30)))),
        max_continuations=max(
            1, min(128, int(config.get("max_continuations", 128)))
        ),
        segment_id=(
            str(config["lease_id"])
            if isinstance(config.get("lease_id"), str)
            else None
        ),
    )


@router.post(
    "/operations/analysis-backfills/{job_id}/complete",
    response_model=AnalysisBackfillPlanResponse,
)
@_serialize_analysis_completion
def complete_analysis_backfill(
    job_id: str,
    payload: AnalysisBackfillCompleteRequest,
    session: DbSession,
) -> AnalysisBackfillPlanResponse:
    """Acknowledge a terminal segment and update the aggregate parent audit.

    Completion never offers the next segment. The 15-minute planner obtains a
    fresh durable lease, preventing a lost HTTP response from creating an
    untracked fan-out in the same n8n execution.
    """

    parent = session.scalar(
        select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
    )
    if parent is None or parent.source != "ANALYSIS_BACKFILL":
        raise HTTPException(status_code=404, detail="analysis backfill not found")
    config = dict(parent.request_json or {})
    if config.get("lease_id") != payload.segment_id:
        if (
            config.get("lease_id") is None
            and config.get("last_finalized_segment_id") == payload.segment_id
        ):
            # The first commit may have succeeded while its HTTP response was
            # lost. Return the exact current aggregate without mutation so the
            # n8n HTTP retry succeeds. Once a new lease exists, this old
            # segment is no longer replayable and falls through to 409.
            return _backfill_status(
                session,
                parent,
                chunk_size=max(1, min(3, int(config.get("chunk_size", 3)))),
                stale_after_hours=max(
                    1,
                    min(24, int(config.get("reservation_ttl_hours", 6))),
                ),
                execution_limit=max(
                    1, min(30, int(config.get("execution_limit", 30)))
                ),
                max_continuations=max(
                    1, min(128, int(config.get("max_continuations", 128)))
                ),
            )
        raise HTTPException(
            status_code=409,
            detail="segment_id is not the active analysis lease",
        )
    leased_chunks = config.get("leased_chunks")
    leased_keys = config.get("leased_keys")
    if not isinstance(leased_chunks, list) or not isinstance(leased_keys, list):
        raise HTTPException(status_code=409, detail="analysis segment lease is malformed")
    flattened_lease = [
        key
        for entry in leased_chunks
        if isinstance(entry, dict) and isinstance(entry.get("notice_keys"), list)
        for key in entry["notice_keys"]
    ]
    if flattened_lease != leased_keys or len(set(leased_keys)) != len(leased_keys):
        raise HTTPException(status_code=409, detail="analysis segment lease is malformed")

    children = _backfill_children(session, parent.id)
    terminal_chunks: set[int] = set()
    running_chunks: set[int] = set()
    for child in children:
        child_config = child.request_json if isinstance(child.request_json, dict) else {}
        if child_config.get("segment_id") != payload.segment_id:
            continue
        child_index = child_config.get("chunk_index")
        if not isinstance(child_index, int):
            continue
        if child.status == "RUNNING":
            running_chunks.add(child_index)
        else:
            terminal_chunks.add(child_index)
    expected_chunks = {
        entry.get("chunk_index")
        for entry in leased_chunks
        if isinstance(entry, dict) and isinstance(entry.get("chunk_index"), int)
    }
    if len(expected_chunks) != len(leased_chunks):
        raise HTTPException(status_code=409, detail="analysis segment chunk map is malformed")
    missing_chunks = expected_chunks - terminal_chunks
    if running_chunks or missing_chunks:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ANALYSIS_SEGMENT_NOT_TERMINAL",
                "segment_id": payload.segment_id,
                "running_chunk_indices": sorted(running_chunks),
                "missing_chunk_indices": sorted(missing_chunks),
                "lease_retained": True,
            },
        )

    # Every exact leased chunk has a terminal child audit (COMPLETED/PARTIAL/
    # FAILED). Clear only this lease; failures remain visible in the aggregate
    # and are eligible for a future cooled retry operation.
    config["last_finalized_segment_id"] = payload.segment_id
    config["last_finalized_at"] = datetime.now(timezone.utc).isoformat()
    config.pop("lease_id", None)
    config.pop("lease_started_at", None)
    config.pop("leased_keys", None)
    config.pop("leased_chunks", None)
    config.pop("lease_request_token", None)
    parent.request_json = config
    chunk_size = max(1, min(3, int(config.get("chunk_size", 3))))
    ttl_hours = max(1, min(24, int(config.get("reservation_ttl_hours", 6))))
    execution_limit = max(1, min(30, int(config.get("execution_limit", 30))))
    max_continuations = max(
        1, min(128, int(config.get("max_continuations", 128)))
    )
    before = _backfill_status(
        session,
        parent,
        chunk_size=chunk_size,
        stale_after_hours=ttl_hours,
        execution_limit=execution_limit,
        max_continuations=max_continuations,
    )
    config["last_finalized_aggregate"] = {
        "planned": before.planned,
        "attempted": before.attempted,
        "completed": before.completed,
        "partial": before.partial,
        "failed": before.failed,
        "remaining": before.remaining,
        "in_flight": before.in_flight,
        "child_jobs": before.child_jobs,
        "openai_calls": before.openai_calls,
    }
    parent.request_json = config
    parent.fetched = before.attempted
    parent.created_count = before.completed
    parent.quarantined_count = before.failed
    if before.status == "DEAD_LETTER":
        parent.status = "DEAD_LETTER"
        parent.error_code = "MAX_CONTINUATIONS_EXCEEDED"
        parent.warnings = sorted(
            set([*(parent.warnings or []), "MAX_CONTINUATIONS_EXCEEDED"])
        )
        parent.completed_at = datetime.now(timezone.utc)
    elif before.remaining:
        parent.status = "PARTIAL"
        parent.error_code = "BACKFILL_INCOMPLETE"
        parent.warnings = sorted(set([*(parent.warnings or []), "BACKFILL_INCOMPLETE"]))
        parent.completed_at = None
    else:
        parent.status = "PARTIAL" if before.partial or before.failed else "COMPLETED"
        parent.error_code = "BACKFILL_CHILD_FAILURE" if before.failed else None
        parent.completed_at = datetime.now(timezone.utc)
        if before.failed:
            parent.warnings = sorted(
                set([*(parent.warnings or []), "BACKFILL_CHILD_FAILURE"])
            )
    session.commit()
    session.refresh(parent)
    return _backfill_status(
        session,
        parent,
        chunk_size=chunk_size,
        stale_after_hours=ttl_hours,
        execution_limit=execution_limit,
        max_continuations=max_continuations,
    )


def _finish_batch_job(
    request: Request,
    *,
    job_id: str,
    status_value: str,
    completed: int,
    skipped: int,
    failed: int,
    warnings: list[str],
) -> None:
    with request.app.state.session_factory() as session:
        job = session.get(IngestionJob, job_id)
        if job is None:  # pragma: no cover - database invariant
            raise RuntimeError("analysis batch audit job disappeared")
        job.status = status_value
        job.fetched = completed + skipped + failed
        job.created_count = completed
        job.duplicate_count = skipped
        job.quarantined_count = failed
        job.warnings = warnings
        job.completed_at = datetime.now(timezone.utc)
        session.commit()


def _dry_run_item(request: Request, notice_key: str) -> AnalysisBatchItemOut:
    with request.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        if notice is None:
            return AnalysisBatchItemOut(
                notice_key=notice_key,
                status="FAILED",
                document_status="NOTICE_NOT_FOUND",
                evaluation_status="NOT_RUN",
                snapshot_status="NOT_RUN",
                warnings=["NOTICE_NOT_FOUND"],
            )
        versions = list(
            session.scalars(
                select(NoticeVersion).where(NoticeVersion.notice_id == notice.id)
            ).all()
        )
        accepted = [
            item
            for item in versions
            if isinstance(item.source_payload, dict)
            and item.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
            and item.source_payload.get("status") == "ACCEPTED"
        ]
        return AnalysisBatchItemOut(
            notice_key=notice_key,
            status="SKIPPED",
            document_status="READY" if accepted else "EXTRACTION_MISSING",
            evaluation_status="NOT_RUN",
            snapshot_status="NOT_RUN",
            warnings=["DRY_RUN_NO_WRITES"]
            + ([] if accepted else ["NO_ACCEPTED_EXTRACTION"]),
        )


def _has_accepted_pps_extraction(request: Request, notice_id: str) -> bool:
    with request.app.state.session_factory() as session:
        return has_current_accepted_pps_extraction(session, notice_id)


def _attach_public_analysis_reason(
    request: Request,
    item: AnalysisBatchItemOut,
) -> AnalysisBatchItemOut:
    """Add the same safe coverage reason exposed by notice list/detail APIs."""

    with request.app.state.session_factory() as session:
        notice = session.scalar(
            select(Notice)
            .where(Notice.notice_key == item.notice_key)
            .options(
                selectinload(Notice.versions),
                selectinload(Notice.evaluations),
            )
        )
        if notice is None:
            return item
        source_kind = "PPS" if notice.notice_key.upper().startswith("PPS-") else "MANUAL"
        reason = public_analysis_reason(
            notice.versions,
            evaluated=bool(notice.evaluations),
            source_kind=source_kind,
        )
        coverage = (
            current_pps_attachment_coverage(session, notice.id)
            if source_kind == "PPS"
            else None
        )
    return item.model_copy(
        update={
            "analysis_state": reason.state,
            "analysis_reason_code": reason.reason_code,
            "analysis_reason": reason.reason,
            "attachments_discovered": coverage.discovered if coverage else 0,
            "attachments_audited": coverage.audited if coverage else 0,
            "attachments_supported": coverage.supported if coverage else 0,
            "attachments_accepted": coverage.accepted if coverage else 0,
            "attachment_coverage_complete": coverage.complete if coverage else True,
            "all_supported_attachments_accepted": (
                coverage.all_supported_accepted if coverage else True
            ),
        }
    )


def _enrich_one_notice(
    request: Request,
    *,
    notice_id: str,
    payload: AnalysisBatchRequest,
    deadline_monotonic: float,
) -> PpsEnrichmentResult:
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        return enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model,
            max_attachments=payload.max_attachments_per_notice,
            dry_run=payload.dry_run,
            # Each attachment is one durable unit. The shared deadline starts
            # another unit only when its full download + two-call worst case
            # still fits, keeping n8n below its HTTP timeout while returning a
            # resumable PARTIAL response for the remaining manifest entries.
            download_timeout_seconds=12,
            openai_timeout_seconds=45,
            openai_max_retries=0,
            deadline_monotonic=deadline_monotonic,
        )


@router.post("/notices/analysis/batch", response_model=AnalysisBatchResponse)
def run_notice_analysis_batch(
    payload: AnalysisBatchRequest,
    request: Request,
) -> AnalysisBatchResponse:
    """Boundedly enrich missing PPS documents, then persist analysis snapshots."""
    job_id, stored_response = _create_batch_job(request, payload)
    if stored_response is not None:
        return stored_response
    try:
        return _execute_notice_analysis_batch(payload, request, job_id=job_id)
    except Exception:
        # A provider/library failure must not leave the operational audit in a
        # permanent RUNNING state. Per-notice failures are handled below and
        # normally produce a 200 PARTIAL response; this is the final boundary.
        _finish_batch_job(
            request,
            job_id=job_id,
            status_value="FAILED",
            completed=0,
            skipped=0,
            failed=len(payload.notice_keys),
            warnings=["INTERNAL_ANALYSIS_BATCH_ERROR"],
        )
        raise


def _execute_notice_analysis_batch(
    payload: AnalysisBatchRequest,
    request: Request,
    *,
    job_id: str,
) -> AnalysisBatchResponse:
    rows: list[AnalysisBatchItemOut] = []
    completed = skipped = failed = 0
    materialized = evaluations = snapshots = 0
    enrichment_requested = min(len(payload.notice_keys), payload.max_notices) if payload.enrich_missing else 0
    enrichment_attempted = enrichment_completed = enrichment_skipped = enrichment_failed = 0
    enrichment_discovered = enrichment_attachments_attempted = enrichment_processed = 0
    enrichment_downloaded_bytes = 0
    enrichment_source_characters = enrichment_analysis_input_characters = 0
    enrichment_members_discovered = enrichment_members_processed = 0
    enrichment_source_complete = enrichment_input_complete = True
    enrichment_openai_calls = 0
    enrichment_warnings: list[str] = []
    enrichment_attachment_results: list[AnalysisAttachmentEnrichmentOut] = []
    # At most two worst-case attachment units fit below this request boundary:
    # 3 redirect hops * 12s + 2 Responses calls * 45s = 126s per unit. n8n's
    # HTTP timeout is 600s; a continuation never starts a third unsafe unit.
    enrichment_deadline = time.monotonic() + 270

    for index, notice_key in enumerate(payload.notice_keys):
        enrichment_targeted = payload.enrich_missing and index < payload.max_notices
        if enrichment_targeted:
            # attempted is the processed target partition (including precheck
            # reuse/not-found/timeout), not merely outbound provider calls.
            # Workflow 10 validates attempted == completed+skipped+failed.
            enrichment_attempted += 1
        with request.app.state.session_factory() as session:
            notice_id = session.scalar(select(Notice.id).where(Notice.notice_key == notice_key))
            session.rollback()
        if notice_id is None:
            rows.append(
                AnalysisBatchItemOut(
                    notice_key=notice_key,
                    status="FAILED",
                    document_status="NOTICE_NOT_FOUND",
                    evaluation_status="NOT_RUN",
                    snapshot_status="NOT_RUN",
                    warnings=["NOTICE_NOT_FOUND"],
                )
            )
            failed += 1
            if enrichment_targeted:
                enrichment_skipped += 1
                enrichment_warnings.append("NOTICE_NOT_FOUND")
            continue

        enrichment_result: PpsEnrichmentResult | None = None
        should_enrich = (
            enrichment_targeted
            and not _has_accepted_pps_extraction(request, notice_id)
        )
        if enrichment_targeted and not should_enrich:
            enrichment_completed += 1
        elif should_enrich and time.monotonic() >= enrichment_deadline:
            enrichment_result = PpsEnrichmentResult(
                status="SKIPPED",
                warnings=["ENRICHMENT_TOTAL_TIMEOUT"],
            )
        elif should_enrich:
            try:
                enrichment_result = _enrich_one_notice(
                    request,
                    notice_id=notice_id,
                    payload=payload,
                    deadline_monotonic=enrichment_deadline,
                )
            except Exception:  # pragma: no cover - provider fail-closed boundary
                # Exact attachment context is unavailable at this outer
                # boundary, so it must never guess by binding the latest
                # manifest. In particular, dry-run remains a zero-write
                # contract even when an unexpected precheck error occurs.
                enrichment_result = PpsEnrichmentResult(
                    status="REVIEW",
                    warnings=[
                        "INTERNAL_ENRICHMENT_ERROR",
                        *(["DRY_RUN_NO_WRITES"] if payload.dry_run else []),
                    ],
                )

        item_enrichment_warnings: list[str] = []
        if enrichment_result is not None:
            enrichment_discovered += enrichment_result.attachments_discovered
            enrichment_attachments_attempted += enrichment_result.attachments_attempted
            enrichment_processed += enrichment_result.attachments_processed
            enrichment_downloaded_bytes += enrichment_result.downloaded_bytes
            enrichment_source_characters += enrichment_result.source_characters
            enrichment_analysis_input_characters += enrichment_result.analysis_input_characters
            enrichment_members_discovered += enrichment_result.members_discovered
            enrichment_members_processed += enrichment_result.members_processed
            enrichment_source_complete = (
                enrichment_source_complete and enrichment_result.source_read_complete
            )
            enrichment_input_complete = (
                enrichment_input_complete and enrichment_result.analysis_input_complete
            )
            enrichment_openai_calls += enrichment_result.openai_calls
            enrichment_attachment_results.extend(
                AnalysisAttachmentEnrichmentOut.model_validate(item)
                for item in enrichment_result.attachment_results
            )
            item_enrichment_warnings.extend(enrichment_result.warnings)
            enrichment_warnings.extend(enrichment_result.warnings)
            if enrichment_result.status in {"COMPLETED", "REUSED"}:
                enrichment_completed += 1
            elif enrichment_result.status in {"PLANNED", "SKIPPED"}:
                enrichment_skipped += 1
            else:
                enrichment_failed += 1

        if payload.dry_run:
            item = _dry_run_item(request, notice_key)
            updates: dict[str, Any] = {
                "warnings": sorted(set([*item.warnings, *item_enrichment_warnings])),
            }
            if enrichment_result is not None and enrichment_result.status == "PLANNED":
                updates["document_status"] = "ENRICHMENT_PLANNED"
            item = item.model_copy(update=updates)
            rows.append(item)
            if item.status == "FAILED":
                failed += 1
            else:
                skipped += 1
            continue

        if (
            enrichment_result is not None
            and "ATTACHMENT_CONTINUATION_REQUIRED" in enrichment_result.warnings
        ):
            # Do not materialise a misleading terminal AnalysisRun while the
            # current manifest still has unaudited attachments. The child job
            # is stored with requeue_notice_keys and the parent leases this
            # exact notice again; prior per-attachment versions are reused.
            rows.append(
                AnalysisBatchItemOut(
                    notice_key=notice_key,
                    status="SKIPPED",
                    document_status="ATTACHMENT_CONTINUATION_REQUIRED",
                    evaluation_status="NOT_RUN",
                    snapshot_status="NOT_RUN",
                    warnings=sorted(set(item_enrichment_warnings)),
                )
            )
            skipped += 1
            continue

        with request.app.state.session_factory() as session:
            try:
                result = run_analysis_pipeline(session, notice_id=notice_id)
            except AnalysisPipelineError as exc:
                rows.append(
                    AnalysisBatchItemOut(
                        notice_key=notice_key,
                        status="FAILED",
                        document_status="PIPELINE_REJECTED",
                        evaluation_status="NOT_CREATED",
                        snapshot_status="NOT_CREATED",
                        warnings=sorted(set([type(exc).__name__, *item_enrichment_warnings])),
                    )
                )
                failed += 1
                continue
            except Exception:  # pragma: no cover - operational fail-closed boundary
                rows.append(
                    AnalysisBatchItemOut(
                        notice_key=notice_key,
                        status="FAILED",
                        document_status="PIPELINE_ERROR",
                        evaluation_status="NOT_CREATED",
                        snapshot_status="NOT_CREATED",
                        warnings=sorted(set(["INTERNAL_PIPELINE_ERROR", *item_enrichment_warnings])),
                    )
                )
                failed += 1
                continue

        completed += 1
        if not result.reused:
            materialized += 1
            evaluations += 1
            snapshots += 1
        rows.append(
            AnalysisBatchItemOut(
                notice_key=notice_key,
                status="COMPLETED",
                document_status=result.status,
                evaluation_status="REUSED" if result.reused else "CREATED",
                snapshot_status="REUSED" if result.reused else "CREATED",
                analysis_run_id=result.analysis_run_id,
                evaluation_id=result.evaluation_id,
                notice_version_id=result.notice_version_id,
                input_sha256=result.input_sha256,
                reused=result.reused,
                materialized_requirements=result.materialized_requirement_count,
                requirement_snapshots=result.requirement_snapshot_count,
                score_snapshots=result.score_snapshot_count,
                recommendation_snapshots=result.recommendation_snapshot_count,
                warnings=sorted(set([*result.warnings, *item_enrichment_warnings])),
            )
        )

    rows = [_attach_public_analysis_reason(request, row) for row in rows]
    warnings = sorted({warning for row in rows for warning in row.warnings})
    partial = failed > 0 or enrichment_failed > 0 or any(
        row.document_status not in {"READY", "COMPLETED"} for row in rows
    )
    status_value = "PARTIAL" if partial else "COMPLETED"
    if status_value == "PARTIAL" and not warnings:
        warnings = ["PARTIAL_ANALYSIS"]
    _finish_batch_job(
        request,
        job_id=job_id,
        status_value=status_value,
        completed=completed,
        skipped=skipped,
        failed=failed,
        warnings=warnings,
    )
    response = AnalysisBatchResponse(
        job_id=job_id,
        status=status_value,
        dry_run=payload.dry_run,
        requested=len(payload.notice_keys),
        processed=completed + skipped + failed,
        completed=completed,
        skipped=skipped,
        failed=failed,
        document_materialized=materialized,
        evaluations_created=evaluations,
        snapshots_refreshed=snapshots,
        openai_calls=enrichment_openai_calls,
        results=rows,
        warnings=warnings,
        enrichment=AnalysisEnrichmentOut(
            requested=enrichment_requested,
            attempted=enrichment_attempted,
            completed=enrichment_completed,
            skipped=enrichment_skipped,
            failed=enrichment_failed,
            attachments_discovered=enrichment_discovered,
            attachments_attempted=enrichment_attachments_attempted,
            attachments_processed=enrichment_processed,
            downloaded_bytes=enrichment_downloaded_bytes,
            source_characters=enrichment_source_characters,
            analysis_input_characters=enrichment_analysis_input_characters,
            source_read_complete=(
                enrichment_source_complete and enrichment_attempted > 0
            ),
            analysis_input_complete=(
                enrichment_input_complete and enrichment_attempted > 0
            ),
            members_discovered=enrichment_members_discovered,
            members_processed=enrichment_members_processed,
            openai_calls=enrichment_openai_calls,
            warnings=sorted(set(enrichment_warnings)),
            attachment_results=enrichment_attachment_results,
        ),
    )
    _store_batch_response(request, job_id=job_id, response=response)
    return response


@router.get("/notices/{notice_key}/analysis-runs")
def list_notice_analysis_runs(
    notice_key: str,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    runs = list(
        session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.notice_id == notice.id)
            .options(
                selectinload(AnalysisRun.requirement_results),
                selectinload(AnalysisRun.scores),
                selectinload(AnalysisRun.recommendations),
            )
            .order_by(AnalysisRun.generated_at.desc())
            .limit(limit)
        ).all()
    )
    return {
        "notice_key": notice_key,
        "count": len(runs),
        "runs": [
            {
                "id": run.id,
                "status": run.status,
                "run_kind": run.run_kind,
                "input_sha256": run.input_sha256,
                "generated_at": run.generated_at,
                "evaluation_id": run.evaluation_id,
                "notice_version_id": run.notice_version_id,
                "basis_versions": run.basis_versions,
                "output_summary": run.output_summary,
                "requirement_results": [
                    {
                        "result_key": item.result_key,
                        "sequence": item.sequence,
                        "requirement_key": item.requirement_key,
                        "policy_class": item.policy_class,
                        "outcome": item.outcome,
                        "reason_code": item.reason_code,
                        "blocking": item.blocking,
                        "evidence_state": item.evidence_state,
                        "result": item.result_json,
                    }
                    for item in run.requirement_results
                ],
                "scores": [
                    {
                        "score_key": item.score_key,
                        "score_type": item.score_type,
                        "value": item.value,
                        "lower_value": item.lower_value,
                        "upper_value": item.upper_value,
                        "unit": item.unit,
                        "status": item.status,
                        "band": item.band,
                        "confidence": item.confidence,
                        "method_version": item.method_version,
                        "basis": item.basis_json,
                    }
                    for item in run.scores
                ],
                "recommendations": [
                    {
                        "recommendation_key": item.recommendation_key,
                        "department_id": item.department_id,
                        "rank": item.rank,
                        "priority_score": item.priority_score,
                        "recommendation": item.recommendation,
                        "confidence": item.confidence,
                        "risk_band": item.risk_band,
                        "detail": item.detail_json,
                    }
                    for item in run.recommendations
                ],
            }
            for run in runs
        ],
    }
