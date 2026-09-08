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
                            text=True, capture_output=True, check=True, timeout=30,
                            cwd=Path(__file__).resolve().parents[1])
    return json.loads(result.stdout)


def synthetic_output():
    return {"document_type": "OTHER", "requirements": [], "quantitative_tables": [],
            "quantitative_table_not_applicable": None, "missing_or_unreadable": [], "summary": "SYN source only."}


def test_actual_schema_fits_both_cap_interpretations_and_keeps_empty_output():
    original = deepcopy(EXTRACTION_SCHEMA)
    output = synthetic_output()
    result = transform(output)
    assert result["counts"] == {"unique": {"unions": 16, "optional": 2}, "expanded": {"unions": 16, "optional": 18}}
    assert result["decoded"] == output
    assert EXTRACTION_SCHEMA == original
    assert ExtractionPayload.model_validate(result["decoded"]).summary == output["summary"]
    schema = result["schema"]
    assert schema["required"] == EXTRACTION_SCHEMA["required"]
    for name, definition in schema["$defs"].items():
        expected = list(EXTRACTION_SCHEMA["$defs"][name]["required"])
        if name == "EvidenceAnchor":
            expected = [key for key in expected if key not in {"page", "section"}]
        assert definition["required"] == expected


def test_only_two_missing_evidence_fields_restore_null_and_original_constraints_still_validate():
    output = synthetic_output()
    output["requirements"] = [{"requirement_id": "SYN-R1", "category": "OTHER", "logic": "SINGLE",
                               "normalized_condition": "SYN source only.", "mandatory": False,
                               "deadline_basis": None, "ambiguity_reason": None,
                               "evidence": [{"attachment_id": "SYN-A1", "quote": "SYN source only.", "confidence": 1}]}]
    result = transform(output)
    anchor = result["decoded"]["requirements"][0]["evidence"][0]
    assert anchor == {**output["requirements"][0]["evidence"][0], "page": None, "section": None}
    ExtractionPayload.model_validate(result["decoded"])
    output["requirements"][0]["evidence"][0].update(page=0, section="SYN explicit section")
    invalid = transform(output)["decoded"]
    assert invalid["requirements"][0]["evidence"][0]["page"] == 0
    with pytest.raises(ValidationError):
        ExtractionPayload.model_validate(invalid)
