from __future__ import annotations

import re
import secrets
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from .analysis_api import AnalysisBatchRequest, run_notice_analysis_batch
from .integrations.openai_extraction import OpenAITelemetry, merge_openai_telemetry
from .models import IngestionJob, Notice
from .notice_freshness import (
    authoritative_pps_notice_is_cancelled,
    latest_current_evaluation,
)
from .pps_enrichment import (
    MAX_ATTACHMENTS_IN_MANIFEST,
    PPS_ATTACHMENT_SOURCE,
    PublicAnalysisReason,
    _current_manifest_attempts,
    has_current_accepted_pps_extraction,
    public_analysis_reason,
)


class ManualAnalysisResponse(BaseModel):
    """Small public projection of a server-side, single-notice analysis."""

    model_config = ConfigDict(from_attributes=True)

    request_id: str | None = None
    notice_key: str
    outcome: Literal["QUEUED", "COMPLETED", "REVIEW", "ALREADY_ANALYZED", "COOLDOWN"]
    analysis_state: Literal["ANALYZED", "REVIEW", "PENDING"]
    analysis_reason_code: str
    analysis_reason: str
    analysis_attempted: bool
    openai_calls: int = 0
    openai_telemetry: OpenAITelemetry = Field(default_factory=OpenAITelemetry)
    message: str


class ManualAnalysisRequest(BaseModel):
    """Caller-approved provider boundary for one manual request."""

    model_config = ConfigDict(extra="forbid")

    run_extraction: bool = False
    recompute_current: bool = False
    retry_reviewed: bool = False

    @model_validator(mode="after")
    def validate_intent(self) -> "ManualAnalysisRequest":
        if self.recompute_current and (self.run_extraction or self.retry_reviewed):
            raise ValueError("recompute_current is a zero-provider-call operation")
        if self.retry_reviewed and not self.run_extraction:
            raise ValueError("retry_reviewed requires run_extraction")
        return self


class QuantitativeDiagnosticIssue(BaseModel):
    """Safe aggregate of one internal validation code for PIN operators."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,100}$")
    disposition: Literal["REVIEW", "INCOMPLETE"]
    count: int = Field(ge=1)


class QuantitativeDiagnosticCaseShape(BaseModel):
    """Non-text structural shape of one extracted score row."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    row_order: int = Field(ge=1, le=100)
    operator: Literal["GTE", "EQ", "IN"]
    comparison_value_present: bool
    category_value_count: int = Field(ge=0, le=100)
    award_kind: Literal["POINTS", "PERCENT_OF_MAX"]
    award_value: float | None = Field(default=None, ge=0, le=1000)
    award_value_within_safe_range: bool
    literal_point_values: list[float] = Field(max_length=8)
    evidence_point_values: list[float] = Field(max_length=8)
    literal_matches_evidence: bool


class QuantitativeDiagnosticCandidateShape(BaseModel):
    """Non-text REVIEW candidate metadata without IDs, digests, or raw values."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_ordinal: int = Field(ge=1)
    table_ordinal: int = Field(ge=1)
    criterion_ordinal: int = Field(ge=1)
    document_type: Literal["NOTICE", "RFP", "SCOPE", "FORM", "OTHER"]
    status: Literal["REVIEW", "INCOMPLETE"]
    issue_codes: list[str] = Field(max_length=30)
    criterion_character_count: int = Field(ge=0, le=2000)
    evidence_character_count: int = Field(ge=0, le=500)
    criterion_literal_matches_evidence: bool
    criterion_has_metric_tokens: bool | None
    evidence_has_metric_tokens: bool | None
    criterion_point_values: list[float] = Field(max_length=8)
    evidence_point_values: list[float] = Field(max_length=8)
    max_points: float | None = Field(default=None, gt=0, le=1000)
    max_points_within_safe_range: bool
    scoring_method: Literal[
        "BRACKET", "THRESHOLD", "FORMULA", "CASE_TABLE", "UNKNOWN"
    ]
    metric: Literal[
        "PERFORMANCE_AMOUNT",
        "PERFORMANCE_COUNT",
        "PERSONNEL_COUNT",
        "CERTIFICATION_COUNT",
        "CREDIT_RATING",
        "FINANCIAL_RATIO",
        "BUSINESS_YEARS",
        "FACILITY_EQUIPMENT_COUNT",
        "AWARD_COUNT",
        "LOCAL_PRESENCE",
        "UNKNOWN",
    ]
    unit_present: bool
    bracket_count: int = Field(ge=0, le=100)
    threshold_present: bool
    formula_present: bool
    recognition_condition_count: int = Field(ge=0, le=20)
    case_count: int = Field(ge=0, le=100)
    cases: list[QuantitativeDiagnosticCaseShape] = Field(max_length=12)


class ManualQuantitativeDiagnosticsResponse(BaseModel):
    """Redacted state plus bounded public-source REVIEW candidate metadata."""

    model_config = ConfigDict(extra="forbid")

    notice_key: str
    profile_status: Literal[
        "MISSING", "AVAILABLE", "REVIEW", "INCOMPLETE", "NOT_APPLICABLE"
    ]
    expected_attachment_count: int = Field(ge=0)
    processed_attachment_count: int = Field(ge=0)
    document_binding_count: int = Field(ge=0)
    table_status_counts: dict[str, int]
    available_candidate_count: int = Field(ge=0)
    review_candidate_count: int = Field(ge=0)
    issues: list[QuantitativeDiagnosticIssue]
    review_candidate_issues: list[QuantitativeDiagnosticIssue]
    activation_reasons: list[str]
    review_candidate_shapes: list[QuantitativeDiagnosticCandidateShape]


router = APIRouter(prefix="/api/v1", tags=["public manual analysis"])

# Public clicks never receive the server API key. This separate BFF boundary
# serialises every anonymous analysis request across workers before it can
# consume provider capacity. PostgreSQL uses the same fixed advisory-lock
# namespace on every instance; local SQLite tests use an in-process lock.
_PUBLIC_MANUAL_LOCK_KEY = 0x5041494D  # "PAIM"
_PUBLIC_MANUAL_PROCESS_LOCK = threading.Lock()
_NON_ATTEMPT_REQUEST_COOLDOWN = timedelta(minutes=5)
_PIN_FAILURE_LOCK = threading.Lock()
_PIN_FAILURE_WINDOW_SECONDS = 10 * 60
_PIN_FAILURES_PER_CLIENT = 5
_PIN_FAILURES_GLOBAL = 20
_SAFE_DIAGNOSTIC_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
_SHA256_SHAPE = re.compile(r"^[A-Fa-f0-9]{64}$")
_MAX_DIAGNOSTIC_CODES = 100
_MAX_DIAGNOSTIC_REVIEW_CANDIDATES = 12
_MAX_DIAGNOSTIC_CASES = 12
_DIAGNOSTIC_POINT_VALUE = re.compile(
    r"(?<![\d.])(\d{1,3}(?:\.\d{1,2})?)\s*점(?!\s*[\d.])"
)
_DIAGNOSTIC_METRIC_TOKENS: dict[str, tuple[tuple[str, ...], ...]] = {
    "PERFORMANCE_AMOUNT": (("실적", "금액"),),
    "PERFORMANCE_COUNT": (("실적", "건수"),),
    "CREDIT_RATING": (("경영상태",), ("신용평가등급",)),
}


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


def _is_authoritatively_cancelled(request: Request, notice: Notice) -> bool:
    """Check PPS authority before reserving public quota or a background job."""

    with request.app.state.session_factory() as session:
        return authoritative_pps_notice_is_cancelled(session, notice)


def _has_complete_current_attachment_audit(request: Request, notice: Notice) -> bool:
    """Prove a judgement-only retry cannot cross the model boundary."""

    with request.app.state.session_factory() as session:
        return has_current_accepted_pps_extraction(session, notice.id)


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


def _manual_feature_enabled(request: Request) -> bool:
    settings = request.app.state.settings
    if not (settings.public_read_only and settings.public_manual_analysis_enabled):
        return False
    # Development remains convenient for local contract tests. Production
    # never exposes a spend endpoint until a separate, narrow operator secret
    # is configured; the broad server API key is deliberately not reused in a
    # browser.
    return bool(
        settings.environment.casefold() != "production"
        or settings.public_manual_analysis_token_valid
    )


def _require_manual_operator(request: Request) -> None:
    settings = request.app.state.settings
    if settings.environment.casefold() != "production":
        return
    expected = (
        settings.public_manual_analysis_token
        if settings.public_manual_analysis_token_valid
        else ""
    )
    supplied = request.headers.get("x-pai-manual-token", "")
    client_key = str(request.client.host if request.client else "unknown")[:128]
    now = time.monotonic()
    valid = bool(
        expected
        and supplied
        and secrets.compare_digest(supplied, expected)
    )
    with _PIN_FAILURE_LOCK:
        failures = list(getattr(request.app.state, "manual_pin_failures", []))
        failures = [
            item
            for item in failures
            if now - float(item[0]) < _PIN_FAILURE_WINDOW_SECONDS
        ]
        if valid:
            request.app.state.manual_pin_failures = [
                item for item in failures if item[1] != client_key
            ]
        else:
            client_failures = sum(1 for _, key in failures if key == client_key)
            if (
                client_failures >= _PIN_FAILURES_PER_CLIENT
                or len(failures) >= _PIN_FAILURES_GLOBAL
            ):
                request.app.state.manual_pin_failures = failures
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="운영 PIN 확인 실패가 반복되어 잠시 잠겼습니다.",
                    headers={"Retry-After": str(_PIN_FAILURE_WINDOW_SECONDS)},
                )
            failures.append((now, client_key))
            request.app.state.manual_pin_failures = failures
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="분석 실행 키를 확인해 주세요.",
        )


@contextmanager
def _manual_execution_slot(request: Request, *, blocking: bool = False):
    engine = request.app.state.engine
    if engine.dialect.name == "postgresql":
        connection = engine.connect()
        acquired = bool(
            connection.scalar(
                text(
                    "SELECT pg_advisory_lock(:lock_key)"
                    if blocking
                    else "SELECT pg_try_advisory_lock(:lock_key)"
                ),
                {"lock_key": _PUBLIC_MANUAL_LOCK_KEY},
            )
        )
        if blocking:
            acquired = True
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

    acquired = _PUBLIC_MANUAL_PROCESS_LOCK.acquire(blocking=blocking)
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
                    IngestionJob.source.in_(
                        ("MANUAL_ANALYSIS", "MANUAL_PRESPEC_ANALYSIS")
                    ),
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


def _reserve_manual_job(
    request: Request,
    notice_key: str,
    *,
    evaluation_only: bool,
    recompute_current: bool,
    retry_reviewed: bool,
) -> str:
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
                    "evaluation_only": evaluation_only,
                    "recompute_current": recompute_current,
                    "retry_reviewed": retry_reviewed,
                    "enrich_missing": not evaluation_only,
                    "max_notices": 1,
                    "max_attachments_per_notice": MAX_ATTACHMENTS_IN_MANIFEST,
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
    openai_telemetry: OpenAITelemetry | None = None,
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
        config["openai_telemetry"] = (
            openai_telemetry or OpenAITelemetry()
        ).model_dump(mode="json")
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


def _safe_diagnostic_code(value: object) -> str:
    candidate = str(value or "")
    for prefix in ("LOGICAL_TABLE_CONFLICT", "LOGICAL_CRITERION_CONFLICT"):
        if candidate.startswith(prefix + "|"):
            return prefix
    return (
        candidate
        if _SAFE_DIAGNOSTIC_CODE.fullmatch(candidate)
        and not _SHA256_SHAPE.fullmatch(candidate)
        else "UNKNOWN_VALIDATION_ISSUE"
    )


def _diagnostic_point_values(value: object) -> list[float]:
    """Expose only bounded point tokens, never arbitrary source numbers."""

    values: list[float] = []
    for raw in _DIAGNOSTIC_POINT_VALUE.findall(str(value or "")):
        number = float(raw)
        if 0 <= number <= 1000 and number not in values:
            values.append(number)
        if len(values) >= 8:
            break
    return values


def _diagnostic_has_metric_tokens(metric: str, value: object) -> bool | None:
    compact = re.sub(r"\s+", "", str(value or ""))
    groups = _DIAGNOSTIC_METRIC_TOKENS.get(metric, ())
    if not groups:
        return None
    return any(all(token in compact for token in group) for group in groups)


def _safe_diagnostic_score(value: object) -> tuple[float | None, bool]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None, False
    return (score, True) if 0 <= score <= 1000 else (None, False)


def _quantitative_review_candidate_shapes(
    notice: Notice,
    profile: object,
) -> list[QuantitativeDiagnosticCandidateShape]:
    """Project only current-manifest REVIEW candidates from persisted extraction."""

    from .integrations.openai_extraction import (
        ExtractionPayload,
        evidence_quote_matches_source,
    )

    manifest_sha256 = str(getattr(profile, "manifest_sha256", "") or "")
    expected_attachment_ids = tuple(
        str(item) for item in getattr(profile, "expected_attachment_ids", ())
    )
    if not _SHA256_SHAPE.fullmatch(manifest_sha256):
        return []
    review_by_key = {
        (
            str(item.source_attachment_id),
            str(item.table_id),
            str(item.criterion_id),
        ): item
        for item in getattr(profile, "review_candidates", ())
    }
    if not review_by_key:
        return []
    source_ordinals = {
        attachment_id: index
        for index, attachment_id in enumerate(expected_attachment_ids, start=1)
    }
    try:
        _attachments, _invalid_count, attempts = _current_manifest_attempts(
            list(getattr(notice, "versions", ()))
        )
    except (AttributeError, TypeError, ValueError):
        return []
    seen: set[tuple[str, str, str]] = set()
    shapes: list[QuantitativeDiagnosticCandidateShape] = []
    for attachment_id in expected_attachment_ids:
        version = attempts.get(attachment_id)
        if version is None:
            continue
        payload = getattr(version, "source_payload", None)
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION"
            or payload.get("source_kind") != PPS_ATTACHMENT_SOURCE
            or payload.get("status") != "ACCEPTED"
            or payload.get("current_manifest_sha256") != manifest_sha256
            or str(payload.get("attachment_id") or "") != attachment_id
        ):
            continue
        try:
            extraction = ExtractionPayload.model_validate(payload.get("result"))
        except ValidationError:
            continue
        for table_ordinal, table in enumerate(extraction.quantitative_tables, start=1):
            for criterion_ordinal, candidate in enumerate(table.criteria, start=1):
                key = (attachment_id, table.table_id, candidate.criterion_id)
                review = review_by_key.get(key)
                if review is None or key in seen:
                    continue
                seen.add(key)
                cases: list[QuantitativeDiagnosticCaseShape] = []
                for item in candidate.cases[:_MAX_DIAGNOSTIC_CASES]:
                    award_value, award_value_safe = _safe_diagnostic_score(
                        item.award_value
                    )
                    cases.append(
                        QuantitativeDiagnosticCaseShape(
                            row_order=item.row_order,
                            operator=item.operator,
                            comparison_value_present=(
                                item.comparison_value is not None
                            ),
                            category_value_count=len(item.category_values),
                            award_kind=item.award_kind,
                            award_value=award_value,
                            award_value_within_safe_range=award_value_safe,
                            literal_point_values=_diagnostic_point_values(
                                item.literal
                            ),
                            evidence_point_values=_diagnostic_point_values(
                                item.evidence.quote
                            ),
                            literal_matches_evidence=evidence_quote_matches_source(
                                item.literal,
                                item.evidence.quote,
                            ),
                        )
                    )
                max_points, max_points_safe = _safe_diagnostic_score(
                    candidate.max_points
                )
                shapes.append(
                    QuantitativeDiagnosticCandidateShape(
                        source_ordinal=source_ordinals[attachment_id],
                        table_ordinal=table_ordinal,
                        criterion_ordinal=criterion_ordinal,
                        document_type=extraction.document_type,
                        status=review.status,
                        issue_codes=sorted(
                            {
                                _safe_diagnostic_code(code)
                                for code in review.issue_codes
                            }
                        )[:30],
                        criterion_character_count=len(candidate.criterion_literal),
                        evidence_character_count=len(candidate.evidence.quote),
                        criterion_literal_matches_evidence=(
                            evidence_quote_matches_source(
                                candidate.criterion_literal,
                                candidate.evidence.quote,
                            )
                        ),
                        criterion_has_metric_tokens=_diagnostic_has_metric_tokens(
                            candidate.metric,
                            candidate.criterion_literal,
                        ),
                        evidence_has_metric_tokens=_diagnostic_has_metric_tokens(
                            candidate.metric,
                            candidate.evidence.quote,
                        ),
                        criterion_point_values=_diagnostic_point_values(
                            candidate.criterion_literal
                        ),
                        evidence_point_values=_diagnostic_point_values(
                            candidate.evidence.quote
                        ),
                        max_points=max_points,
                        max_points_within_safe_range=max_points_safe,
                        scoring_method=candidate.scoring_method,
                        metric=candidate.metric,
                        unit_present=candidate.unit is not None,
                        bracket_count=len(candidate.brackets),
                        threshold_present=candidate.threshold is not None,
                        formula_present=bool(candidate.formula_literal),
                        recognition_condition_count=len(
                            candidate.recognition_conditions
                        ),
                        case_count=len(candidate.cases),
                        cases=cases,
                    )
                )
                if len(shapes) >= _MAX_DIAGNOSTIC_REVIEW_CANDIDATES:
                    return shapes
    return shapes


def _quantitative_diagnostics(
    notice: Notice,
) -> ManualQuantitativeDiagnosticsResponse:
    # Local import avoids the manual-analysis -> analysis-api -> quantitative
    # module cycle during application startup. Both helpers are pure reads over
    # the already-loaded current notice versions.
    from .quantitative_scoring import (
        _current_dynamic_quantitative_profile,
        _profile_activation_reasons,
    )

    profile = _current_dynamic_quantitative_profile(notice)
    if profile is None:
        return ManualQuantitativeDiagnosticsResponse(
            notice_key=notice.notice_key,
            profile_status="MISSING",
            expected_attachment_count=0,
            processed_attachment_count=0,
            document_binding_count=0,
            table_status_counts={},
            available_candidate_count=0,
            review_candidate_count=0,
            issues=[],
            review_candidate_issues=[],
            activation_reasons=[],
            review_candidate_shapes=[],
        )

    issue_counts = Counter(
        (_safe_diagnostic_code(item.code), item.disposition)
        for item in profile.issues
    )
    review_issue_counts = Counter(
        (_safe_diagnostic_code(code), item.status)
        for item in profile.review_candidates
        for code in item.issue_codes
    )
    table_status_counts = Counter(item.status for item in profile.tables)
    return ManualQuantitativeDiagnosticsResponse(
        notice_key=notice.notice_key,
        profile_status=profile.status,
        expected_attachment_count=len(profile.expected_attachment_ids),
        processed_attachment_count=len(profile.processed_attachment_ids),
        document_binding_count=len(profile.document_bindings),
        table_status_counts={
            key: table_status_counts[key]
            for key in sorted(table_status_counts)
        },
        available_candidate_count=len(profile.available_candidates),
        review_candidate_count=len(profile.review_candidates),
        issues=[
            QuantitativeDiagnosticIssue(
                code=code,
                disposition=disposition,
                count=count,
            )
            for (code, disposition), count in sorted(issue_counts.items())[
                :_MAX_DIAGNOSTIC_CODES
            ]
        ],
        review_candidate_issues=[
            QuantitativeDiagnosticIssue(
                code=code,
                disposition=disposition,
                count=count,
            )
            for (code, disposition), count in sorted(review_issue_counts.items())[
                :_MAX_DIAGNOSTIC_CODES
            ]
        ],
        activation_reasons=sorted(
            {
                _safe_diagnostic_code(code)
                for code in _profile_activation_reasons(profile)
            }
        )[:_MAX_DIAGNOSTIC_CODES],
        review_candidate_shapes=_quantitative_review_candidate_shapes(
            notice,
            profile,
        ),
    )


@router.post(
    "/notices/{notice_key}/analysis/quantitative-diagnostics",
    response_model=ManualQuantitativeDiagnosticsResponse,
)
def get_manual_quantitative_diagnostics(
    notice_key: str,
    request: Request,
    response: Response,
) -> ManualQuantitativeDiagnosticsResponse:
    """Return PIN-only codes and bounded public-table REVIEW candidate shapes."""

    if not _manual_feature_enabled(request):
        raise HTTPException(status_code=404, detail="수동 분석 기능이 비활성화되어 있습니다.")
    if not _same_origin_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="홈페이지와 동일한 출처에서만 진단할 수 있습니다.",
        )
    _require_manual_operator(request)
    notice = _load_notice(request, notice_key)
    response.headers["Cache-Control"] = "no-store"
    return _quantitative_diagnostics(notice)


@router.post(
    "/notices/{notice_key}/analysis/request",
    response_model=ManualAnalysisResponse,
)
def request_manual_notice_analysis(
    notice_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: ManualAnalysisRequest | None = None,
) -> ManualAnalysisResponse:
    """Run one idempotent server-side analysis without exposing credentials.

    The endpoint exists only for the public Render UI when explicitly enabled.
    It accepts no caller-selected model, force flag, prompt, URL or attachment,
    and therefore cannot widen the existing stored-PPS analysis boundary.
    """

    settings = request.app.state.settings
    if not _manual_feature_enabled(request):
        raise HTTPException(status_code=404, detail="수동 분석 기능이 비활성화되어 있습니다.")
    if not _same_origin_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="홈페이지와 동일한 출처에서만 분석을 요청할 수 있습니다.",
        )
    _require_manual_operator(request)
    caller_intent = payload or ManualAnalysisRequest()
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
        if _is_authoritatively_cancelled(request, notice):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="취소된 공고는 분석할 수 없습니다.",
            )
        if notice.status != "OPEN" or _utc(notice.deadline) < now:
            raise HTTPException(status_code=409, detail="마감 또는 종료된 공고는 분석할 수 없습니다.")

        reason = _reason(notice)
        # Attachment extraction and a current evaluation are separate durable
        # stages.  Accepted attachment text without a current evaluation must
        # continue into the deterministic scoring pipeline (normally with
        # zero new model calls), while a genuinely current completed result
        # is reused idempotently.
        current_evaluation = latest_current_evaluation(notice)
        if (
            reason.state == "ANALYZED"
            and current_evaluation is not None
            and not caller_intent.recompute_current
        ):
            return _already_analysed(notice, reason)
        complete_current_audit = bool(
            reason.state == "ANALYZED"
            and _has_complete_current_attachment_audit(request, notice)
        )
        if caller_intent.recompute_current and (
            current_evaluation is None or not complete_current_audit
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "저장 근거 재판단 조건이 바뀌었습니다. 최신 공고 상태를 "
                    "새로고침한 뒤 필요한 첨부 분석부터 실행해 주세요."
                ),
            )
        evaluation_only = bool(
            caller_intent.recompute_current
            or (
                reason.state == "ANALYZED"
                and current_evaluation is None
                and complete_current_audit
            )
        )
        if not evaluation_only and not caller_intent.run_extraction:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "공고의 첨부 분석 상태가 바뀌어 Claude 호출이 필요합니다. "
                    "최신 상태와 비용 상한을 확인한 뒤 다시 요청해 주세요."
                ),
            )

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

        if reason.attempted and not evaluation_only:
            attempt_at = max(
                (_utc(version.created_at) for version in notice.versions),
                default=_utc(notice.created_at),
            )
            retry_at = attempt_at + timedelta(
                hours=settings.public_manual_analysis_cooldown_hours
            )
            if retry_at > now and not caller_intent.retry_reviewed:
                return _cooldown_response(
                    notice,
                    reason,
                    message=f"최근 분석을 재사용했습니다. {retry_at.isoformat()} 이후 재분석할 수 있습니다.",
                )

        if (
            settings.public_manual_analysis_hourly_limit > 0
            and len(recent_jobs) >= settings.public_manual_analysis_hourly_limit
        ):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="시간당 수동 분석 한도에 도달했습니다. 자동 분석 큐 또는 다음 시간대를 이용해 주세요.",
                headers={"Retry-After": "3600"},
            )

        # Idempotent reads above remain available even during an upstream
        # provider outage.  A provider credential is required only when this
        # request is about to reserve quota and start a new analysis batch.
        if not settings.extraction_configured and not evaluation_only:
            raise HTTPException(status_code=503, detail="분석 서비스 설정을 확인해 주세요.")

        request_id = _reserve_manual_job(
            request,
            notice.notice_key,
            evaluation_only=evaluation_only,
            recompute_current=caller_intent.recompute_current,
            retry_reviewed=caller_intent.retry_reviewed,
        )
        background_tasks.add_task(
            _execute_reserved_manual_job,
            request,
            request_id,
            notice.notice_key,
            evaluation_only,
        )
        return ManualAnalysisResponse(
            request_id=request_id,
            notice_key=notice.notice_key,
            outcome="QUEUED",
            analysis_state=reason.state,
            analysis_reason_code=reason.reason_code,
            analysis_reason=reason.reason,
            analysis_attempted=reason.attempted,
            message=(
                "저장된 첨부 근거로 자격·정량 판단을 시작했습니다."
                if evaluation_only
                else "모든 공개 첨부 분석을 시작했습니다. 완료 상태를 자동으로 확인합니다."
            ),
        )


def _execute_reserved_manual_job(
    request: Request,
    request_id: str,
    notice_key: str,
    evaluation_only: bool = False,
) -> None:
    with _manual_execution_slot(request, blocking=True):
        payload = AnalysisBatchRequest(
            notice_keys=[notice_key],
            dry_run=False,
            force=False,
            enrich_missing=not evaluation_only,
            max_notices=1,
            max_attachments_per_notice=MAX_ATTACHMENTS_IN_MANIFEST,
        )
        total_openai_calls = 0
        total_openai_telemetry = OpenAITelemetry()
        batch = None
        try:
            # Each HTTP-equivalent batch persists at most two new attachment
            # units. A public background request follows the same durable
            # continuation contract until the complete current manifest is
            # audited; completed units are reused with zero additional calls.
            for _round in range(MAX_ATTACHMENTS_IN_MANIFEST):
                batch = run_notice_analysis_batch(payload, request)
                total_openai_calls += batch.openai_calls
                total_openai_telemetry = merge_openai_telemetry(
                    total_openai_telemetry,
                    batch.openai_telemetry,
                )
                if "ATTACHMENT_CONTINUATION_REQUIRED" not in batch.enrichment.warnings:
                    break
            else:  # pragma: no cover - defensive manifest-bound invariant
                raise RuntimeError("manual attachment continuation limit exceeded")
        except Exception:
            total_openai_telemetry = total_openai_telemetry.model_copy(
                update={"accounting_complete": False}
            )
            _finish_manual_job(
                request,
                request_id=request_id,
                status_value="FAILED",
                openai_calls=total_openai_calls,
                openai_telemetry=total_openai_telemetry,
                failed=1,
            )
            return
        if batch is None:  # pragma: no cover - loop invariant
            return
        _finish_manual_job(
            request,
            request_id=request_id,
            status_value=batch.status,
            openai_calls=total_openai_calls,
            openai_telemetry=total_openai_telemetry,
            completed=batch.completed,
            failed=batch.failed,
            batch_job_id=batch.job_id,
        )


@router.get(
    "/notices/{notice_key}/analysis/requests/{request_id}",
    response_model=ManualAnalysisResponse,
)
def get_manual_notice_analysis_request(
    notice_key: str,
    request_id: str,
    request: Request,
) -> ManualAnalysisResponse:
    if not _manual_feature_enabled(request):
        raise HTTPException(status_code=404, detail="수동 분석 기능이 비활성화되어 있습니다.")
    if not _same_origin_request(request) and request.headers.get(
        "sec-fetch-site", ""
    ).strip().casefold() != "same-origin":
        raise HTTPException(status_code=403, detail="홈페이지와 동일한 출처에서만 조회할 수 있습니다.")
    _require_manual_operator(request)
    with request.app.state.session_factory() as session:
        job = session.get(IngestionJob, request_id)
        if (
            job is None
            or job.source != "MANUAL_ANALYSIS"
            or notice_key not in (job.notice_keys or [])
        ):
            raise HTTPException(status_code=404, detail="분석 요청을 찾을 수 없습니다.")
        job_status = job.status
        openai_calls = job.api_calls
        raw_telemetry = (job.request_json or {}).get("openai_telemetry")
        try:
            openai_telemetry = (
                OpenAITelemetry.model_validate(raw_telemetry)
                if isinstance(raw_telemetry, dict)
                else OpenAITelemetry()
            )
        except ValidationError:
            openai_telemetry = OpenAITelemetry(accounting_complete=False)
    notice = _load_notice(request, notice_key)
    reason = _reason(notice)
    current_evaluation = latest_current_evaluation(notice)
    if job_status == "RUNNING":
        outcome: Literal["QUEUED", "COMPLETED", "REVIEW"] = "QUEUED"
        message = "모든 공개 첨부를 확인하고 있습니다."
    elif job_status in {"COMPLETED", "PARTIAL"}:
        outcome = (
            "COMPLETED"
            if reason.state == "ANALYZED" and current_evaluation is not None
            else "REVIEW"
        )
        message = (
            "분석과 판정이 완료되었습니다."
            if outcome == "COMPLETED"
            else "요청은 처리됐지만 원문 또는 근거 보완이 필요합니다."
        )
    else:
        outcome = "REVIEW"
        message = "분석을 완료하지 못했습니다. 자동 백필 또는 다음 재시도를 확인해 주세요."
    return ManualAnalysisResponse(
        request_id=request_id,
        notice_key=notice_key,
        outcome=outcome,
        analysis_state=reason.state,
        analysis_reason_code=reason.reason_code,
        analysis_reason=reason.reason,
        analysis_attempted=reason.attempted,
        openai_calls=openai_calls,
        openai_telemetry=openai_telemetry,
        message=message,
    )
