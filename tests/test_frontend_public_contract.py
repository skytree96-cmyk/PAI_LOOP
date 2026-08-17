from __future__ import annotations

import re
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"
INDEX_HTML = APP_JS.with_name("index.html")
STYLES_CSS = APP_JS.with_name("styles.css")


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


def test_notice_sort_groups_pass_review_pending_and_fail_before_secondary_order() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    compare_body = _function_body(source, "compareNotices", "analysisPriorityRank")
    rank_body = _function_body(source, "analysisPriorityRank", "nullableNumberSort")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "analysisPriorityRank(a) - analysisPriorityRank(b)" in compare_body
    assert 'sort === "department"' in compare_body
    assert 'sort === "readiness"' in compare_body
    assert 'sort === "risk"' in compare_body
    assert "nullableDateSort(a.deadline, b.deadline)" in compare_body
    assert 'eligibilityStatus === "PASS") return 0' in rank_body
    assert 'eligibilityStatus === "REVIEW") return 1' in rank_body
    assert 'eligibilityStatus === "FAIL") return 3' in rank_body
    assert "return 2" in rank_body
    assert '<option value="judgement">판정 우선 · 마감 임박순</option>' in html
    assert "판정 우선 · 부서 적합도순" in html


def test_private_match_uses_public_text_lines_instead_of_a_dangling_label() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    normalize_body = _function_body(source, "normalizePrivateMatchItem", "collectPrivateMatchDetails")
    fact_key_body = _function_body(source, "normalizeCompanyFactKey", "collectPrivateMatchDetails")
    details_body = _function_body(source, "collectPrivateMatchDetails", "renderPrivateMatchPreview")
    render_body = _function_body(source, "renderPrivateMatchItem", "privateMatchCategoryLabel")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    for allowed in ("normalized_condition", "source_excerpt", "description", "action", "why"):
        assert allowed in normalize_body + details_body
    assert "공개 가능한 상세 판단 근거가 아직 연결되지 않았습니다" in details_body
    assert "private-match-details" in render_body
    assert "회사 증빙 대조 대상이 아닌 공고 정보·체크 항목입니다" in render_body
    assert "별도 회사 증빙 불필요" not in render_body
    assert "별도회사증빙불필요" in fact_key_body
    assert "NOTREQUIRED" in fact_key_body
    assert 'endsWith(":__NONE__")' in fact_key_body
    assert ".private-match-details" in styles


def test_unanalysed_reason_adapter_maps_document_failure_codes_to_korean() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    normalize_body = _function_body(source, "normalizeAnalysisReason", "normalizeRecommendation")

    for code in (
        "NOT_SELECTED",
        "ATTACHMENT_MANIFEST_MISSING",
        "ATTACHMENT_NONE",
        "HWP_ONLY_UNSUPPORTED",
        "HWPX_EXTRACT_FAILED",
        "PDF_EXTRACT_FAILED",
        "OPENAI_REVIEW",
        "UNVERIFIED_QUOTE",
        "QUOTE_UNVERIFIED",
        "PARTIAL",
    ):
        assert code in source
    assert "analysis_reason_code" in normalize_body
    assert "analysisReasonCode" in normalize_body
    assert "폐기된 공고가 아닙니다" in source
    assert "notice.analysisReason" in source
