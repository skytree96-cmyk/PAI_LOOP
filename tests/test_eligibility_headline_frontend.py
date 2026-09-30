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


def test_requirement_and_attachment_cards_start_collapsed() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    requirement = _function_body(source, "renderRequirement", "submissionCheckItemsForDisplay")
    documents = _function_body(source, "renderDocumentAnalyses", "loadPrivateMatchPreview")
    assert '<details class="requirement-item is-${escapeAttribute(mode)}">' in requirement
    assert " open" not in requirement
    assert '<details class="document-analysis-item">' in documents
    assert '<summary class="document-analysis-item__head">' in documents
