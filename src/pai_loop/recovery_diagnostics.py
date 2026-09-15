"""Bounded server-only reads of current attachment and scoring-rule diagnostics."""

from __future__ import annotations

import re
import secrets
from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import load_only, raiseload
from sqlalchemy.orm.attributes import set_committed_value

from .extraction_contracts import classify_attempt_header
from .gateway_diagnostics import GatewayFailure, safe_gateway_failure
from .manual_analysis import _quantitative_diagnostics
from .models import Notice, NoticeVersion, PpsNoticeAuthority
from .pps_enrichment import (
    PPS_METADATA_KIND, PPS_METADATA_SCHEMA, _current_manifest_attempts, _digest,
    pps_attachment_audit_read_scope, pps_attachment_coverage,
    pps_recorded_attachment_attempt_count, public_analysis_reason,
    public_attachment_analysis_statuses,
)
from .recovery_diagnostic_codes import SAFE_PROCESSING_CODES, SAFE_QUANTITATIVE_CODES

PATH = "/api/v1/diagnostics/notice-recovery"
_UNKNOWN_CODE = "UNRECOGNIZED_DIAGNOSTIC_CODE"
_EXTENSIONS = frozenset({".pdf", ".hwpx", ".hwp", ".xlsx", ".xlsm", ".xls", ".docx", ".pptx", ".html", ".htm", ".zip"})
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_MODEL_HTTP_MESSAGE = re.compile(r"모델 API가 HTTP ([45][0-9]{2})를 반환했습니다\.")


class DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


NoticeKey = Annotated[str, Field(pattern=r"^PPS-[A-Za-z0-9_-]+$", max_length=160)]


class RecoveryDiagnosticRequest(DiagnosticModel):
    notice_keys: list[NoticeKey] = Field(min_length=1, max_length=25)

    @field_validator("notice_keys")
    @classmethod
    def unique_keys(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("notice_keys must be unique")
        return value


class AttachmentDiagnostic(DiagnosticModel):
    ordinal: int = Field(ge=1, le=10)
    extension: str | None
    state: Literal["ANALYZED", "REVIEW", "PENDING"]
    public_reason_code: str
    safe_error_code: str | None
    model_http_status: int | None = Field(default=None, ge=400, le=599)
    gateway_failure: GatewayFailure | None = None
    error_code_redacted: bool
    processing_warning_codes: list[str] = Field(max_length=20)
    processing_codes_redacted: bool
    manifest_bound_attempt: bool
    attempt_contract: Literal["CURRENT", "EXACT_PREVIOUS_PROCESSING", "LEGACY_CASE_V1", "LEGACY_CASE_V2", "NONE"]
    stored_document_digest_matches: bool | None
    stored_download_complete: bool | None
    stored_source_read_complete: bool | None
    stored_analysis_input_complete: bool | None
    stored_document_digest_basis: Literal["DOWNLOADED_BYTES", "FAILED_DOWNLOAD_MARKER"] | None


class IssueCount(DiagnosticModel):
    code: str
    disposition: Literal["REVIEW", "INCOMPLETE"]
    count: int = Field(ge=1)


class QuantitativeDiagnostic(DiagnosticModel):
    profile_status: Literal["MISSING", "AVAILABLE", "REVIEW", "INCOMPLETE", "NOT_APPLICABLE"]
    expected_attachment_count: int = Field(ge=0)
    processed_attachment_count: int = Field(ge=0)
    document_binding_count: int = Field(ge=0)
    table_status_counts: dict[str, int]
    available_candidate_count: int = Field(ge=0)
    review_candidate_count: int = Field(ge=0)
    issues: list[IssueCount] = Field(max_length=100)
    review_candidate_issues: list[IssueCount] = Field(max_length=100)
    activation_reasons: list[str] = Field(max_length=100)
    code_lists_may_be_truncated: bool


class NoticeDiagnostic(DiagnosticModel):
    notice_key: NoticeKey
    diagnostic_status: Literal["OK", "UNAVAILABLE"]
    notice_status: Literal["OPEN", "CLOSED", "EXPIRED", "UNKNOWN"]
    provider_disposition: Literal["VALID", "CANCELLED", "QUARANTINED"] | None
    metadata_schema_current: bool | None = None
    analysis_state: Literal["ANALYZED", "REVIEW", "PENDING"] | None = None
    analysis_reason_code: str | None = None
    attachment_count: int | None = None
    invalid_manifest_slot_count: int | None = None
    audited_attachment_count: int | None = None
    accepted_attachment_count: int | None = None
    recorded_attempt_attachment_count: int | None = None
    attachment_coverage_complete: bool | None = None
    attachments: list[AttachmentDiagnostic] = Field(default_factory=list, max_length=10)
    quantitative: QuantitativeDiagnostic | None = None


class RecoveryDiagnosticResponse(DiagnosticModel):
    diagnostic_version: Literal["notice-recovery-diagnostics-v1"] = "notice-recovery-diagnostics-v1"
    notices: list[NoticeDiagnostic] = Field(min_length=1, max_length=25)


def require_diagnostic_server(request: Request) -> None:
    # This endpoint intentionally has no development/no-key or browser fallback.
    # A leaked server key must not make a browser or department cookie a BFF.
    forbidden = {"origin", "referer", "cookie", "x-pai-manual-token", "x-csrf-token", "x-pai-private-evidence-token"}
    if any(name in forbidden or name.startswith("sec-fetch-") for name in request.headers):
        raise HTTPException(403, "서버 전용 진단입니다.")
    expected = request.app.state.settings.api_key
    supplied = request.headers.get("x-pai-loop-api-key", "")
    if not expected or not supplied or not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
        raise HTTPException(401, "서버 인증이 필요합니다.")


router = APIRouter(tags=["server recovery diagnostics"], dependencies=[Depends(require_diagnostic_server)])


def _safe_quant_code(value: str) -> str:
    return value if value in SAFE_QUANTITATIVE_CODES else _UNKNOWN_CODE


def _issue_counts(items) -> list[IssueCount]:
    counts: Counter = Counter()
    for item in items:
        counts[(_safe_quant_code(item.code), item.disposition)] += item.count
    return [IssueCount(code=code, disposition=disposition, count=count)
            for (code, disposition), count in sorted(counts.items())]


def _quantitative_projection(notice: Notice) -> QuantitativeDiagnostic:
    diagnostic = _quantitative_diagnostics(notice, include_candidate_shapes=False)
    table_counts: Counter = Counter()
    for key, value in diagnostic.table_status_counts.items():
        table_counts[key if key in {"AVAILABLE", "REVIEW", "INCOMPLETE", "NOT_APPLICABLE"} else "UNKNOWN"] += value
    return QuantitativeDiagnostic(
        profile_status=diagnostic.profile_status,
        expected_attachment_count=diagnostic.expected_attachment_count,
        processed_attachment_count=diagnostic.processed_attachment_count,
        document_binding_count=diagnostic.document_binding_count,
        table_status_counts=dict(table_counts),
        available_candidate_count=diagnostic.available_candidate_count,
        review_candidate_count=diagnostic.review_candidate_count,
        issues=_issue_counts(diagnostic.issues),
        review_candidate_issues=_issue_counts(diagnostic.review_candidate_issues),
        activation_reasons=sorted({_safe_quant_code(code) for code in diagnostic.activation_reasons}),
        code_lists_may_be_truncated=any(len(values) >= 100 for values in (
            diagnostic.issues, diagnostic.review_candidate_issues, diagnostic.activation_reasons)),
    )


def _attachment_projection(index: int, attachment: dict, attempt: NoticeVersion | None, public: dict) -> AttachmentDiagnostic:
    payload = attempt.source_payload if attempt is not None else {}
    error = payload.get("error_code")
    processing = payload.get("document_processing")
    processing = processing if isinstance(processing, dict) else {}
    warnings = processing.get("warnings")
    warnings = warnings if isinstance(warnings, list) else []
    # Only fixed codes are inspected. Never return member paths, names or hashes.
    members = processing.get("member_issues")
    members = members if isinstance(members, list) else []
    codes = [*warnings, *(item.get("reason") for item in members if isinstance(item, dict))]
    safe_codes = sorted({code for code in codes if isinstance(code, str) and code in SAFE_PROCESSING_CODES})
    digest = payload.get("document_sha256")
    file_digest = attempt.file_sha256 if attempt is not None else None
    extension = PurePath(attachment["file_name"]).suffix.casefold()
    # Match only the exact local HTTP_ERROR template, never provider prose.
    message = payload.get("message")
    http_match = (_MODEL_HTTP_MESSAGE.fullmatch(message)
                  if error == "HTTP_ERROR" and isinstance(message, str) else None)
    return AttachmentDiagnostic(
        ordinal=index, extension=extension if extension in _EXTENSIONS else None,
        state=public["state"], public_reason_code=public["reason_code"],
        safe_error_code=error if isinstance(error, str) and error in SAFE_PROCESSING_CODES else None,
        model_http_status=int(http_match.group(1)) if http_match else None,
        gateway_failure=safe_gateway_failure(payload.get("gateway_failure")) if error == "HTTP_ERROR" and http_match and http_match.group(1) == "500" else None,
        error_code_redacted=bool(error) and error not in SAFE_PROCESSING_CODES if isinstance(error, str) else error is not None,
        processing_warning_codes=safe_codes[:20],
        processing_codes_redacted=len(safe_codes) > 20 or any(not isinstance(code, str) or code not in SAFE_PROCESSING_CODES for code in codes),
        manifest_bound_attempt=attempt is not None,
        attempt_contract=classify_attempt_header(payload) if attempt is not None else "NONE",
        stored_document_digest_matches=(
            digest.casefold() == file_digest.casefold()
            if isinstance(digest, str) and _SHA256.fullmatch(digest)
            and isinstance(file_digest, str) and _SHA256.fullmatch(file_digest) else None
        ),
        stored_download_complete=processing.get("download_complete") if type(processing.get("download_complete")) is bool else None,
        stored_source_read_complete=processing.get("source_read_complete") if type(processing.get("source_read_complete")) is bool else None,
        stored_analysis_input_complete=processing.get("analysis_input_complete") if type(processing.get("analysis_input_complete")) is bool else None,
        stored_document_digest_basis=(processing.get("document_digest_basis")
            if processing.get("document_digest_basis") in ("DOWNLOADED_BYTES", "FAILED_DOWNLOAD_MARKER") else None),
    )


def _notice_projection(notice: Notice, disposition: str | None) -> NoticeDiagnostic:
    status = notice.status.upper()
    deadline = notice.deadline.replace(tzinfo=timezone.utc) if notice.deadline.tzinfo is None else notice.deadline
    if status == "OPEN" and deadline < datetime.now(timezone.utc):
        status = "EXPIRED"
    base = dict(notice_key=notice.notice_key,
                notice_status=status if status in {"OPEN", "CLOSED", "EXPIRED"} else "UNKNOWN",
                provider_disposition=disposition if disposition in {"VALID", "CANCELLED", "QUARANTINED"} else None)
    try:
        versions = notice.versions
        metadata = next((version.source_payload for version in reversed(versions)
                         if isinstance(version.source_payload, dict)
                         and version.source_payload.get("kind") == PPS_METADATA_KIND), None)
        with pps_attachment_audit_read_scope():
            attachments, invalid_count, attempts = _current_manifest_attempts(versions)
            public = public_attachment_analysis_statuses(versions)
            reason = public_analysis_reason(versions, evaluated=False, source_kind="PPS")
            coverage = pps_attachment_coverage(versions)
            rows = [_attachment_projection(index, attachment, attempts.get(attachment["attachment_id"]), public[index - 1])
                    for index, attachment in enumerate(attachments, start=1)]
            return NoticeDiagnostic(
                **base, diagnostic_status="OK",
                metadata_schema_current=metadata.get("schema_version") == PPS_METADATA_SCHEMA if metadata is not None else None,
                analysis_state=reason.state, analysis_reason_code=reason.reason_code,
                attachment_count=coverage.discovered, invalid_manifest_slot_count=invalid_count,
                audited_attachment_count=coverage.audited, accepted_attachment_count=coverage.accepted,
                recorded_attempt_attachment_count=pps_recorded_attachment_attempt_count(versions),
                attachment_coverage_complete=coverage.complete,
                attachments=rows, quantitative=_quantitative_projection(notice),
            )
    except Exception:
        # Malformed stored provider data may contain private prose in an exception.
        # Keep that notice explicit and unavailable; never log/return the exception.
        return NoticeDiagnostic(**base, diagnostic_status="UNAVAILABLE")


def _load_current_manifest_versions(session, notice: Notice) -> None:
    columns = load_only(
        NoticeVersion.id, NoticeVersion.notice_id, NoticeVersion.version_no,
        NoticeVersion.file_sha256, NoticeVersion.document_complete,
        NoticeVersion.extraction_status, NoticeVersion.source_payload,
        NoticeVersion.created_at, raiseload=True,
    )
    metadata = session.scalar(select(NoticeVersion).where(
        NoticeVersion.notice_id == notice.id,
        NoticeVersion.source_payload["kind"].as_string() == PPS_METADATA_KIND,
    ).options(columns, raiseload("*")).order_by(NoticeVersion.version_no.desc()).limit(1))
    versions = [metadata] if metadata is not None else []
    manifest = metadata.source_payload.get("attachment_manifest") if metadata is not None else None
    if isinstance(manifest, list) and metadata.source_payload.get("schema_version") == PPS_METADATA_SCHEMA:
        # Exact same whole-manifest boundary as the shared reader. Retain all
        # generations within it: dropping a rejected newer attempt could revive
        # an older legacy result. Never load superseded manifests or materialized
        # company/evaluation source payloads just to classify current attachments.
        versions.extend(session.scalars(select(NoticeVersion).where(
            NoticeVersion.notice_id == notice.id,
            NoticeVersion.source_payload["kind"].as_string() == "OPENAI_REQUIREMENT_EXTRACTION",
            NoticeVersion.source_payload["current_manifest_sha256"].as_string() == _digest(manifest),
        ).options(columns, raiseload("*"))))
    set_committed_value(notice, "versions", sorted(versions, key=lambda version: version.version_no))


@router.post(PATH, response_model=RecoveryDiagnosticResponse)
def read_recovery_diagnostics(payload: RecoveryDiagnosticRequest, request: Request, response: Response) -> RecoveryDiagnosticResponse:
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.session_factory() as session:
        present = set(session.scalars(select(Notice.notice_key).where(Notice.notice_key.in_(payload.notice_keys))))
        if present != set(payload.notice_keys):
            raise HTTPException(404, "요청한 공고를 모두 찾을 수 없습니다.")
        results = []
        # Load one notice's version payloads at a time. Never load private facts,
        # evaluations, score bases, jobs, documents or other notices' histories.
        for key in payload.notice_keys:
            notice = session.scalar(select(Notice).where(Notice.notice_key == key).options(
                load_only(Notice.id, Notice.notice_key, Notice.bid_notice_no, Notice.status, Notice.deadline, raiseload=True),
                raiseload("*"),
            ))
            if notice is None:
                raise HTTPException(404, "요청한 공고를 모두 찾을 수 없습니다.")
            disposition = session.scalar(select(PpsNoticeAuthority.disposition).where(PpsNoticeAuthority.bid_notice_no == notice.bid_notice_no))
            _load_current_manifest_versions(session, notice)
            results.append(_notice_projection(notice, disposition))
            session.expunge_all()
        return RecoveryDiagnosticResponse(notices=results)
