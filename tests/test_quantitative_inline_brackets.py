from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord,
    _record_fingerprint_data,
    _targeted_record_fingerprint_revisions,
    build_quantitative_candidate_profile,
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint,
)


ATTACHMENT = "SYN-INLINE-BINARY"
DOCUMENT_SHA = "a" * 64
MANIFEST_SHA = "b" * 64


def anchor(quote: str) -> dict:
    return {"attachment_id": ATTACHMENT, "page": 1, "section": "SYN-정량평가",
            "quote": quote, "confidence": 0.99}


def fixture(first: str = "이상", second: str = "미만") -> tuple[dict, str]:
    literal = f"기준비율 60% {first} 4점, {second} 2점"
    criterion = f"SYN-재무비율(4점) : {literal}"
    rows = []
    for operator, points in ((first, 4), (second, 2)):
        lower = operator in {"이상", "초과"}
        rows.append({"label": f"SYN-{operator}", "literal": literal,
                     "min_value": 60 if lower else None,
                     "max_value": None if lower else 60,
                     "min_inclusive": operator == "이상",
                     "max_inclusive": operator == "이하", "points": points,
                     "evidence": anchor(literal)})
    candidate = {
        "criterion_id": "SYN-RATIO-1", "label": "SYN-재무비율",
        "criterion_literal": criterion, "max_points": 4,
        "scoring_method": "BRACKET", "metric": "FINANCIAL_RATIO", "unit": "%",
        "brackets": rows, "threshold": None, "formula_literal": None,
        "required_evidence": ["company.financial.ratio"],
        "evidence": anchor(criterion), "ambiguity_reason": None,
    }
    payload = {
        "document_type": "RFP", "requirements": [],
        "quantitative_tables": [{
            "table_id": "SYN-TABLE-1", "label": "SYN-정량평가", "criteria": [candidate],
            "total_points": 4, "total_evidence": anchor("정량평가 총점 4점"),
            "minimum_score": None, "minimum_evidence": None, "ambiguity_reason": None,
        }],
        "quantitative_table_not_applicable": None, "missing_or_unreadable": [],
        "summary": "SYN-정량평가 합성 입력",
    }
    return payload, "\n".join(("SYN-정량평가", criterion, "정량평가 총점 4점"))


def criterion(payload: dict) -> dict:
    return payload["quantitative_tables"][0]["criteria"][0]


def profile(payload: dict, source: str):
    return build_quantitative_candidate_profile(
        {ATTACHMENT: ExtractionPayload.model_validate(payload)}, {ATTACHMENT: source},
        expected_attachment_ids={ATTACHMENT},
    )


def record(payload: dict, source: str):
    return validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(payload), source_text=source,
        attachment_id=ATTACHMENT, document_sha256=DOCUMENT_SHA, manifest_sha256=MANIFEST_SHA,
    )


def codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def change_literal(payload: dict, source: str, literal: str) -> str:
    candidate = criterion(payload)
    old = candidate["brackets"][0]["literal"]
    for row in candidate["brackets"]:
        row["literal"] = literal
        row["evidence"] = anchor(literal)
    candidate["criterion_literal"] = candidate["criterion_literal"].replace(old, literal)
    candidate["evidence"] = anchor(candidate["criterion_literal"])
    return source.replace(old, literal)


@pytest.mark.parametrize("first,second", [("이상", "미만"), ("초과", "이하"),
                                         ("미만", "이상"), ("이하", "초과")])
def test_exact_complementary_inline_pairs_preserve_every_extracted_value(first, second):
    payload, source = fixture(first, second)
    before = deepcopy(payload)
    result = profile(payload, source)
    assert result.status == "AVAILABLE", codes(result)
    assert payload == before
    stored = record(payload, source)
    restored = ValidatedQuantitativeAttachmentRecord.model_validate_json(stored.model_dump_json())
    merged = merge_validated_quantitative_records([restored],
        expected_documents={ATTACHMENT: DOCUMENT_SHA}, manifest_sha256=MANIFEST_SHA)
    assert merged.status == "AVAILABLE", codes(merged)
    assert [row.model_dump(mode="json") for row in restored.available_candidates[0].brackets] == criterion(payload)["brackets"]
    assert restored.validation_fingerprint_sha256 == validated_quantitative_record_fingerprint(restored)


def test_inline_pair_order_is_not_an_unstated_scoring_input():
    payload, source = fixture()
    criterion(payload)["brackets"].reverse()
    assert profile(payload, source).status == "AVAILABLE"


@pytest.mark.parametrize("mutation", ["award-swap", "missing-low", "wrong-high-only", "duplicate-row",
                                      "third-row", "threshold", "inclusive", "two-bounds", "unit", "metric"])
def test_incorrect_inline_numeric_structures_fail_closed(mutation):
    payload, source = fixture()
    candidate = criterion(payload)
    rows = candidate["brackets"]
    if mutation == "award-swap":
        rows[0]["points"], rows[1]["points"] = 2, 4
    elif mutation == "missing-low":
        rows.pop()
    elif mutation == "wrong-high-only":
        rows.pop()
        rows[0]["points"] = 2
    elif mutation == "duplicate-row":
        rows[1] = deepcopy(rows[0])
    elif mutation == "third-row":
        rows.append(deepcopy(rows[0]))
    elif mutation == "threshold":
        rows[1]["max_value"] = 61
    elif mutation == "inclusive":
        rows[1]["max_inclusive"] = True
    elif mutation == "two-bounds":
        rows[1]["min_value"] = 0
        rows[1]["min_inclusive"] = True
    elif mutation == "unit":
        candidate["unit"] = "배"
    elif mutation == "metric":
        candidate["metric"] = "PERFORMANCE_COUNT"
        candidate["required_evidence"] = ["company.performance.count"]
    result = profile(payload, source)
    assert result.status == "INCOMPLETE", codes(result)
    assert "BRACKET_COMPARATOR_MISMATCH" in codes(result)
    assert not result.available_candidates


@pytest.mark.parametrize("literal", [
    "기준비율 60% 이상 4점, 이하 2점",
    "기준비율 60% 이상 4점, 이상 2점",
    "기준비율 60% 이상 4점, 미만 2점, 20% 미만 0점",
    "기준비율 60% 이상 4점, 미만 2점 또는 가산 1점",
    "기준비율 60% 이상 4점; 미만 2점",
    "기준비율 60% 이상 4점,\n미만 2점",
    "기준비율 60% 이상 4점,\n\n미만 2점",
    "기준비율 60배 이상 4점, 미만 2점",
    "기준비율 -60% 이상 4점, 미만 2점",
])
def test_unsupported_or_ambiguous_inline_language_is_never_partially_accepted(literal):
    payload, source = fixture()
    source = change_literal(payload, source, literal)
    result = profile(payload, source)
    assert "BRACKET_COMPARATOR_MISMATCH" in codes(result)
    assert not result.available_candidates


@pytest.mark.parametrize("mutation", ["duplicate-source", "separate-criterion", "wrong-maximum",
                                      "broad-evidence", "another-attachment", "low-confidence", "altered-quote"])
def test_inline_clause_requires_its_own_unique_current_source(mutation):
    payload, source = fixture()
    candidate = criterion(payload)
    if mutation == "duplicate-source":
        source += "\n" + candidate["criterion_literal"]
    elif mutation == "separate-criterion":
        candidate["criterion_literal"] = "SYN-재무비율(4점)"
        candidate["evidence"] = anchor(candidate["criterion_literal"])
    elif mutation == "wrong-maximum":
        source = source.replace("SYN-재무비율(4점)", "SYN-재무비율(6점)")
        candidate["criterion_literal"] = candidate["criterion_literal"].replace("(4점)", "(6점)")
        candidate["evidence"] = anchor(candidate["criterion_literal"])
    elif mutation == "broad-evidence":
        candidate["evidence"] = anchor(source)
    elif mutation == "another-attachment":
        candidate["brackets"][1]["evidence"]["attachment_id"] = "SYN-OTHER-ATTACHMENT"
    elif mutation == "low-confidence":
        candidate["brackets"][1]["evidence"]["confidence"] = 0.1
    elif mutation == "altered-quote":
        candidate["brackets"][1]["evidence"]["quote"] = "기준비율 61% 이상 4점, 미만 2점"
    result = profile(payload, source)
    assert result.status == "INCOMPLETE", codes(result)
    assert not result.available_candidates


@pytest.mark.parametrize("other_table", [False, True])
def test_different_criteria_cannot_claim_the_same_inline_clause(other_table):
    payload, source = fixture()
    extra = deepcopy(criterion(payload))
    extra["criterion_id"] = "SYN-RATIO-2"
    table = payload["quantitative_tables"][0]
    if other_table:
        other = deepcopy(table)
        other["table_id"] = "SYN-TABLE-2"
        other["criteria"] = [extra]
        payload["quantitative_tables"].append(other)
    else:
        table["criteria"].append(extra)
        table["total_points"] = 8
        table["total_evidence"] = anchor("정량평가 총점 8점")
        source = source.replace("정량평가 총점 4점", "정량평가 총점 8점")
    result = profile(payload, source)
    assert "INLINE_BRACKET_CLAIM_COLLISION" in codes(result)
    assert not result.available_candidates


@pytest.mark.parametrize("mutation", ["missing-total", "ambiguous-table", "missing-source-data"])
def test_proved_inline_clause_does_not_remove_other_table_or_document_blockers(mutation):
    payload, source = fixture()
    table = payload["quantitative_tables"][0]
    if mutation == "missing-total":
        table["total_points"] = table["total_evidence"] = None
        expected = "TABLE_TOTAL_INCOMPLETE"
    elif mutation == "ambiguous-table":
        table["ambiguity_reason"] = "SYN-별도 평가영역의 근거가 불명확함"
        expected = "AMBIGUOUS_TABLE"
    else:
        payload["missing_or_unreadable"] = ["SYN-별도 정량평가표 일부를 읽을 수 없음"]
        expected = "EXTRACTION_DECLARED_INCOMPLETE"
    result = profile(payload, source)
    assert result.status != "AVAILABLE"
    assert expected in codes(result)
    assert "BRACKET_COMPARATOR_MISMATCH" not in codes(result)


@pytest.mark.parametrize("mutation", ["award-swap", "missing-low", "unit", "ownership"])
def test_persisted_inline_record_cannot_bypass_the_shared_proof(mutation):
    payload, source = fixture()
    data = record(payload, source).model_dump(mode="json")
    candidate = data["available_candidates"][0]
    if mutation == "award-swap":
        candidate["brackets"][0]["points"], candidate["brackets"][1]["points"] = 2, 4
    elif mutation == "missing-low":
        candidate["brackets"].pop()
    elif mutation == "unit":
        candidate["unit"] = "배"
    else:
        candidate["criterion_literal"] = "SYN-재무비율 4점"
        candidate["evidence"]["quote"] = candidate["criterion_literal"]
    data["validation_fingerprint_sha256"] = _record_fingerprint_data({
        key: value for key, value in data.items() if key != "validation_fingerprint_sha256"})
    with pytest.raises(ValidationError):
        ValidatedQuantitativeAttachmentRecord.model_validate(data)
    result = merge_validated_quantitative_records([data],
        expected_documents={ATTACHMENT: DOCUMENT_SHA}, manifest_sha256=MANIFEST_SHA)
    assert "RECORD_INVARIANT_VIOLATION" in codes(result)
    assert not result.available_candidates


def legacy_digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def test_targeted_fingerprint_revalidates_only_inline_shapes_and_comparator_issues():
    payload, source = fixture()
    fresh = record(payload, source)
    canonical = fresh.model_dump(mode="json", exclude={"validation_fingerprint_sha256"})
    assert _targeted_record_fingerprint_revisions(canonical) == ("inline-binary-bracket-proof-v1",)
    legacy = fresh.model_copy(update={"validation_fingerprint_sha256": legacy_digest(canonical)})
    merged = merge_validated_quantitative_records([legacy],
        expected_documents={ATTACHMENT: DOCUMENT_SHA}, manifest_sha256=MANIFEST_SHA)
    assert "VALIDATION_FINGERPRINT_MISMATCH" in codes(merged)

    incorrect = deepcopy(payload)
    criterion(incorrect)["brackets"][1]["max_inclusive"] = True
    failed = record(incorrect, source)
    failed_canonical = failed.model_dump(mode="json", exclude={"validation_fingerprint_sha256"})
    assert "inline-binary-bracket-proof-v1" in _targeted_record_fingerprint_revisions(failed_canonical)
    old_failed = failed.model_copy(update={"validation_fingerprint_sha256": legacy_digest(failed_canonical)})
    merged = merge_validated_quantitative_records([old_failed],
        expected_documents={ATTACHMENT: DOCUMENT_SHA}, manifest_sha256=MANIFEST_SHA)
    assert "VALIDATION_FINGERPRINT_MISMATCH" in codes(merged)

    # Ordinary independently anchored rows retain their previous fingerprint.
    candidate = criterion(payload)
    old = candidate["criterion_literal"]
    candidate["criterion_literal"] = "SYN-재무비율 4점"
    candidate["evidence"] = anchor(candidate["criterion_literal"])
    for row, text in zip(candidate["brackets"], ("60% 이상 4점", "60% 미만 2점"), strict=True):
        row["literal"] = text
        row["evidence"] = anchor(text)
    source = source.replace(old, "SYN-재무비율 4점\n60% 이상 4점\n60% 미만 2점")
    unrelated = record(payload, source)
    assert unrelated.status == "AVAILABLE"
    canonical = unrelated.model_dump(mode="json", exclude={"validation_fingerprint_sha256"})
    assert _targeted_record_fingerprint_revisions(canonical) == ()
    assert unrelated.validation_fingerprint_sha256 == legacy_digest(canonical)


@pytest.mark.parametrize("extra", [", 단 특정 유형은 0점", ", 90% 이상 5점"])
def test_unquoted_source_suffix_cannot_be_discarded_from_an_inline_criterion(extra):
    payload, source = fixture()
    text = criterion(payload)["criterion_literal"]
    source = source.replace(text, text + extra)
    result = profile(payload, source)
    assert "BRACKET_COMPARATOR_MISMATCH" in codes(result)
    assert not result.available_candidates


@pytest.mark.parametrize("kind", ["BRACKET", "THRESHOLD", "CASE", "FORMULA", "RECOGNITION"])
def test_another_criterion_cannot_borrow_an_inline_arm_or_its_source_quote(kind):
    payload, source = fixture()
    extra = deepcopy(criterion(payload))
    extra["criterion_id"] = "SYN-RATIO-OTHER"
    extra["criterion_literal"] = "SYN-별도비율 4점"
    extra["evidence"] = anchor(extra["criterion_literal"])
    extra["brackets"] = [deepcopy(extra["brackets"][0])]
    row = extra["brackets"][0]
    row["literal"] = "60% 이상 4점"
    row["evidence"] = anchor(row["literal"])
    if kind == "THRESHOLD":
        extra["scoring_method"] = "THRESHOLD"
        extra["brackets"] = []
        extra["threshold"] = {"literal": row["literal"], "operator": "GTE",
            "threshold_value": 60, "points_if_met": 4, "points_if_not_met": 2,
            "evidence": row["evidence"]}
    elif kind == "CASE":
        extra["scoring_method"] = "CASE_TABLE"
        extra["brackets"] = []
        extra["cases"] = [{"literal": row["literal"],
            "operator": "GTE", "comparison_value": 60, "category_values": [],
            "award_kind": "POINTS", "award_value": 4, "row_order": 1,
            "evidence": row["evidence"]}]
    elif kind == "FORMULA":
        extra["scoring_method"] = "FORMULA"
        extra["brackets"] = []
        extra["formula_literal"] = row["literal"]
        extra["evidence"] = row["evidence"]
    elif kind == "RECOGNITION":
        extra["brackets"] = []
        extra["recognition_conditions"] = [{"literal": row["literal"], "evidence": row["evidence"]}]
    table = payload["quantitative_tables"][0]
    table["criteria"].append(extra)
    table["total_points"] = 8
    table["total_evidence"] = anchor("정량평가 총점 8점")
    source = source.replace("정량평가 총점 4점", "SYN-별도비율 4점\n정량평가 총점 8점")
    result = profile(payload, source)
    assert "INLINE_BRACKET_CLAIM_COLLISION" in codes(result)
    assert not result.available_candidates


def test_matching_numbers_in_another_uniquely_quoted_criterion_are_not_a_shared_claim():
    payload, source = fixture()
    extra = deepcopy(criterion(payload))
    extra["criterion_id"] = "SYN-RATIO-SEPARATE"
    extra["criterion_literal"] = "SYN-별도비율 4점: 60% 이상 4점"
    extra["evidence"] = anchor(extra["criterion_literal"])
    extra["brackets"] = [deepcopy(extra["brackets"][0])]
    extra["brackets"][0]["literal"] = "60% 이상 4점"
    extra["brackets"][0]["evidence"] = anchor(extra["criterion_literal"])
    table = payload["quantitative_tables"][0]
    table["criteria"].append(extra)
    table["total_points"] = 8
    table["total_evidence"] = anchor("정량평가 총점 8점")
    source = source.replace("정량평가 총점 4점", extra["criterion_literal"] + "\n정량평가 총점 8점")
    result = profile(payload, source)
    assert result.status == "AVAILABLE", codes(result)
    stored = record(payload, source)
    assert len(stored.available_candidates) == 2

    # A recomputed fingerprint cannot hide replacement of the independent
    # quote by an ambiguous partial quote that also claims the inline source.
    data = stored.model_dump(mode="json")
    second = next(item for item in data["available_candidates"] if item["criterion_id"] == "SYN-RATIO-SEPARATE")
    second["brackets"][0]["evidence"]["quote"] = "60% 이상 4점"
    data["validation_fingerprint_sha256"] = _record_fingerprint_data({
        key: value for key, value in data.items() if key != "validation_fingerprint_sha256"})
    with pytest.raises(ValidationError, match="multiple criterion owners"):
        ValidatedQuantitativeAttachmentRecord.model_validate(data)


@pytest.mark.parametrize("separator", ["\r", "\v", "\f", "\u2028", "\u2029"])
def test_unicode_and_control_paragraph_boundaries_are_not_inline(separator):
    payload, source = fixture()
    literal = criterion(payload)["brackets"][0]["literal"].replace(", ", "," + separator)
    source = change_literal(payload, source, literal)
    result = profile(payload, source)
    assert "BRACKET_COMPARATOR_MISMATCH" in codes(result)
    assert not result.available_candidates


def independent_arm_fixture() -> tuple[dict, str]:
    payload, source = fixture()
    extra = deepcopy(criterion(payload))
    extra["criterion_id"] = "SYN-RATIO-INDEPENDENT"
    extra["criterion_literal"] = "SYN-재무비율 별도(4점) : 60% 이상 4점"
    extra["evidence"] = anchor(extra["criterion_literal"])
    extra["brackets"] = [deepcopy(extra["brackets"][0])]
    extra["brackets"][0]["literal"] = "60% 이상 4점"
    extra["brackets"][0]["evidence"] = anchor(extra["criterion_literal"])
    table = payload["quantitative_tables"][0]
    table["criteria"].append(extra)
    table["total_points"] = 8
    table["total_evidence"] = anchor("정량평가 총점 8점")
    source = source.replace("정량평가 총점 4점", "\n".join((
        "SYN-후속문장", extra["criterion_literal"], "정량평가 총점 8점")))
    return payload, source


def borrowed_context_quote(payload: dict, context: str) -> str:
    owner = criterion(payload)["criterion_literal"]
    first_arm = owner.split(",")[0]
    if context == "criterion-prefix":
        return first_arm
    if context == "preceding-paragraph":
        return "SYN-정량평가\n" + first_arm
    assert context == "following-paragraph"
    return owner.split("기준비율 ", 1)[1] + "\nSYN-후속문장"


@pytest.mark.parametrize("context", ["criterion-prefix", "preceding-paragraph", "following-paragraph"])
def test_partial_arm_with_surrounding_owner_text_is_a_claim_collision(context):
    payload, source = independent_arm_fixture()
    extra = payload["quantitative_tables"][0]["criteria"][1]
    extra["brackets"][0]["evidence"] = anchor(borrowed_context_quote(payload, context))
    result = profile(payload, source)
    assert result.status == "INCOMPLETE"
    assert "INLINE_BRACKET_CLAIM_COLLISION" in codes(result)
    assert not result.available_candidates


@pytest.mark.parametrize("context", ["criterion-prefix", "preceding-paragraph", "following-paragraph"])
def test_persisted_partial_arm_context_cannot_hide_cross_criterion_ownership(context):
    payload, source = independent_arm_fixture()
    original = record(payload, source)
    assert original.status == "AVAILABLE"
    data = original.model_dump(mode="json")
    extra = next(item for item in data["available_candidates"]
                 if item["criterion_id"] == "SYN-RATIO-INDEPENDENT")
    extra["brackets"][0]["evidence"]["quote"] = borrowed_context_quote(payload, context)
    data["validation_fingerprint_sha256"] = _record_fingerprint_data({
        key: value for key, value in data.items() if key != "validation_fingerprint_sha256"})
    with pytest.raises(ValidationError, match="multiple criterion owners"):
        ValidatedQuantitativeAttachmentRecord.model_validate_json(json.dumps(data))
    merged = merge_validated_quantitative_records([data],
        expected_documents={ATTACHMENT: DOCUMENT_SHA}, manifest_sha256=MANIFEST_SHA)
    assert "RECORD_INVARIANT_VIOLATION" in codes(merged)
    assert not merged.available_candidates


def test_similar_independent_criterion_prefix_and_same_arm_remain_available_after_merge():
    payload, source = independent_arm_fixture()
    result = profile(payload, source)
    assert result.status == "AVAILABLE", codes(result)
    stored = record(payload, source)
    restored = ValidatedQuantitativeAttachmentRecord.model_validate_json(stored.model_dump_json())
    merged = merge_validated_quantitative_records([restored],
        expected_documents={ATTACHMENT: DOCUMENT_SHA}, manifest_sha256=MANIFEST_SHA)
    assert merged.status == "AVAILABLE", codes(merged)
    assert len(merged.available_candidates) == 2
