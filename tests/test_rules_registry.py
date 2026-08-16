from __future__ import annotations

import json
from importlib.resources import files


def test_v21_rule_registry_has_locked_counts_and_codes() -> None:
    path = files("pai_loop").joinpath("data/rules_v2_1.json")
    rules = json.loads(path.read_text(encoding="utf-8"))

    assert len(rules["pass_rules"]) == 15
    assert len(rules["review_rules"]) == 8
    assert len(rules["aggregation_rules"]) == 3
    assert rules["default_fail"]["id"] == "DF-000"
    assert {item["code"] for item in rules["review_rules"]} == {
        "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R09"
    }
    assert rules["evaluation_order"] == ["PASS", "LINKED_REVIEW", "DF-000"]
    assert len(rules["default_fail"]["required_explanation_fields"]) == 5

