from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_formula import case_table_points
from pai_loop.quantitative_rule_extraction import (
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    _candidate_bound_unit_scales_are_consistent,
    _candidate_unit_is_source_bound,
    _case_percent_award_input_literal,
    _compiled_case_table_contract,
    quantitative_request_from_candidate_profile,
)


AID = "SYN-PERCENT-AWARD-ATTACHMENT"


def make_payload(*, percent=True, unit="명", format="inline", financial=False):
    def anchor(quote):
        return dict(attachment_id=AID, page=1, section="SYN 평가표",
                    quote=quote, confidence=0.99)

    metric = "FINANCIAL_RATIO" if financial else "PERSONNEL_COUNT"
    key = "company.financial.ratio" if financial else "company.personnel.count"
    label = "SYN 재무비율" if financial else "SYN 전문인력"
    header = f"{label} 5점"
    rows = [("GTE", 80, 5), ("GTE", 60, 4), ("GTE", 0, 1)] if financial else [
        ("GTE", 5, 5), ("EQ", 4, 4), ("EQ", 3, 3), ("EQ", 2, 2), ("LTE", 1, 1),
    ]
    cases = []
    for order, (operator, value, points) in enumerate(rows, 1):
        suffix = {"GTE": " 이상", "EQ": "", "LTE": " 이하"}[operator]
        condition = f"{value}{unit}{suffix}"
        award_value = points * 20 if percent else points
        award = f"배점의 {award_value}%" if percent else f"{award_value}점"
        if format == "split":
            literal = condition + "\n" + award
        elif format == "bare_percent":
            literal = condition + "\n" + f"{award_value}%"
        elif format == "compact":
            literal = condition.replace(" ", "") + award.replace(" ", "")
        elif format == "word_percent":
            literal = condition + " " + award.replace("%", "퍼센트")
        else:
            literal = condition + " " + award
        cases.append(dict(literal=literal, operator=operator, comparison_value=value,
            category_values=[], award_kind="PERCENT_OF_MAX" if percent else "POINTS",
            award_value=award_value, row_order=order, evidence=anchor(literal)))
    criterion = dict(criterion_id="SYN-CRITERION", label=label, criterion_literal=header,
        max_points=5, scoring_method="CASE_TABLE", metric=metric, unit=unit,
        brackets=[], threshold=None, formula_literal=None, cases=cases,
        recognition_conditions=[], required_evidence=[key], evidence=anchor(header),
        ambiguity_reason=None)
    total = "정량평가 총점 5점"
    table = dict(table_id="SYN-TABLE", label="SYN 평가표", criteria=[criterion],
        total_points=5, total_evidence=anchor(total), minimum_score=None,
        minimum_evidence=None, ambiguity_reason=None)
    raw = dict(document_type="RFP", summary="SYN 단위와 배점 구분",
        requirements=[], quantitative_tables=[table],
        quantitative_table_not_applicable=None, missing_or_unreadable=[])
    source = "\n".join([header, *(case["literal"] for case in cases), total])
    return raw, source


def validated(raw, source):
    payload = ExtractionPayload.model_validate(raw)
    sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(payload, source_text=source,
        attachment_id=AID, document_sha256=sha, manifest_sha256="b"*64)
    profile = merge_validated_quantitative_records([record],
        expected_documents={AID: sha}, manifest_sha256="b"*64)
    return record, profile, quantitative_request_from_candidate_profile(profile)


@pytest.mark.parametrize("format", ["inline", "split", "bare_percent", "compact", "word_percent"])
def test_count_percent_award_uses_input_unit_and_keeps_exact_score(format):
    raw, source = make_payload(format=format)
    before = deepcopy(raw)
    record, profile, request = validated(raw, source)
    assert record.status == profile.status == "AVAILABLE"
    assert request.activation_status == "AUTO_ACTIVE"
    assert request.activation_reasons == []
    program = _compiled_case_table_contract(profile.available_candidates[0])
    assert [case_table_points(program, value) for value in (0, 1, 2, 3, 4, 5, 9)] == [1, 1, 2, 3, 4, 5, 5]
    assert case_table_points(program, None) is None
    assert raw == before


def test_points_and_percent_awards_have_the_same_input_unit_and_scores():
    results = []
    for percent in (False, True):
        raw, source = make_payload(percent=percent)
        record, profile, request = validated(raw, source)
        assert record.status == "AVAILABLE" and request.activation_status == "AUTO_ACTIVE"
        program = _compiled_case_table_contract(profile.available_candidates[0])
        results.append([case_table_points(program, value) for value in range(8)])
    assert results[0] == results[1]


def test_financial_input_percent_is_retained_separately_from_percent_award():
    raw, source = make_payload(unit="%", financial=True)
    record, profile, request = validated(raw, source)
    assert record.status == "AVAILABLE" and request.activation_status == "AUTO_ACTIVE"
    candidate = profile.available_candidates[0]
    assert _case_percent_award_input_literal(candidate, candidate.cases[0]) == "80% 이상"
    assert _candidate_unit_is_source_bound(candidate)
    assert _candidate_bound_unit_scales_are_consistent(candidate)


def test_award_percent_cannot_supply_a_missing_financial_input_unit():
    raw, source = make_payload(unit="%", financial=True)
    for case in raw["quantitative_tables"][0]["criteria"][0]["cases"]:
        old = case["literal"]
        case["literal"] = old.replace("% 이상", " 이상")
        case["evidence"]["quote"] = case["literal"]
        source = source.replace(old, case["literal"])
    record, _, request = validated(raw, source)
    assert record.status == "AVAILABLE"
    assert request.activation_status == "REVIEW_REQUIRED"
    assert "UNIT_NOT_SOURCE_BOUND" in request.activation_reasons


@pytest.mark.parametrize("conflicting_unit", ["대", "%", "년"])
def test_actual_input_unit_conflicts_remain_blocked(conflicting_unit):
    raw, source = make_payload()
    c = raw["quantitative_tables"][0]["criteria"][0]
    row = c["cases"][1]
    old = row["literal"]
    row["literal"] = old.replace("4명", "4" + conflicting_unit)
    row["evidence"]["quote"] = row["literal"]
    source = source.replace(old, row["literal"])
    record, _, request = validated(raw, source)
    assert record.status == "AVAILABLE"
    assert request.activation_status == "REVIEW_REQUIRED"
    assert "BOUND_UNIT_INCONSISTENT" in request.activation_reasons


@pytest.mark.parametrize("literal", [
    "5명 이상 배점의 80%",  # wrong award
    "4명 이상 배점의 100%",  # wrong comparison
    "5명 미만 배점의 100%",  # inverted comparison
    "5명 이상 90% 배점의 100%",  # competing numeric award
    "5명 이상 배점의 100% 또는 80%",  # alternative award
    "5명 이상 배점의 100% 참고",  # nonterminal award
    "배점의 100% 5명 이상",  # reversed source ownership
    "100명 이상",  # award number appears only as input
    "5명 이상 배점의 -100%",  # invalid percentage
    "5명 이상 배점의 100점",  # different award kind
])
def test_percent_award_separation_requires_exact_condition_and_award(literal):
    raw, source = make_payload()
    _, profile, _ = validated(raw, source)
    candidate = profile.available_candidates[0]
    row = candidate.cases[0]
    changed = row.model_copy(update={"literal": literal,
        "evidence": row.evidence.model_copy(update={"quote": literal})})
    assert _case_percent_award_input_literal(candidate, changed) is None
    mutated = candidate.model_copy(update={"cases": (changed, *candidate.cases[1:])})
    assert not _candidate_unit_is_source_bound(mutated)
    assert not _candidate_bound_unit_scales_are_consistent(mutated)


@pytest.mark.parametrize("quote", ["5명 이상", "배점의 100%", "SYN 다른 행", ""])
def test_percent_award_separation_never_extends_partial_evidence(quote):
    raw, source = make_payload()
    _, profile, _ = validated(raw, source)
    candidate = profile.available_candidates[0]
    row = candidate.cases[0]
    changed = row.model_copy(update={"evidence": row.evidence.model_copy(update={"quote": quote})})
    assert _case_percent_award_input_literal(candidate, changed) is None


def test_wrong_extracted_award_does_not_activate_even_when_input_has_that_number():
    raw, source = make_payload(unit="%", financial=True)
    raw["quantitative_tables"][0]["criteria"][0]["cases"][0]["award_value"] = 80
    record, _, request = validated(raw, source)
    assert request.activation_status == "REVIEW_REQUIRED"
    assert "UNIT_NOT_SOURCE_BOUND" in request.activation_reasons or record.status != "AVAILABLE"
