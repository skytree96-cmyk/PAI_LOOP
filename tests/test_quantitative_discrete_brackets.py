from __future__ import annotations

from decimal import Decimal
import math

import pytest
from pydantic import ValidationError

from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeFact,
    ScoreBracket,
    _CANONICAL_METRIC_REGISTRY,
    _estimate_criterion,
    _finite_integer,
    _performance_lower_bound_saturates_max,
    _points_for_numeric_range,
    _points_for_value,
    _rule_error,
)


COUNT_METRICS = (
    "PERFORMANCE_COUNT", "PERSONNEL_COUNT", "CERTIFICATION_COUNT",
    "FACILITY_EQUIPMENT_COUNT", "AWARD_COUNT",
)


def count_criterion(metric="PERFORMANCE_COUNT"):
    spec = _CANONICAL_METRIC_REGISTRY[metric]
    return QuantitativeCriterion(
        criterion_id="SYN-COUNT", category=metric, label="Synthetic count bands", max_points=5,
        metric_key=spec["fact_key"], unit=spec["canonical_unit"], formula_type="BRACKET",
        formula="Synthetic count partition", required_evidence_keys=["SYN-EVIDENCE"],
        source_anchor={"document_label": "SYN-SOURCE", "section": "SYN-TABLE"},
        brackets=[
            ScoreBracket(bracket_id="SYN-ZERO", label="zero", min_value=0, max_value=0,
                         min_inclusive=True, max_inclusive=True, points=0),
            ScoreBracket(bracket_id="SYN-LOW", label="low", min_value=1, max_value=5,
                         min_inclusive=True, max_inclusive=False, points=3),
            ScoreBracket(bracket_id="SYN-MID", label="middle", min_value=5, max_value=10,
                         min_inclusive=True, max_inclusive=False, points=4),
            ScoreBracket(bracket_id="SYN-HIGH", label="high", min_value=10,
                         min_inclusive=True, points=5),
        ],
    )


def synthetic_fact(criterion, *, value=None, lower=None, upper=None):
    return QuantitativeFact(metric_key=criterion.metric_key, evidence_key="SYN-EVIDENCE",
                            evidence_sha256="a" * 64, status="CONFIRMED" if value is not None else "ESTIMATED",
                            value=value, lower_value=lower, upper_value=upper, confidence=0.8)


@pytest.mark.parametrize("metric", COUNT_METRICS)
def test_count_rule_accepts_zero_and_adjacent_integer_bands(metric):
    criterion = count_criterion(metric)
    spec = _CANONICAL_METRIC_REGISTRY[metric]
    assert set(spec["unit_scales"].values()) == {Decimal("1")}
    assert _rule_error(criterion) is None
    for value, points in [(0, 0), (1, 3), (4, 3), (5, 4), (9, 4), (10, 5)]:
        result = _estimate_criterion(criterion, synthetic_fact(criterion, value=value))
        assert result.status == "CONFIRMED"
        assert result.estimated_points == points


@pytest.mark.parametrize("lower,upper,expected", [
    (0, 0, (0, 0)), (0, 1, (0, 3)), (1, 4, (3, 3)), (4, 5, (3, 4)),
    (6, 9, (4, 4)), (0, 10, (0, 5)), (10, 10**12, (5, 5)),
])
def test_count_estimated_range_uses_only_possible_integers(lower, upper, expected):
    criterion = count_criterion()
    result = _estimate_criterion(criterion, synthetic_fact(criterion, lower=lower, upper=upper))
    assert result.status == "ESTIMATED"
    assert (result.lower_points, result.upper_points) == expected
    assert result.estimated_points == (expected[0] if expected[0] == expected[1] else None)


@pytest.mark.parametrize("kind", ["missing_zero", "missing_one", "overlap_one", "missing_tail", "empty_integer_row", "fractional_bound", "duplicate_lower_with_open_upper"])
def test_count_rule_rejects_missing_integer_overlap_and_unsupported_bounds(kind):
    criterion = count_criterion()
    if kind == "missing_zero":
        criterion.brackets = criterion.brackets[1:]
    elif kind == "missing_one":
        criterion.brackets[1].min_inclusive = False
    elif kind == "overlap_one":
        criterion.brackets[0].max_value = 1
    elif kind == "missing_tail":
        criterion.brackets[-1].max_value = 20
    elif kind == "empty_integer_row":
        criterion.brackets[0] = criterion.brackets[0].model_copy(update={"max_value": 1, "min_inclusive": False, "max_inclusive": False})
    elif kind == "fractional_bound":
        criterion.brackets[1].min_value = 0.5
    else:
        criterion.brackets.append(ScoreBracket(bracket_id="SYN-DUPLICATE", label="duplicate", min_value=0, points=1))
    assert _rule_error(criterion) is not None
    assert _estimate_criterion(criterion, synthetic_fact(criterion, value=0)).status == "REVIEW"


@pytest.mark.parametrize("value", [-1, 0.5, float("nan"), float("inf"), float("-inf"), True])
def test_count_scalar_rejects_invalid_domain_values(value):
    criterion = count_criterion()
    assert _points_for_value(criterion, value) is None
    if isinstance(value, float) and not math.isfinite(value):
        with pytest.raises(ValidationError):
            synthetic_fact(criterion, value=value)
    else:
        assert _estimate_criterion(criterion, synthetic_fact(criterion, value=value)).status == "REVIEW"


@pytest.mark.parametrize("lower,upper", [(-1, 1), (0, 1.5), (0.5, 1), (2, 1), (0, float("inf")), (float("nan"), 1)])
def test_count_range_rejects_invalid_domain_endpoints(lower, upper):
    criterion = count_criterion()
    assert _points_for_numeric_range(criterion, lower, upper) is None


@pytest.mark.parametrize("metric", ["PERFORMANCE_AMOUNT", "FINANCIAL_RATIO", "BUSINESS_YEARS"])
def test_continuous_metrics_keep_the_real_number_coverage_contract(metric):
    criterion = count_criterion(metric)
    assert _rule_error(criterion) is not None
    criterion.brackets[0].min_value = None
    assert "공백" in _rule_error(criterion)


def test_continuous_scalar_and_fractional_ranges_still_work():
    criterion = count_criterion("FINANCIAL_RATIO")
    criterion.brackets = [
        ScoreBracket(bracket_id="SYN-LOW", label="below", max_value=1, max_inclusive=False, points=1),
        ScoreBracket(bracket_id="SYN-HIGH", label="above", min_value=1, min_inclusive=True, points=5),
    ]
    assert _rule_error(criterion) is None
    assert _points_for_value(criterion, 0.5) == 1
    assert _points_for_numeric_range(criterion, 0.5, 1.5) == (1, 5)


def test_invalid_count_lower_bound_never_proves_maximum():
    criterion = count_criterion()
    assert _performance_lower_bound_saturates_max(criterion, 10) is True
    for value in (10.5, -1, float("inf"), float("nan")):
        assert _performance_lower_bound_saturates_max(criterion, value) is False


def test_count_rule_does_not_invent_a_company_fact():
    assert _estimate_criterion(count_criterion(), None).status == "UNSCORABLE"


def test_count_range_includes_an_interior_nonmonotonic_band():
    criterion = count_criterion()
    criterion.brackets[1].points = 5
    criterion.brackets[2].points = 1
    result = _estimate_criterion(criterion, synthetic_fact(criterion, lower=1, upper=10))
    assert result.status == "ESTIMATED"
    assert (result.lower_points, result.upper_points) == (1, 5)


def test_count_domain_does_not_override_company_evidence_binding():
    criterion = count_criterion("PERSONNEL_COUNT")
    criterion.fact_binding_sha256 = "b" * 64
    fact = synthetic_fact(criterion, value=10)
    result = _estimate_criterion(criterion, fact)
    assert result.status == "UNSCORABLE"
    assert result.estimated_points is None


def test_unknown_metric_key_never_infers_a_count_domain_from_its_label():
    criterion = count_criterion()
    criterion.metric_key = "SYN-UNREGISTERED-COUNT"
    assert _rule_error(criterion) is not None


@pytest.mark.parametrize("value", [2**53, 2**53 + 1, -(2**53), -(2**53 + 1)])
def test_count_safe_integer_guard_rejects_raw_and_float_coerced_values(value):
    criterion = count_criterion()
    fact = synthetic_fact(criterion, value=value)
    ranged = synthetic_fact(criterion, lower=value, upper=value)
    # Pydantic converts the unsafe odd integer to its even float neighbor.
    # Guard both the original value and the stored/model-converted value.
    assert _finite_integer(value) is False
    assert _finite_integer(fact.value) is False
    assert _points_for_value(criterion, value) is None
    assert _points_for_value(criterion, fact.value) is None
    assert _points_for_numeric_range(criterion, value, value) is None
    assert _points_for_numeric_range(criterion, ranged.lower_value, ranged.upper_value) is None
    assert _estimate_criterion(criterion, fact).status == "REVIEW"
    assert _estimate_criterion(criterion, ranged).status == "REVIEW"
    assert _performance_lower_bound_saturates_max(criterion, value) is False


def safe_limit_criterion(cutoff):
    criterion = count_criterion()
    criterion.brackets = [
        ScoreBracket(bracket_id="SYN-BELOW-LIMIT", label="below", max_value=cutoff, points=1),
        ScoreBracket(bracket_id="SYN-AT-LIMIT", label="at", min_value=cutoff, points=5),
    ]
    return criterion


@pytest.mark.parametrize("cutoff", [2**53, 2**53 + 1])
def test_count_safe_integer_guard_rejects_float_coerced_bracket_bounds(cutoff):
    criterion = safe_limit_criterion(cutoff)
    assert _finite_integer(criterion.brackets[0].max_value) is False
    assert _finite_integer(criterion.brackets[1].min_value) is False
    assert _rule_error(criterion) is not None


def test_count_safe_integer_limit_preserves_exact_and_estimated_scores():
    limit = 2**53 - 1
    criterion = safe_limit_criterion(limit)
    assert _finite_integer(limit) is True
    assert _finite_integer(-limit) is True
    assert _rule_error(criterion) is None
    exact = synthetic_fact(criterion, value=limit)
    ranged = synthetic_fact(criterion, lower=limit - 1, upper=limit)
    assert exact.value == limit
    assert _points_for_value(criterion, limit - 1) == 1
    assert _points_for_value(criterion, limit) == 5
    assert _points_for_numeric_range(criterion, limit, limit) == (5, 5)
    assert _points_for_numeric_range(criterion, limit - 1, limit) == (1, 5)
    assert _estimate_criterion(criterion, exact).estimated_points == 5
    estimate = _estimate_criterion(criterion, ranged)
    assert estimate.status == "ESTIMATED"
    assert (estimate.lower_points, estimate.upper_points) == (1, 5)


def test_count_safe_integer_guard_rejects_huge_python_int_before_coercion():
    value = 10**400
    criterion = count_criterion()
    assert _finite_integer(value) is False
    assert _points_for_value(criterion, value) is None
    assert _points_for_numeric_range(criterion, value, value) is None
    assert _performance_lower_bound_saturates_max(criterion, value) is False
