"""Synthetic printed intervals retain both bounds and reject lossy CASE rows."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord, _CLOSED_COUNT_BRACKET_UNITS,
    merge_validated_quantitative_records, validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QuantitativeFact, _CANONICAL_METRIC_REGISTRY,
    estimate_quantitative_score, quantitative_request_from_candidate_profile,
)

ATT = "SYN-PRINTED-INTERVAL"


def fixture(*, metric="PERSONNEL_COUNT", unit="명", inline=False):
    def anchor(quote):
        return dict(attachment_id=ATT, page=1, section="SYN-TABLE", quote=quote, confidence=1)
    heading = "SYN-항목 배점 8점"
    specs = [(None, 3, False, False, 1, f"3{unit} 미만"),
             (3, 5, True, True, 4, f"3~5{unit}"),
             (6, 8, True, True, 6, f"6{unit}∼8{unit}"),
             (9, None, True, False, 8, f"9{unit} 이상")]
    rows = []
    for index, (lower, upper, li, ui, points, condition) in enumerate(specs):
        literal = f"{condition} {points}점" if inline else f"{condition}\n{points}점"
        rows.append(dict(label=f"SYN-ROW-{index}", literal=literal, min_value=lower, max_value=upper,
                         min_inclusive=li, max_inclusive=ui, points=points, evidence=anchor(literal)))
    candidate = dict(criterion_id="SYN-CRITERION", label="SYN-항목", criterion_literal=heading,
        max_points=8, metric=metric, unit=unit, scoring_method="BRACKET", brackets=rows,
        threshold=None, cases=[], formula_literal=None, recognition_conditions=[],
        required_evidence=[_CANONICAL_METRIC_REGISTRY[metric]["fact_key"]],
        evidence=anchor(heading), ambiguity_reason=None)
    payload = dict(document_type="RFP", requirements=[], summary="SYN", missing_or_unreadable=[],
        quantitative_table_not_applicable=None, quantitative_tables=[dict(table_id="SYN-TABLE",
            label="SYN-정량", total_points=8, total_evidence=anchor("SYN-정량 합계 8점"),
            minimum_score=None, minimum_evidence=None, ambiguity_reason=None, criteria=[candidate])])
    source = "\n".join([heading, *(row["literal"] for row in rows), "SYN-정량 합계 8점"])
    return payload, source


def validate(raw, source):
    return validate_quantitative_attachment_extraction(ExtractionPayload.model_validate(raw),
        source_text=source, attachment_id=ATT, document_sha256="a" * 64, manifest_sha256="b" * 64)


@pytest.mark.parametrize("inline", [False, True])
def test_closed_personnel_intervals_survive_storage_and_score_exact_integer_edges(inline):
    raw, source = fixture(inline=inline)
    original = deepcopy(raw)
    record = validate(raw, source)
    assert record.status == "AVAILABLE", [i.code for i in record.issues]
    assert raw == original
    stored = ValidatedQuantitativeAttachmentRecord.model_validate_json(record.model_dump_json())
    profile = merge_validated_quantitative_records([stored], expected_documents={ATT: "a" * 64}, manifest_sha256="b" * 64)
    request = quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    criterion = request.criteria[0]
    for value, expected in [(0, 1), (2, 1), (3, 4), (5, 4), (6, 6), (8, 6), (9, 8), (100, 8)]:
        request.facts = [QuantitativeFact(metric_key="company.personnel.count", value=value,
            status="CONFIRMED", evidence_key="company.personnel.count", evidence_reference="SYN-verified",
            evidence_sha256="c" * 64, fact_binding_sha256=criterion.fact_binding_sha256, confidence=1)]
        assert estimate_quantitative_score(request).estimated_points == expected
    request.facts = []
    assert estimate_quantitative_score(request).estimated_points is None


@pytest.mark.parametrize("mutation", ["upper-lost", "exclusive", "wrong-award", "wrong-unit", "unknown-unit",
    "fractional", "reverse", "missing-award", "borrowed-bound", "two-intervals", "continuous-metric"])
def test_unproven_ranges_remain_blocked(mutation):
    raw, source = fixture()
    candidate = raw["quantitative_tables"][0]["criteria"][0]
    row = candidate["brackets"][1]
    if mutation == "upper-lost": row["max_value"] = None
    elif mutation == "exclusive": row["max_inclusive"] = False
    elif mutation == "wrong-award": row["points"] = 5
    elif mutation == "wrong-unit": candidate["unit"] = "개"
    elif mutation == "unknown-unit": candidate["unit"] = "SYN-UNKNOWN"
    elif mutation == "fractional": row["min_value"] = 3.5
    elif mutation == "reverse": row.update(min_value=5, max_value=3)
    elif mutation == "continuous-metric":
        candidate.update(metric="FINANCIAL_RATIO", unit="%", required_evidence=["company.financial.ratio"])
    else:
        before = row["literal"]
        after = {"missing-award":"3~5명", "borrowed-bound":"3명\n4점", "two-intervals":"3~5명 또는 6~8명\n4점"}[mutation]
        row["literal"] = row["evidence"]["quote"] = after
        source = source.replace(before, after)
    record = validate(raw, source)
    assert not record.available_candidates
    assert any(i.code in {"BRACKET_COMPARATOR_MISMATCH", "INVALID_BRACKET_BOUNDS", "BRACKET_NUMBER_MISMATCH"} for i in record.issues)


def test_reloading_a_stored_proof_rejects_erased_or_exclusive_upper_bound():
    raw, source = fixture()
    record = validate(raw, source)
    assert record.status == "AVAILABLE"
    for change in ({"max_value":None}, {"max_inclusive":False}, {"points":5}):
        data = record.model_dump(mode="json")
        data["available_candidates"][0]["brackets"][1].update(change)
        with pytest.raises(ValidationError):
            ValidatedQuantitativeAttachmentRecord.model_validate(data)


def test_count_units_are_limited_to_the_existing_metric_registry():
    for metric, units in _CLOSED_COUNT_BRACKET_UNITS.items():
        assert units == frozenset(_CANONICAL_METRIC_REGISTRY[metric]["unit_scales"])


@pytest.mark.parametrize("metric,unit", [(metric, unit) for metric, units in _CLOSED_COUNT_BRACKET_UNITS.items() for unit in sorted(units)])
def test_closed_intervals_keep_registered_count_dimensions(metric, unit):
    raw, source = fixture(metric=metric, unit=unit)
    # Isolate the new complete-row grammar from existing open-tail grammars.
    candidate = raw["quantitative_tables"][0]["criteria"][0]
    candidate["brackets"] = [candidate["brackets"][1]]
    record = validate(raw, source)
    assert record.status == "AVAILABLE", [i.code for i in record.issues]
    assert record.available_candidates[0].brackets[0].max_value == 5


def test_explicit_two_sided_percentage_rows_preserve_exclusive_upper_edges():
    raw, _ = fixture(metric="FINANCIAL_RATIO", unit="%")
    candidate = raw["quantitative_tables"][0]["criteria"][0]
    specs = [(None, 15, "15% 미만"), (15, 35, "15% 이상 35% 미만"),
             (35, 75, "35% 이상 75% 미만"), (75, None, "75% 이상")]
    for row, (lower, upper, condition) in zip(candidate["brackets"], specs, strict=True):
        row.update(min_value=lower, max_value=upper, min_inclusive=lower is not None,
                   max_inclusive=False, literal=f"{condition}\n{row['points']}점")
        row["evidence"]["quote"] = row["literal"]
    source = "\n".join([candidate["criterion_literal"], *(r["literal"] for r in candidate["brackets"]), "SYN-정량 합계 8점"])
    record = validate(raw, source)
    assert record.status == "AVAILABLE", [i.code for i in record.issues]
    assert record.available_candidates[0].brackets[1].max_value == 35
    assert record.available_candidates[0].brackets[1].max_inclusive is False
    raw["quantitative_tables"][0]["criteria"][0]["brackets"][1]["max_value"] = None
    rejected = validate(raw, source)
    assert "BRACKET_COMPARATOR_MISMATCH" in {i.code for i in rejected.issues}
    assert not rejected.available_candidates


def test_dropped_upper_bounds_are_not_repaired_in_old_case_output():
    raw, source = fixture()
    candidate = raw["quantitative_tables"][0]["criteria"][0]
    candidate["scoring_method"] = "CASE_TABLE"
    candidate["cases"] = [dict(literal=row["literal"], operator="LT" if index==0 else "GTE",
        comparison_value=row["max_value"] if index==0 else row["min_value"], comparison_upper_value=None,
        category_values=[], award_kind="POINTS", award_value=row["points"], row_order=index+1, evidence=row["evidence"])
        for index,row in enumerate(candidate.pop("brackets"))]
    candidate["brackets"] = []
    before = deepcopy(raw)
    record = validate(raw, source)
    assert raw == before and not record.available_candidates
    assert {"CASE_NUMBER_MISMATCH", "CASE_COMPARATOR_MISMATCH", "CASE_TABLE_NOT_DETERMINISTIC"}.issubset({i.code for i in record.issues})
