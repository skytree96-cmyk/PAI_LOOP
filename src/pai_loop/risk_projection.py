"""Allowlisted business-risk arithmetic for the redacted browser response."""
from __future__ import annotations

import math
from typing import Any

from .evaluator import RISK_WEIGHTS


def public_risk_basis(explanation: Any) -> dict[str, Any] | None:
    risk = explanation.get("risk") if isinstance(explanation, dict) else None
    if not isinstance(risk, dict):
        return None

    def numeric(value: Any, maximum: float) -> bool:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and 0 <= value <= maximum and math.isfinite(value))

    def axes(field: str, maximum: float) -> dict[str, float]:
        values = risk.get(field)
        return {key: values[key] for key in RISK_WEIGHTS
                if isinstance(values, dict) and numeric(values.get(key), maximum)}

    values = axes("axis_values", 100)
    result: dict[str, Any] = {
        "axis_values": values,
        "weights": axes("weights", 1),
        "evidenced_axis_count": len(values),
        "missing_axes": [key for key in RISK_WEIGHTS if key not in values],
        "axis_basis": {},
    }
    if risk.get("status") in ("AVAILABLE", "INSUFFICIENT_EVIDENCE", "WITHHELD_R07"):
        result["status"] = risk["status"]
    if numeric(risk.get("minimum_evidenced_axes"), 6):
        result["minimum_evidenced_axes"] = risk["minimum_evidenced_axes"]
    # No arbitrary strings, raw methods, company facts, or evidence identifiers.
    sources = {
        "qualification": {"DETERMINISTIC_ELIGIBILITY_EVALUATION"},
        "execution": {"DETERMINISTIC_READINESS_EVALUATION"},
        "competition": {"STORED_3Y_AWARD_HISTORY"},
        "profitability": {"STORED_3Y_AWARD_RATE_PREDICTION"},
        "operation": {"MATERIALIZED_POLICY_REQUIREMENTS"},
        "document": {"VALIDATED_EXTRACTION_STATUS"},
    }
    fields = {
        "qualification": {"requirement_count": 1_000_000},
        "execution": {"readiness_score": 100},
        "competition": {"sample_count": 1_000_000},
        "profitability": {"sample_count": 1_000_000, "award_rate_center": 1_000_000},
        "operation": {"incomplete_action_count": 1_000_000, "mandatory_checklist_count": 1_000_000},
        "document": {"minimum_extraction_confidence": 1, "source_count": 1_000_000},
    }
    basis = risk.get("axis_basis")
    for key in values:
        item = basis.get(key) if isinstance(basis, dict) else None
        if not isinstance(item, dict):
            continue
        allowed_sources = sources[key] | {"NOTICE_RISK_DIMENSIONS_AUTHORITATIVE_OVERRIDE"}
        if not isinstance(item.get("source"), str) or item["source"] not in allowed_sources:
            continue
        safe = {"source": item["source"]}
        for name, maximum in fields[key].items():
            if numeric(item.get(name), maximum):
                safe[name] = item[name]
        if key == "qualification" and item.get("outcome") in ("PASS", "REVIEW", "FAIL"):
            safe["outcome"] = item["outcome"]
        if key == "document" and item.get("run_status") in ("COMPLETED", "REVIEW_REQUIRED", "FAILED", "PARTIAL"):
            safe["run_status"] = item["run_status"]
        result["axis_basis"][key] = safe
    return result
