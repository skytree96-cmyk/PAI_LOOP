from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from .analysis_api import AnalysisBatchRequest, run_notice_analysis_batch
from .models import IngestionJob, Notice
from .pps_enrichment import PublicAnalysisReason, public_analysis_reason


class ManualAnalysisResponse(BaseModel):
    """Small public projection of a server-side, single-notice analysis."""

    model_config = ConfigDict(from_attributes=True)

    request_id: str | None = None
    notice_key: str
    outcome: Literal["COMPLETED", "REVIEW", "ALREADY_ANALYZED", "COOLDOWN"]
    analysis_state: Literal["ANALYZED", "REVIEW", "PENDING"]
    analysis_reason_code: str
    analysis_reason: str
    analysis_attempted: bool
    openai_calls: int = 0
    message: str


router = APIRouter(prefix="/api/v1", tags=["public manual analysis"])

# Public clicks never receive the server API key. This separate BFF boundary
# serialises every anonymous analysis request across workers before it can
# consume provider capacity. PostgreSQL uses the same fixed advisory-lock
# namespace on every instance; local SQLite tests use an in-process lock.
_PUBLIC_MANUAL_LOCK_KEY = 0x5041494D  # "PAIM"
_PUBLIC_MANUAL_PROCESS_LOCK = threading.Lock()
_NON_ATTEMPT_REQUEST_COOLDOWN = timedelta(minutes=5)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _source_kind(notice: Notice) -> str:
    return "PPS" if notice.notice_key.upper().startswith("PPS-") else "MANUAL"


def _reason(notice: Notice) -> PublicAnalysisReason:
    return public_analysis_reason(
        notice.versions,
        evaluated=bool(notice.evaluations),
        source_kind=_source_kind(notice),
    )


def _load_notice(request: Request, notice_key: str) -> Notice:
    with request.app.state.session_factory() as session:
        notice = session.scalar(
            select(Notice)
            .where(Notice.notice_key == notice_key)
            .options(
                selectinload(Notice.versions),
                selectinload(Notice.evaluations),
            )
        )
        if notice is None:
            raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
        session.expunge(notice)
        return notice


def _same_origin_request(request: Request) -> bool:
    """Require a browser same-origin POST without trusting client credentials."""

    origin = request.headers.get("origin", "").strip()
    if not origin:
        return False
    parsed = urlsplit(origin)
    expected = request.url
    expected_host = (request.url.hostname or "").casefold()
    if not parsed.hostname or parsed.hostname.casefold() != expected_host:
        return False
    if (
        parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    settings = request.app.state.settings
    origin_scheme = parsed.scheme.casefold()
    if origin_scheme != expected.scheme.casefold():
        return False
    if settings.environment.casefold() == "production" and origin_scheme != "https":
        return False
    try:
        origin_port = parsed.port or (443 if origin_scheme == "https" else 80)
    except ValueError:
        return False
    expected_port = expected.port or (443 if expected.scheme.casefold() == "https" else 80)
    if origin_port != expected_port:
        return False
    fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
    return not fetch_site or fetch_site == "same-origin"


@contextmanager
def _manual_execution_slot(request: Request):
    engine = request.app.state.engine
    if engine.dialect.name == "postgresql":
        connection = engine.connect()
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": _PUBLIC_MANUAL_LOCK_KEY},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _PUBLIC_MANUAL_LOCK_KEY},
                )
            connection.close()
        return

    acquired = _PUBLIC_MANUAL_PROCESS_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _PUBLIC_MANUAL_PROCESS_LOCK.release()


def _manual_jobs_since(
    request: Request,
    *,
    cutoff: datetime,
) -> list[IngestionJob]:
    with request.app.state.session_factory() as session:
        return list(
            session.scalars(
                select(IngestionJob)
                .where(
                    IngestionJob.source == "MANUAL_ANALYSIS",
                    IngestionJob.created_at >= cutoff,
                )
                .order_by(IngestionJob.created_at.desc())
            ).all()
        )


def _cooldown_response(
    notice: Notice,
    reason: PublicAnalysisReason,
    *,
    message: str,
) -> ManualAnalysisResponse:
    return ManualAnalysisResponse(
        notice_key=notice.notice_key,
        outcome="COOLDOWN",
        analysis_state=reason.state,
        analysis_reason_code=reason.reason_code,
        analysis_reason=reason.reason,
        analysis_attempted=reason.attempted,
        message=message,
    )


def _reserve_manual_job(request: Request, notice_key: str) -> str:
    request_id = str(uuid.uuid4())
    with request.app.state.session_factory() as session:
        session.add(
            IngestionJob(
                id=request_id,
                source="MANUAL_ANALYSIS",
                mode="LIVE",
                status="RUNNING",
                window_json={"scope": "ONE_OPEN_PPS_NOTICE"},
                request_json={
                    "trigger": "PUBLIC_SAME_ORIGIN",
                    "force": False,
                    "max_notices": 1,
                    "max_attachments_per_notice": 1,
                    "credential_exposed": False,
                },
                matched=1,
                notice_keys=[notice_key],
            )
        )
        session.commit()
    return request_id


def _finish_manual_job(
    request: Request,
    *,
    request_id: str,
    status_value: str,
    openai_calls: int = 0,
    completed: int = 0,
    failed: int = 0,
    batch_job_id: str | None = None,
) -> None:
    with request.app.state.session_factory() as session:
        job = session.get(IngestionJob, request_id)
        if job is None:  # pragma: no cover - persistence invariant
            return
        config = dict(job.request_json or {})
        if batch_job_id:
            config["batch_job_id"] = batch_job_id
        job.request_json = config
        job.status = status_value
        job.api_calls = openai_calls
        job.created_count = completed
        job.quarantined_count = failed
        job.completed_at = datetime.now(timezone.utc)
        session.commit()


def _already_analysed(notice: Notice, reason: PublicAnalysisReason) -> ManualAnalysisResponse:
    return ManualAnalysisResponse(
        notice_key=notice.notice_key,
        outcome="ALREADY_ANALYZED",
        analysis_state=reason.state,
        analysis_reason_code=reason.reason_code,
        analysis_reason=reason.reason,
        analysis_attempted=reason.attempted,
        message="이미 현재 공고 버전의 분석이 완료되어 기존 결과를 그대로 사용했습니다.",
    )


@router.post(
    "/notices/{notice_key}/analysis/request",
    response_model=ManualAnalysisResponse,
)
def request_manual_notice_analysis(
    notice_key: str,
    request: Request,
) -> ManualAnalysisResponse:
    """Run one idempotent server-side analysis without exposing credentials.

    The endpoint exists only for the public Render UI when explicitly enabled.
    It accepts no caller-selected model, force flag, prompt, URL or attachment,
    and therefore cannot widen the existing stored-PPS analysis boundary.
    """

    settings = request.app.state.settings
    if not (settings.public_read_only and settings.public_manual_analysis_enabled):
        raise HTTPException(status_code=404, detail="수동 분석 기능이 비활성화되어 있습니다.")
    if not _same_origin_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="홈페이지와 동일한 출처에서만 분석을 요청할 수 있습니다.",
        )
    with _manual_execution_slot(request) as acquired:
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="다른 수동 분석을 처리 중입니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": "15"},
            )

        now = datetime.now(timezone.utc)
        notice = _load_notice(request, notice_key)
        if _source_kind(notice) != "PPS":
            raise HTTPException(status_code=422, detail="조달청 공고만 수동 분석할 수 있습니다.")
        if notice.status != "OPEN" or _utc(notice.deadline) < now:
            raise HTTPException(status_code=409, detail="마감 또는 종료된 공고는 분석할 수 없습니다.")

        reason = _reason(notice)
        if reason.state == "ANALYZED":
            return _already_analysed(notice, reason)

        recent_jobs = _manual_jobs_since(request, cutoff=now - timedelta(hours=1))
        same_notice_jobs = [
            job for job in recent_jobs if notice.notice_key in (job.notice_keys or [])
        ]
        if same_notice_jobs and _utc(same_notice_jobs[0].created_at) >= now - _NON_ATTEMPT_REQUEST_COOLDOWN:
            return _cooldown_response(
                notice,
                reason,
                message="같은 공고의 최근 요청을 재사용했습니다. 잠시 후 상태를 다시 확인해 주세요.",
            )

        if reason.attempted:
            attempt_at = max(
                (_utc(version.created_at) for version in notice.versions),
                default=_utc(notice.created_at),
            )
            retry_at = attempt_at + timedelta(
                hours=settings.public_manual_analysis_cooldown_hours
            )
            if retry_at > now:
                return _cooldown_response(
                    notice,
                    reason,
                    message=f"최근 분석을 재사용했습니다. {retry_at.isoformat()} 이후 재분석할 수 있습니다.",
                )

        if len(recent_jobs) >= settings.public_manual_analysis_hourly_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="시간당 수동 분석 한도에 도달했습니다. 자동 분석 큐 또는 다음 시간대를 이용해 주세요.",
                headers={"Retry-After": "3600"},
            )

        # Idempotent reads above remain available even during an upstream
        # provider outage.  A provider credential is required only when this
        # request is about to reserve quota and start a new analysis batch.
        if not settings.openai_api_key:
            raise HTTPException(status_code=503, detail="분석 서비스 설정을 확인해 주세요.")

        request_id = _reserve_manual_job(request, notice.notice_key)
        payload = AnalysisBatchRequest(
            notice_keys=[notice.notice_key],
            dry_run=False,
            force=False,
            enrich_missing=True,
            max_notices=1,
            max_attachments_per_notice=1,
        )
        try:
            batch = run_notice_analysis_batch(payload, request)
        except Exception:
            _finish_manual_job(
                request,
                request_id=request_id,
                status_value="FAILED",
                failed=1,
            )
            raise

        updated_notice = _load_notice(request, notice.notice_key)
        updated_reason = _reason(updated_notice)
        outcome: Literal["COMPLETED", "REVIEW"] = (
            "COMPLETED" if updated_reason.state == "ANALYZED" else "REVIEW"
        )
        _finish_manual_job(
            request,
            request_id=request_id,
            status_value="COMPLETED" if batch.failed == 0 else "PARTIAL",
            openai_calls=batch.openai_calls,
            completed=batch.completed,
            failed=batch.failed,
            batch_job_id=batch.job_id,
        )
        return ManualAnalysisResponse(
            request_id=request_id,
            notice_key=updated_notice.notice_key,
            outcome=outcome,
            analysis_state=updated_reason.state,
            analysis_reason_code=updated_reason.reason_code,
            analysis_reason=updated_reason.reason,
            analysis_attempted=updated_reason.attempted,
            openai_calls=batch.openai_calls,
            message=(
                "분석과 판정이 완료되었습니다."
                if outcome == "COMPLETED"
                else "요청은 처리됐지만 원문 또는 근거 보완이 필요합니다."
            ),
        )
