from pathlib import Path
import subprocess


SOURCE = (Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js").read_text(
    encoding="utf-8"
)


def _function_body(name: str, next_name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    end = SOURCE.index(f"function {next_name}(", start)
    return SOURCE[start:end]


def test_complete_attachment_audit_can_retry_incomplete_quantitative_rules() -> None:
    helper = _function_body(
        "quantitativeRuleRetryRequired",
        "invalidateQuantitativeEstimate",
    )
    assert 'rule_source_status).toUpperCase() === "INCOMPLETE"' in helper
    assert 'activation_status).toUpperCase() === "REVIEW_REQUIRED"' in helper

    evaluation_only = _function_body(
        "evaluationOnlyManualAnalysis",
        "manualAnalysisAuthHeaders",
    )
    assert "!quantitativeRuleRetryRequired(notice)" in evaluation_only


def test_manual_action_refreshes_rule_status_before_selecting_retry_mode() -> None:
    request_body = _function_body("requestManualAnalysis", "handleNoticeKeydown")
    refresh = "await loadQuantitativeEstimate(noticeKey, { force: true })"
    availability = "const availability = manualAnalysisAvailability(notice)"
    assert refresh in request_body
    assert request_body.index(refresh) < request_body.index(availability)
    assert '["ANALYZED", "EVALUATED"].includes(notice?.analysisState)' in request_body
    assert "requestBody.retry_reviewed = true" in request_body


def test_quantitative_retry_has_an_operator_visible_label_and_reason() -> None:
    availability = _function_body(
        "manualAnalysisAvailability",
        "manualAnalysisLabel",
    )
    assert 'code: "QUANTITATIVE_RETRY"' in availability
    assert 'label: "정량 근거 재검증"' in availability
    assert "공고별 정량표 근거가 검토 상태" in availability


def test_unestablished_table_render_is_an_extraction_gap_without_duplicate_ambiguity() -> None:
    render = _function_body("renderQuantitativeEstimate", "quantSummaryCard")
    script = r"""
const assert = require('node:assert/strict');
const els = Object.fromEntries(['scoreOverview', 'quantSourceStatus', 'quantOpinion',
  'quantSourceAnchor', 'quantAssumptionList', 'quantTableBody', 'quantObservationList',
  'quantSeparationNote'].map(key => [key, {innerHTML: '', textContent: ''}]));
const numberOrNull = value => value == null ? null : Number(value);
const escapeHtml = value => String(value);
const formatNumber = (value, digits) => Number(value).toFixed(digits);
const quantSummaryCard = (label, value, detail) => `${label}: ${value}: ${detail}`;
const quantReadinessLabel = value => String(value);
const quantStatusLabel = value => String(value);
const emptyPanel = (title, detail) => `${title}: ${detail}`;
const base = {total_max_points: null, lower_points: null, upper_points: null,
  rule_source_status: 'INCOMPLETE', source_validation_status: 'INCOMPLETE',
  activation_status: 'REVIEW_REQUIRED', overall_status: 'UNSCORABLE',
  criteria: [], evidence_observations: [], assumptions: []};
const codes = ['ALTERNATIVE_TABLE_AMBIGUOUS', 'QUANTITATIVE_TABLE_NOT_ESTABLISHED',
  'EXTRACTION_DECLARED_INCOMPLETE'];
const data = {...base, activation_reasons: codes};
const before = JSON.stringify(data);
renderQuantitativeEstimate(data);
assert.equal(JSON.stringify(data), before);
assert.match(els.quantAssumptionList.innerHTML, /추출·검증 결과/);
assert.match(els.quantAssumptionList.innerHTML, /원문 배점표 유무는 추가 확인/);
assert.match(els.quantAssumptionList.innerHTML, /첨부 추출 결과가 일부 불완전/);
assert.doesNotMatch(els.quantAssumptionList.innerHTML, /복수 평가표/);
assert.match(els.scoreOverview.innerHTML, /배점표 미확보/);
assert.doesNotMatch(els.scoreOverview.innerHTML, /배점표 발견/);
assert.doesNotMatch(els.quantSourceAnchor.textContent, /배점표 후보 확인/);
assert.doesNotMatch(els.quantTableBody.innerHTML, /배점표는 확인했지만/);
renderQuantitativeEstimate({...base, activation_reasons: ['ALTERNATIVE_TABLE_AMBIGUOUS']});
assert.match(els.quantAssumptionList.innerHTML, /복수 평가표/);
assert.match(els.scoreOverview.innerHTML, /배점표 발견/);
renderQuantitativeEstimate({...base, rule_source_status: 'NOT_APPLICABLE',
  activation_status: 'NOT_APPLICABLE', source_validation_status: 'NOT_APPLICABLE', activation_reasons: []});
assert.match(els.scoreOverview.innerHTML, /정량평가 비적용/);
assert.match(els.quantTableBody.innerHTML, /이 공고에는 회사 정량점수를 적용하지 않습니다/);
"""
    result = subprocess.run(["node", "-e", render + script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
