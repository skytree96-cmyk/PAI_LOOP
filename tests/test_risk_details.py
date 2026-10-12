import json
import subprocess

from pai_loop.risk_projection import public_risk_basis
from test_department_accounts_frontend import APP, BEHAVIOR_HARNESS


def test_public_risk_allowlist_excludes_private_data_and_invalid_scores():
    result = public_risk_basis({"risk": {
        "status": "WITHHELD_R07", "private": "SYN-PRIVATE",
        "axis_values": {"qualification": 0, "execution": True, "document": float("nan"), "competition": 101, "secret": 30},
        "weights": {"qualification": .25, "secret": .5},
        "axis_basis": {"qualification": {
            "source": "DETERMINISTIC_ELIGIBILITY_EVALUATION", "outcome": "PASS",
            "requirement_count": 2, "method": "SYN-PRIVATE", "fact_key": "SYN-PRIVATE",
            "evidence": {"registration": "SYN-PRIVATE"},
        }},
    }})
    assert result["axis_values"] == {"qualification": 0}
    assert result["status"] == "WITHHELD_R07"
    assert len(result["missing_axes"]) == 5
    assert "SYN-PRIVATE" not in json.dumps(result)
    assert result["axis_basis"]["qualification"] == {
        "source": "DETERMINISTIC_ELIGIBILITY_EVALUATION", "outcome": "PASS", "requirement_count": 2,
    }
    assert public_risk_basis({}) is None
    assert public_risk_basis({"risk": {}})["axis_values"] == {}


def test_public_risk_preserves_only_safe_arithmetic_inputs_for_all_axes():
    basis = {
        "qualification": {"source": "NOTICE_RISK_DIMENSIONS_AUTHORITATIVE_OVERRIDE"},
        "execution": {"source": "DETERMINISTIC_READINESS_EVALUATION", "readiness_score": 75},
        "competition": {"source": "STORED_3Y_AWARD_HISTORY", "sample_count": 12},
        "profitability": {"source": "STORED_3Y_AWARD_RATE_PREDICTION", "award_rate_center": 87.5, "sample_count": 12},
        "operation": {"source": "MATERIALIZED_POLICY_REQUIREMENTS", "incomplete_action_count": 1, "mandatory_checklist_count": 2},
        "document": {"source": "VALIDATED_EXTRACTION_STATUS", "run_status": "COMPLETED", "minimum_extraction_confidence": .95, "source_count": 2},
    }
    risk = {"status": "AVAILABLE", "minimum_evidenced_axes": 4,
            "axis_values": dict.fromkeys(basis, 10), "axis_basis": basis}
    projected = public_risk_basis({"risk": risk})
    assert projected["axis_basis"] == basis
    assert projected["minimum_evidenced_axes"] == 4
    assert projected["missing_axes"] == []
    risk["axis_basis"] = {"qualification": {"source": ["SYN-INVALID"]}}
    assert public_risk_basis({"risk": risk})["axis_basis"] == {}


def test_browser_uses_stored_axes_and_renders_all_six_with_calculations():
    harness = BEHAVIOR_HARNESS.replace(
        "normalizeNotice,normalizeResultLearningNotice,",
        "normalizeNotice,normalizeResultLearningNotice,renderRiskPanel,",
    )
    script = r'''
const raw={...noticeRaw,source_kind:'SYNTHETIC',analysis_state:'EVALUATED',
 latest_evaluation:{id:'SYN-E',eligibility:'PASS',risk_score:14.7,
 explanation:{risk:{status:'AVAILABLE',axis_values:{qualification:10,execution:20,operation:25,document:10},
 weights:{qualification:.25,execution:.2,operation:.1,document:.2,competition:.15,profitability:.1},
 axis_basis:{execution:{source:'DETERMINISTIC_READINESS_EVALUATION',readiness_score:80},
 operation:{source:'MATERIALIZED_POLICY_REQUIREMENTS',incomplete_action_count:1,mandatory_checklist_count:1}}}}},
 risk_dimensions:{qualification:99}};
let n=u.normalizeNotice(raw);u.renderRiskPanel(n);
assert.equal(n.riskAxes.find(a=>a.label==='자격 조건').score,10);
assert.equal((u.els.riskBars.innerHTML.match(/class="risk-row /g)||[]).length,6);
assert.match(u.els.riskBars.innerHTML,/100 − 준비도 80 = 20점/);
assert.match(u.els.riskBars.innerHTML,/필수조치 1건 × 20/);
assert.match(u.els.riskBars.innerHTML,/÷ 75% = 14.7점/);
assert.match(u.els.riskBars.innerHTML,/미산정/);
assert.match(u.els.riskTotalLabel.textContent,/총점/);
raw.latest_evaluation.explanation.risk.status='WITHHELD_R07';
n=u.normalizeNotice(raw);u.renderRiskPanel(n);
assert.match(u.els.riskTotalLabel.textContent,/산정 보류/);
assert.match(u.els.riskBars.innerHTML,/원문 근거 검증/);
raw.latest_evaluation.explanation.risk.axis_values={};
n=u.normalizeNotice(raw);u.renderRiskPanel(n);
assert.equal(n.riskAxes.length,0); // Never fall back to a stale manual override.
assert.match(u.els.riskTotalLabel.textContent,/0\/6/);
raw.latest_evaluation.explanation.risk.axis_values={qualification:0};
n=u.normalizeNotice(raw);u.renderRiskPanel(n);
assert.equal(n.riskAxes[0].score,0);
assert.match(u.els.riskTotalLabel.textContent,/1\/6/);
raw.latest_evaluation=null;
raw.analysis_state='PENDING';
n=u.normalizeNotice(raw);u.renderRiskPanel(n);
assert.equal(n.riskAxes.length,0);
assert.equal(u.els.riskTotalLabel.textContent,'분석 전');
raw.status='CLOSED';
raw.historical_evaluation={id:'SYN-HISTORY',eligibility:'PASS',risk_score:10,explanation:{risk:{
 status:'AVAILABLE',axis_values:{qualification:10,execution:10,operation:10,document:10}}}};
n=u.normalizeNotice(raw);u.renderRiskPanel(n);
assert.equal(n.riskAxes[0].score,10);
assert.match(u.els.riskTotalLabel.textContent,/당시 총점/);
'''
    result = subprocess.run(["node", "-e", harness + script],
                            input=APP.read_text(encoding="utf-8"),
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
