from __future__ import annotations

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord,
    build_quantitative_candidate_profile,
    validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    ScoreBracket,
    _numeric_bracket_matches,
    _points_for_value,
    _rule_error,
)


def synthetic_singleton(*, min_inclusive=True, max_inclusive=True, literal=None):
    def anchor(text):
        return {"attachment_id": "SYN-ATT", "page": 1, "section": "SYN-TABLE", "quote": text, "confidence": 0.99}

    row = literal or "0건 이상 0건 이하 0점"
    source = "\n".join(["실적 건수 5점", row, "총점 5점"])
    payload = ExtractionPayload.model_validate({
        "document_type": "RFP", "requirements": [], "missing_or_unreadable": [],
        "summary": "Synthetic scoring fixture", "quantitative_tables": [{
            "table_id": "SYN-TABLE", "label": "Synthetic table", "total_points": 5,
            "minimum_score": None, "minimum_evidence": None, "ambiguity_reason": None,
            "total_evidence": anchor("총점 5점"), "criteria": [{
                "criterion_id": "SYN-COUNT", "label": "실적 건수", "criterion_literal": "실적 건수 5점",
                "max_points": 5, "scoring_method": "BRACKET", "metric": "PERFORMANCE_COUNT", "unit": "건",
                "threshold": None, "formula_literal": None, "ambiguity_reason": None,
                "required_evidence": ["company.performance.count"], "evidence": anchor("실적 건수 5점"),
                "brackets": [{"label": "SYN-ZERO", "literal": row, "min_value": 0, "max_value": 0,
                              "min_inclusive": min_inclusive, "max_inclusive": max_inclusive,
                              "points": 0, "evidence": anchor(row)}],
            }],
        }],
    })
    return payload, source


def candidate_profile(**kwargs):
    payload, source = synthetic_singleton(**kwargs)
    return build_quantitative_candidate_profile({"SYN-ATT": payload}, {"SYN-ATT": source}, expected_attachment_ids={"SYN-ATT"})


def test_closed_singleton_validates_and_persisted_record_roundtrips():
    payload, source = synthetic_singleton()
    profile = candidate_profile()
    assert profile.status == "AVAILABLE"
    assert len(profile.available_candidates) == 1
    record = validate_quantitative_attachment_extraction(payload, source_text=source, attachment_id="SYN-ATT",
                                                        document_sha256="a" * 64, manifest_sha256="b" * 64)
    assert record.status == "AVAILABLE"
    assert ValidatedQuantitativeAttachmentRecord.model_validate(record.model_dump(mode="json")) == record


@pytest.mark.parametrize("left,right", [(False, True), (True, False), (False, False)])
def test_exclusive_singleton_remains_invalid_in_both_layers(left, right):
    profile = candidate_profile(min_inclusive=left, max_inclusive=right)
    assert "INVALID_BRACKET_BOUNDS" in {issue.code for issue in profile.issues}
    assert not profile.available_candidates
    with pytest.raises(ValidationError):
        ScoreBracket(bracket_id="SYN-ZERO", label="zero", min_value=0, max_value=0,
                     min_inclusive=left, max_inclusive=right, points=0)
    payload, source = synthetic_singleton()
    record = validate_quantitative_attachment_extraction(payload, source_text=source, attachment_id="SYN-ATT",
                                                        document_sha256="a" * 64, manifest_sha256="b" * 64)
    raw = record.model_dump(mode="json")
    raw["available_candidates"][0]["brackets"][0].update(min_inclusive=left, max_inclusive=right)
    with pytest.raises(ValidationError, match="bracket bounds are invalid"):
        ValidatedQuantitativeAttachmentRecord.model_validate(raw)


def test_singleton_keeps_independent_number_and_comparator_failures():
    profile = candidate_profile(literal="실적없음")
    codes = {issue.code for issue in profile.issues}
    assert "INVALID_BRACKET_BOUNDS" not in codes
    assert {"BRACKET_NUMBER_MISMATCH", "BRACKET_COMPARATOR_MISMATCH"} <= codes
    assert not profile.available_candidates


@pytest.mark.parametrize("value,matched", [(-0.01, False), (0, True), (0.01, False)])
def test_existing_evaluator_matches_only_the_singleton_point(value, matched):
    bracket = ScoreBracket(bracket_id="SYN-ZERO", label="zero", min_value=0, max_value=0,
                           min_inclusive=True, max_inclusive=True, points=0)
    assert _numeric_bracket_matches(bracket, value) is matched


def test_existing_evaluator_executes_a_contiguous_singleton_partition():
    criterion = QuantitativeCriterion(
        criterion_id="SYN-C", category="SYN", label="synthetic", max_points=5,
        metric_key="SYN-VALUE", formula_type="BRACKET", formula="SYN bracket partition",
        required_evidence_keys=["SYN-EVIDENCE"],
        source_anchor={"document_label": "SYN-DOC", "section": "SYN-SECTION", "page": 1},
        brackets=[
            ScoreBracket(bracket_id="SYN-NEG", label="negative", max_value=0, max_inclusive=False, points=1),
            ScoreBracket(bracket_id="SYN-ZERO", label="zero", min_value=0, max_value=0,
                         min_inclusive=True, max_inclusive=True, points=0),
            ScoreBracket(bracket_id="SYN-POS", label="positive", min_value=0, min_inclusive=False, points=5),
        ],
    )
    assert _rule_error(criterion) is None
    assert [_points_for_value(criterion, value) for value in (-1, 0, 1)] == [1, 0, 5]


def test_neighboring_closed_ranges_still_overlap():
    payload, source = synthetic_singleton()
    candidate = payload.quantitative_tables[0].criteria[0]
    rows = []
    for low, high, points in [(6, 10, 2), (10, 14, 3)]:
        row = f"{low}건 이상 {high}건 이하 {points}점"
        rows.append(candidate.brackets[0].model_copy(update={
            "min_value": low, "max_value": high, "points": points, "literal": row,
            "evidence": candidate.brackets[0].evidence.model_copy(update={"quote": row}),
        }))
        source += "\n" + row
    candidate.brackets = rows
    profile = build_quantitative_candidate_profile({"SYN-ATT": payload}, {"SYN-ATT": source}, expected_attachment_ids={"SYN-ATT"})
    assert "OVERLAPPING_BRACKETS" in {issue.code for issue in profile.issues}
    assert not profile.available_candidates


def test_reversed_bounds_still_fail():
    payload, source = synthetic_singleton(literal="1건 이상 0건 이하 0점")
    payload.quantitative_tables[0].criteria[0].brackets[0].min_value = 1
    profile = build_quantitative_candidate_profile({"SYN-ATT": payload}, {"SYN-ATT": source}, expected_attachment_ids={"SYN-ATT"})
    assert "INVALID_BRACKET_BOUNDS" in {issue.code for issue in profile.issues}
    assert not profile.available_candidates


def test_singleton_does_not_hide_missing_criterion_literal():
    payload, source = synthetic_singleton()
    payload.quantitative_tables[0].criteria[0].criterion_literal = "SYN-MISSING-CRITERION 5점"
    profile = build_quantitative_candidate_profile({"SYN-ATT": payload}, {"SYN-ATT": source}, expected_attachment_ids={"SYN-ATT"})
    assert "CRITERION_LITERAL_MISMATCH" in {issue.code for issue in profile.issues}
    assert not profile.available_candidates
