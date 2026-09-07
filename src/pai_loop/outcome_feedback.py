from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth import require_api_key
from .outcome_write_lock import lock_outcome_notice
from .integrations.awards import OpeningResultsIncomplete
from .integrations.company_awards import (
    DEFAULT_COMPANY_BUSINESS_NUMBER,
    DEFAULT_COMPANY_NAME,
)
from .integrations.outcome_feedback import (
    ExactNoticeAwardFetch,
    PpsOutcomeFeedbackClient,
    canonical_pps_revision,
)
from .integrations.pps import PpsApiError
from .models import (
    BidOutcome,
    Evaluation,
    IngestionJob,
    Notice,
    PpsNoticeAuthority,
    UserDecision,
)
from .outcome_identity import normalise_opening_identity
from .outcome_participation import COMPANY_IDENTITY_SOURCE, PARTICIPATION_KIND, PARTICIPATION_OPERATION


OUTCOME_FEEDBACK_SCHEMA = "pai-loop-pps-outcome-feedback-1.0.0"
OUTCOME_FEEDBACK_SOURCE = "PPS_AUTO_FEEDBACK"
_MAX_NOTICE_KEYS = 25
_DEFAULT_WALL_SECONDS = 75.0
_PPS_METADATA_KIND = "PPS_NOTICE_METADATA"
_MAX_AUTOMATIC_CANDIDATES = 5_000
_MAX_COMPLETED_AUDIT_JOBS = 10_000


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class PpsOutcomeFeedbackRequest(ApiModel):
    """Bounded internal refresh; omitted keys select recent eligible PPS rows."""

    notice_keys: list[str] = Field(default_factory=list, max_length=_MAX_NOTICE_KEYS)
    max_notices: int = Field(default=10, ge=1, le=_MAX_NOTICE_KEYS)
    lookback_days: int = Field(default=730, ge=1, le=1095)
    max_pages_per_notice: int = Field(default=1, ge=1, le=2)
    include_participation: bool = False
    participation_max_pages: int = Field(default=1, ge=1, le=2)
    dry_run: bool = False

    @field_validator("notice_keys")
    @classmethod
    def normalise_notice_keys(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            key = " ".join(str(raw or "").split())
            if not key:
                raise ValueError("notice_keys에는 빈 값이 올 수 없습니다.")
            if len(key) > 160:
                raise ValueError("notice_key는 160자를 초과할 수 없습니다.")
            if key not in seen:
                seen.add(key)
                result.append(key)
        return result


class OutcomeFeedbackItem(ApiModel):
    notice_key: str
    bid_notice_no: str | None = None
    revision_no: str | None = None
    result: Literal[
        "CREATED",
        "UPDATED",
        "UNCHANGED",
        "DRY_RUN_CREATE",
        "DRY_RUN_UPDATE",
        "NO_RESULT",
        "REVIEW",
        "SKIPPED",
        "ERROR",
    ]
    outcome_status: Literal["WON", "LOST"] | None = None
    outcome_key: str | None = None
    exact_result_count: int = 0
    api_calls: int = 0
    reason_code: str
    warnings: list[str] = Field(default_factory=list)


class PpsOutcomeFeedbackResponse(ApiModel):
    job_id: str
    status: Literal["COMPLETED", "PARTIAL", "FAILED"]
    dry_run: bool
    requested_count: int
    selected_count: int
    processed_count: int
    api_calls: int
    fetched: int
    exact_matches: int
    created: int
    updated: int
    unchanged: int
    review: int
    skipped: int
    errors: int
    openai_calls: Literal[0] = 0
    items: list[OutcomeFeedbackItem]
    warnings: list[str]


router = APIRouter(
    prefix="/api/v1/outcome-feedback",
    tags=["PPS outcome feedback"],
    dependencies=[Depends(require_api_key)],
)


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_pps_notice(notice: Notice) -> bool:
    if notice.notice_key.upper().startswith("PPS-"):
        return True
    return any(
        isinstance(version.source_payload, dict)
        and version.source_payload.get("kind") == _PPS_METADATA_KIND
        for version in notice.versions
    )


def _eligibility_reason(
    session: Session,
    notice: Notice,
    *,
    now: datetime,
) -> str | None:
    if not _is_pps_notice(notice):
        return "NOT_PPS_NOTICE"
    if notice.status.upper() == "CANCELLED":
        return "NOTICE_CANCELLED"
    if _as_utc(notice.deadline) > now:
        return "NOTICE_NOT_ENDED"

    authority = session.get(PpsNoticeAuthority, notice.bid_notice_no)
    if authority is None:
        return None
    if authority.disposition.upper() == "CANCELLED":
        return "PPS_AUTHORITY_CANCELLED"
    if authority.disposition.upper() != "VALID":
        return "PPS_AUTHORITY_NOT_VALID"
    if canonical_pps_revision(authority.revision_no) != canonical_pps_revision(
        notice.revision_no
    ):
        return "PPS_REVISION_SUPERSEDED"
    return None


def _query_window(notice: Notice, *, today: date) -> tuple[date, date, str]:
    if notice.published_at is not None:
        basis = _as_utc(notice.published_at).date()
        return basis - timedelta(days=3), min(today, basis + timedelta(days=3)), "PUBLISHED_AT"
    # Legacy/manual imports can lack publication time. Keep this fallback
    # bounded; absence of a result remains NO_RESULT instead of broad guessing.
    basis = _as_utc(notice.deadline).date()
    return basis - timedelta(days=31), min(today, basis + timedelta(days=3)), "DEADLINE_FALLBACK"


def _organisation_label(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _row_time(row: dict[str, Any]) -> datetime:
    for field in ("awarded_at", "registered_at", "opened_at"):
        value = row.get(field)
        if isinstance(value, datetime):
            return _as_utc(value)
    return datetime.min.replace(tzinfo=timezone.utc)


def _select_provider_result(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """Choose one final fact while failing closed on provider identity conflict."""

    if not rows:
        return None, False, None
    ordered = sorted(
        rows,
        key=lambda row: (
            _row_time(row),
            str(row.get("classification_no") or ""),
            str(row.get("rebid_no") or ""),
            str(row.get("identity") or ""),
        ),
        reverse=True,
    )
    company_name = _organisation_label(DEFAULT_COMPANY_NAME)
    business_matches = [
        row for row in ordered if row.get("company_business_number_match") is True
    ]
    if business_matches:
        return business_matches[0], True, "BUSINESS_NUMBER_EXACT"

    invalid_identity = any(
        row.get("company_business_number_status") == "PRESENT_INVALID"
        and _organisation_label(row.get("winner_name")) == company_name
        for row in ordered
    )
    if invalid_identity:
        return None, False, "COMPANY_IDENTITY_INVALID"

    conflicting = any(
        row.get("company_business_number_match") is False
        and _organisation_label(row.get("winner_name")) == company_name
        for row in ordered
    )
    if conflicting:
        return None, False, "COMPANY_IDENTITY_CONFLICT"

    name_matches = [
        row
        for row in ordered
        if row.get("company_business_number_match") is None
        and row.get("company_business_number_status") == "ABSENT"
        and _organisation_label(row.get("winner_name")) == company_name
    ]
    if name_matches:
        return name_matches[0], True, "COMPANY_NAME_EXACT"
    return ordered[0], False, "OTHER_WINNER"


def _latest_human_opening_record(
    session: Session,
    notice: Notice,
    *,
    opening_identity: dict[str, str],
) -> BidOutcome | None:
    candidates = list(
        session.scalars(
            select(BidOutcome)
        .where(
            BidOutcome.notice_id == notice.id,
            or_(
                BidOutcome.source.is_(None),
                BidOutcome.source != OUTCOME_FEEDBACK_SOURCE,
            ),
        )
        .order_by(BidOutcome.updated_at.desc(), BidOutcome.observed_at.desc(), BidOutcome.created_at.desc(), BidOutcome.id.desc())
        ).all()
    )
    for candidate in candidates:
        evidence = candidate.evidence_json if isinstance(candidate.evidence_json, dict) else {}
        if normalise_opening_identity(evidence.get("opening_identity")) != opening_identity:
            continue
        # Resolve the latest human-reviewed record before checking its status.
        # A withdrawn/draft correction must not revive an older submission.
        workflow = evidence.get("_workflow")
        if not isinstance(workflow, dict) or workflow.get("human_reviewed") is not True:
            continue
        return candidate
    return None


def _submission_basis(session: Session, notice: Notice, *, opening_identity: dict[str, str]) -> BidOutcome | None:
    candidate = _latest_human_opening_record(session, notice, opening_identity=opening_identity)
    if candidate is not None and (
        str(candidate.evidence_json["_workflow"].get("record_status") or "").upper() == "VALIDATED"
        and candidate.status in {"SUBMITTED", "WON", "LOST"}
    ):
        return candidate
    return None


def _human_participation_conflict(session: Session, notice: Notice, identity: dict[str, str], outcome_status: str) -> bool:
    candidate = _latest_human_opening_record(session, notice, opening_identity=identity)
    return candidate is not None and (
        str(candidate.evidence_json["_workflow"].get("record_status") or "").upper() != "VALIDATED"
        or candidate.status not in {"SUBMITTED", outcome_status}
    )


def _automatic_outcome_key(opening_identity: dict[str, str]) -> str:
    digest = hashlib.sha256(
        ("PPS|FINAL_AWARD|" + json.dumps(
            opening_identity, sort_keys=True, separators=(",", ":"),
        )).encode("utf-8")
    ).hexdigest()[:40]
    return f"pps-final-award:{digest}"


def _latest_evaluation_id(session: Session, notice: Notice) -> str | None:
    return session.scalar(
        select(Evaluation.id)
        .where(Evaluation.notice_id == notice.id)
        .order_by(Evaluation.evaluated_at.desc(), Evaluation.id.desc())
        .limit(1)
    )


def _latest_decision_id(session: Session, notice: Notice) -> str | None:
    return session.scalar(
        select(UserDecision.id)
        .where(UserDecision.notice_id == notice.id)
        .order_by(UserDecision.created_at.desc(), UserDecision.id.desc())
        .limit(1)
    )


def _provider_reference(operation_path: str, row: dict[str, Any]) -> str:
    identity = str(row.get("identity") or "")
    return f"PPS:1230000:{operation_path}:{identity}"[:1000]


def _outcome_values(
    session: Session,
    notice: Notice,
    *,
    selected: dict[str, Any],
    opening_identity: dict[str, str],
    outcome_status: Literal["WON", "LOST"],
    exact_result_count: int,
    company_match_basis: str,
    participation: BidOutcome | None,
    operation_path: str,
) -> dict[str, Any]:
    occurred_at = selected.get("awarded_at") or selected.get("opened_at")
    if isinstance(occurred_at, datetime):
        occurred_at = _as_utc(occurred_at)
    else:
        occurred_at = _as_utc(notice.deadline)
    evidence = {
        "schema_version": OUTCOME_FEEDBACK_SCHEMA,
        "provider": "PPS_DATA_GO_KR_1230000",
        "operation": operation_path,
        "provider_identity": selected.get("identity"),
        "provider_result_sha256": selected.get("provider_result_sha256"),
        "opening_identity": opening_identity,
        "exact_match": {
            "verified": True,
            "bid_notice_no": notice.bid_notice_no,
            "revision_no": notice.revision_no,
            "canonical_revision": canonical_pps_revision(notice.revision_no),
        },
        "classification_no": selected.get("classification_no"),
        "rebid_no": selected.get("rebid_no"),
        "participant_count": selected.get("participant_count"),
        "opened_at": (
            selected["opened_at"].isoformat()
            if isinstance(selected.get("opened_at"), datetime)
            else None
        ),
        "awarded_at": (
            selected["awarded_at"].isoformat()
            if isinstance(selected.get("awarded_at"), datetime)
            else None
        ),
        "company_match_basis": company_match_basis,
        "participation_basis": (
            {
                "kind": "STORED_BID_OUTCOME",
                "outcome_id": participation.id,
                "outcome_key": participation.outcome_key,
                "status": participation.status,
                "record_status": "VALIDATED",
                "human_reviewed": True,
                "opening_identity": normalise_opening_identity(
                    participation.evidence_json.get("opening_identity")
                ),
            }
            if participation is not None
            else {"kind": "PROVIDER_WINNER_EXACT_MATCH"}
        ),
        "selected_from_exact_result_count": exact_result_count,
    }
    return {
        "evaluation_id": _latest_evaluation_id(session, notice),
        "decision_id": _latest_decision_id(session, notice),
        "status": outcome_status,
        "submitted_bid_amount": None,
        "submitted_bid_rate": None,
        "winning_bid_amount": selected.get("award_amount"),
        "winning_bid_rate": selected.get("award_rate"),
        "technical_score": None,
        "price_score": None,
        "total_score": None,
        "rank": 1 if outcome_status == "WON" else None,
        "winner_name": str(selected.get("winner_name") or "")[:255] or None,
        "reason_code": (
            "PPS_EXACT_WINNER_MATCH"
            if outcome_status == "WON"
            else "PPS_AWARD_AFTER_CONFIRMED_SUBMISSION"
        ),
        "loss_reason": None,
        "risk_summary": None,
        "source": OUTCOME_FEEDBACK_SOURCE,
        "source_reference": _provider_reference(operation_path, selected),
        "evidence_json": evidence,
        "occurred_at": occurred_at,
    }


def _same_value(left: object, right: object) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _as_utc(left) == _as_utc(right)
    return left == right


def _upsert_outcome(
    session: Session,
    notice: Notice,
    *,
    outcome_key: str,
    values: dict[str, Any],
    dry_run: bool,
    precise_timestamp: bool = False,
) -> Literal["CREATED", "UPDATED", "UNCHANGED", "DRY_RUN_CREATE", "DRY_RUN_UPDATE"]:
    existing = session.scalar(
        select(BidOutcome).where(
            BidOutcome.notice_id == notice.id,
            BidOutcome.outcome_key == outcome_key,
        )
    )
    if existing is not None and existing.source != OUTCOME_FEEDBACK_SOURCE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="다른 작성 주체의 결과를 자동 환류로 수정할 수 없습니다.",
        )
    if existing is not None and not precise_timestamp:
        previous_proof = (existing.evidence_json or {}).get("participation_basis")
        if isinstance(previous_proof, dict) and previous_proof.get("kind") == PARTICIPATION_KIND:
            # An old scheduled caller cannot erase proof, bid facts or history
            # after the explicit participant path has established this event.
            return "UNCHANGED"
    if existing is None:
        if not dry_run:
            session.add(
                BidOutcome(
                    notice_id=notice.id,
                    outcome_key=outcome_key,
                    observed_at=datetime.now(timezone.utc),
                    **({"updated_at": datetime.now(timezone.utc)} if precise_timestamp else {}),
                    **values,
                )
            )
        return "DRY_RUN_CREATE" if dry_run else "CREATED"

    changes = {
        field: value
        for field, value in values.items()
        if not _same_value(getattr(existing, field), value)
    }
    if not changes:
        return "UNCHANGED"
    if not dry_run:
        for field, value in changes.items():
            setattr(existing, field, value)
        if precise_timestamp:
            existing.updated_at = datetime.now(timezone.utc)
    return "DRY_RUN_UPDATE" if dry_run else "UPDATED"


def _select_notices(
    session: Session,
    payload: PpsOutcomeFeedbackRequest,
    *,
    now: datetime,
) -> tuple[list[Notice], list[str]]:
    if payload.notice_keys:
        rows = list(
            session.scalars(
                select(Notice).where(Notice.notice_key.in_(payload.notice_keys))
            ).all()
        )
        by_key = {row.notice_key: row for row in rows}
        return (
            [by_key[key] for key in payload.notice_keys if key in by_key],
            [key for key in payload.notice_keys if key not in by_key],
        )

    cutoff = now - timedelta(days=payload.lookback_days)
    candidates = list(
        session.scalars(
            select(Notice)
            .where(
                Notice.deadline <= now,
                Notice.deadline >= cutoff,
                Notice.status != "CANCELLED",
                Notice.notice_key.like("PPS-%"),
            )
            .order_by(Notice.deadline.desc(), Notice.notice_key)
            .limit(_MAX_AUTOMATIC_CANDIDATES)
        ).all()
    )
    candidate_keys = {notice.notice_key for notice in candidates}
    completed_jobs = list(
        session.scalars(
            select(IngestionJob)
            .where(
                IngestionJob.source == "PPS_OUTCOME",
                IngestionJob.completed_at.is_not(None),
            )
            .order_by(IngestionJob.completed_at.desc(), IngestionJob.created_at.desc())
            .limit(_MAX_COMPLETED_AUDIT_JOBS)
        ).all()
    )
    last_checked_at: dict[str, datetime] = {}
    for completed_job in completed_jobs:
        checked_at = _as_utc(completed_job.completed_at)  # type: ignore[arg-type]
        for key in completed_job.notice_keys or []:
            if (
                isinstance(key, str)
                and key in candidate_keys
                and key not in last_checked_at
            ):
                last_checked_at[key] = checked_at

    # A binary 24-hour recent/not-recent bucket can starve older notices when
    # the daily scheduler slips by a few seconds. Unchecked rows come first;
    # checked rows rotate strictly oldest-first. Equal audit times prefer the
    # more recent deadline, then the stable public notice key.
    unseen_time = datetime.min.replace(tzinfo=timezone.utc)

    def rotation_key(notice: Notice) -> tuple[int, datetime, float, str]:
        checked_at = last_checked_at.get(notice.notice_key)
        deadline = _as_utc(notice.deadline)
        return (
            0 if checked_at is None else 1,
            checked_at or unseen_time,
            -deadline.timestamp(),
            notice.notice_key,
        )

    candidates.sort(key=rotation_key)
    return candidates[: payload.max_notices], []


def _mark_job_failed(session: Session, job_id: str, *, code: str, warning: str) -> None:
    session.rollback()
    job = session.get(IngestionJob, job_id)
    if job is None:
        return
    job.status = "FAILED"
    job.error_code = code
    job.warnings = list(dict.fromkeys([*(job.warnings or []), warning]))[:100]
    job.completed_at = datetime.now(timezone.utc)
    session.commit()


@router.post("/pps/refresh", response_model=PpsOutcomeFeedbackResponse)
def refresh_pps_outcomes(
    payload: PpsOutcomeFeedbackRequest,
    request: Request,
    session: DbSession,
) -> PpsOutcomeFeedbackResponse:
    """Idempotently feed exact PPS final results into stored decision outcomes.

    This operation never calls OpenAI. A third-party winner becomes LOST only
    when a separate stored outcome proves that the company actually submitted.
    """

    settings = request.app.state.settings
    if not settings.pps_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PPS_API_KEY가 서버에 설정되지 않았습니다.",
        )

    now = datetime.now(timezone.utc)
    today = now.date()
    selected_notices, missing_keys = _select_notices(session, payload, now=now)
    requested_count = len(payload.notice_keys) if payload.notice_keys else len(selected_notices)
    job = IngestionJob(
        source="PPS_OUTCOME",
        mode="DRY_RUN" if payload.dry_run else "LIVE",
        status="RUNNING",
        window_json={
            "from": (now - timedelta(days=payload.lookback_days)).date().isoformat(),
            "to": today.isoformat(),
        },
        keyword=None,
        request_json={
            "schema_version": OUTCOME_FEEDBACK_SCHEMA,
            "notice_keys": payload.notice_keys,
            "max_notices": payload.max_notices,
            "lookback_days": payload.lookback_days,
            "max_pages_per_notice": payload.max_pages_per_notice,
            "include_participation": payload.include_participation,
            "participation_max_pages": payload.participation_max_pages,
            "dry_run": payload.dry_run,
            "openai_calls": 0,
        },
        notice_keys=[],
        warnings=[],
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    items = [
        OutcomeFeedbackItem(
            notice_key=key,
            result="SKIPPED",
            reason_code="NOTICE_NOT_FOUND",
        )
        for key in missing_keys
    ]
    counters = {
        "fetched": 0,
        "exact_matches": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "review": 0,
        "skipped": len(missing_keys),
        "ineligible_skipped": 0,
        "errors": 0,
        "quarantined": 0,
    }
    aggregate_warnings: list[str] = []
    operation_path = settings.pps_award_operation
    wall_deadline = time.monotonic() + _DEFAULT_WALL_SECONDS

    try:
        client_context = (
            PpsOutcomeFeedbackClient(
                service_key=settings.pps_api_key,
                base_url=settings.pps_base_url,
                timeout_seconds=12,
                max_retries=1,
            )
            if selected_notices
            else nullcontext(None)
        )
        with client_context as client:
            for notice in selected_notices:
                assert client is not None
                ineligible = _eligibility_reason(session, notice, now=now)
                if ineligible:
                    counters["skipped"] += 1
                    counters["ineligible_skipped"] += 1
                    items.append(
                        OutcomeFeedbackItem(
                            notice_key=notice.notice_key,
                            bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no,
                            result="SKIPPED",
                            reason_code=ineligible,
                        )
                    )
                    continue
                if time.monotonic() >= wall_deadline:
                    counters["errors"] += 1
                    items.append(
                        OutcomeFeedbackItem(
                            notice_key=notice.notice_key,
                            bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no,
                            result="ERROR",
                            reason_code="WALL_TIME_LIMIT",
                        )
                    )
                    aggregate_warnings.append("WALL_TIME_LIMIT")
                    continue

                start, end, window_basis = _query_window(notice, today=today)
                calls_before = client.request_count
                observation_started_at = datetime.now(timezone.utc)
                try:
                    fetched = client.fetch_exact_notice_awards(
                        bid_notice_no=notice.bid_notice_no,
                        revision_no=notice.revision_no,
                        start=start,
                        end=end,
                        company_business_number=DEFAULT_COMPANY_BUSINESS_NUMBER,
                        operation_path=operation_path,
                        rows=100,
                        max_pages_per_window=payload.max_pages_per_notice,
                        deadline_monotonic=wall_deadline,
                        require_complete=payload.include_participation,
                    )
                except OpeningResultsIncomplete:
                    counters["review"] += 1
                    items.append(OutcomeFeedbackItem(
                        notice_key=notice.notice_key, bid_notice_no=notice.bid_notice_no,
                        revision_no=notice.revision_no, result="REVIEW",
                        api_calls=client.request_count - calls_before,
                        reason_code="PPS_FINAL_RESULT_INCOMPLETE",
                    ))
                    continue
                except PpsApiError:
                    counters["errors"] += 1
                    items.append(
                        OutcomeFeedbackItem(
                            notice_key=notice.notice_key,
                            bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no,
                            result="ERROR",
                            api_calls=client.request_count - calls_before,
                            reason_code="PPS_AWARD_API_ERROR",
                        )
                    )
                    aggregate_warnings.append("PPS_AWARD_API_ERROR")
                    continue

                # Re-check exact identity at the service boundary. Tests and
                # future adapters cannot bypass this fail-closed guard.
                exact_rows = [
                    row
                    for row in fetched.rows
                    if str(row.get("bid_notice_no") or "").strip()
                    == notice.bid_notice_no
                    and canonical_pps_revision(row.get("revision_no"))
                    == canonical_pps_revision(notice.revision_no)
                ]
                boundary_mismatches = len(fetched.rows) - len(exact_rows)
                counters["fetched"] += fetched.fetched_count
                counters["exact_matches"] += len(exact_rows)
                counters["quarantined"] += (
                    fetched.mismatched_count
                    + fetched.quarantined_count
                    + boundary_mismatches
                )
                item_warnings: list[str] = []
                if fetched.hit_page_limit:
                    item_warnings.append("PAGE_LIMIT_REACHED")
                    aggregate_warnings.append("PAGE_LIMIT_REACHED")
                if fetched.hit_time_limit:
                    item_warnings.append("WALL_TIME_LIMIT")
                    aggregate_warnings.append("WALL_TIME_LIMIT")
                if fetched.mismatched_count or boundary_mismatches:
                    item_warnings.append("PROVIDER_IDENTITY_MISMATCH_DROPPED")
                if fetched.quarantined_count:
                    item_warnings.append("INCOMPLETE_PROVIDER_RESULT_DROPPED")
                if len(exact_rows) > 1:
                    item_warnings.append("MULTIPLE_EXACT_RESULTS")

                # A bounded provider response is not proof that the selected
                # rows are the complete final result set. In particular, a
                # later page can contain our company as a winner. Never turn a
                # page/time-limited response into a durable WON/LOST fact.
                if fetched.hit_page_limit or fetched.hit_time_limit:
                    counters["review"] += 1
                    items.append(
                        OutcomeFeedbackItem(
                            notice_key=notice.notice_key,
                            bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no,
                            result="REVIEW",
                            exact_result_count=len(exact_rows),
                            api_calls=fetched.api_calls,
                            reason_code="PPS_FINAL_RESULT_INCOMPLETE",
                            warnings=item_warnings,
                        )
                    )
                    continue

                # One notice/revision can contain several classifications or
                # rebids. Do not select an old win or borrow another round's
                # submission. This bounded endpoint requires one final opening.
                identities = [normalise_opening_identity(
                    row.get("opening_identity") if "opening_identity" in row else row
                ) for row in exact_rows]
                identity_problem = (
                    "PPS_OPENING_IDENTITY_MISSING" if any(value is None for value in identities)
                    else "PPS_OPENING_IDENTITY_MISMATCH" if any(
                        identity != normalise_opening_identity(row)
                        for identity, row in zip(identities, exact_rows, strict=True)
                    )
                    else "PPS_MULTIPLE_OPENING_IDENTITIES" if len({
                        json.dumps(value, sort_keys=True) for value in identities
                    }) > 1 else None
                )
                if identity_problem:
                    counters["review"] += 1
                    items.append(OutcomeFeedbackItem(
                        notice_key=notice.notice_key, bid_notice_no=notice.bid_notice_no,
                        revision_no=notice.revision_no, result="REVIEW",
                        exact_result_count=len(exact_rows), api_calls=fetched.api_calls,
                        reason_code=identity_problem, warnings=item_warnings,
                    ))
                    continue

                selected, company_won, company_basis = _select_provider_result(exact_rows)
                if company_basis in {
                    "COMPANY_IDENTITY_CONFLICT",
                    "COMPANY_IDENTITY_INVALID",
                }:
                    counters["review"] += 1
                    counters["quarantined"] += 1
                    items.append(
                        OutcomeFeedbackItem(
                            notice_key=notice.notice_key,
                            bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no,
                            result="REVIEW",
                            exact_result_count=len(exact_rows),
                            api_calls=fetched.api_calls,
                            reason_code="COMPANY_IDENTITY_CONFLICT",
                            warnings=item_warnings,
                        )
                    )
                    continue
                if selected is None:
                    items.append(
                        OutcomeFeedbackItem(
                            notice_key=notice.notice_key,
                            bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no,
                            result="NO_RESULT",
                            exact_result_count=0,
                            api_calls=fetched.api_calls,
                            reason_code="PPS_FINAL_RESULT_NOT_FOUND",
                            warnings=item_warnings,
                        )
                    )
                    continue

                opening_identity = identities[0]
                assert opening_identity is not None  # The explicit guard above proved it.
                outcome_key = _automatic_outcome_key(opening_identity)
                participation = None
                provider_participant = None
                if payload.include_participation:
                    strict_problem = None
                    if (len(exact_rows) != 1 or fetched.mismatched_count or fetched.quarantined_count or boundary_mismatches):
                        strict_problem = "PPS_FINAL_RESULT_INCONSISTENT"
                    elif (selected.get("company_business_number_status") != "PRESENT_VALID"
                          or not isinstance(selected.get("company_business_number_match"), bool)
                          or not re.fullmatch(r"[0-9a-f]{64}", str(selected.get("provider_result_sha256") or ""))):
                        strict_problem = "FINAL_WINNER_IDENTITY_UNCONFIRMED"
                    if strict_problem is None:
                        try:
                            companies = client.fetch_exact_opening_participation(
                                **opening_identity, company_business_number=DEFAULT_COMPANY_BUSINESS_NUMBER,
                                rows=100, max_pages=payload.participation_max_pages,
                                deadline_monotonic=wall_deadline,
                            )
                            counters["fetched"] += len(companies)
                            own = [row for row in companies if row.get("company_business_number_match") is True]
                            winners = [row for row in companies if row.get("final_winner_match") is True]
                            if not companies or any(normalise_opening_identity(row.get("opening_identity")) != opening_identity
                                                    or not isinstance(row.get("company_business_number_match"), bool)
                                                    for row in companies):
                                strict_problem = "PPS_PARTICIPATION_INCOMPLETE"
                            elif (len(winners) != 1 or winners[0].get("company_business_number_match") is not selected["company_business_number_match"]
                                  or (selected.get("participant_count") is not None and selected["participant_count"] != len(companies))):
                                strict_problem = "PPS_FINAL_PARTICIPANTS_INCONSISTENT"
                            elif len(own) != 1:
                                strict_problem = "COMPANY_PARTICIPATION_NOT_CONFIRMED"
                            else:
                                provider_participant = own[0]
                        except OpeningResultsIncomplete:
                            strict_problem = "PPS_PARTICIPATION_INCOMPLETE"
                        except PpsApiError:
                            strict_problem = "PPS_PARTICIPATION_API_ERROR"
                    fetched = replace(fetched, api_calls=client.request_count - calls_before)
                    if strict_problem:
                        counters["review"] += 1
                        items.append(OutcomeFeedbackItem(
                            notice_key=notice.notice_key, bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no, result="REVIEW",
                            exact_result_count=len(exact_rows), api_calls=fetched.api_calls,
                            reason_code=strict_problem, warnings=item_warnings,
                        ))
                        continue
                    outcome_status = "WON" if selected["company_business_number_match"] else "LOST"
                elif company_won:
                    outcome_status: Literal["WON", "LOST"] = "WON"
                else:
                    participation = _submission_basis(
                        session,
                        notice,
                        opening_identity=opening_identity,
                    )
                    if participation is None:
                        counters["review"] += 1
                        items.append(
                            OutcomeFeedbackItem(
                                notice_key=notice.notice_key,
                                bid_notice_no=notice.bid_notice_no,
                                revision_no=notice.revision_no,
                                result="REVIEW",
                                exact_result_count=len(exact_rows),
                                api_calls=fetched.api_calls,
                                reason_code="PARTICIPATION_OPENING_NOT_CONFIRMED",
                                warnings=item_warnings,
                            )
                        )
                        continue
                    outcome_status = "LOST"

                values = _outcome_values(
                    session,
                    notice,
                    selected=selected,
                    opening_identity=opening_identity,
                    outcome_status=outcome_status,
                    exact_result_count=len(exact_rows),
                    company_match_basis=company_basis or "OTHER_WINNER",
                    participation=participation,
                    operation_path=operation_path,
                )
                if provider_participant is not None:
                    # Finish provider reads before the short serialized write.
                    # Re-read human corrections and newer provider observations.
                    notice_id, notice_key = notice.id, notice.notice_key
                    session.rollback()
                    lock_outcome_notice(session, notice_key)
                    notice = session.get(Notice, notice_id)
                    assert notice is not None
                    existing = session.scalar(select(BidOutcome).where(
                        BidOutcome.notice_id == notice.id, BidOutcome.outcome_key == outcome_key))
                    stale = existing is not None and _as_utc(existing.updated_at) > observation_started_at
                    ineligible = _eligibility_reason(session, notice, now=datetime.now(timezone.utc))
                    if stale or ineligible or _human_participation_conflict(session, notice, opening_identity, outcome_status):
                        session.rollback()
                        counters["review"] += 1
                        items.append(OutcomeFeedbackItem(
                            notice_key=notice.notice_key, bid_notice_no=notice.bid_notice_no,
                            revision_no=notice.revision_no, result="REVIEW",
                            exact_result_count=len(exact_rows), api_calls=fetched.api_calls,
                            reason_code="NEWER_RESULT_PRESERVED" if stale else ineligible or "HUMAN_PARTICIPATION_CONFLICT",
                        ))
                        continue
                    participant_digest = hashlib.sha256(json.dumps(
                        provider_participant, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    ).encode()).hexdigest()
                    values["evidence_json"]["participation_basis"] = {
                        "kind": PARTICIPATION_KIND, "operation": PARTICIPATION_OPERATION,
                        "complete": True, "company_identifier_match": True,
                        "winner_is_company": outcome_status == "WON", "opening_identity": opening_identity,
                        "company_identity_source": COMPANY_IDENTITY_SOURCE,
                        "final_result_sha256": selected.get("provider_result_sha256"),
                        "participant_result_sha256": participant_digest,
                    }
                    values["submitted_bid_amount"] = provider_participant.get("bid_amount")
                    values["decision_id"] = None  # Company proof cannot identify an owning department.
                    values["reason_code"] = "PPS_EXACT_PARTICIPANT_AND_FINAL_WINNER"
                    # Preserve earlier automatic evidence when a provider fact changes.
                    if existing is not None:
                        history = list((existing.evidence_json or {}).get("_provider_history", []))
                        previous = {key: value for key, value in (existing.evidence_json or {}).items() if key != "_provider_history"}
                        if previous != values["evidence_json"]:
                            history.append({"status": existing.status, "evidence": previous,
                                            "observed_at": _as_utc(existing.updated_at).isoformat()})
                        if history:
                            values["evidence_json"]["_provider_history"] = history
                result = _upsert_outcome(
                    session,
                    notice,
                    outcome_key=outcome_key,
                    values=values,
                    dry_run=payload.dry_run,
                    precise_timestamp=provider_participant is not None,
                )
                if not payload.dry_run:
                    session.commit()
                elif provider_participant is not None:
                    session.rollback()
                counter = {
                    "CREATED": "created",
                    "DRY_RUN_CREATE": "created",
                    "UPDATED": "updated",
                    "DRY_RUN_UPDATE": "updated",
                    "UNCHANGED": "unchanged",
                }[result]
                counters[counter] += 1
                items.append(
                    OutcomeFeedbackItem(
                        notice_key=notice.notice_key,
                        bid_notice_no=notice.bid_notice_no,
                        revision_no=notice.revision_no,
                        result=result,
                        outcome_status=outcome_status,
                        outcome_key=outcome_key,
                        exact_result_count=len(exact_rows),
                        api_calls=fetched.api_calls,
                        reason_code=str(values["reason_code"]),
                        warnings=[*item_warnings, f"QUERY_WINDOW_BASIS_{window_basis}"],
                    )
                )
            api_calls = client.request_count if client is not None else 0
    except Exception:
        _mark_job_failed(
            session,
            job.id,
            code="PPS_OUTCOME_CLIENT_ERROR",
            warning="낙찰 결과 자동 환류 클라이언트가 예기치 않게 종료되었습니다.",
        )
        raise

    provider_partial = any(
        warning in {"PAGE_LIMIT_REACHED", "WALL_TIME_LIMIT"}
        for warning in aggregate_warnings
    )
    if counters["errors"] and counters["errors"] >= len(selected_notices):
        job_status: Literal["COMPLETED", "PARTIAL", "FAILED"] = "FAILED"
    elif counters["errors"] or provider_partial:
        job_status = "PARTIAL"
    else:
        job_status = "COMPLETED"
    aggregate_warnings = list(dict.fromkeys(aggregate_warnings))
    job.status = job_status
    job.api_calls = api_calls
    job.fetched = counters["fetched"]
    job.matched = counters["exact_matches"]
    job.created_count = counters["created"]
    job.updated_count = counters["updated"]
    job.duplicate_count = counters["unchanged"]
    job.quarantined_count = counters["quarantined"]
    # This audit field records every selected notice, including NO_RESULT and
    # REVIEW, so subsequent automatic batches can rotate through the backlog.
    job.notice_keys = [notice.notice_key for notice in selected_notices]
    job.warnings = aggregate_warnings
    job.error_code = "PPS_OUTCOME_ALL_FAILED" if job_status == "FAILED" else None
    job.completed_at = datetime.now(timezone.utc)
    try:
        session.commit()
    except Exception:
        _mark_job_failed(
            session,
            job.id,
            code="PPS_OUTCOME_PERSISTENCE_ERROR",
            warning="낙찰 결과 자동 환류 감사 로그 저장에 실패했습니다.",
        )
        raise

    return PpsOutcomeFeedbackResponse(
        job_id=job.id,
        status=job_status,
        dry_run=payload.dry_run,
        requested_count=requested_count,
        selected_count=len(selected_notices),
        processed_count=len(selected_notices) - counters["ineligible_skipped"],
        api_calls=api_calls,
        fetched=counters["fetched"],
        exact_matches=counters["exact_matches"],
        created=counters["created"],
        updated=counters["updated"],
        unchanged=counters["unchanged"],
        review=counters["review"],
        skipped=counters["skipped"],
        errors=counters["errors"],
        items=items,
        warnings=aggregate_warnings,
    )
