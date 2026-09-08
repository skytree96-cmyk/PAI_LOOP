"""One durable, fail-closed larger-output allowance per frozen source/input.

This is an execution reservation, not an analysis result or usage counter. Its
dedicated operational source must survive retention. No provider prose is stored.
"""
from datetime import datetime, timezone
from collections.abc import Callable
import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .extraction_contracts import classify_attempt_header
from .gateway_diagnostics import safe_gateway_failure
from .models import IngestionJob, NoticeVersion

LONG_OUTPUT_ONCE = "LONG_OUTPUT_ONCE"
LONG_OUTPUT_TOKENS = 32_000
LONG_OUTPUT_TIMEOUT_SECONDS = 300
LONG_OUTPUT_MAX_CALLS = 1


def long_output_source_boundary(notice, metadata) -> str:
    deadline = notice.deadline
    deadline = deadline.replace(tzinfo=timezone.utc) if deadline.tzinfo is None else deadline.astimezone(timezone.utc)
    source = {"notice_key": notice.notice_key, "status": notice.status, "deadline": deadline.isoformat(),
              "bid_notice_no": notice.bid_notice_no, "metadata": None if metadata is None else {
                  "id": metadata.id, "file_sha256": metadata.file_sha256, "payload": metadata.source_payload}}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def eligible_long_output_failure(version: NoticeVersion) -> bool:
    payload = version.source_payload if isinstance(version.source_payload, dict) else {}
    processing = payload.get("document_processing")
    failure = safe_gateway_failure(payload.get("gateway_failure"))
    return bool(
        payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
        and payload.get("source_kind") == "PPS_PUBLIC_ATTACHMENT"
        and payload.get("status") == "REVIEW" and payload.get("error_code") == "HTTP_ERROR"
        and payload.get("message") == "모델 API가 HTTP 500를 반환했습니다."
        and classify_attempt_header(payload) == "CURRENT"
        and isinstance(processing, dict)
        and processing.get("retry_budget_policy") is None
        and processing.get("source_read_complete") is True
        and processing.get("analysis_input_complete") is True
        and version.file_sha256 == payload.get("document_sha256")
        and failure is not None and failure.detail_code == "NATIVE_STOP_MAX_TOKENS"
        and failure.stop_reason == "max_tokens" and failure.usage is not None
        and failure.usage.output_tokens == 20_000
        and _source_identity(version) is not None
    )


def _source_identity(version: NoticeVersion) -> str | None:
    payload = version.source_payload if isinstance(version.source_payload, dict) else {}
    processing = payload.get("document_processing")
    if not isinstance(processing, dict) or not isinstance(payload.get("attachment_id"), str) or not payload["attachment_id"]:
        return None
    digests = [payload.get("current_manifest_sha256"), payload.get("manifest_sha256"),
               processing.get("source_text_sha256"), processing.get("analysis_input_sha256")]
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in digests):
        return None
    # Attempt/version IDs and container timestamps must not mint a new allowance
    # for unchanged manifest, source text and complete model input.
    return json.dumps([version.notice_id, payload["attachment_id"], *digests], separators=(",", ":"))


def long_output_claim_id(version: NoticeVersion) -> str:
    identity = _source_identity(version)
    if identity is None:
        raise ValueError("LONG_OUTPUT_SOURCE_IDENTITY_INVALID")
    return str(uuid5(NAMESPACE_URL, f"pai-loop:{LONG_OUTPUT_ONCE}:{identity}"))


def long_output_consumed(session: Session, version_id: str) -> bool:
    version = session.get(NoticeVersion, version_id)
    return version is None or session.get(IngestionJob, long_output_claim_id(version)) is not None


def consume_long_output(session: Session, *, version_id: str,
                        validate_current: Callable[[NoticeVersion], bool]) -> str:
    """Commit before client construction. Any conflict/uncertain commit blocks I/O."""
    if session.in_transaction():
        raise RuntimeError("LONG_OUTPUT_TRANSACTION_NOT_CLEAN")
    try:
        with session.begin():
            version = session.get(NoticeVersion, version_id, populate_existing=True)
            if version is None or not eligible_long_output_failure(version) or not validate_current(version):
                raise ValueError("LONG_OUTPUT_FAILURE_NOT_ELIGIBLE")
            claim_id = long_output_claim_id(version)
            session.add(IngestionJob(
                id=claim_id, source=LONG_OUTPUT_ONCE, mode="LIVE", status="RESERVED",
                window_json={}, notice_keys=[],
                request_json={"policy": LONG_OUTPUT_ONCE, "original_failure_id": version_id,
                              "state": "CONSUMED_NOT_DISPATCHED"},
                warnings=["LONG_OUTPUT_RESERVED_NOT_DISPATCHED"], completed_at=datetime.now(timezone.utc),
            ))
    except IntegrityError:
        raise ValueError("LONG_OUTPUT_ALREADY_CONSUMED") from None
    return claim_id


def mark_long_output_dispatch(session: Session, claim_id: str, version_id: str) -> None:
    """A committed dispatch intent precedes I/O; an interrupted intent is unknown."""
    if session.in_transaction():
        raise RuntimeError("LONG_OUTPUT_TRANSACTION_NOT_CLEAN")
    with session.begin():
        changed = session.execute(update(IngestionJob).where(
            IngestionJob.id == claim_id, IngestionJob.source == LONG_OUTPUT_ONCE,
            IngestionJob.request_json["original_failure_id"].as_string() == version_id,
            IngestionJob.request_json["policy"].as_string() == LONG_OUTPUT_ONCE,
            IngestionJob.request_json["state"].as_string() == "CONSUMED_NOT_DISPATCHED",
        ).values(status="DISPATCH_STARTED", request_json={
            "policy": LONG_OUTPUT_ONCE, "original_failure_id": version_id, "state": "DISPATCH_STARTED",
        }, warnings=["LONG_OUTPUT_DISPATCH_OUTCOME_UNCONFIRMED"]))
        if changed.rowcount != 1:
            raise ValueError("LONG_OUTPUT_ALREADY_DISPATCHED")
