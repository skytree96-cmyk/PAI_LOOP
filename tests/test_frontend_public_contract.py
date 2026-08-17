from __future__ import annotations

import re
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"
INDEX_HTML = APP_JS.with_name("index.html")


def _function_body(source: str, name: str, next_name: str) -> str:
    pattern = rf"  function {re.escape(name)}\b(?P<body>.*?)\n  function {re.escape(next_name)}\b"
    match = re.search(pattern, source, flags=re.DOTALL)
    assert match, f"{name} frontend contract was not found"
    return match.group("body")


def test_public_document_evidence_is_flattened_with_an_explicit_allowlist() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    collect_body = _function_body(source, "collectNoticeEvidence", "flattenDocumentEvidence")
    body = _function_body(source, "flattenDocumentEvidence", "flattenRequirementEvidence")

    assert "documentEvidence.length ? documentEvidence : requirementEvidence" in collect_body
    for allowed in ("document_name", "requirements", "evidence", "page", "section", "quote", "confidence"):
        assert allowed in body
    for forbidden in (
        "source_payload",
        "response_id",
        "api_key",
        "access_token",
        "company_fact",
        "required_value",
        "attachment_id",
    ):
        assert forbidden not in body
    assert '["ACCEPTED", "COMPLETE"].includes(status)' in body
    assert 'status: "PROVISIONAL"' in body


def test_raw_requirement_evidence_uses_only_public_source_anchors() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    body = _function_body(source, "flattenRequirementEvidence", "normalizeHistory")

    assert "source.source_excerpt" in body
    assert "source.source_location" in body
    assert "source.parse_confidence" in body
    for forbidden in ("fact_key", "required_value", "evidence_key", "company_value"):
        assert forbidden not in body


def test_notice_search_contract_uses_server_query_and_open_status() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    request_body = _function_body(source, "buildNoticeRequestPath", "scheduleNoticeSearch")
    filter_body = _function_body(source, "applyFilters", "compareNotices")

    assert 'params.set("q", query)' in request_body
    assert 'params.set("status", "OPEN")' in request_body
    assert 'notice.noticeStatus.toUpperCase() !== "OPEN"' in filter_body
    assert "!notice.isNew" not in filter_body
    assert 'id="noticeSearchHelp"' in INDEX_HTML.read_text(encoding="utf-8")
