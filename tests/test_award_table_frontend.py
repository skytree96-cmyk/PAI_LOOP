from __future__ import annotations

from pathlib import Path
import re
import subprocess


APP_JS = Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"
INDEX_HTML = APP_JS.with_name("index.html")
STYLES_CSS = APP_JS.with_name("styles.css")


def _function_body(source: str, name: str, next_name: str | None = None) -> str:
    # App functions close at the same two-space indentation. Do not assume
    # adjacency: presentation helpers may be inserted between existing ones.
    pattern = rf"^  (?:async )?function {re.escape(name)}\b(?P<body>.*?^  \}})"
    match = re.search(pattern, source, flags=re.DOTALL | re.MULTILINE)
    assert match, f"{name} frontend contract was not found"
    return match.group("body")


def _award_renderer_source(source: str) -> str:
    start = source.index("  const AWARD_TABLE_BASIS_LABELS =")
    end = source.index("\n  function renderHistory", start)
    return source[start:end] + "\nfunction formatBudget" + _function_body(source, "formatBudget")


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
    flat = re.search(r'<div\b[^>]*id="historyAwardFlat"[^>]*>', html)
    assert flat
    for attribute in ('class="history-award-table__scroll"', 'tabindex="0"', 'role="region"',
                      'aria-labelledby="historyAwardTableHeading"', 'aria-describedby="historyAwardTableNotes"'):
        assert attribute in flat[0]
    assert '<caption class="sr-only">' in html
    assert ".history-award-table__scroll { overflow-x: auto; }" in styles
    assert ".history-award-table__scroll:focus-visible" in styles


def test_award_view_controls_and_project_summary_are_accessible() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")
    assert re.search(r'awardHistoryView:\s*\{[^}]*year:\s*"all"[^}]*view:\s*"group"', app)
    for element in ("historyAwardYearFilters", "historyAwardViewButtons"):
        assert re.search(rf'els\.{element}\.addEventListener\("click",\s*handleAwardHistoryViewChange\)', app)
    for element_id in ("historyAwardSummary", "historyAwardProjectCount", "historyAwardRowCount",
                       "historyAwardScoreCount", "historyAwardRange", "historyAwardYearFilters",
                       "historyAwardViewButtons", "historyAwardGroups", "historyAwardFlat", "historyAwardTableState"):
        assert f'id="{element_id}"' in html
    assert 'aria-label="조회 연도"' in html
    assert 'aria-label="표 보기 방식"' in html
    for view, target in (("group", "historyAwardGroups"), ("flat", "historyAwardFlat")):
        button = re.search(rf'<button\b[^>]*data-award-view="{view}"[^>]*>', html)
        assert button, view
        assert 'type="button"' in button[0]
        assert f'aria-controls="{target}"' in button[0]
        assert 'aria-pressed=' in button[0]


def test_the_public_panel_still_starts_no_remote_award_request() -> None:
    app = APP_JS.read_text(encoding="utf-8")
    body = _function_body(app, "renderAnnualAwardTable", "renderHistory")

    assert "award-history/refresh" not in body
    assert "apiRequest" not in body
    assert "외부 조회를 시작하지 않습니다" in body


def test_award_table_renders_states_missing_values_and_candidate_labels() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    adapter = _award_renderer_source(source)
    script = r"""
const assert = require("node:assert/strict");
const field = () => ({ textContent: "", innerHTML: "", hidden: false, dataset: {},
  setAttribute(name, value) { this[name] = String(value); },
  querySelectorAll() { return []; },
  classList: { add() {}, remove() {}, toggle() {} },
});
const els = {
  historyAwardTableBasis: field(),
  historyAwardTableBody: field(),
  historyAwardTableNotes: field(),
  historyAwardSummary: field(), historyAwardProjectCount: field(), historyAwardRowCount: field(),
  historyAwardScoreCount: field(), historyAwardRange: field(), historyAwardToolbar: field(),
  historyAwardYearFilters: field(), historyAwardViewButtons: field(), historyAwardGroups: field(),
  historyAwardFlat: field(), historyAwardTableState: field(),
};
const state = { selectedNotice: { noticeKey: "SYN-current" },
  awardHistoryView: { noticeKey: "SYN-current", year: "all", view: "flat" } };
const escapeHtml = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeAttribute = escapeHtml;
const numberOrNull = (value) => (value === null || value === undefined || Number.isNaN(Number(value)) ? null : Number(value));
const formatNumber = (value, digits = 0) => Number(value).toFixed(digits);
const safeHttpUrl = (value) => (/^https:\/\//.test(String(value || "")) ? String(value) : "");
const stringValue = (value) => value === null || value === undefined ? "" : String(value);
const arrayValue = (value) => Array.isArray(value) ? value : [];

assert.equal(awardTableAmount(123456789), "123,456,789원");
assert.equal(awardTableAmount(0), "0원");
assert.equal(awardTableAmount(100.5), "100.5원");
assert.match(awardTableAmount(null), /미확인/);
assert.doesNotMatch(awardTableScore(0), /미확인/);

const row = (overrides) => ({
  year: 2025,
  result_group_key: "SYN-group-2025",
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
        ["node", "-e", adapter + "\n" + script],
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


def test_award_groups_preserve_project_identity_facts_and_year_view_selection() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    script = r"""
const assert = require("node:assert/strict");
const field = () => ({ textContent: "", innerHTML: "", hidden: false, dataset: {},
  setAttribute(name, value) { this[name] = String(value); }, querySelectorAll() { return []; },
  classList: { add() {}, remove() {}, toggle() {} },
});
const els = Object.fromEntries([
  "historyAwardTableBasis", "historyAwardTableBody", "historyAwardTableNotes", "historyAwardSummary",
  "historyAwardProjectCount", "historyAwardRowCount", "historyAwardScoreCount", "historyAwardRange",
  "historyAwardToolbar", "historyAwardYearFilters", "historyAwardViewButtons", "historyAwardGroups",
  "historyAwardFlat", "historyAwardTableState",
].map((name) => [name, field()]));
const state = { selectedNotice: { noticeKey: "SYN-current" },
  awardHistoryView: { noticeKey: "SYN-current", year: "all", view: "group" } };
const escapeHtml = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const escapeAttribute = escapeHtml;
const numberOrNull = (value) => value === null || value === undefined || Number.isNaN(Number(value)) ? null : Number(value);
const formatNumber = (value, digits = 0) => Number(value).toFixed(digits);
const safeHttpUrl = (value) => /^https:\/\//.test(String(value || "")) ? String(value) : "";
const stringValue = (value) => value === null || value === undefined ? "" : String(value);
const arrayValue = (value) => Array.isArray(value) ? value : [];
const row = (overrides = {}) => ({ year: 2025, project_title: "SYN 교육사업", agency: "SYN 기관",
  result_group_key: "SYN-group-current",
  bid_notice_no: "SYN-NOTICE", revision_no: "000", match_kind: "SAME_PROJECT", similarity_score: 95,
  source_status: "COLLECTED", source_notice_url: "https://example.test/SYN-award", event_date: "2025-05-01",
  company_name: "SYN-A", bid_amount: null, technical_evaluation: null, price_evaluation: null,
  total_evaluation: null, opening_rank: 1, participation_kind: "PARTICIPANT", ...overrides });
const rows = [
  row(), row({ company_name: "SYN-B", opening_rank: null, participation_kind: "WINNER", total_evaluation: 0 }),
  row({ company_name: "SYN-C", revision_no: "001", result_group_key: "SYN-group-revised" }),
  row({ company_name: "SYN-D", year: 2024, project_title: "SYN 이전연도 사업", result_group_key: "SYN-group-earlier" }),
  row({ company_name: "SYN-E", result_group_key: null }),
  row({ company_name: "SYN-F", result_group_key: null }),
];
const before = JSON.stringify(rows);
const groups = groupAnnualAwardRows(rows);
assert.equal(groups.length, 5); // One exact project joins two companies; all other identities stay separate.
const projects = groups.map((group) => renderAwardProject(group, false));
assert.equal(projects.filter((html) => html.includes("SYN-A") && html.includes("SYN-B")).length, 1);
const joined = projects.find((html) => html.includes("SYN-A") && html.includes("SYN-B"));
const joinedSummary = joined.match(/<summary>([\s\S]*?)<\/summary>/)[1];
assert.match(joinedSummary, /SYN-B/); // Explicit winner wins over the rank-one participant.
assert.doesNotMatch(joinedSummary, /SYN-A/);
assert.ok(!projects.some((html) => html.includes("SYN-A") && html.includes("SYN-C")));
assert.ok(!projects.some((html) => html.includes("SYN-A") && html.includes("SYN-D")));
assert.ok(!projects.some((html) => html.includes("SYN-E") && html.includes("SYN-F")));
// Notice number, revision and year alone cannot identify a classification/rebid result.
assert.equal(groupAnnualAwardRows([
  row(), row({ result_group_key: "SYN-group-other-result" }),
]).length, 2);
// Older responses omit the result identity: keep even identical references separate.
assert.equal(groupAnnualAwardRows([
  row({ result_group_key: undefined }), row({ result_group_key: undefined }),
]).length, 2);
assert.equal(groupAnnualAwardRows([
  row({ result_group_key: null, bid_notice_no: "" }), row({ result_group_key: null, bid_notice_no: "" }),
]).length, 2);
// The response-scoped key is also bounded by the reported year.
assert.equal(groupAnnualAwardRows([row(), row({ year: 2024 })]).length, 2);
assert.equal(JSON.stringify(rows), before);
assert.deepEqual(groupAnnualAwardRows([]), []);
assert.match(renderAwardProject(groups[0], true), /<details\b[^>]*\bopen(?:\s|>|=)/);
assert.doesNotMatch(renderAwardProject(groups[0], false), /<details\b[^>]*\bopen(?:\s|>|=)/);

// Rank one is not proof of winning. Uncollected scores and partial/failed snapshots stay explicit.
const uncertain = renderAwardProject(groupAnnualAwardRows([row({
  participation_kind: "UNKNOWN", source_status: "PARTIAL", match_kind: "SIMILAR_CANDIDATE",
  company_name: "<script>SYN-unknown</script>", project_title: "<img src=x>SYN task", source_notice_url: "javascript:SYN",
})])[0], true);
assert.doesNotMatch(uncertain, /<script>|<img src=x>|href="javascript:/);
assert.match(uncertain, /유사 사업 후보/);
assert.match(uncertain, /동일 발주 확정 아님/);
assert.match(uncertain, /부분 응답/);
assert.match(uncertain, /구분 미확인/);
assert.doesNotMatch(uncertain, /participation is-winner/);
const unknownSummary = uncertain.match(/<summary>([\s\S]*?)<\/summary>/)[1];
assert.match(unknownSummary, /낙찰 업체<\/small><strong>미확인/);
assert.ok((uncertain.match(/award-table__missing">미확인</g) || []).length >= 4);
const failed = renderAwardProject(groupAnnualAwardRows([row({ source_status: "ERROR" })])[0], false);
assert.match(failed, /조회 실패/);

const payload = { notice_key: "SYN-current", annual_award_table: {
  years: [2026, 2025, 2024], match_basis: "SAME_PROJECT_AND_AGENCY", row_count: rows.length, rows,
} };
state.awardHistoryMeta = { "SYN-current": { status: "ready", intelligence: payload } };
const viewButtons = ["group", "flat"].map((view) => ({ ...field(), dataset: { awardView: view } }));
els.historyAwardViewButtons.querySelectorAll = () => viewButtons;
function press(dataset, inside = true) {
  const button = { dataset };
  let focused = false;
  const currentTarget = { contains: (node) => inside && node === button,
    querySelector: () => ({ focus(options) { assert.equal(options.preventScroll, true); focused = true; } }) };
  handleAwardHistoryViewChange({ target: { closest: () => button }, currentTarget });
  return focused;
}
renderAnnualAwardTable(payload, "ready");
assert.equal(els.historyAwardGroups.hidden, false);
assert.equal(els.historyAwardFlat.hidden, true);
assert.match(els.historyAwardProjectCount.textContent, /5/);
assert.match(els.historyAwardRowCount.textContent, /6/);
assert.equal(els.historyAwardScoreCount.textContent, "1건"); // An actual zero score counts as available.
assert.equal(viewButtons[0]["aria-pressed"], "true");
assert.equal(viewButtons[1]["aria-pressed"], "false");
assert.equal((els.historyAwardGroups.innerHTML.match(/<details\b[^>]*\bopen(?:\s|>|=)/g) || []).length, 1);
assert.match(els.historyAwardYearFilters.innerHTML, /data-award-year="all"/);
for (const year of payload.annual_award_table.years) assert.match(els.historyAwardYearFilters.innerHTML, new RegExp(`data-award-year="${year}"`));

assert.equal(press({ awardYear: "2024" }), true);
assert.equal(state.awardHistoryView.year, "2024");
assert.match(els.historyAwardGroups.innerHTML, /SYN-D/);
assert.doesNotMatch(els.historyAwardGroups.innerHTML, /SYN-A|SYN-B|SYN-C|SYN-E|SYN-F/);
assert.equal(press({ awardView: "flat" }), true);
assert.equal(state.awardHistoryView.view, "flat");
assert.equal(els.historyAwardGroups.hidden, true);
assert.equal(els.historyAwardFlat.hidden, false);
assert.match(els.historyAwardTableBody.innerHTML, /SYN-D/);
assert.doesNotMatch(els.historyAwardTableBody.innerHTML, /SYN-A|SYN-B|SYN-C|SYN-E|SYN-F/);
assert.equal(viewButtons[0]["aria-pressed"], "false");
assert.equal(viewButtons[1]["aria-pressed"], "true");
const validSelection = JSON.stringify(state.awardHistoryView);
for (const invalid of [{ awardYear: "1900" }, { awardView: "unknown" }]) assert.equal(press(invalid), false);
assert.equal(press({ awardYear: "2025" }, false), false);
assert.equal(JSON.stringify(state.awardHistoryView), validSelection);

// A year without rows keeps its empty state rather than borrowing another year's record.
state.awardHistoryView.year = "2026";
renderAnnualAwardTable(payload, "ready");
assert.doesNotMatch(els.historyAwardTableBody.innerHTML, /SYN-[A-F]/);
assert.match(els.historyAwardTableBody.innerHTML + els.historyAwardTableState.textContent, /기록.*없|없.*기록/);
state.awardHistoryView.year = "all";
state.awardHistoryView.view = "group";
for (const status of ["loading", "error"]) {
  renderAnnualAwardTable(payload, status);
  assert.match(els.historyAwardGroups.innerHTML, /SYN-A/);
  assert.match(els.historyAwardGroups.innerHTML + els.historyAwardTableState.textContent, /이전/);
}
assert.equal(JSON.stringify(rows), before);
"""
    result = subprocess.run(["node", "-e", _award_renderer_source(source) + "\n" + script],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
