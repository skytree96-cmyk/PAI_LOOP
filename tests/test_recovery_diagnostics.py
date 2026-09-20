from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from pai_loop import manual_analysis, recovery_diagnostics as diagnostics
from pai_loop.models import Notice, NoticeVersion, PpsNoticeAuthority
from pai_loop.extraction_contracts import LEGACY_CASE_CONTRACT
from pai_loop.manifest_bounds import MAX_MANIFEST_ATTACHMENTS
from pai_loop.pps_enrichment import _digest
from test_pps_enrichment import _analysis_versions

KEY = "PPS-SYN-DIAGNOSTIC-001"
SERVER = {"X-PAI-LOOP-API-KEY": "SYN-diagnostic-server-only"}
CANARY = "SYN_PRIVATE_DIAGNOSTIC_CANARY"


@pytest.fixture
def diagnostic_client(client):
    client.headers.pop("X-PAI-LOOP-API-KEY", None)
    client.app.state.settings = replace(client.app.state.settings,
        api_key=SERVER["X-PAI-LOOP-API-KEY"], public_read_only=True,
        department_accounts_enabled=False, public_manual_analysis_enabled=False)
    return client


def seed(client, *, key=KEY, extension=".xls", error="XLS_CODEPAGE_UNVERIFIED", status="REVIEW", mutate=None):
    versions = _analysis_versions(extension, error_code=error, status=status)
    for version in versions[1:]:
        version.source_payload.update({"document_sha256": version.file_sha256,
            "source_label": CANARY + extension, "message": CANARY,
            "result_private": {"quote": CANARY}, "provider_response_id": CANARY,
            "source_text": CANARY, "model": CANARY})
    if mutate:
        mutate(versions)
    with client.app.state.session_factory() as session:
        notice = Notice(notice_key=key, bid_notice_no=key, title=CANARY,
            agency=CANARY, status="OPEN", deadline=datetime(2027, 1, 1, tzinfo=timezone.utc))
        session.add(notice)
        session.flush()
        for version in versions:
            version.notice_id = notice.id
            session.add(version)
        session.commit()
        return [version.id for version in versions]


def read(client, keys=None, headers=None):
    return client.post(diagnostics.PATH, json={"notice_keys": keys or [KEY]}, headers=SERVER if headers is None else headers)


@pytest.mark.parametrize("headers", [
    {}, {"X-PAI-LOOP-API-KEY": "SYN-wrong"}, {"X-PAI-Manual-Token": "SYN-pin"},
    {"X-PAI-Private-Evidence-Token": "SYN-private"}, {"Cookie": "pai_department_session=SYN-session"},
])
def test_credentials_cannot_fall_back_to_public_pin_or_account(diagnostic_client, headers):
    response = read(diagnostic_client, headers=headers)
    assert response.status_code in {401, 403}
    assert response.headers["cache-control"] == "no-store"
    assert CANARY not in response.text


@pytest.mark.parametrize("accounts_enabled", [False, True])
@pytest.mark.parametrize("browser_header", [
    {"Origin": "http://testserver"}, {"Referer": "http://testserver/notices"},
    {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Mode": "cors"},
    {"Cookie": "pai_department_session=SYN-session"}, {"X-PAI-Manual-Token": "SYN-pin"},
    {"X-CSRF-Token": "SYN-csrf"},
])
def test_server_key_does_not_authorize_browser_or_account_context(diagnostic_client, accounts_enabled, browser_header):
    diagnostic_client.app.state.settings = replace(diagnostic_client.app.state.settings, department_accounts_enabled=accounts_enabled)
    # The common entry boundary rejects the retired PIN before route dispatch.
    expected = 401 if "X-PAI-Manual-Token" in browser_header else 403
    assert read(diagnostic_client, headers={**SERVER, **browser_header}).status_code == expected


def test_key_is_required_even_in_development_without_configured_key(diagnostic_client):
    diagnostic_client.app.state.settings = replace(diagnostic_client.app.state.settings, api_key=None)
    assert read(diagnostic_client).status_code == 401


@pytest.mark.parametrize("body", [
    {"notice_keys": []}, {"notice_keys": [KEY] * 26}, {"notice_keys": [KEY, KEY]},
    {"notice_keys": ["MANUAL-SYN"]}, {"notice_keys": [123]},
    {"notice_keys": "PPS-SYN"}, {"notice_keys": ["PPS-" + "x" * 160]},
    {"notice_keys": [KEY], "raw_secret": CANARY}, {"notice_keys": [CANARY]},
])
def test_validation_is_bounded_unique_strict_and_never_echoes_input(diagnostic_client, body):
    response = diagnostic_client.post(diagnostics.PATH, headers=SERVER, json=body)
    assert response.status_code == 422
    assert response.json()["code"] == "DIAGNOSTIC_VALIDATION_ERROR"
    assert CANARY not in response.text and "input" not in response.text


def test_unknown_notices_fail_explicitly_before_partial_projection(diagnostic_client, monkeypatch):
    seed(diagnostic_client)
    monkeypatch.setattr(diagnostics, "_notice_projection", lambda *_: pytest.fail("must preflight all explicit keys"))
    response = read(diagnostic_client, [KEY, "PPS-SYN-MISSING"])
    assert response.status_code == 404
    assert "notices" not in response.json()


def test_twenty_five_unique_keys_preserve_request_order_without_sources(diagnostic_client):
    keys = [f"PPS-SYN-BOUND-{i:02d}" for i in range(25)]
    for key in keys:
        seed(diagnostic_client, key=key, error=None)
    response = read(diagnostic_client, list(reversed(keys)))
    assert response.status_code == 200
    assert [row["notice_key"] for row in response.json()["notices"]] == list(reversed(keys))
    assert len(response.content) < 60_000


@pytest.mark.parametrize("count", [11, MAX_MANIFEST_ATTACHMENTS, MAX_MANIFEST_ATTACHMENTS + 1])
def test_full_manifest_diagnostic_keeps_all_supported_slots_and_reports_overflow(diagnostic_client, count):
    def expand_manifest(versions):
        metadata, attempt = versions
        template = metadata.source_payload["attachment_manifest"][0]
        manifest = [dict(template, attachment_id=f"PPS-ATT-{slot:024x}",
                         file_name=f"SYN-attachment-{slot}.pdf", slot=slot)
                    for slot in range(1, count + 1)]
        metadata.source_payload["attachment_manifest"] = manifest
        last_supported = manifest[min(count, MAX_MANIFEST_ATTACHMENTS) - 1]
        attempt.source_payload.update(attachment_id=last_supported["attachment_id"],
            manifest_sha256=_digest(last_supported), current_manifest_sha256=_digest(manifest))

    seed(diagnostic_client, extension=".pdf", error="PDF_TEXT_EXTRACTION_FAILED", mutate=expand_manifest)
    response = read(diagnostic_client)
    assert response.status_code == 200
    row, = response.json()["notices"]
    assert row["diagnostic_status"] == "OK"
    supported = min(count, MAX_MANIFEST_ATTACHMENTS)
    assert row["attachment_count"] == count
    assert row["invalid_manifest_slot_count"] == max(0, count - MAX_MANIFEST_ATTACHMENTS)
    assert [item["ordinal"] for item in row["attachments"]] == list(range(1, supported + 1))
    assert all(item["state"] == "PENDING" for item in row["attachments"][:-1])
    assert row["attachments"][-1]["safe_error_code"] == "PDF_TEXT_EXTRACTION_FAILED"
    assert row["attachments"][-1]["manifest_bound_attempt"] is True
    assert row["audited_attachment_count"] == 1
    assert row["accepted_attachment_count"] == 0
    assert row["attachment_coverage_complete"] is False
    assert CANARY not in response.text


@pytest.mark.parametrize("extension,error,public", [
    (".xls", "XLS_CODEPAGE_UNVERIFIED", "DOCUMENT_EXTRACT_FAILED"),
    (".xls", "XLS_PARSE_FAILED", "DOCUMENT_EXTRACT_FAILED"),
    (".xls", "XLS_FORMULA_EXPRESSIONS_UNAVAILABLE", "DOCUMENT_EXTRACT_FAILED"),
    (".pdf", "ATTACHMENT_NETWORK_ERROR", "PDF_EXTRACT_FAILED"),
    (".pdf", "ATTACHMENT_HTTP_403", "PDF_EXTRACT_FAILED"),
    (".pdf", "PDF_TEXT_EXTRACTION_FAILED", "PDF_EXTRACT_FAILED"),
    (".hwpx", "HWPX_XML_INVALID", "HWPX_EXTRACT_FAILED"),
    (".xls", "ARCHIVE_COMPRESSION_RATIO_LIMIT", "DOCUMENT_EXTRACT_FAILED"),
])
def test_exact_allowlisted_processing_codes_do_not_collapse_to_public_labels(diagnostic_client, extension, error, public):
    seed(diagnostic_client, extension=extension, error=error)
    response = read(diagnostic_client)
    assert response.status_code == 200, response.text
    notice, = response.json()["notices"]
    attachment, = notice["attachments"]
    assert notice["diagnostic_status"] == "OK"
    assert (notice["attachment_count"], notice["audited_attachment_count"], notice["accepted_attachment_count"]) == (1, 1, 0)
    assert attachment["safe_error_code"] == error
    assert attachment["public_reason_code"] == public
    assert attachment["extension"] == extension
    assert attachment["ordinal"] == 1 and attachment["manifest_bound_attempt"] is True
    assert attachment["attempt_contract"] == "CURRENT"
    assert attachment["stored_document_digest_matches"] is True
    for forbidden in (CANARY, "source_payload", "document_name", "file_sha256", "document_sha256", "source_anchor", "provider_response_id", "2" * 64):
        assert forbidden not in response.text


@pytest.mark.parametrize("error,public", [("HTTP_ERROR", "MODEL_HTTP_FAILED"), (CANARY, "OPENAI_REVIEW"), ("XLS_" + CANARY, "DOCUMENT_EXTRACT_FAILED")])
def test_provider_and_unknown_codes_expose_only_public_category(diagnostic_client, error, public):
    seed(diagnostic_client, error=error)
    response = read(diagnostic_client)
    attachment, = response.json()["notices"][0]["attachments"]
    assert attachment["safe_error_code"] is None and attachment["error_code_redacted"] is True
    assert attachment["public_reason_code"] == public
    assert CANARY not in response.text


@pytest.mark.parametrize("status", [403, 429, 502])
def test_model_http_status_uses_only_exact_local_template(diagnostic_client, status):
    def mutate(versions):
        versions[-1].source_payload["message"] = f"모델 API가 HTTP {status}를 반환했습니다."
    seed(diagnostic_client, extension=".hwpx", error="HTTP_ERROR", mutate=mutate)
    response = read(diagnostic_client)
    attachment, = response.json()["notices"][0]["attachments"]
    assert attachment["model_http_status"] == status
    assert attachment["public_reason_code"] == "MODEL_HTTP_FAILED"
    assert "message" not in response.text and CANARY not in response.text


@pytest.mark.parametrize("error,message", [
    ("HTTP_ERROR", CANARY + "모델 API가 HTTP 403를 반환했습니다."),
    ("HTTP_ERROR", "모델 API가 HTTP 429를 반환했습니다." + CANARY),
    ("HTTP_ERROR", "모델 API가 HTTP 502를 반환했습니다.\n"),
    ("HTTP_ERROR", "HTTP 403 " + CANARY),
    ("HTTP_ERROR", "모델 API가 HTTP 200를 반환했습니다."),
    ("HTTP_ERROR", "모델 API가 HTTP 600를 반환했습니다."),
    ("HTTP_ERROR", "모델 API가 HTTP ４０３를 반환했습니다."),
    ("HTTP_ERROR", {"status": 403, "body": CANARY}),
    ("HTTP_ERROR", None),
    ("MODEL_HTTP_FAILED", "모델 API가 HTTP 403를 반환했습니다."),
    ("HTTP_ERROR " + CANARY, "모델 API가 HTTP 403를 반환했습니다."),
    (None, "모델 API가 HTTP 403를 반환했습니다."),
])
def test_model_http_status_rejects_freeform_or_non_http_error(diagnostic_client, error, message):
    def mutate(versions):
        versions[-1].source_payload["message"] = message
    seed(diagnostic_client, error=error, mutate=mutate)
    response = read(diagnostic_client)
    attachment, = response.json()["notices"][0]["attachments"]
    assert attachment["model_http_status"] is None
    assert "message" not in response.text and CANARY not in response.text


@pytest.mark.parametrize("stale", ["current_manifest_sha256", "processing_version"])
def test_model_http_status_never_comes_from_an_unselected_attempt(diagnostic_client, stale):
    def mutate(versions):
        versions[-1].source_payload.update({stale: CANARY,
            "message": "모델 API가 HTTP 403를 반환했습니다."})
    seed(diagnostic_client, error="HTTP_ERROR", mutate=mutate)
    attachment, = read(diagnostic_client).json()["notices"][0]["attachments"]
    assert attachment["manifest_bound_attempt"] is False
    assert attachment["model_http_status"] is None


def test_processing_warning_codes_explain_xls_formula_limit_without_private_members(diagnostic_client):
    def mutate(versions):
        versions[-1].source_payload["document_processing"] = {"warnings": ["XLS_FORMULA_EXPRESSIONS_UNAVAILABLE", CANARY],
            "member_issues": [{"reason": "XLSX_EXTERNAL_LINK_NOT_FETCHED", "member_path": CANARY, "member_path_sha256": "3" * 64}]}
    seed(diagnostic_client, mutate=mutate)
    response = read(diagnostic_client)
    row = response.json()["notices"][0]["attachments"][0]
    assert row["processing_warning_codes"] == ["XLSX_EXTERNAL_LINK_NOT_FETCHED", "XLS_FORMULA_EXPRESSIONS_UNAVAILABLE"]
    assert row["processing_codes_redacted"] is True
    assert CANARY not in response.text and "3" * 64 not in response.text


@pytest.mark.parametrize("stale", [None, "prompt_version", "processing_version", "quantitative_validation_record", "current_manifest_sha256", "metadata_schema"])
def test_accepted_current_audit_cannot_promote_stale_or_unbound_attempts(diagnostic_client, stale):
    def mutate(versions):
        if stale == "metadata_schema":
            versions[0].source_payload["schema_version"] = CANARY
        elif stale:
            versions[-1].source_payload[stale] = CANARY
    seed(diagnostic_client, extension=".pdf", error=None, status="ACCEPTED", mutate=mutate)
    response = read(diagnostic_client)
    row = response.json()["notices"][0]
    assert row["diagnostic_status"] == "OK"
    attachment, = row["attachments"]
    assert row["accepted_attachment_count"] == (0 if stale else 1)
    assert attachment["manifest_bound_attempt"] is (stale is None)
    assert attachment["attempt_contract"] == ("NONE" if stale else "CURRENT")
    assert row["recorded_attempt_attachment_count"] == (0 if stale in {"current_manifest_sha256", "metadata_schema"} else 1)
    assert CANARY not in response.text


def test_changed_metadata_does_not_read_old_manifest_or_materialized_private_payloads(diagnostic_client):
    def mutate(versions):
        old = versions[0]
        replacement = deepcopy(old.source_payload)
        replacement["attachment_manifest"][0]["file_name"] = "SYN-replaced.pdf"
        versions.append(NoticeVersion(version_no=3, file_sha256="3" * 64, document_complete=False,
            extraction_status="METADATA", extraction_confidence=1, source_payload=replacement))
        versions.append(NoticeVersion(version_no=4, file_sha256="4" * 64, document_complete=True,
            extraction_status="COMPLETE", extraction_confidence=1, source_payload={"kind": "SYN_PRIVATE_MATERIALIZATION", "secret": CANARY}))
    ids = seed(diagnostic_client, extension=".pdf", error=None, status="ACCEPTED", mutate=mutate)
    loaded = []
    def record(_session, instance):
        if isinstance(instance, NoticeVersion):
            loaded.append(instance.id)
    event.listen(Session, "loaded_as_persistent", record)
    try:
        response = read(diagnostic_client)
    finally:
        event.remove(Session, "loaded_as_persistent", record)
    row = response.json()["notices"][0]
    assert row["accepted_attachment_count"] == row["audited_attachment_count"] == 0
    assert set(loaded) == {ids[2]}
    assert CANARY not in response.text


def test_incomplete_metadata_is_not_reported_as_current(diagnostic_client):
    seed(diagnostic_client, mutate=lambda versions: versions[0].source_payload.update(attachment_manifest="invalid"))
    row = read(diagnostic_client).json()["notices"][0]
    assert row["diagnostic_status"] == "OK"
    assert row["invalid_manifest_slot_count"] == 1
    assert row["attachment_coverage_complete"] is False
    assert row["analysis_reason_code"] == "ATTACHMENT_COVERAGE_INCOMPLETE"


def test_quantitative_projection_reuses_pure_counts_without_candidate_shapes(diagnostic_client, monkeypatch):
    seed(diagnostic_client)
    monkeypatch.setattr(manual_analysis, "_quantitative_candidate_shapes", lambda *_args, **_kwargs: pytest.fail("shapes are unnecessary"))
    response = read(diagnostic_client)
    row = response.json()["notices"][0]
    assert row["diagnostic_status"] == "OK"
    quant = row["quantitative"]
    assert quant["expected_attachment_count"] == 1
    assert quant["profile_status"] == "INCOMPLETE"
    assert "CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE" in quant["activation_reasons"] or "SOURCE_VALIDATION_ISSUES_PRESENT" in quant["activation_reasons"]
    assert "candidate_shapes" not in response.text


def test_unknown_quantitative_codes_are_redacted_without_losing_occurrence_counts(diagnostic_client, monkeypatch):
    seed(diagnostic_client)
    diagnostic = SimpleNamespace(profile_status="REVIEW", expected_attachment_count=1, processed_attachment_count=1,
        document_binding_count=1, table_status_counts={"REVIEW": 1, CANARY: 2}, available_candidate_count=0, review_candidate_count=2,
        issues=[SimpleNamespace(code=CANARY, disposition="REVIEW", count=3), SimpleNamespace(code="XLS_" + CANARY, disposition="REVIEW", count=2)],
        review_candidate_issues=[SimpleNamespace(code="CASE_ROWS_INCOMPLETE", disposition="REVIEW", count=4)],
        activation_reasons=[CANARY, "SOURCE_VALIDATION_ISSUES_PRESENT"])
    monkeypatch.setattr(diagnostics, "_quantitative_diagnostics", lambda *_args, **_kwargs: diagnostic)
    response = read(diagnostic_client)
    quant = response.json()["notices"][0]["quantitative"]
    assert quant["issues"] == [{"code": "UNRECOGNIZED_DIAGNOSTIC_CODE", "disposition": "REVIEW", "count": 5}]
    assert quant["review_candidate_issues"][0]["count"] == 4
    assert quant["table_status_counts"] == {"REVIEW": 1, "UNKNOWN": 2}
    assert CANARY not in response.text


def test_read_does_not_write_or_call_provider_download_queue_or_private_tables(diagnostic_client, monkeypatch):
    from pai_loop import analysis_api, document_extraction, pps_enrichment
    seed(diagnostic_client)
    def forbidden(*_args, **_kwargs):
        pytest.fail("read-only diagnostic invoked a write/provider path")
    for module, name in [(analysis_api, "run_notice_analysis_batch"), (document_extraction, "extract_document_content"),
                         (pps_enrichment, "enrich_notice_from_pps"), (manual_analysis, "_reserve_manual_job")]:
        monkeypatch.setattr(module, name, forbidden)
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
    event.listen(diagnostic_client.app.state.engine, "before_cursor_execute", capture)
    try:
        first = read(diagnostic_client)
        second = read(diagnostic_client)
    finally:
        event.remove(diagnostic_client.app.state.engine, "before_cursor_execute", capture)
    assert first.json() == second.json()
    assert first.json()["notices"][0]["diagnostic_status"] == "OK"
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert len(statements) <= 10
    joined = " ".join(statements).lower()
    for table in ("company_facts", "evidence", "evaluations", "analysis_runs", "ingestion_jobs", "user_decisions", "account_sessions"):
        assert table not in joined


def test_projection_failure_is_explicit_per_notice_and_does_not_log_private_exception(diagnostic_client, monkeypatch, caplog):
    seed(diagnostic_client)
    def fail(*_args, **_kwargs):
        raise ValueError(CANARY)
    monkeypatch.setattr(diagnostics, "_quantitative_diagnostics", fail)
    response = read(diagnostic_client)
    assert response.status_code == 200
    row = response.json()["notices"][0]
    assert row["diagnostic_status"] == "UNAVAILABLE" and row["attachment_count"] is None
    assert row["attachments"] == [] and row["quantitative"] is None
    assert CANARY not in response.text and CANARY not in caplog.text


def test_current_cancellation_is_reported_without_provider_payload(diagnostic_client):
    seed(diagnostic_client)
    with diagnostic_client.app.state.session_factory() as session:
        session.add(PpsNoticeAuthority(bid_notice_no=KEY, disposition="CANCELLED", authority_sha256="9" * 64))
        session.commit()
    row = read(diagnostic_client).json()["notices"][0]
    assert row["provider_disposition"] == "CANCELLED"


@pytest.mark.parametrize("download,basis", [(True, "DOWNLOADED_BYTES"), (False, "FAILED_DOWNLOAD_MARKER"), ("true", CANARY)])
def test_digest_equality_does_not_invent_download_provenance(diagnostic_client, download, basis):
    def mutate(versions):
        versions[-1].source_payload["document_processing"] = {
            "download_complete": download, "document_digest_basis": basis,
            "source_read_complete": False, "analysis_input_complete": False}
    seed(diagnostic_client, mutate=mutate)
    response = read(diagnostic_client)
    row = response.json()["notices"][0]["attachments"][0]
    assert row["stored_document_digest_matches"] is True
    assert row["stored_download_complete"] == (download if type(download) is bool else None)
    assert row["stored_document_digest_basis"] == (basis if basis != CANARY else None)
    assert row["stored_source_read_complete"] is row["stored_analysis_input_complete"] is False
    assert CANARY not in response.text


@pytest.mark.parametrize("accounts_enabled", [False, True])
def test_real_server_key_read_is_independent_of_browser_and_paid_feature_flags(diagnostic_client, accounts_enabled):
    diagnostic_client.app.state.settings = replace(diagnostic_client.app.state.settings,
        department_accounts_enabled=accounts_enabled, public_manual_analysis_enabled=False)
    seed(diagnostic_client)
    assert read(diagnostic_client).json()["notices"][0]["diagnostic_status"] == "OK"


def test_partial_batch_preserves_other_notices_when_one_projection_fails(diagnostic_client, monkeypatch):
    other = "PPS-SYN-DIAGNOSTIC-002"
    seed(diagnostic_client)
    seed(diagnostic_client, key=other)
    original = diagnostics._quantitative_diagnostics
    def projection(notice, **kwargs):
        if notice.notice_key == KEY:
            raise ValueError(CANARY)
        return original(notice, **kwargs)
    monkeypatch.setattr(diagnostics, "_quantitative_diagnostics", projection)
    response = read(diagnostic_client, [KEY, other])
    assert [row["diagnostic_status"] for row in response.json()["notices"]] == ["UNAVAILABLE", "OK"]
    assert CANARY not in response.text


@pytest.mark.parametrize("new_generation", [False, True])
def test_supported_legacy_contract_is_distinct_and_cannot_cross_new_generation_barrier(diagnostic_client, new_generation):
    def mutate(versions):
        payload = versions[-1].source_payload
        if new_generation:
            current = deepcopy(payload)
            current.update(status="ACCEPTED", quantitative_validation_record=CANARY)
            versions.append(NoticeVersion(version_no=3, file_sha256="2" * 64,
                document_complete=True, extraction_status="ACCEPTED", extraction_confidence=1,
                source_payload=current))
        payload.update(prompt_version=LEGACY_CASE_CONTRACT.prompt,
            schema_version=LEGACY_CASE_CONTRACT.schema, processing_version=LEGACY_CASE_CONTRACT.processing)
    seed(diagnostic_client, mutate=mutate)
    row = read(diagnostic_client).json()["notices"][0]
    assert row["accepted_attachment_count"] == 0
    assert row["attachments"][0]["attempt_contract"] == ("NONE" if new_generation else "LEGACY_CASE_V1")
    assert row["attachments"][0]["manifest_bound_attempt"] is (not new_generation)


def test_no_metadata_has_unknown_freshness_and_missing_profile_not_invented_success(diagnostic_client):
    seed(diagnostic_client, mutate=lambda versions: versions.clear())
    row = read(diagnostic_client).json()["notices"][0]
    assert row["metadata_schema_current"] is None
    assert row["attachment_coverage_complete"] is False
    assert row["quantitative"]["profile_status"] == "MISSING"
    assert row["attachments"] == []


def test_malformed_json_never_echoes_secret_text(diagnostic_client):
    response = diagnostic_client.post(diagnostics.PATH,
        content='{"notice_keys": [' + CANARY, headers={**SERVER, "Content-Type": "application/json"})
    assert response.status_code == 422
    assert CANARY not in response.text
