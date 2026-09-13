"""The actual production schema crosses the JavaScript transport projection.

Only synthetic source/output is supplied. No provider, credentials, or network.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import EXTRACTION_SCHEMA, ExtractionPayload
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction


def as_transport(output):
    result = deepcopy(output)
    for field in ("quantitative_tables", "quantitative_table_not_applicable"):
        if field in result:
            result[field] = json.dumps(result[field], ensure_ascii=False)
    return result


def transform(output, *, encoded=False):
    node = shutil.which("node")
    assert node, "Node is required for the native gateway schema contract"
    result = subprocess.run([node, "scripts/test-native-gateway.mjs", "--schema-stdin"],
                            input=json.dumps({"schema": EXTRACTION_SCHEMA, "output": output if encoded else as_transport(output)}),
                            text=True, encoding="utf-8", capture_output=True, check=True, timeout=30,
                            cwd=Path(__file__).resolve().parents[1])
    return json.loads(result.stdout)


def synthetic_output():
    return {"document_type": "OTHER", "requirements": [], "quantitative_tables": [],
            "quantitative_table_not_applicable": None, "missing_or_unreadable": [], "summary": "SYN source only."}


def test_actual_schema_fits_both_cap_interpretations_and_keeps_empty_output():
    original = deepcopy(EXTRACTION_SCHEMA)
    output = synthetic_output()
    result = transform(output)
    assert result["counts"] == {"unique": {"unions": 4, "optional": 0}, "expanded": {"unions": 4, "optional": 0}}
    assert result["decoded"] == output
    assert EXTRACTION_SCHEMA == original
    assert ExtractionPayload.model_validate(result["decoded"]).summary == output["summary"]
    schema = result["schema"]
    assert schema["required"] == EXTRACTION_SCHEMA["required"]
    for name, definition in schema["$defs"].items():
        expected = list(EXTRACTION_SCHEMA["$defs"][name]["required"])
        assert definition["required"] == expected
    assert set(schema["$defs"]) == {"EvidenceAnchor", "ExtractedRequirement"}
    for field in ("quantitative_tables", "quantitative_table_not_applicable"):
        assert schema["properties"][field]["type"] == "string"
    for field, kind in [("page", "integer"), ("section", "string")]:
        projected = schema["$defs"]["EvidenceAnchor"]["properties"][field]
        assert projected["anyOf"][0]["type"] == kind
        assert projected["anyOf"][1] == {"type": "null"}


def output_with_anchor():
    output = synthetic_output()
    output["requirements"] = [{"requirement_id": "SYN-R1", "category": "OTHER", "logic": "SINGLE",
                               "normalized_condition": "SYN source only.", "mandatory": False,
                               "deadline_basis": None, "ambiguity_reason": None,
                               "evidence": [{"attachment_id": "SYN-A1", "page": None, "section": None,
                                             "quote": "SYN source only.", "confidence": 1}]}]
    return output


def test_anchor_original_nullable_values_and_original_constraints_still_validate():
    output = output_with_anchor()
    result = transform(output)
    anchor = result["decoded"]["requirements"][0]["evidence"][0]
    assert anchor == {**output["requirements"][0]["evidence"][0], "page": None, "section": None}
    ExtractionPayload.model_validate(result["decoded"])
    output["requirements"][0]["evidence"][0].update(page=0, section="SYN explicit section")
    invalid = transform(output)["decoded"]
    assert invalid["requirements"][0]["evidence"][0]["page"] == 0
    with pytest.raises(ValidationError):
        ExtractionPayload.model_validate(invalid)


@pytest.mark.parametrize("field,value", [
    ("page", []), ("page", [1]), ("page", True), ("page", 1.5),
    ("page", "1"), ("page", [None]), ("page", [1, 2]), ("page", [[1]]),
    ("section", []), ("section", ["SYN"]), ("section", True),
    ("section", 1), ("section", [None]), ("section", ["SYN", "SYN"]),
])
def test_invalid_anchor_transport_rejected_without_coercion(field, value):
    output = output_with_anchor()
    output["requirements"][0]["evidence"][0][field] = value
    with pytest.raises(subprocess.CalledProcessError):
        transform(output)


@pytest.mark.parametrize("field", ["page", "section", "quote", "confidence"])
def test_missing_anchor_fields_are_never_filled(field):
    output = output_with_anchor()
    del output["requirements"][0]["evidence"][0][field]
    with pytest.raises(subprocess.CalledProcessError):
        transform(output)


def complete_output():
    anchor = {"attachment_id": "SYN-A1", "page": 2, "section": "SYN section",
              "quote": "SYN source only.", "confidence": 1}
    rule = {"criterion_id": "SYN-C1", "label": "SYN", "criterion_literal": "SYN",
            "max_points": 10, "scoring_method": "CASE_TABLE", "metric": "CREDIT_RATING",
            "unit": None, "brackets": [{"label": "SYN", "literal": "SYN", "min_value": None,
                                       "max_value": None, "min_inclusive": True,
                                       "max_inclusive": False, "points": 1, "evidence": anchor}],
            "threshold": {"literal": "SYN", "operator": "GTE", "threshold_value": 1,
                          "points_if_met": 1, "points_if_not_met": None, "evidence": anchor},
            "formula_literal": None, "cases": [{"literal": "SYN", "operator": "IN",
                "comparison_value": None, "comparison_upper_value": None,
                "category_values": ["SYN-A", "SYN-B"],
                "award_kind": "POINTS", "award_value": 1, "row_order": 1, "evidence": anchor}],
            "recognition_conditions": [{"literal": "SYN", "evidence": anchor}],
            "required_evidence": ["SYN-A", "SYN-B"], "evidence": anchor, "ambiguity_reason": None}
    output = output_with_anchor()
    output["requirements"][0]["evidence"] = [anchor]
    output["quantitative_tables"] = [{"table_id": "SYN-T1", "label": "SYN", "criteria": [rule],
        "total_points": 10, "total_evidence": anchor, "minimum_score": None,
        "minimum_evidence": anchor, "ambiguity_reason": None}]
    output["quantitative_table_not_applicable"] = {"reason_literal": "SYN", "evidence": anchor}
    output["missing_or_unreadable"] = ["SYN-A", "SYN-B"]
    return output


def test_whole_quantitative_types_anchors_and_arrays_round_trip_then_original_validation():
    output = complete_output()
    before = deepcopy(output)
    result = transform(output)
    decoded = result["decoded"]
    assert decoded == output
    assert output == before
    ExtractionPayload.model_validate(decoded)
    for name, definition in result["schema"]["$defs"].items():
        for field, prop in definition["properties"].items():
            if "enum" in prop:
                assert prop["enum"] == EXTRACTION_SCHEMA["$defs"][name]["properties"][field]["enum"]
    output["requirements"][0]["category"] = "SYN-INVALID-ENUM"
    with pytest.raises(subprocess.CalledProcessError):
        transform(output)


@pytest.mark.parametrize("field", ["quantitative_tables", "quantitative_table_not_applicable"])
@pytest.mark.parametrize("invalid", [None, [], {}, 0, True, "", " ", "[] trailing", "[] []",
                                      "```json\n[]\n```", "[1,]", "{", "NaN", "1e400"])
def test_quantitative_string_transport_rejects_wrong_types_or_non_strict_json(field, invalid):
    output = as_transport(synthetic_output())
    output[field] = invalid
    with pytest.raises(subprocess.CalledProcessError):
        transform(output, encoded=True)


@pytest.mark.parametrize("field", ["quantitative_tables", "quantitative_table_not_applicable"])
def test_missing_quantitative_fields_are_never_filled(field):
    output = as_transport(synthetic_output())
    del output[field]
    with pytest.raises(subprocess.CalledProcessError):
        transform(output, encoded=True)


@pytest.mark.parametrize("escaped", [False, True])
def test_duplicate_inner_object_keys_are_rejected_even_when_values_are_valid(escaped):
    output = as_transport(complete_output())
    key = "table_\\u0069d" if escaped else "table_id"
    output["quantitative_tables"] = output["quantitative_tables"].replace("{", '{"' + key + '":"SYN-DUP",', 1)
    with pytest.raises(subprocess.CalledProcessError):
        transform(output, encoded=True)


@pytest.mark.parametrize("mutate", [
    lambda table: table["criteria"][0].update(metric="SYN-INVALID"),
    lambda table: table["criteria"][0].update(max_points="10"),
    lambda table: table["criteria"][0].update(extra="SYN"),
    lambda table: table["criteria"][0].pop("cases"),
    lambda table: table["criteria"][0].update(evidence={"page": 1}),
])
def test_native_string_does_not_bypass_original_quantitative_types_enums_or_required(mutate):
    output = complete_output()
    mutate(output["quantitative_tables"][0])
    with pytest.raises(subprocess.CalledProcessError):
        transform(output)


def test_quantitative_ranges_still_fail_original_pydantic_validation():
    output = complete_output()
    output["quantitative_tables"][0]["criteria"][0]["max_points"] = -1
    decoded = transform(output)["decoded"]
    assert decoded["quantitative_tables"][0]["criteria"][0]["max_points"] == -1
    with pytest.raises(ValidationError):
        ExtractionPayload.model_validate(decoded)


@pytest.mark.parametrize("field,value", [("quantitative_tables", "null"),
                                       ("quantitative_table_not_applicable", "[]")])
def test_valid_json_with_wrong_original_top_level_type_is_rejected(field, value):
    output = as_transport(synthetic_output())
    output[field] = value
    with pytest.raises(subprocess.CalledProcessError):
        transform(output, encoded=True)


def test_ordinary_nested_strings_are_not_interpreted_as_more_json_transport():
    output = complete_output()
    literal = 'SYN "quote" \\ slash\n{"quantitative_tables":"[]","x":1,"x":2}'
    output["summary"] = literal
    output["quantitative_tables"][0]["criteria"][0]["criterion_literal"] = literal
    output["quantitative_tables"][0]["criteria"][0]["cases"][0]["category_values"] = ["[]", "null", literal]
    decoded = transform(output)["decoded"]
    assert decoded == output
    ExtractionPayload.model_validate(decoded)


def test_observed_synthetic_provider_payload_decodes_and_validates_original_schema():
    # Root's one approved synthetic provider probe, reproduced locally only.
    output = {"document_type": "OTHER", "requirements": [], "quantitative_tables": "[]",
              "quantitative_table_not_applicable": "null",
              "missing_or_unreadable": ["소스 문서에 실질적인 조달 요건이나 정량 평가표 내용이 포함되어 있지 않아 추출할 정보가 없음"],
              "summary": "제공된 SOURCE는 'SYN 합성 형식 검증 문장입니다'라는 짧은 검증용 문장으로, 조달 요건이나 정량 평가 기준을 포함하지 않아 추출 가능한 항목이 없음."}
    decoded = transform(output, encoded=True)["decoded"]
    assert decoded == {**output, "quantitative_tables": [], "quantitative_table_not_applicable": None}
    validated = ExtractionPayload.model_validate(decoded)
    assert validated.quantitative_tables == []
    assert validated.quantitative_table_not_applicable is None


def count_range_output(*, inline=False):
    """Every transport field is explicit; no model defaults fill provider gaps."""
    def anchor(quote):
        return {"attachment_id": "SYN-A1", "page": 1, "section": "SYN",
                "quote": quote, "confidence": 1}

    heading = "SYN-교육 용역 수행실적 (5점)"
    scope = "공고일 기준 최근 3년 교육 관련 수행완료 실적의 건수, 부가세 포함, 공동수급 전체 인정"
    rows = []
    for index, (operator, lower, upper, points, condition) in enumerate([
        ("GTE", 3, None, 5, "3건 이상"),
        ("BETWEEN", 1, 2, 3, "1건 이상 2건 이하"),
        ("NOT_SUBMITTED", None, None, 0, "미제출"),
    ], 1):
        literal = f"{condition} {points}점" if inline else f"{condition}\n{points}"
        rows.append({"operator": operator, "comparison_value": lower,
                     "comparison_upper_value": upper, "category_values": [],
                     "award_kind": "POINTS", "award_value": points, "row_order": index,
                     "literal": literal, "evidence": anchor(literal)})
    rule = {"criterion_id": "SYN-C1", "label": "교육 용역 수행실적", "criterion_literal": heading,
            "max_points": 5, "scoring_method": "CASE_TABLE", "metric": "PERFORMANCE_COUNT",
            "unit": "건", "brackets": [], "threshold": None, "formula_literal": None,
            "cases": rows, "recognition_conditions": [{"literal": scope, "evidence": anchor(scope)}],
            "required_evidence": ["company.performance.count"], "evidence": anchor(heading),
            "ambiguity_reason": None}
    output = synthetic_output()
    output["document_type"] = "RFP"
    total = "정량평가 합계 5점"
    output["quantitative_tables"] = [{"table_id": "SYN-T1", "label": "정량평가", "criteria": [rule],
        "total_points": 5, "total_evidence": anchor(total), "minimum_score": None,
        "minimum_evidence": None, "ambiguity_reason": None}]
    return output, "\n".join([heading, scope, *(row["literal"] for row in rows), total])


def validate_count_source(decoded, source):
    return validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(decoded), source_text=source,
        attachment_id="SYN-A1", document_sha256="a" * 64, manifest_sha256="b" * 64,
    )


@pytest.mark.parametrize("inline", [False, True])
def test_new_count_case_transport_round_trip_preserves_bounds_status_and_source_validation(inline):
    output, source = count_range_output(inline=inline)
    decoded = transform(output)["decoded"]
    assert decoded == output
    validated = validate_count_source(decoded, source)
    assert validated.status == "AVAILABLE", [issue.code for issue in validated.issues]
    cases = validated.available_candidates[0].cases
    assert [(case.operator, case.comparison_value, case.comparison_upper_value)
            for case in cases] == [("GTE", 3, None), ("BETWEEN", 1, 2), ("NOT_SUBMITTED", None, None)]
    assert cases[-1].award_value == 0


@pytest.mark.parametrize("row_index", [0, 1, 2])
def test_new_case_transport_never_fills_an_omitted_nullable_upper_bound(row_index):
    output, _ = count_range_output()
    del output["quantitative_tables"][0]["criteria"][0]["cases"][row_index]["comparison_upper_value"]
    with pytest.raises(subprocess.CalledProcessError):
        transform(output)


@pytest.mark.parametrize("upper", [True, "2", [], {}])
def test_new_case_upper_bound_transport_rejects_type_coercion(upper):
    output, _ = count_range_output()
    output["quantitative_tables"][0]["criteria"][0]["cases"][1]["comparison_upper_value"] = upper
    with pytest.raises(subprocess.CalledProcessError):
        transform(output)


@pytest.mark.parametrize("row_index,patch", [
    (1, {"comparison_upper_value": None}), (1, {"comparison_upper_value": 1}),
    (1, {"comparison_upper_value": 1.5}), (2, {"comparison_value": 0}),
    (2, {"award_value": 1}),
])
def test_new_case_transport_does_not_bypass_original_case_shape_validation(row_index, patch):
    output, _ = count_range_output()
    output["quantitative_tables"][0]["criteria"][0]["cases"][row_index].update(patch)
    decoded = transform(output)["decoded"]
    with pytest.raises(ValidationError):
        ExtractionPayload.model_validate(decoded)


@pytest.mark.parametrize("row_index,literal", [(1, "1건 이상 2건 미만\n3"), (2, "미확인\n0")])
def test_new_case_valid_transport_still_requires_exact_range_and_submission_source(row_index, literal):
    output, source = count_range_output()
    row = output["quantitative_tables"][0]["criteria"][0]["cases"][row_index]
    source = source.replace(row["literal"], literal)
    row.update(literal=literal, evidence={**row["evidence"], "quote": literal})
    decoded = transform(output)["decoded"]
    validated = validate_count_source(decoded, source)
    assert not validated.available_candidates
    assert "CASE_NUMBER_MISMATCH" in {issue.code for issue in validated.issues}
