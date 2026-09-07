from __future__ import annotations

import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"


def test_submission_confirmation_uses_only_explicit_source_anchors() -> None:
    """Exercise the real public detail adapter, projection and rendered output."""
    script = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = require("node:fs").readFileSync(0, "utf8");
const context = vm.createContext({
  document: {documentElement: {dataset: {}}, getElementById() {return null;}, addEventListener() {}},
  window: {matchMedia() {return {matches: false};}},
  URL, URLSearchParams,
});
const exports = `globalThis.ui = {state, els, normalizeNotice, submissionCheckItemsForDisplay,
  renderSubmissionCheckItem, renderActions, eligibilityRequirementsForDisplay};`;
vm.runInContext(source.replace(/\}\)\(\);\s*$/, exports + "\n})();"), context);
const u = context.ui;
u.state.source = "api";
const raw = {notice_key: "SYN-confirmations", title: "SYN 확인 사항 공고", status: "OPEN",
  source_kind: "PPS", analysis_state: "EVALUATED", source_url: "https://example.org/SYN-notice",
  actions: ["SYN 기존 자격 검토 행동"],
  requirements: [{id: "SYN-mandatory", condition: "SYN 필수 참가자격", result: "FAIL", mandatory: true}],
};
const labels = ["전자입찰 여부", "입찰 마감일", "제안서 마감일", "제안서 제출 유형", "입찰보증보험",
  "총괄책임자 PT 진행 여부", "연수원·강의장 보유 여부", "참여인력 자격조건", "정산 여부", "비영리 이윤제외"];
const document = (quotes, status = "ACCEPTED", name = "SYN-공고문.pdf") => ({
  status, document_name: name, requirements: quotes.map((quote, i) => ({
    requirement_id: `SYN-${i}`, normalized_condition: "SYN 모델 요약", category: "OTHER",
    mandatory: true, evidence: [{quote, attachment_id: "SYN-attachment", page: i + 1,
      section: "SYN 제출 절차", confidence: 0.95}],
  })),
});
const project = value => u.submissionCheckItemsForDisplay(u.normalizeNotice(value));
const get = (items, id) => items.find(item => item.id === id);
const texts = (items, id) => Array.from(get(items, id).sources, item => item.quote);
const html = items => items.map(u.renderSubmissionCheckItem).join("");

// Missing extraction never becomes 'no', 'yes', zero or a compliance result.
const missing = project(raw);
assert.equal(missing.length, 10);
assert.deepEqual(Array.from(missing, item => item.label), labels);
assert.ok(missing.every(item => item.sources.length === 0));
assert.equal((html(missing).match(/원문 확인 필요/g) || []).length, 10);
assert.doesNotMatch(html(missing), /SYN 기존|SYN 필수|충족|해당 없음/);

// Both affirmative and negative clauses are preserved verbatim, with conditions.
const affirmative = [
  "본 입찰은 전자입찰 방식으로 집행합니다.",
  "입찰서 제출 마감일은 2099.09.12. 17:00입니다.",
  "제안서 제출 마감일은 2099.09.11. 14:00입니다.",
  "제안서는 방문 또는 우편으로 제출해야 합니다.",
  "입찰보증보험 증권을 제출해야 합니다.",
  "총괄책임자가 PT 발표를 직접 진행해야 합니다.",
  "연수원 또는 강의장을 보유해야 합니다.",
  "참여인력은 관련 분야 석사학위 및 3년 이상 경력을 보유해야 합니다.",
  "사업비는 사후 정산합니다.",
  "비영리 법인은 이윤을 제외합니다.",
];
const negative = [
  "본 입찰은 전자입찰을 실시하지 않습니다.",
  "입찰서 제출 마감일은 2099.09.12. 17:00입니다.",
  "제안서 제출 마감일은 2099.09.11. 14:00입니다.",
  "제안서는 우편 제출을 허용하지 않으며 방문 제출해야 합니다.",
  "입찰보증금 납부는 면제하되 지급각서를 제출해야 합니다.",
  "총괄책임자의 PT 발표는 요구하지 않습니다.",
  "연수원 또는 강의장 보유는 필요하지 않으며 임차를 허용합니다.",
  "참여인력의 석사학위는 요구하지 않으나 3년 이상 경력은 필요합니다.",
  "사업비는 사후 정산하지 않습니다.",
  "비영리 법인의 이윤은 제외하지 않습니다.",
];
for (const quotes of [affirmative, negative]) {
  const items = project({...raw, document_analyses: [document(quotes)]});
  items.forEach((item, i) => {
    assert.deepEqual(Array.from(item.sources, item => item.quote), [quotes[i]], item.label);
    assert.equal(item.sources[0].location, `SYN-공고문.pdf · ${i + 1}쪽 · SYN 제출 절차`);
    assert.ok(item.sources[0].evidenceId);
  });
  assert.equal((html(items).match(/data-evidence-jump=/g) || []).length, 10);
}

// Bid metadata is independently sourced and cannot fill a proposal deadline.
const deadlineOnly = project({...raw, deadline: "2099-09-12T08:00:00Z"});
assert.match(texts(deadlineOnly, "bid-deadline")[0], /2099.*09.*12.*17:00.*KST/);
assert.equal(get(deadlineOnly, "bid-deadline").sources[0].sourceUrl, raw.source_url);
assert.equal(get(deadlineOnly, "proposal-deadline").sources.length, 0);
const proposalOnly = project({...raw, document_analyses: [document([affirmative[2]])]});
assert.deepEqual(texts(proposalOnly, "proposal-deadline"), [affirmative[2]]);
assert.equal(get(proposalOnly, "bid-deadline").sources.length, 0);
assert.equal(get(proposalOnly, "proposal-method").sources.length, 0);
const mixedHeading = project({...raw, document_analyses: [document([
  "전자입찰 방식입니다. 제안서 제출 마감일은 2099.09.11. 14:00입니다.",
  "가격제안서 제출 마감일은 2099.09.12. 17:00입니다.",
])]});
assert.equal(get(mixedHeading, "bid-deadline").sources.length, 0);
assert.equal(get(mixedHeading, "proposal-deadline").sources.length, 1);
assert.equal(get(mixedHeading, "proposal-method").sources.length, 0);

// Staffing qualifications do not get replaced by the PT duty or a general gate.
const staff = "연구책임자는 박사학위 취득 후 5년 이상의 연구경력을 보유해야 합니다.";
const staffOnly = project({...raw, document_analyses: [document([staff, "사업자는 자격증을 보유해야 합니다."])]});
assert.deepEqual(texts(staffOnly, "personnel"), [staff]);
assert.equal(get(staffOnly, "lead-presentation").sources.length, 0);
const ptOnly = project({...raw, document_analyses: [document([affirmative[5]])]});
assert.equal(get(ptOnly, "personnel").sources.length, 0);

// Unsupported summaries, review attempts, and stale/non-analyzed attachments
// never supply a source. Retain all contradictory current source statements.
const noAnchor = document([]);
noAnchor.requirements = [{normalized_condition: affirmative[4], evidence: []}];
for (const value of [
  {...raw, document_analyses: [noAnchor]},
  {...raw, document_analyses: [document(affirmative, "REVIEW")]},
  {...raw, document_analyses: [document(affirmative)], attachment_analysis_statuses: [
    {document_name: "SYN-공고문.pdf", state: "REVIEW"}]},
  {...raw, document_analyses: [document(affirmative)], attachment_analysis_statuses: [
    {document_name: "SYN-current.pdf", state: "ANALYZED"}]},
  {...raw, document_analyses: [document(affirmative), document(negative)]},
  {...raw, document_analyses: [document(affirmative)], attachment_analysis_statuses: [
    {document_name: "SYN-공고문.pdf", state: "ANALYZED"},
    {document_name: "SYN-공고문.pdf", state: "REVIEW"}]},
]) assert.ok(project(value).every(item => item.sources.length === 0));
const current = project({...raw, document_analyses: [document(affirmative)], attachment_analysis_statuses: [
  {document_name: "SYN-공고문.pdf", state: "ANALYZED"}]});
assert.ok(current.every(item => item.sources.length === 1));
const conflicts = project({...raw, document_analyses: [document([affirmative[8], negative[8]])]});
assert.deepEqual(texts(conflicts, "settlement"), [affirmative[8], negative[8]]);

// Identical wording is not a document/location identity. The existing evidence
// collector deduplicates quotes, so a missing exact target must have no jump.
const duplicateQuote = "사업비는 사후 정산합니다.";
const duplicateDocument = document([duplicateQuote], "ACCEPTED", "SYN-제안요청서.pdf");
duplicateDocument.requirements[0].evidence[0].page = 3;
const duplicateNotice = u.normalizeNotice({...raw, document_analyses: [
  document([duplicateQuote]), duplicateDocument,
]});
const duplicateSources = get(u.submissionCheckItemsForDisplay(duplicateNotice), "settlement").sources;
assert.equal(duplicateSources.length, 2);
assert.equal(duplicateSources[0].evidenceId, duplicateNotice.evidence[0].id);
assert.match(duplicateSources[1].location, /SYN-제안요청서.pdf · 3쪽/);
assert.equal(duplicateSources[1].evidenceId, "");
assert.equal((html(u.submissionCheckItemsForDisplay(duplicateNotice)).match(/data-evidence-jump=/g) || []).length, 1);
const duplicatePages = u.normalizeNotice({...raw, document_analyses: [document([duplicateQuote, duplicateQuote])]});
const pageSources = get(u.submissionCheckItemsForDisplay(duplicatePages), "settlement").sources;
assert.equal(pageSources.length, 2);
assert.ok(pageSources[0].evidenceId);
assert.equal(pageSources[1].evidenceId, "");
// Even fully matching document/location/quote must identify exactly one card.
duplicateNotice.evidence.push({...duplicateNotice.evidence[0], id: "SYN-ambiguous-card"});
assert.ok(get(u.submissionCheckItemsForDisplay(duplicateNotice), "settlement").sources.every(item => !item.evidenceId));

// A literal section heading can disambiguate a short source quote. It does
// not authorize using a generated normalized_condition without a quote.
const sectioned = document(["제출일시: 2099. 9. 10. 09:00~17:00", "직접 방문 제출 (우편 및 이메일 접수 불가)", "제출기간: 2099. 9. 1.~9. 12."]);
sectioned.requirements[0].evidence[0].section = "4. 입찰참가등록 및 기술제안서 제출";
sectioned.requirements[1].evidence[0].section = "4. 입찰참가등록 및 기술제안서 제출 다항";
sectioned.requirements[2].evidence[0].section = "5. 가격제안(입찰) 제출";
let sectionItems = project({...raw, document_analyses: [sectioned]});
assert.equal(get(sectionItems, "proposal-deadline").sources.length, 1);
assert.equal(get(sectionItems, "proposal-method").sources.length, 1);
assert.equal(get(sectionItems, "bid-deadline").sources.length, 1);
assert.match(texts(sectionItems, "bid-deadline")[0], /9\. 12/);
sectioned.requirements.forEach(item => {item.evidence[0].section = "SYN 일반 유의사항";});
sectionItems = project({...raw, document_analyses: [sectioned]});
for (const id of ["proposal-deadline", "proposal-method", "bid-deadline"]) assert.equal(get(sectionItems, id).sources.length, 0);

const malicious = project({...raw, source_url: "javascript:alert(1)", deadline: "2099-09-12T08:00:00Z",
  document_analyses: [document(["정산하지 않습니다. <script>SYN</script>"])]});
assert.doesNotMatch(html(malicious), /<script>|javascript:/);
assert.match(html(malicious), /&lt;script&gt;/);

// UI replacement leaves mandatory eligibility and raw records untouched.
const notice = u.normalizeNotice({...raw, document_analyses: [document(affirmative)]});
const before = JSON.stringify(notice);
const requirements = u.eligibilityRequirementsForDisplay(notice);
u.els.actionCard = {hidden: true};
u.els.actionList = {innerHTML: ""};
u.renderActions(notice);
assert.equal((u.els.actionList.innerHTML.match(/<li /g) || []).length, 10);
assert.equal(u.els.actionCard.hidden, false);
assert.equal(u.eligibilityRequirementsForDisplay(notice), requirements);
assert.equal(requirements[0].status, "FAIL");
assert.equal(JSON.stringify(notice), before);
u.renderActions(u.normalizeNotice({...raw, provider_disposition: "CANCELLED"}));
assert.equal(u.els.actionCard.hidden, true);
assert.equal(u.els.actionList.innerHTML, "");
"""
    subprocess.run(
        ["node", "-e", script],
        input=APP_JS.read_text(encoding="utf-8"),
        text=True,
        encoding="utf-8",
        check=True,
    )
