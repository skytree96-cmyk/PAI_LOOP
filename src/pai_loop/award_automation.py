"""Protected, resumable PPS award refresh. This module never runs analysis."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, or_, select, text
from sqlalchemy.orm import Session, selectinload

from .api import (DbSession, _comparable_utc, _derive_award_keyword,
                  _pps_authorities_by_notice_id, _pps_authority_notice_projection,
                  _revision_preference, _source_kind, _stored_notice_authority_row, refresh_award_history)
from .auth import require_api_key
from .award_automation_models import AwardRefreshAttempt, AwardRefreshState
from .award_scope import AWARD_SCOPE_VERSION, resolve_notice_award_scope
from .models import IngestionJob, Notice, PpsNoticeAuthority, new_id
from .schemas import AwardHistoryRefreshRequest

SCHEMA = "award-refresh-automation-1.0"
LEASE_SECONDS = 900  # Longer than the existing 480-second collector wall limit.
MAX_CYCLE_ATTEMPTS = 3
BATCH_WALL_SECONDS = 480
MIN_NOTICE_WALL_SECONDS = 60
_LOCK_KEY = 0x504149415752
_PROCESS_LOCK = threading.RLock()
_SERVICE_CATEGORIES = {"용역", "일반용역", "학술연구용역", "기술용역", "SERVICE", "SERVICES"}
router = APIRouter(prefix="/api/v1/operations/award-refresh", tags=["operations"],
                   dependencies=[Depends(require_api_key)])


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_after_days: int = Field(default=30, ge=1, le=365)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_notices: int = Field(default=1, ge=1, le=10, strict=True)
    daily_api_budget: int = Field(default=1000, ge=1, le=1000, strict=True)
    per_notice_api_budget: int = Field(default=150, ge=1, le=1000, strict=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _serialized(session: Session):
    """Serialize claims and budget reservations across production processes."""
    with _PROCESS_LOCK:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        try:
            yield
            session.commit()
        except Exception:
            session.rollback()
            raise


def _classification(notice: Notice, active_ids: set[str]) -> tuple[str, str | None]:
    source = _source_kind(notice)
    if source == "SYNTHETIC":
        return "SKIPPED", "SYNTHETIC_NOTICE"
    if source != "PPS":
        return "SKIPPED", "NON_PPS_NOTICE"
    if notice.id not in active_ids:
        return "SKIPPED", "INACTIVE_NOTICE"
    category = (notice.category or "").strip().upper()
    if category not in _SERVICE_CATEGORIES:
        return "UNSUPPORTED", "UNSUPPORTED_AWARD_CATEGORY" if category else "SERVICE_CATEGORY_UNCONFIRMED"
    if not resolve_notice_award_scope(notice).available:
        return "UNSUPPORTED", "AWARD_AGENCY_UNAVAILABLE"
    try:
        _derive_award_keyword(notice.title)
    except HTTPException:
        return "UNSUPPORTED", "AWARD_KEYWORD_UNAVAILABLE"
    return "PENDING", None


def _active_notice_ids(session: Session, notices: list[Notice], now: datetime) -> set[str]:
    """Reuse provider ordering and representative projection, never title guesses."""
    pps = [notice for notice in notices if _source_kind(notice) == "PPS"]
    if not pps:
        return set()
    notice_nos = {notice.bid_notice_no for notice in pps}
    authorities = {row.bid_notice_no: row for row in session.scalars(select(PpsNoticeAuthority).where(
        PpsNoticeAuthority.bid_notice_no.in_(notice_nos)).execution_options(populate_existing=True))}
    representatives = _pps_authorities_by_notice_id(session, pps)
    # Legacy notices without the compact authority row still use the same
    # revision/event/deadline ordering across ALL retained sibling revisions.
    legacy_by_no: dict[str, list[Notice]] = {}
    legacy_nos = notice_nos - authorities.keys()
    if legacy_nos:
        for related in session.scalars(_pps_authority_notice_projection(select(Notice).where(
            Notice.bid_notice_no.in_(legacy_nos)))):
            if _source_kind(related) == "PPS":
                legacy_by_no.setdefault(related.bid_notice_no, []).append(related)
    legacy_ids = set()
    for siblings in legacy_by_no.values():
        latest = max(siblings, key=lambda row: (
            *_revision_preference(_stored_notice_authority_row(row), now=now),
            _comparable_utc(row.created_at).timestamp(), row.notice_key, row.id))
        stored = _stored_notice_authority_row(latest)
        if stored.get("notice_kind") != "취소공고" and not stored.get("direct_contract_signal"):
            legacy_ids.add(latest.id)
    active = set()
    for notice in pps:
        if notice.status.upper() != "OPEN" or _comparable_utc(notice.deadline) <= now:
            continue
        authority = authorities.get(notice.bid_notice_no)
        if authority is not None:
            if (notice.id not in representatives or authority.disposition != "VALID" or not authority.required_fields_complete
                or authority.deadline is None or _comparable_utc(authority.deadline) <= now
                or authority.direct_contract_signal):
                continue
        elif notice.id not in legacy_ids:
            continue
        active.add(notice.id)
    return active


def _basis(notice: Notice) -> str:
    scope = resolve_notice_award_scope(notice)
    try:
        keyword = _derive_award_keyword(notice.title)
    except HTTPException:
        keyword = None
    # The search scope is part of freshness. A completed legacy title-only
    # search cannot satisfy a demand-agency-and-keyword refresh.
    identity = {
        "scope_version": AWARD_SCOPE_VERSION,
        "notice_key": notice.notice_key,
        "category": notice.category,
        "title": notice.title,
        "demand_agency_code": scope.demand_agency_code,
        "demand_agency_name": scope.demand_agency_name,
        "keyword": keyword,
    }
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _backoff(state: AwardRefreshState, now: datetime) -> None:
    # Preserve PARTIAL/FAILED after three attempts for operator-visible review.
    state.next_attempt_at = (
        now + timedelta(hours=6 * (2 ** (state.cycle_attempts - 1)))
        if state.cycle_attempts < MAX_CYCLE_ATTEMPTS else None
    )


def _recover_expired(session: Session, now: datetime) -> None:
    for state in session.scalars(select(AwardRefreshState).where(
        AwardRefreshState.status == "RUNNING", AwardRefreshState.leased_until <= now,
    )):
        attempt = session.get(AwardRefreshAttempt, state.lease_token)
        job = _attempt_job(session, state.lease_token)
        if attempt is not None:
            attempt.status = "LEASE_EXPIRED"
            attempt.completed_at = now
            # A killed worker has unknown actual usage; retain its reservation.
        if job is not None and job.status in {"COMPLETED", "PARTIAL", "FAILED"}:
            outcome = ("COMPLETED" if job.matched else "NO_RESULTS") if job.status == "COMPLETED" else job.status
            state.status, state.reason = outcome, "LEASE_RECOVERED_FROM_JOB"
            state.last_job_id, state.records = job.id, job.matched
            state.api_calls += job.api_calls
            if attempt is not None:
                attempt.status, attempt.api_calls, attempt.job_id = outcome, job.api_calls, job.id
        else:
            state.status, state.reason = "FAILED", "LEASE_EXPIRED"
        state.lease_token, state.leased_until = None, None
        state.updated_at = now
        if state.status in {"COMPLETED", "NO_RESULTS"}:
            state.refreshed_at, state.next_attempt_at = now, None
        else:
            _backoff(state, now)
    session.flush()


def _attempt_job(session: Session, token: str | None) -> IngestionJob | None:
    if not token:
        return None
    return session.scalar(select(IngestionJob).where(
        IngestionJob.source == "PPS_AWARD",
        IngestionJob.request_json["automation_attempt_id"].as_string() == token,
    ).order_by(IngestionJob.created_at.desc()).limit(1))


def _budget(session: Session, now: datetime) -> tuple[int, int]:
    """Use the PPS KST calendar-day quota; legacy response field names remain."""
    cutoff = now.astimezone(timezone(timedelta(hours=9))).replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    jobs = {job.id: job for job in session.scalars(select(IngestionJob).where(
        IngestionJob.source.in_(["PPS_AWARD", "PPS_OUTCOME"]),
        or_(IngestionJob.created_at >= cutoff, IngestionJob.completed_at >= cutoff,
            IngestionJob.status == "RUNNING"),
    ))}
    calls = sum(max(0, job.api_calls) for job in jobs.values())
    reserved = 0
    for attempt in session.scalars(select(AwardRefreshAttempt).where(or_(
        AwardRefreshAttempt.started_at >= cutoff, AwardRefreshAttempt.completed_at >= cutoff,
        AwardRefreshAttempt.status == "RUNNING",
    ))):
        included = max(0, jobs[attempt.job_id].api_calls) if attempt.job_id in jobs else 0
        if attempt.api_calls is None:
            reserved += max(0, attempt.reserved_calls - included)
        else:
            # Keep the durable ledger authoritative if operational jobs are pruned.
            calls += max(0, attempt.api_calls - included)
    return calls, reserved


def _snapshot(session: Session, now: datetime) -> dict:
    states = list(session.scalars(select(AwardRefreshState)))
    notices = list(session.scalars(select(Notice)))
    active_ids = _active_notice_ids(session, notices, now)
    counts = Counter("SKIPPED" if state.status != "RUNNING" and state.notice_id not in active_ids
                     else state.status for state in states)
    total = len(notices)
    calls, reserved = _budget(session, now)
    eligible = sum(state.notice_id in active_ids and state.status in {"PENDING", "PARTIAL", "FAILED"}
                   and state.next_attempt_at is not None and state.next_attempt_at <= now for state in states)
    return {
        "schema_version": SCHEMA, "total": total,
        "complete": counts["COMPLETED"], "no_results": counts["NO_RESULTS"],
        "partial": counts["PARTIAL"], "pending": counts["PENDING"], "running": counts["RUNNING"],
        "failed": counts["FAILED"], "unsupported": counts["UNSUPPORTED"], "skipped": counts["SKIPPED"],
        "unplanned": max(0, total - len(states)), "eligible": eligible,
        "api_calls_24h": calls, "budget_reserved_24h": reserved, "ai_calls": 0,
    }


@router.get("/status")
def award_refresh_status(session: DbSession) -> dict:
    return _snapshot(session, _now())


@router.post("/plan")
def plan_award_refresh(payload: PlanRequest, session: DbSession) -> dict:
    now = _now()
    enrolled = requeued = 0
    with _serialized(session):
        session.expire_all()
        _recover_expired(session, now)
        states = {state.notice_id: state for state in session.scalars(select(AwardRefreshState))}
        notices = list(session.scalars(select(Notice).options(
            selectinload(Notice.award_scope_versions), selectinload(Notice.award_agency_metadata),
        ).order_by(Notice.created_at, Notice.id)))
        active_ids = _active_notice_ids(session, notices, now)
        for notice in notices:
            classification, reason = _classification(notice, active_ids)
            basis = _basis(notice)
            state = states.get(notice.id)
            if state is None:
                state = AwardRefreshState(notice_id=notice.id, status=classification, reason=reason,
                    basis_sha256=basis, next_attempt_at=now if classification == "PENDING" else None,
                    created_at=now, updated_at=now)
                session.add(state)
                enrolled += 1
            elif state.status != "RUNNING":
                changed = state.basis_sha256 != basis
                stale = state.status in {"COMPLETED", "NO_RESULTS"} and state.refreshed_at is not None and (
                    state.refreshed_at <= now - timedelta(days=payload.refresh_after_days))
                now_supported = classification == "PENDING" and state.status in {"UNSUPPORTED", "SKIPPED"}
                if changed or now_supported or (classification != "PENDING" and state.status != classification) or stale:
                    state.status, state.reason, state.basis_sha256 = classification, reason, basis
                    state.next_attempt_at = now if classification == "PENDING" else None
                    state.cycle_attempts, state.updated_at = 0, now
                    requeued += 1
        session.flush()
        result = _snapshot(session, now)
    return {**result, "status": "PLANNED", "enrolled": enrolled, "requeued": requeued}


def _run_result(session: Session, status: str, *, notice_key: str | None = None,
                job_id: str | None = None, attempted: int = 0, api_calls: int = 0, records: int = 0) -> dict:
    return {**_snapshot(session, _now()), "status": status, "attempted": attempted,
            "notice_key": notice_key, "job_id": job_id, "api_calls": api_calls, "records": records}


def _run_one(payload: RunRequest, request: Request, session: Session) -> dict:
    now = _now()
    with _serialized(session):
        session.expire_all()
        _recover_expired(session, now)
        if session.scalar(select(AwardRefreshState.notice_id).where(AwardRefreshState.status == "RUNNING").limit(1)):
            return _run_result(session, "BUSY")
        selections = list(session.execute(select(AwardRefreshState, Notice).join(Notice, Notice.id == AwardRefreshState.notice_id)
            .options(selectinload(Notice.award_scope_versions), selectinload(Notice.award_agency_metadata))
            .where(AwardRefreshState.status.in_(["PENDING", "PARTIAL", "FAILED"]),
                   AwardRefreshState.next_attempt_at <= now)
            .order_by(case((AwardRefreshState.attempts == 0, 0), else_=1),
                      case(((Notice.status == "OPEN") & (Notice.deadline >= now), 0), else_=1),
                      AwardRefreshState.next_attempt_at, Notice.published_at.desc().nullslast(), Notice.id)))
        active_ids = _active_notice_ids(session, [notice for _state, notice in selections], now)
        selected = None
        for state, notice in selections:
            classification, reason = _classification(notice, active_ids)
            if classification == "PENDING":
                if selected is None:
                    selected = state, notice
                continue
            state.status, state.reason, state.next_attempt_at = classification, reason, None
            state.basis_sha256, state.updated_at = _basis(notice), now
        session.flush()
        if selected is None:
            return _run_result(session, "IDLE")
        state, notice = selected
        used, reserved = _budget(session, now)
        remaining = payload.daily_api_budget - used - reserved
        if remaining < min(50, payload.per_notice_api_budget):
            return _run_result(session, "DAILY_BUDGET_REACHED")
        allocated = min(payload.per_notice_api_budget, remaining)
        token = new_id()
        state.status, state.reason = "RUNNING", None
        state.basis_sha256 = _basis(notice)
        state.lease_token, state.leased_until = token, now + timedelta(seconds=LEASE_SECONDS)
        state.attempts += 1
        state.cycle_attempts += 1
        state.updated_at = now
        session.add(AwardRefreshAttempt(id=token, notice_id=notice.id, status="RUNNING",
            started_at=now, reserved_calls=allocated))
        notice_key, notice_id = notice.notice_key, notice.id
    # The reservation transaction is committed before external I/O. No DB lock
    # or analysis lease is held while the bounded PPS-only collector runs.
    response = None
    error_status = None
    try:
        request.state.award_automation_attempt_id = token
        response = refresh_award_history(notice_key, AwardHistoryRefreshRequest(
            years=3, page_size=100, max_pages_per_window=3, dry_run=False,
            include_opening_results=True, max_opening_result_notices=30,
            opening_result_max_pages=3, max_api_calls=allocated,
        ), request, session)
    except HTTPException as exc:
        error_status = exc.status_code
        session.rollback()
    except Exception:
        session.rollback()
        error_status = 500
    with _serialized(session):
        session.expire_all()
        state = session.get(AwardRefreshState, notice_id)
        attempt = session.get(AwardRefreshAttempt, token)
        finished_at = _now()
        if response is not None:
            job_id, actual_calls, records = response.job_id, response.api_calls, response.records
            outcome = "PARTIAL" if response.status == "PARTIAL" else ("COMPLETED" if records else "NO_RESULTS")
        else:
            # The collector always creates its audit before external I/O. Never
            # expose exception prose, raw request data, or credentials.
            job = _attempt_job(session, token)
            job_id = job.id if job else None
            actual_calls = job.api_calls if job and job.status != "RUNNING" else None
            records = 0
            outcome = "FAILED"
        attempt.status, attempt.completed_at, attempt.job_id = outcome, finished_at, job_id
        attempt.api_calls = actual_calls
        if state.lease_token == token:
            state.status, state.last_job_id, state.records = outcome, job_id, records
            state.api_calls += actual_calls or 0
            state.lease_token, state.leased_until = None, None
            state.updated_at = finished_at
            state.reason = f"HTTP_{error_status}" if error_status else None
            if outcome in {"COMPLETED", "NO_RESULTS"}:
                state.refreshed_at, state.next_attempt_at = finished_at, None
            else:
                _backoff(state, finished_at)
        session.flush()
        result = _run_result(session, "COMPLETED" if outcome == "NO_RESULTS" else outcome,
            notice_key=notice_key, job_id=job_id, attempted=1, api_calls=actual_calls or 0, records=records)
        if response is not None and any(str(warning).startswith("AWARD_PROVIDER_RATE_LIMIT:")
                                        for warning in getattr(response, "warnings", ())):
            result["provider_rate_limited"] = True
    return result


@router.post("/run")
def run_award_refresh(payload: RunRequest, request: Request, session: DbSession) -> dict:
    if not request.app.state.settings.pps_api_key:
        raise HTTPException(status_code=503, detail="PPS_API_KEY가 서버에 설정되지 않았습니다.")
    deadline = time.monotonic() + BATCH_WALL_SECONDS
    request.state.award_automation_deadline = deadline
    attempted = api_calls = records = 0
    outcomes = []
    last_result = None
    while attempted < payload.max_notices:
        if deadline - time.monotonic() < MIN_NOTICE_WALL_SECONDS:
            break
        result = _run_one(payload, request, session)
        if not result["attempted"]:
            if not attempted:
                return result
            break
        last_result = result
        attempted += result["attempted"]
        api_calls += result["api_calls"]
        records += result["records"]
        outcomes.append(result["status"])
        if result.get("provider_rate_limited"):
            break
    if last_result is None:
        return _run_result(session, "IDLE")
    outcome = "FAILED" if "FAILED" in outcomes else "PARTIAL" if "PARTIAL" in outcomes else "COMPLETED"
    return _run_result(session, outcome, attempted=attempted, api_calls=api_calls, records=records,
                       notice_key=last_result["notice_key"], job_id=last_result["job_id"])
