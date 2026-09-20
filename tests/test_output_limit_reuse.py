"""Same-input output-limit recovery uses no new paid calls (synthetic I/O only)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import zipfile

import httpx
import pytest
from sqlalchemy import select

import pai_loop.pps_enrichment as enrichment
from pai_loop.gateway_diagnostics import safe_gateway_failure
from pai_loop.integrations.openai_extraction import OpenAIExtractionClient
from pai_loop.models import IngestionJob, Notice, NoticeVersion
from test_failed_attachment_retry_scope import failed_case
from test_long_output_once import FAILURE, long_case
from test_pps_enrichment import _QUANTITATIVE_RETRY_SOURCE


def source_bytes(suffix="SYN 4"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", "<s>" + "".join(
            f"<p>{line}</p>" for line in _QUANTITATIVE_RETRY_SOURCE.splitlines()
        ) + f"<p>{suffix}</p></s>")
    return buffer.getvalue()


@pytest.fixture
def cooled_output_limit(long_case):
    client, notice_id, row_id, _ = long_case
    content = source_bytes()
    extraction = enrichment.extract_pps_document_content("SYN-4.hwpx", content)
    processing = enrichment._document_processing_audit(
        extraction, enrichment.select_document_analysis_input(extraction.text.strip()),
    )
    with client.app.state.session_factory() as session:
        row = session.get(NoticeVersion, row_id)
        payload = deepcopy(row.source_payload)
        payload.update(model="claude-sonnet-5", document_sha256=hashlib.sha256(content).hexdigest(),
                       document_processing=processing)
        row.file_sha256 = payload["document_sha256"]
        row.source_payload = payload
        row.created_at = datetime.now(timezone.utc) - timedelta(days=3)
        session.commit()
    return client, notice_id, row_id, content


def run_ordinary(case, *, mode="scheduled", content=None):
    client, notice_id, row_id, original_content = case
    downloads, calls = [], []

    def download(request):
        downloads.append(request.url.params["fileSeq"])
        return httpx.Response(200, content=original_content if content is None else content)

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(500, json={"gateway_error": FAILURE})

    def factory(**kwargs):
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(provider))

    scope = None
    if mode == "failed_scope":
        with client.app.state.session_factory() as session:
            notice = session.get(Notice, notice_id)
            scope = enrichment.failed_attachment_retry_snapshot(
                list(notice.versions), error_codes=["HTTP_ERROR"], max_attachments=1,
                notice_key=notice.notice_key, revision_no=notice.revision_no,
            )
    with client.app.state.session_factory() as session:
        result = enrichment.enrich_notice_from_pps(
            session, notice_id=notice_id, openai_api_key="SYN-key", openai_model="claude-sonnet-5",
            llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.invalid/",
            transport=httpx.MockTransport(download), openai_client_factory=factory,
            retry_reviewed_version_ids=frozenset({row_id}) if mode != "scheduled" else frozenset(),
            failed_attachment_retry=scope,
        )
    return result, downloads, calls


@pytest.mark.parametrize("mode", ["scheduled", "manual", "failed_scope"])
def test_rechecked_identical_20k_stop_keeps_original_failure_without_model_call(cooled_output_limit, mode):
    client, notice_id, row_id, _ = cooled_output_limit
    with client.app.state.session_factory() as session:
        original = {row.id: deepcopy(row.source_payload) for row in session.get(Notice, notice_id).versions}
        # Keep the source recheck reachable even after the cooldown/manual retry.
        assert row_id in enrichment.current_retryable_review_version_ids(list(session.get(Notice, notice_id).versions))
    result, downloads, calls = run_ordinary(cooled_output_limit, mode=mode)
    assert downloads == ["4"] and calls == [] and result.openai_calls == 0
    failure = next(item for item in result.attachment_results if item.version_id == row_id)
    assert failure.status == "REVIEW" and failure.openai_calls == 0
    with client.app.state.session_factory() as session:
        assert {row.id: row.source_payload for row in session.get(Notice, notice_id).versions} == original
        assert not session.scalars(select(IngestionJob).where(IngestionJob.source == "LONG_OUTPUT_ONCE")).all()


def test_same_url_changed_document_reenters_normal_20k_processing(cooled_output_limit):
    result, downloads, calls = run_ordinary(cooled_output_limit, content=source_bytes("SYN replacement source"))
    assert downloads == ["4"] and len(calls) == result.openai_calls == 1
    assert calls[0]["max_output_tokens"] == 20000 and "budget_policy" not in calls[0]


@pytest.mark.parametrize("mutation", [
    "source_hash", "input_hash", "manifest", "full_manifest", "prompt", "schema", "processing", "model",
    "unknown", "malformed", "model_execution", "request_rejected", "transient", "usage",
    "source_incomplete", "input_incomplete", "latest_transient",
])
def test_changed_identity_or_unproven_failure_remains_retryable(cooled_output_limit, mutation):
    client, notice_id, row_id, _ = cooled_output_limit
    with client.app.state.session_factory() as session:
        row = session.get(NoticeVersion, row_id)
        payload = deepcopy(row.source_payload)
        if mutation == "source_hash": payload["document_processing"]["source_text_sha256"] = "a" * 64
        elif mutation == "input_hash": payload["document_processing"]["analysis_input_sha256"] = "b" * 64
        elif mutation == "manifest": payload["manifest_sha256"] = "c" * 64
        elif mutation == "full_manifest": payload["current_manifest_sha256"] = "d" * 64
        elif mutation == "prompt": payload["prompt_version"] = "SYN-old"
        elif mutation == "schema": payload["schema_version"] = "SYN-old"
        elif mutation == "processing": payload["processing_version"] = "SYN-old"
        elif mutation == "model": payload["model"] = "SYN-old-model"
        elif mutation == "unknown": payload.pop("gateway_failure")
        elif mutation == "malformed": payload["gateway_failure"]["raw"] = "SYN-rejected-extra-field"
        elif mutation in {"model_execution", "request_rejected"}:
            payload["gateway_failure"] = dict(version="gateway-failure-v1",
                stage="MODEL_EXECUTION" if mutation == "model_execution" else "INPUT_VALIDATION",
                code="MODEL_EXECUTION_FAILED" if mutation == "model_execution" else "REQUEST_REJECTED",
                upstream_http_status=None)
            assert safe_gateway_failure(payload["gateway_failure"]) is not None
        elif mutation in {"transient", "latest_transient"}: payload["error_code"] = "NETWORK_ERROR"
        elif mutation == "usage": payload["gateway_failure"]["usage"].update(output_tokens=19000, total_tokens=19100)
        elif mutation == "source_incomplete": payload["document_processing"]["source_read_complete"] = False
        elif mutation == "input_incomplete": payload["document_processing"]["analysis_input_complete"] = False
        if mutation == "latest_transient":
            session.add(NoticeVersion(notice_id=notice_id, version_no=row.version_no + 1,
                file_sha256=row.file_sha256, source_payload=payload, created_at=row.created_at))
        else:
            row.source_payload = payload
        session.commit()
    result, downloads, calls = run_ordinary(cooled_output_limit)
    assert downloads == ["4"] and len(calls) == result.openai_calls == 1
