"""참가자격 종합 배지가 화면에 보이는 개별 판정 카드와 어긋나지 않는지 확인한다."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "src" / "pai_loop" / "static" / "app.js"


def _function_body(source: str, name: str, next_name: str) -> str:
    pattern = rf"  (?:async )?function {re.escape(name)}\b(?P<body>.*?)\n  (?:async )?function {re.escape(next_name)}\b"
    match = re.search(pattern, source, flags=re.DOTALL)
    assert match, f"{name} frontend contract was not found"
    return match.group("body")


def test_headline_follows_current_policy_cards_and_keeps_stored_fail() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    # effectiveEligibilityStatus + currentPolicyEligibilityCards
    helpers = "function effectiveEligibilityStatus" + _function_body(
        source, "effectiveEligibilityStatus", "effectiveRecommendation"
    )
    script = r"""
const assert = require("node:assert/strict");
const STATUS_LABELS = {PASS:"충족",PASS_EXCEPTION:"조건부 충족",PASS_CURRENT:"현재 충족",REVIEW:"확인 필요",FAIL:"미충족",UNKNOWN:"확인 필요"};
const arrayValue = x => Array.isArray(x) ? x : [];
const stringValue = (v, f = "") => (v === undefined || v === null || String(v).trim() === "" ? f : String(v).trim());
let cards = [];
const eligibilityRequirementsForDisplay = () => cards;
const state = { privateMatchPreviews: { N1: { status: "ready" } } };
const notice = { noticeKey: "N1", eligibilityStatus: "REVIEW" };

cards = [{ status: "PASS_CURRENT" }, { status: "PASS_EXCEPTION" }];
assert.equal(effectiveEligibilityStatus(notice), "PASS_EXCEPTION");

cards = [{ status: "PASS_CURRENT" }, { status: "FAIL" }];
assert.equal(effectiveEligibilityStatus(notice), "FAIL");

cards = [{ status: "PASS_CURRENT" }, { status: "REVIEW" }];
assert.equal(effectiveEligibilityStatus(notice), "REVIEW");

cards = [{ status: "PASS_CURRENT" }];
assert.equal(effectiveEligibilityStatus({ ...notice, eligibilityStatus: "FAIL" }), "FAIL");

// 카드 판정을 아직 불러오지 않았으면 저장된 종합값을 그대로 쓴다.
state.privateMatchPreviews.N1.status = "loading";
assert.equal(effectiveEligibilityStatus(notice), "REVIEW");

// 저장된 요구조건이 있으면 그 판정이 우선한다(현재 정책 카드는 보지 않는다).
state.privateMatchPreviews.N1.status = "ready";
cards = [{ status: "FAIL" }];
assert.equal(effectiveEligibilityStatus({ ...notice, eligibilityStatus: "PASS", requirements: [{ status: "PASS" }] }), "PASS");
"""
    subprocess.run(["node", "-e", helpers + "\n" + script], check=True)


def test_detail_cards_never_demote_a_stored_pass_in_the_list() -> None:
    """상세에 들어갔다 나오면 목록 배지는 올라가기만 한다(충족 → 조건부 충족 금지)."""
    source = APP_JS.read_text(encoding="utf-8")
    helpers = "function effectiveEligibilityStatus" + _function_body(
        source, "effectiveEligibilityStatus", "effectiveRecommendation"
    )
    script = r"""
const assert = require("node:assert/strict");
const STATUS_LABELS = {PASS:"충족",PASS_EXCEPTION:"조건부 충족",PASS_CURRENT:"현재 충족",REVIEW:"확인 필요",FAIL:"미충족",UNKNOWN:"확인 필요"};
const arrayValue = x => Array.isArray(x) ? x : [];
const stringValue = (v, f = "") => (v === undefined || v === null || String(v).trim() === "" ? f : String(v).trim());
let cards = [{ status: "PASS_CURRENT" }, { status: "PASS_EXCEPTION" }];
const eligibilityRequirementsForDisplay = () => cards;
const state = { privateMatchPreviews: { N1: { status: "ready", data: {} } } };
const notice = { noticeKey: "N1", eligibilityStatus: "PASS" };

// 서버 종합값이 없는 응답(배포 전 서버)도 같은 규칙으로 저장된 충족을 지킨다.
assert.equal(effectiveEligibilityStatus(notice), "PASS");
cards = [{ status: "REVIEW" }];
assert.equal(effectiveEligibilityStatus(notice), "PASS");
cards = [{ status: "FAIL" }];
assert.equal(effectiveEligibilityStatus(notice), "FAIL");

// 서버가 종합한 값이 있으면 그 값을 쓴다.
cards = [{ status: "PASS_EXCEPTION" }];
state.privateMatchPreviews.N1.data.eligibilityOverall = "PASS";
assert.equal(effectiveEligibilityStatus(notice), "PASS");
state.privateMatchPreviews.N1.data.eligibilityOverall = "PASS_EXCEPTION";
assert.equal(effectiveEligibilityStatus({ ...notice, eligibilityStatus: "REVIEW" }), "PASS_EXCEPTION");
"""
    subprocess.run(["node", "-e", helpers + "\n" + script], check=True)


def test_requirement_and_attachment_cards_start_collapsed() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    requirement = _function_body(source, "renderRequirement", "submissionCheckItemsForDisplay")
    documents = _function_body(source, "renderDocumentAnalyses", "loadPrivateMatchPreview")
    assert '<details class="requirement-item is-${escapeAttribute(mode)}">' in requirement
    assert " open" not in requirement
    assert '<details class="document-analysis-item">' in documents
    assert '<summary class="document-analysis-item__head">' in documents


def test_visible_list_rows_auto_load_quantitative_forecast_two_at_a_time() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    helpers = "function observeVisibleQuantitativeSummaries" + _function_body(
        source, "observeVisibleQuantitativeSummaries", "enqueueQuantitativeAutoLoad"
    )
    helpers += "\nfunction enqueueQuantitativeAutoLoad" + _function_body(
        source, "enqueueQuantitativeAutoLoad", "drainQuantitativeAutoLoad"
    )
    helpers += "\nfunction drainQuantitativeAutoLoad" + _function_body(
        source, "drainQuantitativeAutoLoad", "isResultEntryView"
    )
    script = r"""
const assert = require("node:assert/strict");
const state = { source: "api", quantitativeEstimates: {}, notices: [
  { noticeKey: "A" }, { noticeKey: "B" }, { noticeKey: "C" }, { noticeKey: "X", cancelled: true }, { noticeKey: "OFF" },
] };
const isCancelledNotice = (notice) => Boolean(notice.cancelled);
const elements = ["A", "B", "C", "X", "OFF"].map((key) => ({ dataset: { noticeQuantitative: key } }));
const document = { querySelectorAll: () => elements };
let callback;
class IntersectionObserver { constructor(cb) { callback = cb; } observe() {} unobserve() {} disconnect() {} }
const started = []; const resolvers = [];
const loadQuantitativeEstimate = (key) => { started.push(key); state.quantitativeEstimates[key] = { status: "loading" };
  return new Promise((resolve) => resolvers.push(resolve)); };
observeVisibleQuantitativeSummaries();
callback(elements.map((target) => ({ target, isIntersecting: target.dataset.noticeQuantitative !== "OFF" })));
assert.deepEqual(started, ["A", "B"]);
resolvers.shift()();
setTimeout(() => {
  assert.deepEqual(started, ["A", "B", "C"]);
  state.source = "demo"; started.length = 0; callback = null;
  observeVisibleQuantitativeSummaries();
  assert.equal(callback, null);
}, 0);
"""
    subprocess.run(["node", "-e", helpers + "\n" + script], check=True)



def test_manager_go_turns_review_or_fail_into_pass_everywhere() -> None:
    """담당자 판단이 참여(GO)면 참가자격 판정 자체가 충족이 된다(AI 의견 포함)."""
    source = APP_JS.read_text(encoding="utf-8")
    helpers = "function effectiveEligibilityStatus" + _function_body(
        source, "effectiveEligibilityStatus", "effectiveRecommendation"
    )
    helpers += "\nfunction effectiveRecommendation" + _function_body(
        source, "effectiveRecommendation", "aiJudgmentMarkup"
    )
    script = r"""
const assert = require("node:assert/strict");
const STATUS_LABELS = {PASS:"충족",PASS_EXCEPTION:"조건부 충족",PASS_CURRENT:"현재 충족",REVIEW:"확인 필요",FAIL:"미충족",UNKNOWN:"확인 필요"};
const RECOMMENDATION_LABELS = {GO:"적극 검토",CONDITIONAL_GO:"조건부 검토",HOLD:"조건부 검토",NO_GO:"비추천",DEFERRED:"판단 보류",UNKNOWN:"확인 필요"};
const arrayValue = x => Array.isArray(x) ? x : [];
const stringValue = (v, f = "") => (v === undefined || v === null || String(v).trim() === "" ? f : String(v).trim());
const state = { privateMatchPreviews: {} };
const eligibilityRequirementsForDisplay = () => [];

for (const stored of ["REVIEW", "FAIL"]) {
  const base = { noticeKey: "N", eligibilityStatus: stored, recommendation: "GO" };
  assert.equal(effectiveEligibilityStatus({ ...base, decision: "GO" }), "PASS");
  assert.equal(automaticEligibilityStatus({ ...base, decision: "GO" }), stored);
  for (const decision of ["HOLD", "CONDITIONAL_GO", "NO_GO", undefined]) {
    assert.equal(effectiveEligibilityStatus({ ...base, decision }), stored);
  }
  // AI 검토 의견도 담당자 참여를 반영해 자격 때문에 보류로 내리지 않는다.
  assert.equal(effectiveRecommendation({ ...base, decision: "GO" }), "GO");
  assert.equal(effectiveRecommendation(base), "DEFERRED");
}
// 이미 충족 계열이면 그대로 둔다.
assert.equal(effectiveEligibilityStatus({ noticeKey: "N", eligibilityStatus: "PASS", decision: "GO" }), "PASS");
"""
    subprocess.run(["node", "-e", helpers + "\n" + script], check=True)


def test_queue_views_and_teams_preview_follow_the_manager_go_rule() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    dashboard = _function_body(source, "dashboardEligibilityStatus", "storedDashboardEligibilityStatus")
    teams = _function_body(source, "renderTeamsPreview", "buildAdaptiveCardPayload")
    teams += _function_body(source, "buildAdaptiveCardPayload", "normalizeTeamsMockLog")
    assert "managerOverridesEligibility(notice, status)" in dashboard
    assert "!isCancelledNotice(notice)" in dashboard
    assert "effectiveEligibilityStatus(notice)" in teams


def test_policy_preview_redraws_the_detail_from_the_selected_notice() -> None:
    """판정 카드 도착 시 상세는 담당자 판단까지 불러온 선택 공고로 다시 그린다(목록 사본 아님)."""
    source = APP_JS.read_text(encoding="utf-8")
    body = _function_body(source, "loadPrivateMatchPreview", "normalizePrivateMatchPreview")
    finally_block = body[body.index("} finally {"):]
    assert "const shown = state.selectedNotice;" in finally_block
    for call in ("renderPrivateMatchPreview", "renderEligibilityPanel", "renderActions", "refreshEligibilitySummaryMetric"):
        assert f"{call}(shown)" in finally_block
