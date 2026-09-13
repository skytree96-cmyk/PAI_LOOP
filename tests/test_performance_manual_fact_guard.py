"""SYN manual recognition conditions cannot score from unproved estimates."""
import pytest

from pai_loop.quantitative_performance import PerformanceRecognitionScope
from pai_loop.quantitative_scoring import estimate_quantitative_score
from test_quantitative_count_ranges import fact, fixture, request


def manual_request(clause, *, legacy=False):
    payload, source = fixture()
    condition = payload["quantitative_tables"][0]["criteria"][0]["recognition_conditions"][0]
    original = condition["literal"]
    condition["literal"] += ", " + clause
    condition["evidence"]["quote"] = condition["literal"]
    req = request(payload, source.replace(original, condition["literal"]))
    if legacy:
        stored_scope = req.criteria[0].performance_scope.model_dump()
        stored_scope.pop("manual_verification_conditions")
        req.criteria[0].performance_scope = PerformanceRecognitionScope.model_validate(stored_scope)
    return req


@pytest.mark.parametrize("clause", [
    "단일규모 계약금액 5천만원 이상, 50인 이상만 해당",
    "단일 계약 당 연간 기준 총 계약금액 1억원 이상",
])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("changes", [
    {"status": "ESTIMATED", "value": 7},
    {"status": "ESTIMATED", "lower_value": 7},
    {"status": "ESTIMATED", "lower_value": 7, "upper_value": 9},
    {"value": 7, "evidence_reference": None},
    {"value": 7, "evidence_reference": " "},
    {"value": 7, "evidence_sha256": None},
])
def test_manual_conditions_require_confirmed_evidenced_values(clause, legacy, changes):
    req = manual_request(clause, legacy=legacy)
    req.facts = [fact(req, **changes)]
    result = estimate_quantitative_score(req)
    assert result.criteria[0].status == "REVIEW"
    assert result.criteria[0].estimated_points is None
    assert result.estimated_points is None
    assert result.confirmed_points == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_confirmed_aggregate_keeps_its_score_and_stale_binding_does_not(legacy):
    req = manual_request("계약별 50명 이상", legacy=legacy)
    req.facts = [fact(req, value=7)]
    assert estimate_quantitative_score(req).confirmed_points == 5
    for binding in (None, "f" * 64):
        req.facts = [fact(req, value=7, fact_binding_sha256=binding)]
        assert estimate_quantitative_score(req).estimated_points is None


def test_automatic_scope_still_supports_an_estimated_saturated_lower_bound():
    req = request()
    req.facts = [fact(req, status="ESTIMATED", lower_value=7)]
    result = estimate_quantitative_score(req)
    assert result.criteria[0].status == "ESTIMATED"
    assert result.estimated_points == 5
