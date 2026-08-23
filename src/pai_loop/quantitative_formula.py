from __future__ import annotations

import ast
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
