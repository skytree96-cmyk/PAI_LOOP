from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pai_loop.quantitative_formula import (
    CREDIT_RATING_ORDER,
    CaseTableRowLiteral,
    CompiledCaseTable,
    case_table_points,
    compile_case_table,
)


def _row(operator: str, boundary: float, points: float) -> CaseTableRowLiteral:
    return CaseTableRowLiteral(
        operator=operator, comparison_value=boundary, award_value=points,
    )


def _compile(*rows: CaseTableRowLiteral, kind: str = "DISCRETE"):
    return compile_case_table(rows, value_kind=kind, maximum_points=7)


def _complete_count_table(operator: str):
    return _compile(
        _row("GTE", 6, 7),
        _row("EQ", 5, 6),
        _row("EQ", 4, 5),
        _row("EQ", 3, 4),
        _row("EQ", 2, 3),
        _row(operator, 1 if operator == "LTE" else 2, 1),
    )


@pytest.mark.parametrize("operator", ["LTE", "LT"])
@pytest.mark.parametrize("value,points", [(0, 1), (1, 1), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (99, 7)])
def test_explicit_lower_count_tail_respects_every_boundary(operator, value, points):
    table = _complete_count_table(operator)
    assert table is not None
    assert case_table_points(table, value) == points
    assert table.rows[-1].operator == operator
    assert table.rows[-1].comparison_value == (1 if operator == "LTE" else 2)


@pytest.mark.parametrize("operator", ["LTE", "LT"])
@pytest.mark.parametrize("value", [None, False, True, "0", "SYN missing", -1, Decimal("-2"), 0.5, 1.5, float("nan"), float("inf"), Decimal("NaN")])
def test_lower_tail_never_coerces_missing_invalid_or_fractional_count_to_zero(operator, value):
    table = _complete_count_table(operator)
    assert table is not None
    assert case_table_points(table, value) is None


@pytest.mark.parametrize("operator,boundary", [("LTE", 0), ("LT", 1)])
def test_explicit_zero_only_tail_requires_an_actual_zero(operator, boundary):
    table = _compile(_row("GTE", 1, 7), _row(operator, boundary, 1))
    assert table is not None
    assert case_table_points(table, Decimal("0")) == 1
    assert case_table_points(table, None) is None
    assert case_table_points(table, False) is None
    assert case_table_points(table, -1) is None
    assert case_table_points(table, 1) == 7


@pytest.mark.parametrize("operator,boundary", [("LTE", 1), ("LT", 2)])
def test_source_omitted_integer_gaps_are_not_filled_by_a_lower_tail(operator, boundary):
    table = _compile(_row("GTE", 7, 7), _row("EQ", 4, 4), _row(operator, boundary, 1))
    assert table is not None
    assert [case_table_points(table, value) for value in range(8)] == [1, 1, None, None, 4, None, None, 7]


@pytest.mark.parametrize("rows", [
    (_row("LTE", 1, 1),),
    (_row("LT", 2, 1),),
    (_row("EQ", 2, 3), _row("LTE", 1, 1)),
    (_row("GTE", 6, 7), _row("LTE", 4, 4), _row("EQ", 3, 3)),
    (_row("GTE", 6, 7), _row("LTE", 3, 3), _row("LT", 2, 1)),
    (_row("GTE", 6, 7), _row("LT", 3, 3), _row("LTE", 1, 1)),
    (_row("GTE", 6, 7), _row("EQ", 2, 3), _row("LTE", 2, 1)),
    (_row("GTE", 6, 7), _row("EQ", 2, 3), _row("LT", 3, 1)),
    (_row("GTE", 6, 7), _row("LTE", 6, 1)),
    (_row("GTE", 6, 7), _row("LT", 7, 1)),
    (_row("GTE", 6, 7), _row("LT", 0, 1)),
    (_row("GTE", 6, 7), _row("LTE", -1, 1)),
    (_row("GTE", 6.5, 7), _row("LTE", 1, 1)),
    (_row("GTE", 6, 7), _row("LTE", 1.5, 1)),
    (_row("GTE", 6, 7), _row("LT", 1.5, 1)),
    (_row("GTE", 6, 7), _row("EQ", 2.5, 3), _row("LTE", 1, 1)),
    (_row("GTE", 4, 7), _row("GTE", 6, 6), _row("LTE", 1, 1)),
    (_row("GTE", 6, 7), _row("EQ", 4, 5), _row("GTE", 2, 3), _row("LTE", 1, 1)),
    (_row("GTE", 6, 7), _row("EQ", 4, 5), _row("EQ", 4, 3), _row("LTE", 1, 1)),
    (_row("GTE", 6, 7), _row("EQ", 4, 3), _row("LTE", 1, 4)),
])
def test_lower_tail_rejects_unsupported_order_overlap_empty_domain_and_invalid_bounds(rows):
    assert _compile(*rows) is None


@pytest.mark.parametrize("kind", ["NUMERIC", "CATEGORICAL", "CREDIT_RATING"])
@pytest.mark.parametrize("operator", ["LTE", "LT"])
def test_lower_numeric_operators_are_not_enabled_for_other_value_kinds(kind, operator):
    assert _compile(_row("GTE", 6, 7), _row(operator, 1, 1), kind=kind) is None


@pytest.mark.parametrize("operator", ["LTE", "LT"])
@pytest.mark.parametrize("invalid_fields", [
    {"comparison_value": None},
    {"comparison_value": True},
    {"comparison_value": float("inf")},
    {"category_values": ("SYN category",)},
])
def test_lower_numeric_source_row_keeps_strict_numeric_shape(operator, invalid_fields):
    with pytest.raises(ValidationError):
        CaseTableRowLiteral(**{
            "operator": operator, "comparison_value": 1, "award_value": 1,
            **invalid_fields,
        })


def test_valid_compiled_tail_round_trips_and_tampered_program_is_rejected():
    table = _complete_count_table("LT")
    assert table is not None
    raw = table.model_dump(mode="json")
    restored = CompiledCaseTable.model_validate(raw)
    assert restored == table
    raw["rows"][-1]["comparison_value"] = 3
    with pytest.raises(ValidationError):
        CompiledCaseTable.model_validate(raw)


def test_leading_gte_cutoffs_and_percentage_awards_keep_their_original_meaning():
    rows = (
        _row("GTE", 8, 7), _row("GTE", 5, 5), _row("EQ", 4, 3),
        CaseTableRowLiteral(operator="LTE", comparison_value=3, award_kind="PERCENT_OF_MAX", award_value=10),
    )
    table = _compile(*rows)
    assert table is not None
    assert [case_table_points(table, value) for value in (0, 3, 4, 5, 7, 8)] == [0.7, 0.7, 3, 5, 5, 7]


_LEGACY_PROGRAMS = {
    "numeric": ("NUMERIC", 7, [
        dict(operator="GTE", comparison_value=90, award_value=7),
        dict(operator="GTE", comparison_value=60, award_value=4),
    ], "35cf6fe4b8d57395a3424db7c3a38c8940213c97bb8a8000f3bd78be03073f61", [(59, None), (60, 4), (90, 7)]),
    "discrete": ("DISCRETE", 7, [
        dict(operator="GTE", comparison_value=6, award_value=7),
        dict(operator="EQ", comparison_value=4, award_value=4),
        dict(operator="EQ", comparison_value=0, award_value=1),
    ], "0c2c3940c55e93dcdc4cf8d3cf4e470f7a9e322275362b38ca3a70f6c4a80f7f", [(0, 1), (1, None), (4, 4), (5, None), (6, 7)]),
    "categorical": ("CATEGORICAL", 3, [
        dict(operator="IN", category_values=("SYN-A", "SYN-B"), award_value=3),
        dict(operator="IN", category_values=("SYN-C",), award_value=1),
    ], "719e9bb61f3524ed95bc2d0369bd56f1b742eaf86e717e2cea2dbe51611a218f", [("SYN-A", 3), ("SYN-C", 1), ("SYN-unknown", None)]),
    "credit": ("CREDIT_RATING", 10, [
        dict(operator="IN", category_values=CREDIT_RATING_ORDER,
             source_literal=", ".join(CREDIT_RATING_ORDER), award_value=10),
    ], "c4864412e69ce9480790b18467f6fc58ed4f64bccbdb85309ff14116e2a0ea07", [("A0", 10), ("D", 10), ("SYN-unknown", None)]),
}


@pytest.mark.parametrize("name", _LEGACY_PROGRAMS)
def test_legacy_program_canonical_hash_and_results_are_unchanged(name):
    # Hashes were captured before adding lower-tail operators. New model fields
    # or a rewritten legacy program would invalidate persisted proof material.
    kind, maximum, rows, expected_hash, probes = _LEGACY_PROGRAMS[name]
    table = compile_case_table(tuple(CaseTableRowLiteral(**row) for row in rows),
                               value_kind=kind, maximum_points=maximum)
    assert table is not None
    canonical = json.dumps(table.model_dump(mode="json"), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == expected_hash
    for value, points in probes:
        assert case_table_points(table, value) == points
