from __future__ import annotations

from pathlib import Path
import re
import subprocess


APP_JS = Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"
INDEX_HTML = APP_JS.with_name("index.html")
STYLES_CSS = APP_JS.with_name("styles.css")


def _function_body(source: str, name: str, next_name: str) -> str:
    pattern = rf"  (?:async )?function {re.escape(name)}\b(?P<body>.*?)\n  (?:async )?function {re.escape(next_name)}\b"
    match = re.search(pattern, source, flags=re.DOTALL)
    assert match, f"{name} frontend contract was not found"
    return match.group("body")


def test_the_replaced_intelligence_cards_are_gone_from_every_asset() -> None:
    """The table replaces the risk, prediction, coverage and warning cards."""

    app = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    for removed in (
        "historyConcentration",
        "historyPrediction",
        "historyCoverage",
        "historyWarnings",
        "history-intel-card",
        "history-coverage-grid",
    ):
        assert removed not in app, removed
        assert removed not in html, removed
        assert removed not in styles, removed

    # The panel's own loading/error/empty contract is untouched.
    for kept in ("historyStatusLabel", "historyStatusText", "historyList"):
        assert kept in app and kept in html, kept
    assert 'id="historyList" hidden' in html
    assert "items.map(renderHistory)" not in app


def test_the_award_table_markup_carries_the_agreed_columns_and_a11y() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert 'id="historyAwardTableBody"' in html
    assert 'id="historyAwardTableBasis"' in html
    assert 'id="historyAwardTableNotes"' in html
    for column in ("연도 · 사업", "업체명", "투찰금액", "기술평가", "가격평가", "종합평가", "구분"):
        assert f"<th scope=\"col\">{column}</th>" in html, column
    # The wide table scrolls inside a focusable, labelled region rather than
    # pushing the panel sideways.
    assert 'class="history-award-table__scroll" tabindex="0" role="region"' in html
    assert 'aria-labelledby="historyAwardTableHeading"' in html
    assert 'aria-describedby="historyAwardTableNotes"' in html
    assert '<caption class="sr-only">' in html
    assert ".history-award-table__scroll { overflow-x: auto; }" in styles
    assert ".history-award-table__scroll:focus-visible" in styles


def test_the_public_panel_still_starts_no_remote_award_request() -> None:
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "renderAnnualAwardTable", "renderHistory")

    assert "award-history/refresh" not in body
    assert "apiRequest" not in body
    assert "외부 조회를 시작하지 않습니다" in body


def test_award_table_renders_states_missing_values_and_candidate_labels() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    adapter = "\n".join(
        f"function {name}" + _function_body(source, name, following)
        for name, following in (
            ("awardTableMessageRow", "awardTableScore"),
            ("awardTableScore", "awardTableAmount"),
            ("awardTableAmount", "renderAwardTableRow"),
            ("renderAwardTableRow", "renderAnnualAwardTable"),
            ("renderAnnualAwardTable", "renderHistory"),
            ("formatBudget", "formatNumber"),
        )
    )
    constants = re.search(
        r"  const AWARD_TABLE_BASIS_LABELS = \{.*?const AWARD_TABLE_COLUMNS = 7;",
        source,
        flags=re.DOTALL,
    )
    assert constants
    script = r"""
const assert = require("node:assert/strict");
const field = () => ({ textContent: "", innerHTML: "" });
const els = {
  historyAwardTableBasis: field(),
  historyAwardTableBody: field(),
  historyAwardTableNotes: field(),
};
const escapeHtml = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeAttribute = escapeHtml;
const numberOrNull = (value) => (value === null || value === undefined || Number.isNaN(Number(value)) ? null : Number(value));
const formatNumber = (value, digits = 0) => Number(value).toFixed(digits);
const safeHttpUrl = (value) => (/^https:\/\//.test(String(value || "")) ? String(value) : "");

assert.equal(awardTableAmount(123456789), "123,456,789원");
assert.equal(awardTableAmount(0), "0원");
assert.equal(awardTableAmount(100.5), "100.5원");
assert.match(awardTableAmount(null), /미확인/);
assert.doesNotMatch(awardTableScore(0), /미확인/);

const row = (overrides) => ({
  year: 2025,
  project_title: "2025년 SYN 리더십 교육과정 위탁운영",
  agency: "SYN 발주기관",
  bid_notice_no: "SYN-2025",
  revision_no: "000",
  match_kind: "SAME_PROJECT",
  similarity_score: 95,
  source_status: "COLLECTED",
  source_notice_url: "https://www.g2b.go.kr/",
  event_date: "2025-05-01",
  company_name: "SYN-기관A",
  bid_amount: 100000000,
  technical_evaluation: 90,
  price_evaluation: 9.5,
  total_evaluation: 99.5,
  opening_rank: 1,
  participation_kind: "WINNER",
  ...overrides,
});

// Loading, error and empty stay three distinguishable states.
renderAnnualAwardTable(null, "loading");
assert.equal(els.historyAwardTableBasis.textContent, "기준 확인 중");
assert.match(els.historyAwardTableBody.innerHTML, /읽고 있습니다/);
renderAnnualAwardTable(null, "error");
assert.equal(els.historyAwardTableBasis.textContent, "조회 실패");
assert.match(els.historyAwardTableBody.innerHTML, /외부 조회를 시작하지 않았습니다/);
renderAnnualAwardTable({ annual_award_table: { years: [2026, 2025, 2024], match_basis: "NONE", rows: [], notes: [] } }, "ready");
assert.match(els.historyAwardTableBasis.textContent, /2026 · 2025 · 2024/);
assert.match(els.historyAwardTableBody.innerHTML, /표시할 저장 기록이 없습니다/);
renderAnnualAwardTable(null, "demo");
assert.equal(els.historyAwardTableBasis.textContent, "예시 데이터");

// A populated table: the year columns follow the server, not the browser.
renderAnnualAwardTable({
  annual_award_table: {
    years: [2026, 2025, 2024],
    match_basis: "SAME_PROJECT_AND_AGENCY",
    rows: [row({}), row({ company_name: "SYN-기관B", participation_kind: "PARTICIPANT", price_evaluation: null, bid_amount: null })],
    notes: ["기술평가는 입찰의 기술점수입니다."],
  },
}, "ready");
const html = els.historyAwardTableBody.innerHTML;
assert.match(els.historyAwardTableBasis.textContent, /동일 사업명 · 동일 발주기관/);
assert.match(html, /SYN-기관A/);
assert.match(html, /낙찰<\/span>/);
assert.match(html, /참여<\/span>/);
// Absent values are labelled, never rendered as 0 and never borrowed.
assert.equal((html.match(/award-table__missing">미확인</g) || []).length, 2);
assert.doesNotMatch(html, /award-table__missing">0/);
assert.match(html, /https:\/\/www\.g2b\.go\.kr\//);
assert.match(html, /공고 원문 열기 · SYN-2025-000/);
assert.match(els.historyAwardTableNotes.innerHTML, /기술점수입니다/);
assert.match(html, /2026년 · 표시할 저장 기록/);
const retained = { annual_award_table: { years: [2026, 2025, 2024], match_basis: "MIXED_BY_YEAR", rows: [row({ source_status: "PARTIAL" })], notes: [] } };
for (const status of ["loading", "error"]) {
  renderAnnualAwardTable(retained, status);
  assert.match(els.historyAwardTableBody.innerHTML, /SYN-기관A/);
  assert.match(els.historyAwardTableBody.innerHTML, /이전/);
  assert.match(els.historyAwardTableBody.innerHTML, /부분 응답/);
}

// A similar candidate is labelled as one and never reads as the same job.
renderAnnualAwardTable({
  annual_award_table: {
    years: [2026, 2025, 2024],
    match_basis: "SIMILAR_CANDIDATES_ONLY",
    rows: [row({ match_kind: "SIMILAR_CANDIDATE", participation_kind: "UNKNOWN", similarity_score: 71.4 })],
    notes: [],
  },
}, "ready");
const candidate = els.historyAwardTableBody.innerHTML;
assert.match(candidate, /유사 사업 후보 · 제목 유사도 71\.4% · 동일 발주 확정 아님/);
assert.match(candidate, /is-candidate/);
assert.match(candidate, /구분 미확인/);

// An un-opened award still shows its winner with three missing score columns.
renderAnnualAwardTable({
  annual_award_table: {
    years: [2026, 2025, 2024],
    match_basis: "SAME_PROJECT_AND_AGENCY",
    rows: [row({
      year: 2024,
      source_status: "NOT_COLLECTED",
      source_notice_url: null,
      technical_evaluation: null,
      price_evaluation: null,
      total_evaluation: null,
    })],
    notes: [],
  },
}, "ready");
const uncollected = els.historyAwardTableBody.innerHTML;
assert.match(uncollected, /2024/);
assert.match(uncollected, /개찰자료 미수집/);
assert.equal((uncollected.match(/award-table__missing">미확인</g) || []).length, 3);
assert.doesNotMatch(uncollected, /<a class="award-table__source"/);

// Escaping holds for provider-supplied text.
renderAnnualAwardTable({
  annual_award_table: {
    years: [2026, 2025, 2024],
    match_basis: "SAME_PROJECT_AND_AGENCY",
    rows: [row({ company_name: "<script>SYN</script>" })],
    notes: [],
  },
}, "ready");
assert.doesNotMatch(els.historyAwardTableBody.innerHTML, /<script>/);
"""
    result = subprocess.run(
        ["node", "-e", constants.group(0) + "\n" + adapter + "\n" + script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_award_reload_keeps_valid_rows_on_failure_or_partial_response_and_can_retry() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    loader = "async function loadStoredAwardHistory" + _function_body(source, "loadStoredAwardHistory", "renderAwardHistoryPanel")
    script = r"""
const assert = require("node:assert/strict");
const notice = { noticeKey: "SYN-current", awardHistory: [{ winner: "SYN-A" }], raw: {} };
const row = { year: 2025, company_name: "SYN-A", bid_notice_no: "SYN-old", match_kind: "SAME_PROJECT", bid_amount: 10, technical_evaluation: null, price_evaluation: null, total_evaluation: null };
const valid = { notice_key: "SYN-current", records: [{ winner: "SYN-A" }], annual_award_table: { rows: [row], row_count: 1, years: [2026, 2025, 2024] } };
const state = { source: "api", notices: [notice], selectedNotice: notice, awardHistoryMeta: { "SYN-current": { status: "ready", intelligence: valid } } };
const normalizeHistory = (row) => row;
const sanitizeNoticeAwardHistory = (raw) => raw;
const humanizeError = (error) => error.message;
const updateAwardHistorySummaryMetric = () => {};
let snapshots = [];
const renderAwardHistoryPanel = () => snapshots.push(state.awardHistoryMeta["SYN-current"].intelligence);
let response;
const apiRequest = async () => { if (response instanceof Error) throw response; return response; };
(async () => {
  for (response of [new Error("SYN failure"), {}, { ...valid, annual_award_table: { ...valid.annual_award_table, rows: [] } }, { ...valid, annual_award_table: { ...valid.annual_award_table, rows: [{ ...row, bid_amount: undefined }] } }]) {
    await loadStoredAwardHistory("SYN-current", { force: true });
    assert.equal(state.awardHistoryMeta["SYN-current"].status, "error");
    assert.equal(state.awardHistoryMeta["SYN-current"].intelligence, valid);
    assert.equal(state.selectedNotice, notice);
    assert.equal(state.selectedNotice.awardHistory[0].winner, "SYN-A");
  }
  assert.ok(snapshots.every((snapshot) => snapshot === valid));
  response = valid;
  await loadStoredAwardHistory("SYN-current"); // error remains retryable without force
  assert.equal(state.awardHistoryMeta["SYN-current"].status, "ready");
  response = { ...valid, records: [], annual_award_table: { ...valid.annual_award_table, rows: [], row_count: 0 } };
  await loadStoredAwardHistory("SYN-current", { force: true });
  assert.equal(state.awardHistoryMeta["SYN-current"].status, "empty");
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", loader + "\n" + script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
