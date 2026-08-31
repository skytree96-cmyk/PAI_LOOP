from __future__ import annotations

import ast
import math
import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FormulaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DeterministicFormula(FormulaModel):
    """A bounded, single-metric arithmetic expression compiled from source text.

    Only the variable ``x``, finite decimal constants, parentheses, and the
    four arithmetic operators are accepted.  The original procurement text
    remains the authority; this object is merely a deterministic execution
    plan and never contains executable Python names or calls.
    """

    expression: str = Field(min_length=1, max_length=300, pattern=r"^[x0-9eE.+\-*/() ]+$")
    source_unit_scale: float = Field(default=1, gt=0)
    minimum_points: float = Field(default=0, ge=0)
    maximum_points: float = Field(gt=0)
    rounding_mode: Literal["NONE", "HALF_UP", "FLOOR", "CEILING", "TRUNCATE"] = "NONE"
    rounding_digits: int = Field(default=6, ge=0, le=6)

    @model_validator(mode="after")
    def validate_bounds(self) -> "DeterministicFormula":
        if self.minimum_points > self.maximum_points:
            raise ValueError("formula minimum_points must not exceed maximum_points")
        _parse_expression(self.expression)
        return self


class CategoryScore(FormulaModel):
    values: tuple[str, ...] = Field(min_length=1, max_length=50)
    points: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_values(self) -> "CategoryScore":
        normalized = [_normalize_category(value) for value in self.values]
        if any(not value for value in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("category values must be non-empty and unique")
        return self


CaseTableOperator = Literal["GTE", "EQ", "IN"]
CaseTableAwardKind = Literal["POINTS", "PERCENT_OF_MAX"]
CaseTableValueKind = Literal["NUMERIC", "DISCRETE", "CATEGORICAL"]


class CaseTableRowLiteral(FormulaModel):
    """One source-validated condition-to-award row.

    There is deliberately no fallback/else representation. The upstream
    extraction boundary remains responsible for proving that every supplied
    value and category is present in an exact source anchor.
    """

    operator: CaseTableOperator
    comparison_value: float | None = None
    category_values: tuple[str, ...] = Field(default=(), max_length=100)
    award_kind: CaseTableAwardKind = "POINTS"
    award_value: float = Field(ge=0)

    @field_validator("comparison_value", "award_value", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("case table numeric values must not be boolean")
        return value

    @model_validator(mode="after")
    def validate_row_shape(self) -> "CaseTableRowLiteral":
        if self.operator in {"GTE", "EQ"}:
            if self.comparison_value is None or self.category_values:
                raise ValueError("numeric case rows require only comparison_value")
        elif self.comparison_value is not None or not self.category_values:
            raise ValueError("categorical case rows require only category_values")

        normalized = [_normalize_category(value) for value in self.category_values]
        if any(not value for value in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("case table category values must be non-empty and unique")
        if self.award_kind == "PERCENT_OF_MAX" and self.award_value > 100:
            raise ValueError("case table percentage awards must not exceed 100")
        return self


class CompiledCaseTableRow(FormulaModel):
    operator: CaseTableOperator
    comparison_value: float | None = None
    category_values: tuple[str, ...] = Field(default=(), max_length=100)
    points: float = Field(ge=0)


class CompiledCaseTable(FormulaModel):
    """A deterministic, source-order-preserving CASE_TABLE execution plan."""

    value_kind: CaseTableValueKind
    rows: tuple[CompiledCaseTableRow, ...] = Field(min_length=1, max_length=100)
    maximum_points: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_program(self) -> "CompiledCaseTable":
        if not _case_table_rows_are_safe(
            self.rows,
            value_kind=self.value_kind,
            maximum_points=self.maximum_points,
        ):
            raise ValueError("compiled case table rows are not deterministic")
        return self


_METRIC_TOKENS = (
    "실적금액",
    "실적건수",
    "수행실적",
    "보유인력",
    "전문인력",
    "인원수",
    "인원",
    "건수",
    "수량",
    "비율",
    "업력",
    "연수",
    "값",
)
_CAP_MAX_RE = re.compile(r"(?:최대|상한)\s*(?P<value>\d+(?:\.\d+)?)\s*점")
_CAP_MIN_RE = re.compile(r"(?:최소|하한)\s*(?P<value>\d+(?:\.\d+)?)\s*점")
_ROUND_RE = re.compile(
    r"소수점\s*(?P<digits>[0-6])\s*(?:자리|째자리)?\s*"
    r"(?P<mode>반올림|버림|절사|올림)"
)
_CATEGORY_ROW_RE = re.compile(
    r"^(?P<label>[A-Za-z0-9가-힣+_.\-/·~ ]{1,120}?)\s*"
    r"(?:[:=]|은|는)?\s*(?P<points>\d+(?:\.\d+)?)\s*점$"
)
_BOOLEAN_TRUE_CATEGORIES = frozenset(
    {"y", "yes", "true", "1", "보유", "있음", "해당"}
)
_BOOLEAN_FALSE_CATEGORIES = frozenset(
    {"n", "no", "false", "0", "미보유", "없음", "비해당"}
)


def _normalize_source(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return normalized.replace("×", "*").replace("÷", "/").replace("−", "-")


def _normalize_category(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _parse_expression(expression: str) -> ast.Expression:
    try:
        parsed = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise ValueError("formula expression is not valid arithmetic") from exc
    nodes = list(ast.walk(parsed))
    if len(nodes) > 64:
        raise ValueError("formula expression is too complex")
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.UAdd,
        ast.USub,
        ast.Name,
        ast.Load,
        ast.Constant,
    )
    if any(not isinstance(node, allowed) for node in nodes):
        raise ValueError("formula expression contains an unsupported operation")
    names = [node.id for node in nodes if isinstance(node, ast.Name)]
    if not names or set(names) != {"x"}:
        raise ValueError("formula expression must use exactly the metric variable x")
    constants = [node.value for node in nodes if isinstance(node, ast.Constant)]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in constants):
        raise ValueError("formula expression constants must be finite numbers")
    if any(not math.isfinite(float(value)) for value in constants):
        raise ValueError("formula expression constants must be finite numbers")
    return parsed


def _decimal(value: int | float | Decimal) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("formula value is not numeric") from exc
    if not result.is_finite():
        raise ValueError("formula value must be finite")
    return result


def _eval_node(node: ast.AST, x: Decimal) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, x)
    if isinstance(node, ast.Name):
        if node.id != "x":
            raise ValueError("unsupported formula variable")
        return x
    if isinstance(node, ast.Constant):
        return _decimal(node.value)
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, x)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, x)
        right = _eval_node(node.right, x)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("formula division by zero")
            return left / right
    raise ValueError("unsupported formula expression")


def _eval_interval(
    node: ast.AST,
    lower: Decimal,
    upper: Decimal,
) -> tuple[Decimal, Decimal]:
    """Evaluate a conservative interval for the supported arithmetic DSL.

    Endpoint sampling is not sufficient for formulae such as ``x * x`` or a
    reciprocal whose denominator crosses zero inside the supplied range.  The
    interval implementation deliberately over-approximates those expressions;
    it can therefore return a wider score range, but never a falsely narrow
    one.
    """

    if isinstance(node, ast.Expression):
        return _eval_interval(node.body, lower, upper)
    if isinstance(node, ast.Name):
        if node.id != "x":
            raise ValueError("unsupported formula variable")
        return lower, upper
    if isinstance(node, ast.Constant):
        value = _decimal(node.value)
        return value, value
    if isinstance(node, ast.UnaryOp):
        item_lower, item_upper = _eval_interval(node.operand, lower, upper)
        if isinstance(node.op, ast.UAdd):
            return item_lower, item_upper
        return -item_upper, -item_lower
    if isinstance(node, ast.BinOp):
        left_lower, left_upper = _eval_interval(node.left, lower, upper)
        right_lower, right_upper = _eval_interval(node.right, lower, upper)
        if isinstance(node.op, ast.Add):
            return left_lower + right_lower, left_upper + right_upper
        if isinstance(node.op, ast.Sub):
            return left_lower - right_upper, left_upper - right_lower
        if isinstance(node.op, ast.Mult):
            products = (
                left_lower * right_lower,
                left_lower * right_upper,
                left_upper * right_lower,
                left_upper * right_upper,
            )
            return min(products), max(products)
        if isinstance(node.op, ast.Div):
            if right_lower <= 0 <= right_upper:
                raise ValueError("formula denominator crosses zero in the supplied range")
            quotients = (
                left_lower / right_lower,
                left_lower / right_upper,
                left_upper / right_lower,
                left_upper / right_upper,
            )
            return min(quotients), max(quotients)
    raise ValueError("unsupported formula expression")


def _round_value(value: Decimal, spec: DeterministicFormula) -> Decimal:
    if spec.rounding_mode == "NONE":
        return value
    quantum = Decimal(1).scaleb(-spec.rounding_digits)
    mode = {
        "HALF_UP": ROUND_HALF_UP,
        "FLOOR": ROUND_FLOOR,
        "CEILING": ROUND_CEILING,
        "TRUNCATE": ROUND_DOWN,
    }[spec.rounding_mode]
    return value.quantize(quantum, rounding=mode)


def evaluate_formula(spec: DeterministicFormula, value: float) -> float:
    parsed = _parse_expression(spec.expression)
    source_value = _decimal(value) / _decimal(spec.source_unit_scale)
    result = _round_value(_eval_node(parsed, source_value), spec)
    bounded = max(_decimal(spec.minimum_points), min(_decimal(spec.maximum_points), result))
    return round(float(bounded), 6)


def evaluate_formula_range(
    spec: DeterministicFormula,
    lower: float,
    upper: float,
) -> tuple[float, float]:
    """Return conservative bounds for a supported one-variable expression."""

    if lower > upper:
        raise ValueError("formula range lower must not exceed upper")
    parsed = _parse_expression(spec.expression)
    source_scale = _decimal(spec.source_unit_scale)
    source_lower = _decimal(lower) / source_scale
    source_upper = _decimal(upper) / source_scale
    result_lower, result_upper = _eval_interval(
        parsed,
        source_lower,
        source_upper,
    )
    rounded_lower = _round_value(result_lower, spec)
    rounded_upper = _round_value(result_upper, spec)
    minimum = _decimal(spec.minimum_points)
    maximum = _decimal(spec.maximum_points)
    bounded_lower = max(minimum, min(maximum, rounded_lower))
    bounded_upper = max(minimum, min(maximum, rounded_upper))
    return round(float(bounded_lower), 6), round(float(bounded_upper), 6)


def compile_arithmetic_formula(
    literal: str,
    *,
    maximum_points: float,
    source_unit_scale: float = 1,
) -> DeterministicFormula | None:
    """Compile a strict source formula such as ``점수=(실적금액/1억)*10``.

    Free-form prose and multi-variable formulae deliberately return ``None``.
    This keeps automatic activation broad enough for common deterministic
    tables while preserving REVIEW for semantics that were not fully stated.
    """

    source = _normalize_source(literal).strip()
    if not source or len(source) > 1_000:
        return None
    max_match = _CAP_MAX_RE.search(source)
    min_match = _CAP_MIN_RE.search(source)
    maximum = float(max_match.group("value")) if max_match else float(maximum_points)
    minimum = float(min_match.group("value")) if min_match else 0.0
    if maximum <= 0 or maximum > maximum_points or minimum < 0 or minimum > maximum:
        return None

    rounding_mode: Literal["NONE", "HALF_UP", "FLOOR", "CEILING", "TRUNCATE"] = "NONE"
    rounding_digits = 6
    round_match = _ROUND_RE.search(source)
    if round_match:
        rounding_digits = int(round_match.group("digits"))
        rounding_mode = {
            "반올림": "HALF_UP",
            "버림": "FLOOR",
            "절사": "TRUNCATE",
            "올림": "CEILING",
        }[round_match.group("mode")]

    # Only the explicit right-hand side of an equals sign is executable.  A
    # prose description without this boundary remains REVIEW.
    parts = re.split(r"[=＝]", source, maxsplit=1)
    if len(parts) != 2:
        return None
    expression = parts[1]
    expression = _CAP_MAX_RE.sub("", expression)
    expression = _CAP_MIN_RE.sub("", expression)
    expression = _ROUND_RE.sub("", expression)
    expression = re.split(r"[,;]|(?:단,)|(?:다만)", expression, maxsplit=1)[0]
    tokens_found = [token for token in _METRIC_TOKENS if token in expression]
    if "x" in expression.casefold():
        expression = re.sub(r"(?i)\bx\b", "x", expression)
    elif len(tokens_found) == 1:
        expression = expression.replace(tokens_found[0], "x")
    else:
        return None
    expression = re.sub(
        r"(?<=\d)\s*(?:천만원|천만|백만원|백만|억원|억|만원|만|천원|천|점|원|건|명|인|개|회|대|년|%|퍼센트)",
        "",
        expression,
    )
    expression = re.sub(r"\s+", "", expression)
    if not re.fullmatch(r"[x0-9eE.+\-*/()]+", expression):
        return None
    try:
        spec = DeterministicFormula(
            expression=expression,
            source_unit_scale=source_unit_scale,
            minimum_points=minimum,
            maximum_points=maximum,
            rounding_mode=rounding_mode,
            rounding_digits=rounding_digits,
        )
        # Compile-time probes reject zero division and obviously explosive
        # expressions before a company fact is ever considered.
        for probe in (0.0, 1.0, 10.0):
            evaluate_formula(spec, probe)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return spec


def compile_category_formula(
    literal: str,
    *,
    maximum_points: float,
) -> tuple[CategoryScore, ...] | None:
    """Compile explicit category-to-point rows from one verified literal.

    Accepted rows must be separated by a newline or semicolon.  Deliberately
    avoiding fuzzy comma heuristics prevents a credit-rating label list from
    being grouped under the wrong point value.
    """

    source = _normalize_source(literal).strip()
    rows = [row.strip(" ,") for row in re.split(r"[;\n]+", source) if row.strip(" ,")]
    compiled: list[CategoryScore] = []
    seen: set[str] = set()
    for row in rows:
        match = _CATEGORY_ROW_RE.fullmatch(row)
        if match is None:
            # Silently dropping one malformed source row could activate an
            # incomplete category table and award a score to the wrong value.
            return None
        points = float(match.group("points"))
        if points > maximum_points:
            return None
        label = re.sub(r"^(?:등급|평가등급|신용등급)\s*", "", match.group("label")).strip()
        values = tuple(
            value.strip()
            for value in re.split(r"[/·]", label)
            if value.strip()
        )
        normalized = {_normalize_category(value) for value in values}
        if not values or len(normalized) != len(values) or seen & normalized:
            return None
        seen.update(normalized)
        try:
            compiled.append(CategoryScore(values=values, points=points))
        except ValueError:
            return None
    return tuple(compiled) if len(compiled) >= 2 else None


def boolean_categories_complete(categories: tuple[CategoryScore, ...]) -> bool:
    """Require one disjoint, explicit source row for true and false."""

    if len(categories) != 2:
        return False
    true_rows: list[int] = []
    false_rows: list[int] = []
    for index, row in enumerate(categories):
        values = {_normalize_category(item) for item in row.values}
        if values & _BOOLEAN_TRUE_CATEGORIES:
            true_rows.append(index)
        if values & _BOOLEAN_FALSE_CATEGORIES:
            false_rows.append(index)
    return (
        len(true_rows) == 1
        and len(false_rows) == 1
        and true_rows[0] != false_rows[0]
    )


def category_values_are_disjoint(categories: tuple[CategoryScore, ...]) -> bool:
    """Reject aliases that normalize to the same value across source rows."""

    seen: set[str] = set()
    for row in categories:
        normalized = {_normalize_category(item) for item in row.values}
        if seen & normalized:
            return False
        seen.update(normalized)
    return True


def category_points(
    categories: tuple[CategoryScore, ...],
    value: str | bool,
) -> float | None:
    if isinstance(value, bool):
        aliases = (
            _BOOLEAN_TRUE_CATEGORIES if value else _BOOLEAN_FALSE_CATEGORIES
        )
        matches = [
            row.points
            for row in categories
            if aliases & {_normalize_category(item) for item in row.values}
        ]
        return matches[0] if len(matches) == 1 else None
    normalized = _normalize_category(value)
    matches = [
        row.points
        for row in categories
        if normalized in {_normalize_category(item) for item in row.values}
    ]
    return matches[0] if len(matches) == 1 else None


def _strictly_descending(values: Sequence[Decimal]) -> bool:
    return all(left > right for left, right in zip(values, values[1:], strict=False))


def _case_table_rows_are_safe(
    rows: Sequence[CompiledCaseTableRow],
    *,
    value_kind: CaseTableValueKind,
    maximum_points: float | None,
) -> bool:
    if not rows or len(rows) > 100 or isinstance(maximum_points, bool):
        return False
    try:
        maximum = _decimal(maximum_points) if maximum_points is not None else None
        points = [_decimal(row.points) for row in rows]
    except ValueError:
        return False
    if maximum is not None and maximum <= 0:
        return False
    if any(point < 0 or (maximum is not None and point > maximum) for point in points):
        return False

    if value_kind == "CATEGORICAL":
        seen: set[str] = set()
        for row in rows:
            if (
                row.operator != "IN"
                or row.comparison_value is not None
                or not row.category_values
            ):
                return False
            normalized = {_normalize_category(value) for value in row.category_values}
            if (
                any(not value for value in normalized)
                or len(normalized) != len(row.category_values)
                or seen & normalized
            ):
                return False
            seen.update(normalized)
        return True

    if value_kind not in {"NUMERIC", "DISCRETE"}:
        return False
    if any(
        row.comparison_value is None or row.category_values or row.operator == "IN"
        for row in rows
    ):
        return False
    try:
        comparisons = [_decimal(row.comparison_value) for row in rows]
    except ValueError:
        return False
    if not _strictly_descending(comparisons):
        return False
    if any(right > left for left, right in zip(points, points[1:], strict=False)):
        return False

    if value_kind == "NUMERIC":
        return all(row.operator == "GTE" for row in rows)

    if any(value != value.to_integral_value() for value in comparisons):
        return False
    saw_equality = False
    for row in rows:
        if row.operator == "EQ":
            saw_equality = True
        elif row.operator != "GTE" or saw_equality:
            return False
    return True


def compile_case_table(
    rows: Sequence[CaseTableRowLiteral],
    *,
    value_kind: CaseTableValueKind,
    maximum_points: float | None = None,
) -> CompiledCaseTable | None:
    """Compile source-order CASE rows without inventing a fallback case."""

    source_rows = tuple(rows)
    if (
        not source_rows
        or len(source_rows) > 100
        or any(not isinstance(row, CaseTableRowLiteral) for row in source_rows)
        or isinstance(maximum_points, bool)
    ):
        return None
    try:
        maximum = _decimal(maximum_points) if maximum_points is not None else None
    except ValueError:
        return None
    if maximum is not None and maximum <= 0:
        return None

    compiled: list[CompiledCaseTableRow] = []
    try:
        for row in source_rows:
            award = _decimal(row.award_value)
            if row.award_kind == "PERCENT_OF_MAX":
                if maximum is None:
                    return None
                award = maximum * award / Decimal("100")
            if award < 0 or (maximum is not None and award > maximum):
                return None
            compiled.append(
                CompiledCaseTableRow(
                    operator=row.operator,
                    comparison_value=row.comparison_value,
                    category_values=row.category_values,
                    points=round(float(award), 6),
                )
            )
        return CompiledCaseTable(
            value_kind=value_kind,
            rows=tuple(compiled),
            maximum_points=(float(maximum) if maximum is not None else None),
        )
    except (OverflowError, TypeError, ValueError):
        return None


def case_table_points(
    table: CompiledCaseTable,
    value: int | float | Decimal | str | bool,
) -> float | None:
    """Return the first explicit matching row, or ``None`` when unscorable."""

    if table.value_kind == "CATEGORICAL":
        if not isinstance(value, str):
            return None
        normalized = _normalize_category(value)
        if not normalized:
            return None
        for row in table.rows:
            if normalized in {_normalize_category(item) for item in row.category_values}:
                return row.points
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        actual = _decimal(value)
    except ValueError:
        return None
    if table.value_kind == "DISCRETE" and actual != actual.to_integral_value():
        return None
    for row in table.rows:
        assert row.comparison_value is not None
        comparison = _decimal(row.comparison_value)
        if row.operator == "GTE" and actual >= comparison:
            return row.points
        if row.operator == "EQ" and actual == comparison:
            return row.points
    return None
