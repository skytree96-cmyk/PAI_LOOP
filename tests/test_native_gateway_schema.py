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


def transform(output):
    node = shutil.which("node")
    assert node, "Node is required for the native gateway schema contract"
    result = subprocess.run([node, "scripts/test-native-gateway.mjs", "--schema-stdin"],
                            input=json.dumps({"schema": EXTRACTION_SCHEMA, "output": output}),
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
    assert result["counts"] == {"unique": {"unions": 16, "optional": 0}, "expanded": {"unions": 16, "optional": 0}}
    assert result["decoded"] == output
    assert EXTRACTION_SCHEMA == original
    assert ExtractionPayload.model_validate(result["decoded"]).summary == output["summary"]
    schema = result["schema"]
    assert schema["required"] == EXTRACTION_SCHEMA["required"]
    for name, definition in schema["$defs"].items():
        expected = list(EXTRACTION_SCHEMA["$defs"][name]["required"])
        assert definition["required"] == expected
    for field, kind in [("page", "integer"), ("section", "string")]:
        projected = schema["$defs"]["EvidenceAnchor"]["properties"][field]
        assert projected["type"] == "array"
        assert projected["items"]["type"] == kind
        assert "maxItems" not in projected


def output_with_anchor():
    output = synthetic_output()
    output["requirements"] = [{"requirement_id": "SYN-R1", "category": "OTHER", "logic": "SINGLE",
                               "normalized_condition": "SYN source only.", "mandatory": False,
                               "deadline_basis": None, "ambiguity_reason": None,
                               "evidence": [{"attachment_id": "SYN-A1", "page": [], "section": [],
                                             "quote": "SYN source only.", "confidence": 1}]}]
    return output


def test_explicit_empty_arrays_restore_null_and_original_constraints_still_validate():
    output = output_with_anchor()
    result = transform(output)
    anchor = result["decoded"]["requirements"][0]["evidence"][0]
    assert anchor == {**output["requirements"][0]["evidence"][0], "page": None, "section": None}
    ExtractionPayload.model_validate(result["decoded"])
    output["requirements"][0]["evidence"][0].update(page=[0], section=["SYN explicit section"])
    invalid = transform(output)["decoded"]
    assert invalid["requirements"][0]["evidence"][0]["page"] == 0
    with pytest.raises(ValidationError):
        ExtractionPayload.model_validate(invalid)


@pytest.mark.parametrize("field,value", [
    ("page", None), ("page", 1), ("page", [True]), ("page", [1.5]),
    ("page", ["1"]), ("page", [None]), ("page", [1, 2]), ("page", [[1]]),
    ("section", None), ("section", "SYN"), ("section", [True]),
    ("section", [1]), ("section", [None]), ("section", ["SYN", "SYN"]),
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


def test_all_original_anchor_paths_decode_without_changing_other_arrays_or_enums():
    anchor = {"attachment_id": "SYN-A1", "page": [2], "section": ["SYN section"],
              "quote": "SYN source only.", "confidence": 1}
    rule = {"criterion_id": "SYN-C1", "label": "SYN", "criterion_literal": "SYN",
            "max_points": 10, "scoring_method": "CASE_TABLE", "metric": "CREDIT_RATING",
            "unit": None, "brackets": [{"label": "SYN", "literal": "SYN", "min_value": None,
                                       "max_value": None, "min_inclusive": True,
                                       "max_inclusive": False, "points": 1, "evidence": anchor}],
            "threshold": {"literal": "SYN", "operator": "GTE", "threshold_value": 1,
                          "points_if_met": 1, "points_if_not_met": None, "evidence": anchor},
            "formula_literal": None, "cases": [{"literal": "SYN", "operator": "IN",
                "comparison_value": None, "category_values": ["SYN-A", "SYN-B"],
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
    before = deepcopy(output)
    result = transform(output)
    decoded = result["decoded"]
    expected = deepcopy(output)
    # All nine schema reference sites are exercised; shared Python fixture objects
    # become independent JSON objects, so expected conversion is intentionally once.
    expected_anchor = expected["requirements"][0]["evidence"][0]
    expected_anchor.update(page=2, section="SYN section")
    assert decoded == expected
    assert output == before
    ExtractionPayload.model_validate(decoded)
    for name, definition in EXTRACTION_SCHEMA["$defs"].items():
        for field, prop in definition["properties"].items():
            if "enum" in prop:
                assert result["schema"]["$defs"][name]["properties"][field]["enum"] == prop["enum"]
    output["requirements"][0]["category"] = "SYN-INVALID-ENUM"
    with pytest.raises(subprocess.CalledProcessError):
        transform(output)
