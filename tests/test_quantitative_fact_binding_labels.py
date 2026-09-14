"""SYN only: binding diagnostics must not create or alter a numeric award."""

import pytest

from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeEstimateRequest,
    QuantitativeFact,
    ScoreBracket,
    _estimate_criterion,
    estimate_quantitative_score,
)


def criterion(floor=0):
    return QuantitativeCriterion(
        criterion_id="SYN-BOUND", category="PERSONNEL_COUNT", label="SYN 인력 수",
        max_points=5, metric_key="company.personnel.count", unit="명",
        formula_type="BRACKET", formula="SYN 0명 이상 1명 미만 최소점, 1명 이상 5점",
        required_evidence_keys=["SYN-EVIDENCE"], fact_binding_sha256="a" * 64,
        source_anchor={"document_label": "SYN", "section": "SYN-TABLE"},
        rule_floor_points=floor, floor_condition="SYN 명시 최소점 적용 조건" if floor else None,
        brackets=[
            ScoreBracket(bracket_id="SYN-LOW", label="SYN low", min_value=0,
                         max_value=1, points=floor),
            ScoreBracket(bracket_id="SYN-HIGH", label="SYN high", min_value=1, points=5),
        ],
    )


def fact(binding):
    return QuantitativeFact(
        metric_key="company.personnel.count", value=2, status="CONFIRMED",
        evidence_key="SYN-EVIDENCE", evidence_reference="SYN-AUDIT",
        evidence_sha256="c" * 64, fact_binding_sha256=binding, confidence=1,
    )


@pytest.mark.parametrize("floor", [0, 1])
@pytest.mark.parametrize("binding,status,phrase", [
    (None, "UNSCORABLE", "결합 정보가 없어"),
    ("b" * 64, "REVIEW", "현재 평가항목"),
])
def test_generic_and_stale_labels_preserve_unscored_values(floor, binding, status, phrase):
    rule = criterion(floor)
    result = _estimate_criterion(rule, fact(binding))
    assert result.status == status
    assert phrase in result.rationale
    assert result.estimated_points is None
    assert (result.lower_points, result.upper_points, result.confidence) == (floor, 5, 0)
    assert result.evidence_reference == "SYN-AUDIT"
    assert result.evidence_sha256 == "c" * 64
    generic = _estimate_criterion(rule, fact(None))
    assert result.model_dump(exclude={"status", "rationale"}) == generic.model_dump(
        exclude={"status", "rationale"}
    )


def test_current_binding_still_awards_the_same_points():
    result = _estimate_criterion(criterion(), fact("a" * 64))
    assert result.status == "CONFIRMED"
    assert (result.estimated_points, result.lower_points, result.upper_points) == (5, 5, 5)


def test_request_does_not_route_another_binding_to_the_current_criterion():
    # A present binding can belong to a different row with the same metric.
    # Keep that selection guard: the direct stale label is not a new fallback.
    request = QuantitativeEstimateRequest(
        ruleset_version="SYN", criteria=[criterion()], facts=[fact("b" * 64)],
        source_validation_status="SOURCE_VALIDATED", activation_status="AUTO_ACTIVE",
    )
    result = estimate_quantitative_score(request)
    assert result.overall_status == "UNSCORABLE"
    assert result.criteria[0].status == "UNSCORABLE"
    assert result.estimated_points is None
    assert (result.lower_points, result.upper_points, result.unscorable_points) == (0, 5, 5)
    assert result.criteria[0].evidence_reference is None
