"""SYN exclusion scenarios are separate from company facts and actual scores."""
import pytest
from pydantic import ValidationError

from pai_loop.quantitative_performance_scenario import (
    VerifiedPerformanceScenarioRecord, estimate_performance_count_exclusion_scenario,
)
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion, QuantitativeFact, ScoreBracket, estimate_quantitative_score,
)
from test_performance_manual_fact_guard import manual_request
from test_quantitative_count_ranges import fixture, request


def reviewed_records(req, count):
    return [VerifiedPerformanceScenarioRecord(
        record_key=f"SYN-included-{index}", evidence_reference=f"SYN-evidence-{index}",
        evidence_sha256="b" * 64, fact_binding_sha256=req.criteria[0].fact_binding_sha256,
        recognition_conditions_verified=True,
    ) for index in range(count)]


def scenario(req, count=0, **changes):
    args = dict(
        criterion_id=req.criteria[0].criterion_id, included_records=reviewed_records(req, count),
        excluded_record_keys=["SYN-unverified-1", "SYN-unverified-2"],
        exclusion_reason="SYN 인정조건 증빙 미확인",
    )
    args.update(changes)
    return estimate_performance_count_exclusion_scenario(req, **args)


def zero_band_request():
    payload, source = fixture(inline=True)
    row = payload["quantitative_tables"][0]["criteria"][0]["cases"][-2]
    original = row["literal"]
    row["literal"] = "0건∼2건 3.5점"
    row["evidence"]["quote"] = row["literal"]
    row["comparison_value"] = 0
    return request(payload, source.replace(original, row["literal"]))


def test_zero_subset_uses_the_explicit_source_band_without_asserting_actual_zero():
    req = zero_band_request()
    before = req.model_dump_json()
    ordinary_before = estimate_quantitative_score(req).model_dump_json()
    result = scenario(req)
    assert result.status == "ESTIMATED_SCENARIO"
    assert result.scenario_count == 0
    assert result.estimated_points == 3.5  # Printed 0-2 row, not an invented 0 award.
    assert result.purpose == "UNVERIFIED_PERFORMANCE_EXCLUDED"
    assert result.persistence_eligible is False
    assert result.company_actual_count_confirmed is False
    assert result.eligibility_assessed is False
    assert "잠정 시나리오" in result.rationale
    assert req.model_dump_json() == before
    assert estimate_quantitative_score(req).model_dump_json() == ordinary_before
    assert estimate_quantitative_score(req).estimated_points is None


def test_unknown_zero_count_cannot_use_the_separate_non_submission_row():
    req = request()
    assert req.criteria[0].case_table.rows[-1].operator == "NOT_SUBMITTED"
    result = scenario(req)
    assert result.scenario_count == 0
    assert result.status == "REVIEW_REQUIRED"
    assert result.estimated_points is None
    assert result.review_code == "SCENARIO_COUNT_NOT_SOURCE_SCORED"


@pytest.mark.parametrize("count,points", [(1, 3.5), (4, 4), (7, 5)])
@pytest.mark.parametrize("clause", ["계약별 50명 이상", "단일 계약 당 연간 기준 총 계약금액 1억원 이상"])
def test_only_explicitly_verified_and_bound_subset_contributes(count, points, clause):
    req = manual_request(clause)
    result = scenario(req, count)
    assert result.status == "ESTIMATED_SCENARIO"
    assert result.estimated_points == points
    assert result.scenario_count == count
    assert len(result.included_records) == count
    # No aggregate fact was injected and the ordinary manual-evidence guard stays.
    assert not req.facts
    assert estimate_quantitative_score(req).estimated_points is None


@pytest.mark.parametrize("changes", [
    {"recognition_conditions_verified": False}, {"evidence_reference": " "},
    {"evidence_sha256": None}, {"fact_binding_sha256": None},
])
def test_included_records_require_an_explicit_complete_evidence_attestation(changes):
    values = reviewed_records(request(), 1)[0].model_dump()
    with pytest.raises(ValidationError):
        VerifiedPerformanceScenarioRecord.model_validate({**values, **changes})


def test_stale_or_other_criterion_evidence_cannot_supply_a_subset():
    req = manual_request("계약별 50명 이상")
    proof = reviewed_records(req, 1)[0].model_copy(update={"fact_binding_sha256": "f" * 64})
    result = scenario(req, included_records=[proof])
    assert result.status == "REVIEW_REQUIRED"
    assert result.estimated_points is None
    assert result.review_code == "SCENARIO_RECORD_BINDING_MISMATCH"


def test_prebuilt_record_cannot_bypass_recognition_attestation_validation():
    req = request()
    proof = reviewed_records(req, 1)[0].model_copy(
        update={"recognition_conditions_verified": False}
    )
    with pytest.raises(ValidationError):
        scenario(req, included_records=[proof])


@pytest.mark.parametrize("failure", ["duplicate_included", "duplicate_excluded", "overlap"])
def test_a_record_cannot_be_counted_twice_or_both_included_and_excluded(failure):
    req = request()
    proof = reviewed_records(req, 1)[0]
    args = {"included_records": [proof]}
    if failure == "duplicate_included":
        args["included_records"] = [proof, proof]
    elif failure == "duplicate_excluded":
        args["excluded_record_keys"] = ["SYN-excluded", "SYN-excluded"]
    else:
        args["excluded_record_keys"] = [proof.record_key]
    with pytest.raises(ValueError, match="IDENTITIES_OVERLAP"):
        scenario(req, **args)


@pytest.mark.parametrize("failure", ["activation", "missing_scope", "changed_scope", "binding", "rule"])
def test_uncertain_source_rules_still_fail_closed_even_for_zero_subset(failure):
    req = zero_band_request()
    criterion = req.criteria[0]
    if failure == "activation":
        req.activation_status = "REVIEW_REQUIRED"
    elif failure == "missing_scope":
        criterion.performance_scope = None
    elif failure == "changed_scope":
        criterion.performance_scope = criterion.performance_scope.model_copy(
            update={"lookback_years": 9}
        )
    elif failure == "binding":
        criterion.fact_binding_sha256 = None
    else:
        criterion.case_table = None
    result = scenario(req)
    assert result.status == "REVIEW_REQUIRED"
    assert result.estimated_points is None


def test_non_monotonic_count_program_cannot_be_called_conservative():
    req = zero_band_request()
    criterion = req.criteria[0]
    criterion.formula_type = "BRACKET"
    criterion.case_table = None
    criterion.brackets = [
        ScoreBracket(bracket_id="SYN-low", label="SYN below 2", max_value=2, points=5),
        ScoreBracket(bracket_id="SYN-high", label="SYN 2 or above", min_value=2, points=1),
    ]
    result = scenario(req)
    assert result.status == "REVIEW_REQUIRED"
    assert result.estimated_points is None
    assert result.review_code == "NON_MONOTONIC_SCENARIO_RULE"


def test_existing_engine_already_preserves_confirmed_partial_and_conservative_range():
    req = manual_request("계약별 50명 이상")
    credit = QuantitativeCriterion(
        criterion_id="SYN-credit", category="CREDIT_RATING", label="SYN 신용평가",
        max_points=5, metric_key="company.credit_rating", formula_type="CATEGORICAL",
        formula="SYN A0 = 5점", categories=[{"values": ("A0",), "points": 5}],
        source_anchor=req.criteria[0].source_anchor,
        required_evidence_keys=["company.credit_rating"], fact_binding_sha256="c" * 64,
    )
    req.criteria.append(credit)
    req.facts = [QuantitativeFact(
        metric_key="company.credit_rating", status="CONFIRMED", value="A0",
        evidence_key="company.credit_rating", evidence_reference="SYN-credit-proof",
        evidence_sha256="d" * 64, fact_binding_sha256="c" * 64, confidence=1,
    )]
    before = estimate_quantitative_score(req)
    assert before.estimated_points is None
    assert (before.confirmed_points, before.lower_points, before.upper_points) == (5, 5, 10)
    assert before.criteria[0].estimated_points is None
    assert scenario(req).estimated_points is None  # The count source has no 0-count row.
    assert estimate_quantitative_score(req) == before
    nonperformance = scenario(req, criterion_id="SYN-credit")
    assert nonperformance.review_code == "NOT_PERFORMANCE_COUNT"
    assert estimate_quantitative_score(req).criteria[1].estimated_points == 5
