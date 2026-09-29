from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from importlib import resources
from itertools import combinations
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .auth import public_read_allowed, require_api_key
from .evaluator import evidence_state, fact_is_effective
from .eligibility_policy import load_public_company_profile
from .integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ExtractionPayload,
    QuantitativeTableCandidate,
    evidence_quote_matches_source,
)
from .models import AnalysisRun, CompanyFact, CompanyPerformanceRecord, Notice, ScoreSnapshot
from .pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_METADATA_KIND,
    PPS_METADATA_SCHEMA,
    PPS_PROCESSING_VERSION,
    _current_manifest_attempts,
    _validated_manifest_attachments,
)
from .quantitative_rule_extraction import (
    AttachmentDocumentBinding,
    ImmutableQuantitativeCase,
    ImmutableQuantitativeRuleCandidate,
    ImmutableQuantitativeTable,
    QuantitativeCandidateProfile,
    ValidatedQuantitativeAttachmentRecord,
    merge_validated_quantitative_records,
    _case_condition_matches,
    _case_award_matches_literal,
    _normalise_anchor_text,
    _score_cell_matches,
    _short_count_literal_has_owned_context,
)
from .public_performance import load_public_performance_seed
from .quantitative_formula import (
    CaseTableRowLiteral,
    CategoryScore,
    CompiledCaseTable,
    DeterministicFormula,
    boolean_categories_complete,
    category_points,
    category_values_are_disjoint,
    case_table_points,
    compile_arithmetic_formula,
    compile_case_table,
    compile_category_formula,
    compile_credit_rating_values,
    evaluate_formula,
    evaluate_formula_range,
)
from .quantitative_performance import (
    PerformanceRecognitionScope,
    _unsupported_recognition_reason,
    derive_performance_value,
    parse_performance_recognition_scope,
)
from .quantitative_financial import (
    FinancialRecognitionScope,
    derive_financial_value,
    load_financial_statement,
    parse_financial_recognition_scope,
)
from .quantitative_out_of_scope import out_of_scope_reason
from .quantitative_row_approval import (
    ROW_APPROVAL_ASSUMPTION,
    ROW_APPROVAL_FACT_KEY,
    ROW_APPROVAL_REASON,
    ROW_APPROVAL_SOURCE,
    ROW_APPROVAL_WARNING,
    SUFFICIENT_ROW_FACT_KEY,
    SUFFICIENT_ROW_SOURCE,
    SUFFICIENT_ROW_WARNING,
    SUFFICIENT_WAIVABLE_ISSUE_CODES,
    WAIVABLE_ISSUE_CODES,
    QuantitativeRowApproval,
    QuantitativeSufficientRowApproval,
    credit_conditions_digest,
    hyphen_case_rows_match,
    raw_candidate_sha256,
    row_program,
    sufficient_row_choices,
)
from .quantitative_company_first import (
    COMPANY_FIRST_FLOOR_NOTE,
    COMPANY_FIRST_LABEL,
    COMPANY_FIRST_REASON,
    COMPANY_FIRST_STATEMENT,
    BetaScore,
    beta_score,
    company_credit_grade,
    company_first_enabled,
    financial_ratios,
)
from .quantitative_personnel import (
    DerivedPersonnelValue,
    PersonnelRecognitionScope,
    derive_personnel_value,
    load_personnel_roster,
    parse_personnel_recognition_scope,
    personnel_reference_time,
)


QUANTITATIVE_ENGINE_VERSION = "pai-loop-quantitative-engine-1.9.1"
QUANTITATIVE_PROFILE_RESOURCE = "data/quantitative_notice_profiles.json"

EstimateStatus = Literal["CONFIRMED", "ESTIMATED", "UNSCORABLE", "REVIEW"]
# 개별 항목만 가질 수 있는 추가 상태. 회사 데이터(fact)는 이 값을 쓸 수 없고,
# 공고 전체 판정에도 쓰이지 않는다.
CriterionStatus = Literal[
    "CONFIRMED", "ESTIMATED", "UNSCORABLE", "REVIEW", "OUT_OF_SCOPE"
]
ReadinessBand = Literal["GREEN", "YELLOW", "RED", "GRAY"]
SourceValidationStatus = Literal[
    "SOURCE_VALIDATED",
    "REVIEW_REQUIRED",
    "INCOMPLETE",
    "MISSING",
    "NOT_APPLICABLE",
]
ActivationStatus = Literal[
    "AUTO_ACTIVE", "PARTIAL_ACTIVE", "PARTIAL_SOURCE", "REVIEW_REQUIRED",
    "NOT_APPLICABLE",
]
PublicCriterionDisplayCode = Literal[
    "PERFORMANCE_AMOUNT",
    "PERFORMANCE_COUNT",
    "PERFORMANCE",
    "PERSONNEL_COUNT",
    "CERTIFICATION_COUNT",
    "CREDIT_RATING",
    "FINANCIAL_RATIO",
    "BUSINESS_YEARS",
    "FACILITY_EQUIPMENT_COUNT",
    "AWARD_COUNT",
    "LOCAL_PRESENCE",
    "SOCIAL_RESPONSIBILITY",
    "SAFETY_HEALTH",
    "OTHER",
]

PUBLIC_QUANTITATIVE_CRITERIA_SCHEMA_VERSION = (
    "public-quantitative-criteria-1.0.0"
)
_PUBLIC_CRITERION_DISPLAY_CODE_BY_CATEGORY: dict[
    str, PublicCriterionDisplayCode
] = {
    "PERFORMANCE_AMOUNT": "PERFORMANCE_AMOUNT",
    "PERFORMANCE_COUNT": "PERFORMANCE_COUNT",
    "PERSONNEL_COUNT": "PERSONNEL_COUNT",
    "CERTIFICATION_COUNT": "CERTIFICATION_COUNT",
    "CREDIT_RATING": "CREDIT_RATING",
    "FINANCIAL_RATIO": "FINANCIAL_RATIO",
    "BUSINESS_YEARS": "BUSINESS_YEARS",
    "FACILITY_EQUIPMENT_COUNT": "FACILITY_EQUIPMENT_COUNT",
    "AWARD_COUNT": "AWARD_COUNT",
    "LOCAL_PRESENCE": "LOCAL_PRESENCE",
    "수행실적": "PERFORMANCE",
    "전문인력": "PERSONNEL_COUNT",
    "경영상태": "CREDIT_RATING",
    "사회적책임": "SOCIAL_RESPONSIBILITY",
    "안전보건": "SAFETY_HEALTH",
}
_PUBLIC_CRITERION_LABEL_BY_DISPLAY_CODE: dict[
    PublicCriterionDisplayCode, str
] = {
    "PERFORMANCE_AMOUNT": "용역수행 실적(금액)",
    "PERFORMANCE_COUNT": "용역수행 실적(건수)",
    "PERFORMANCE": "용역수행 실적",
    "PERSONNEL_COUNT": "전문인력 보유",
    "CERTIFICATION_COUNT": "인증 보유",
    "CREDIT_RATING": "제안업체 경영상태",
    "FINANCIAL_RATIO": "재무비율",
    "BUSINESS_YEARS": "업력",
    "FACILITY_EQUIPMENT_COUNT": "시설·장비 보유",
    "AWARD_COUNT": "수상 실적",
    "LOCAL_PRESENCE": "지역 소재",
    "SOCIAL_RESPONSIBILITY": "사회적 책임",
    "SAFETY_HEALTH": "안전·보건",
    "OTHER": "기타 정량 평가항목",
}


class QuantModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PublicQuantitativeCriterionSnapshot(QuantModel):
    """Public-only score row persisted without source or company bindings."""

    display_code: PublicCriterionDisplayCode
    max_points: float = Field(gt=0)
    estimated_points: float | None = Field(ge=0)
    lower_points: float = Field(ge=0)
    upper_points: float = Field(ge=0)
    status: CriterionStatus

    @model_validator(mode="before")
    @classmethod
    def reject_coerced_or_non_finite_numbers(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        for field_name in (
            "max_points",
            "estimated_points",
            "lower_points",
            "upper_points",
        ):
            field_value = value.get(field_name)
            if field_name == "estimated_points" and field_value is None:
                continue
            try:
                is_finite_number = (
                    not isinstance(field_value, bool)
                    and isinstance(field_value, (int, float))
                    and math.isfinite(float(field_value))
                )
            except (OverflowError, TypeError, ValueError):
                is_finite_number = False
            if not is_finite_number:
                raise ValueError(
                    "public criterion points must be finite JSON numbers"
                )
        return value

    @model_validator(mode="after")
    def validate_points_and_status(self) -> "PublicQuantitativeCriterionSnapshot":
        for field_value in (
            self.max_points,
            self.estimated_points,
            self.lower_points,
            self.upper_points,
        ):
            if field_value is not None and _canonical_public_points(field_value) != field_value:
                raise ValueError("public criterion points must use two-decimal precision")
        if self.lower_points > self.upper_points or self.upper_points > self.max_points:
            raise ValueError("public criterion range must fit within max_points")
        if self.estimated_points is not None and (
            self.status not in {"CONFIRMED", "ESTIMATED"}
            or self.estimated_points != self.lower_points
            or self.estimated_points != self.upper_points
        ):
            raise ValueError("public exact estimate must equal both range bounds")
        if self.status == "CONFIRMED" and (
            self.lower_points != self.upper_points
            or self.estimated_points != self.lower_points
        ):
            raise ValueError("public confirmed criterion must be exact")
        if (
            self.status == "ESTIMATED"
            and self.lower_points == self.upper_points
            and self.estimated_points != self.lower_points
        ):
            raise ValueError("public exact estimated criterion must retain its estimate")
        if self.status in {"REVIEW", "UNSCORABLE"} and self.estimated_points is not None:
            raise ValueError("public unresolved criterion cannot have an exact estimate")
        if self.status == "OUT_OF_SCOPE" and (
            self.lower_points != 0 or self.upper_points != self.max_points
        ):
            raise ValueError("public excluded criterion cannot imply an awarded score")
        return self


class PublicQuantitativeCriteriaSnapshot(QuantModel):
    schema_version: Literal["public-quantitative-criteria-1.0.0"]
    items: list[PublicQuantitativeCriterionSnapshot] = Field(max_length=200)


class SourceAnchor(QuantModel):
    document_label: str = Field(min_length=1, max_length=200)
    document_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    section: str = Field(min_length=1, max_length=300)
    page: int | None = Field(default=None, ge=1)
    quote: str | None = Field(default=None, max_length=1_000)


class ScoreBracket(QuantModel):
    bracket_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=300)
    min_value: float | None = None
    max_value: float | None = None
    min_inclusive: bool = True
    max_inclusive: bool = False
    boolean_value: bool | None = None
    points: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ScoreBracket":
        if (
            self.min_value is not None
            and self.max_value is not None
            and (
                self.min_value > self.max_value
                or (
                    self.min_value == self.max_value
                    and not (self.min_inclusive and self.max_inclusive)
                )
            )
        ):
            raise ValueError("score bracket min_value must be below max_value")
        return self


class QuantitativeCriterion(QuantModel):
    criterion_id: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=300)
    max_points: float = Field(gt=0)
    metric_key: str = Field(min_length=1, max_length=160)
    unit: str | None = Field(default=None, max_length=80)
    formula_type: Literal[
        "BRACKET",
        "BOOLEAN",
        "THRESHOLD",
        "FORMULA",
        "CATEGORICAL",
        "CASE_TABLE",
    ]
    formula: str = Field(min_length=1, max_length=1_000)
    brackets: list[ScoreBracket] = Field(default_factory=list)
    categories: list[CategoryScore] = Field(default_factory=list, max_length=100)
    threshold_operator: Literal["GT", "GTE", "LT", "LTE", "EQ"] | None = None
    threshold_value: float | None = None
    threshold_points_if_met: float | None = Field(default=None, ge=0)
    threshold_points_if_not_met: float | None = Field(default=None, ge=0)
    deterministic_formula: DeterministicFormula | None = None
    case_table: CompiledCaseTable | None = None
    performance_scope: PerformanceRecognitionScope | None = None
    financial_scope: FinancialRecognitionScope | None = None
    personnel_scope: PersonnelRecognitionScope | None = None
    rule_floor_points: float = Field(default=0, ge=0)
    floor_condition: str | None = Field(default=None, max_length=1_000)
    rule_base_points: float | None = Field(default=None, ge=0)
    base_condition: str | None = Field(default=None, max_length=1_000)
    source_anchor: SourceAnchor | None = None
    required_evidence_keys: list[str] = Field(default_factory=list, max_length=30)
    fact_binding_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class QuantitativeFact(QuantModel):
    metric_key: str = Field(min_length=1, max_length=160)
    status: EstimateStatus
    value: float | bool | str | None = None
    lower_value: float | None = None
    upper_value: float | None = None
    # Only an independently verified submission audit may set this. Missing
    # company records, empty documents and numeric zero never imply this state.
    submission_status: Literal["NOT_SUBMITTED"] | None = None
    evidence_key: str | None = Field(default=None, max_length=240)
    evidence_reference: str | None = Field(default=None, max_length=240)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    fact_binding_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    confidence: float = Field(default=0, ge=0, le=1)
    rationale: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def validate_range(self) -> "QuantitativeFact":
        if self.submission_status is not None and (
            self.status != "CONFIRMED" or self.value is not None
            or self.lower_value is not None or self.upper_value is not None
            or not self.evidence_reference or not self.evidence_sha256
            or not self.fact_binding_sha256
        ):
            raise ValueError("submission status requires an explicit confirmed, evidenced criterion binding without a numeric value")
        if (
            self.lower_value is not None
            and self.upper_value is not None
            and self.lower_value > self.upper_value
        ):
            raise ValueError("fact lower_value must not exceed upper_value")
        return self


class QuantitativeReviewCriterion(QuantModel):
    """One source-table row excluded from automatic calculation.

    The row's label and maximum are retained only so a partial result keeps the
    full table denominator. No value or formula is invented for the row.
    """

    criterion_id: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=300)
    max_points: float = Field(gt=0)
    issue_codes: list[str] = Field(min_length=1, max_length=30)


class QuantitativeEstimateRequest(QuantModel):
    ruleset_version: str = Field(min_length=1, max_length=160)
    rule_source_status: Literal[
        "AVAILABLE", "MISSING", "INCOMPLETE", "NOT_APPLICABLE"
    ] = "AVAILABLE"
    source_validation_status: SourceValidationStatus = "REVIEW_REQUIRED"
    activation_status: ActivationStatus = "REVIEW_REQUIRED"
    activation_reasons: list[str] = Field(default_factory=list, max_length=100)
    minimum_score: float | None = Field(default=None, ge=0)
    criteria: list[QuantitativeCriterion] = Field(default_factory=list, max_length=100)
    review_criteria: list[QuantitativeReviewCriterion] = Field(
        default_factory=list, max_length=100
    )
    facts: list[QuantitativeFact] = Field(default_factory=list, max_length=200)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    missing_reason: str | None = Field(default=None, max_length=2_000)
    source_anchor: SourceAnchor | None = None

    @model_validator(mode="after")
    def validate_activation_contract(self) -> "QuantitativeEstimateRequest":
        if self.activation_status == "AUTO_ACTIVE" and (
            self.rule_source_status != "AVAILABLE"
            or self.source_validation_status != "SOURCE_VALIDATED"
            or self.activation_reasons
            or self.review_criteria
        ):
            raise ValueError(
                "AUTO_ACTIVE requires source-validated AVAILABLE rules without activation reasons"
            )
        if self.activation_status == "PARTIAL_ACTIVE" and (
            self.rule_source_status != "AVAILABLE"
            or self.source_validation_status != "REVIEW_REQUIRED"
            or not self.activation_reasons
            or not self.criteria
            or not self.review_criteria
        ):
            raise ValueError(
                "PARTIAL_ACTIVE requires verified criteria plus explicitly reviewed rows"
            )
        if self.activation_status == "PARTIAL_SOURCE" and (
            self.rule_source_status != "INCOMPLETE"
            or self.source_validation_status != "INCOMPLETE"
            or not self.activation_reasons
            or not self.criteria
            or self.review_criteria
            or self.minimum_score is not None
        ):
            raise ValueError(
                "PARTIAL_SOURCE requires source-validated rows inside an incomplete"
                " manifest, with the unresolved remainder stated as reasons"
            )
        if self.activation_status != "PARTIAL_ACTIVE" and self.review_criteria:
            raise ValueError("review_criteria are allowed only for PARTIAL_ACTIVE")
        return self


class CriterionEstimate(QuantModel):
    criterion_id: str
    category: str
    label: str
    max_points: float
    formula: str
    rule_floor_points: float
    floor_condition: str | None
    rule_base_points: float | None
    base_condition: str | None
    source_anchor: SourceAnchor | None
    evidence_key: str | None
    evidence_reference: str | None = None
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    fact_binding_sha256: str | None
    estimated_points: float | None
    lower_points: float
    upper_points: float
    confidence: float
    status: CriterionStatus
    rationale: str
    assumptions: list[str] = Field(default_factory=list)


class EvidenceObservation(QuantModel):
    observation_key: str
    label: str
    value: int | float | str
    unit: str | None = None
    status: Literal["CANDIDATE_ONLY", "NOT_APPLIED"]
    evidence_key: str
    rationale: str


class QuantitativeEstimateResult(QuantModel):
    engine_version: str
    ruleset_version: str
    source_anchor: SourceAnchor | None
    rule_source_status: Literal[
        "AVAILABLE", "MISSING", "INCOMPLETE", "NOT_APPLICABLE"
    ]
    source_validation_status: SourceValidationStatus
    activation_status: ActivationStatus
    activation_reasons: list[str] = Field(default_factory=list)
    overall_status: EstimateStatus
    total_max_points: float | None
    confirmed_points: float | None
    estimated_points: float | None
    lower_points: float | None
    upper_points: float | None
    unscorable_points: float | None
    # 정성·총괄·가격처럼 정량 합계·상한에서 제외해 별도로 보존한 배점.
    out_of_scope_points: float = 0
    evidence_coverage_pct: float
    readiness_pct: float | None
    readiness_band: ReadinessBand
    minimum_score: float | None
    meets_minimum: bool | None
    confidence: float
    criteria: list[CriterionEstimate] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence_observations: list[EvidenceObservation] = Field(default_factory=list)
    opinion: str
    separation_notice: str


class QuantitativeProfileError(RuntimeError):
    pass


def _round_points(value: float) -> float:
    return float(
        Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def _canonical_public_points(value: float) -> float | None:
    try:
        rounded = _round_points(value)
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        return None
    return rounded if math.isfinite(rounded) else None


def _sum_public_points(values: Iterable[float]) -> float | None:
    try:
        total = sum((Decimal(str(value)) for value in values), Decimal("0"))
        rounded = float(
            total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        return None
    return rounded if math.isfinite(rounded) else None


def _is_discrete_count_bracket(criterion: QuantitativeCriterion) -> bool:
    return (
        criterion.formula_type == "BRACKET"
        and criterion.metric_key in _DISCRETE_COUNT_FACT_KEYS
    )


# Count facts/bounds pass through float fields. Reject the first value at which
# adjacent integers can collapse to the same float, including already-rounded
# model values, before doing any float conversion here.
_MAX_SAFE_COUNT_INTEGER = 2**53 - 1


def _finite_integer(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (
        abs(value) <= _MAX_SAFE_COUNT_INTEGER
        and math.isfinite(value)
        and float(value).is_integer()
    )


def _count_bracket_bounds(bracket: ScoreBracket) -> tuple[int, int | None] | None:
    """Project one source bracket onto nonnegative integers without changing it."""
    if any(value is not None and not _finite_integer(value) for value in (bracket.min_value, bracket.max_value)):
        return None
    lower = 0 if bracket.min_value is None else max(0, int(bracket.min_value) + (not bracket.min_inclusive))
    upper = None if bracket.max_value is None else int(bracket.max_value) - (not bracket.max_inclusive)
    if upper is not None and lower > upper:
        return None
    return lower, upper


def _count_bracket_rule_error(criterion: QuantitativeCriterion) -> str | None:
    bounds = [_count_bracket_bounds(bracket) for bracket in criterion.brackets]
    if not bounds or any(bound is None for bound in bounds):
        return "정수 개수 배점 구간의 경계가 정수가 아니거나 포함하는 비음수 정수가 없습니다."
    ordered = sorted(
        (bound for bound in bounds if bound is not None),
        key=lambda bound: (bound[0], math.inf if bound[1] is None else bound[1]),
    )
    next_integer = 0
    for index, (lower, upper) in enumerate(ordered):
        if lower < next_integer:
            return "정수 개수 배점 구간이 같은 정수에서 서로 겹칩니다."
        if lower > next_integer:
            return "정수 개수 배점 구간 사이에 점수가 정의되지 않은 정수 공백이 있습니다."
        if upper is None:
            return None if index == len(ordered) - 1 else "열린 상한 구간 뒤에 다른 구간을 둘 수 없습니다."
        next_integer = upper + 1
    return "정수 개수 배점 구간이 가능한 최댓값까지 빠짐없이 이어지지 않습니다."


def _rule_error(criterion: QuantitativeCriterion) -> str | None:
    if criterion.source_anchor is None:
        return "평가표 원문 위치가 연결되지 않았습니다."
    if not criterion.required_evidence_keys:
        return "필요 증빙 키가 정의되지 않았습니다."
    if (
        criterion.performance_scope is not None
        and criterion.performance_scope.metric_key != criterion.metric_key
    ):
        return "실적 인정범위와 평가항목의 회사 사실 키가 일치하지 않습니다."
    if (
        criterion.fact_binding_sha256 is not None
        and criterion.metric_key
        in {"company.performance.amount", "company.performance.count"}
        and criterion.performance_scope is None
    ):
        return "공고별 실적 평가항목에 원문 인정기간·범위·VAT 조건이 없습니다."
    if criterion.rule_floor_points > criterion.max_points:
        return "원문상 최소점수가 항목 만점을 초과합니다."
    if criterion.rule_floor_points and not criterion.floor_condition:
        return "원문상 최소점수의 적용 조건이 정의되지 않았습니다."
    if criterion.rule_base_points is not None and criterion.rule_base_points > criterion.max_points:
        return "원문상 기본점수가 항목 만점을 초과합니다."
    if criterion.rule_base_points is not None and not criterion.base_condition:
        return "원문상 기본점수의 적용 조건이 정의되지 않았습니다."
    threshold_configured = any(
        value is not None
        for value in (
            criterion.threshold_operator,
            criterion.threshold_value,
            criterion.threshold_points_if_met,
            criterion.threshold_points_if_not_met,
        )
    )
    if criterion.formula_type == "CASE_TABLE":
        if (
            criterion.case_table is None
            or criterion.brackets
            or criterion.categories
            or criterion.deterministic_formula is not None
            or threshold_configured
        ):
            return "CASE_TABLE 산식이 완전하게 정의되지 않았습니다."
        if (
            criterion.case_table.maximum_points is not None
            and criterion.case_table.maximum_points != criterion.max_points
        ):
            return "CASE_TABLE의 배점 상한이 평가항목 만점과 일치하지 않습니다."
        if any(row.points > criterion.max_points for row in criterion.case_table.rows):
            return "CASE_TABLE 배점이 항목 만점을 초과합니다."
        return None
    if criterion.formula_type == "FORMULA":
        if criterion.deterministic_formula is None:
            return "원문 산식을 안전한 결정론 산식으로 변환하지 못했습니다."
        if (
            criterion.brackets
            or criterion.categories
            or criterion.case_table is not None
            or threshold_configured
        ):
            return "FORMULA 산식에 다른 배점 구조를 함께 사용할 수 없습니다."
        if criterion.deterministic_formula.maximum_points > criterion.max_points:
            return "산식 상한이 항목 만점을 초과합니다."
        return None
    if criterion.formula_type == "CATEGORICAL":
        if (
            criterion.brackets
            or not criterion.categories
            or criterion.deterministic_formula is not None
            or criterion.case_table is not None
            or threshold_configured
        ):
            return "범주형 배점표가 완전하게 정의되지 않았습니다."
        if not category_values_are_disjoint(tuple(criterion.categories)):
            return "범주형 배점 값이 서로 중복됩니다."
        if any(row.points > criterion.max_points for row in criterion.categories):
            return "범주형 배점이 항목 만점을 초과합니다."
        if criterion.metric_key == "company.local_presence" and not boolean_categories_complete(
            tuple(criterion.categories)
        ):
            return "보유 여부 배점은 보유·미보유 양쪽 원문 행이 각각 필요합니다."
        return None
    if criterion.formula_type == "THRESHOLD":
        if (
            criterion.brackets
            or criterion.categories
            or criterion.deterministic_formula is not None
            or criterion.case_table is not None
            or criterion.threshold_operator is None
            or criterion.threshold_value is None
            or criterion.threshold_points_if_met is None
            or criterion.threshold_points_if_not_met is None
        ):
            return "임계값 산식의 비교조건 또는 충족·미충족 배점이 불완전합니다."
        if max(
            criterion.threshold_points_if_met,
            criterion.threshold_points_if_not_met,
        ) > criterion.max_points:
            return "임계값 배점이 항목 만점을 초과합니다."
        return None
    if not criterion.brackets:
        return "배점 구간이 정의되지 않았습니다."
    if (
        criterion.categories
        or criterion.deterministic_formula is not None
        or criterion.case_table is not None
        or threshold_configured
    ):
        return "배점 구간 산식에 다른 배점 구조를 함께 사용할 수 없습니다."
    if any(bracket.points > criterion.max_points for bracket in criterion.brackets):
        return "배점 구간 점수가 항목 만점을 초과합니다."
    if any(bracket.points < criterion.rule_floor_points for bracket in criterion.brackets):
        return "배점 구간 점수가 원문상 최소점수보다 낮습니다."
    if criterion.formula_type == "BOOLEAN":
        values = [bracket.boolean_value for bracket in criterion.brackets]
        if len(values) != 2 or sorted(
            value for value in values if value is not None
        ) != [False, True]:
            return "BOOLEAN 산식은 true/false 배점 구간이 각각 필요합니다."
        if any(bracket.min_value is not None or bracket.max_value is not None for bracket in criterion.brackets):
            return "BOOLEAN 산식에는 숫자 구간을 함께 사용할 수 없습니다."
        return None

    numeric = sorted(
        criterion.brackets,
        key=lambda item: -math.inf if item.min_value is None else item.min_value,
    )
    if any(item.boolean_value is not None for item in numeric):
        return "BRACKET 산식에는 boolean 구간을 사용할 수 없습니다."
    if _is_discrete_count_bracket(criterion):
        return _count_bracket_rule_error(criterion)
    previous_max: float | None = None
    previous_max_inclusive = False
    for index, item in enumerate(numeric):
        if index == 0 and item.min_value is not None:
            return "배점 구간이 가능한 최솟값부터 빠짐없이 이어지지 않습니다."
        if index and previous_max is None:
            return "열린 상한 구간 뒤에 다른 구간을 둘 수 없습니다."
        if index and item.min_value is None:
            return "첫 구간이 아닌 배점 구간에는 하한값이 필요합니다."
        if previous_max is not None and item.min_value is not None:
            if item.min_value < previous_max or (
                item.min_value == previous_max
                and previous_max_inclusive
                and item.min_inclusive
            ):
                return "배점 구간이 서로 겹칩니다."
            if item.min_value > previous_max or (
                item.min_value == previous_max
                and previous_max_inclusive == item.min_inclusive
            ):
                return "배점 구간 사이에 점수가 정의되지 않은 공백이 있습니다."
        previous_max = item.max_value
        previous_max_inclusive = item.max_inclusive
    if numeric[-1].max_value is not None:
        return "배점 구간이 가능한 최댓값까지 빠짐없이 이어지지 않습니다."
    return None


def _numeric_bracket_matches(bracket: ScoreBracket, value: float) -> bool:
    if bracket.min_value is not None:
        if value < bracket.min_value or (
            value == bracket.min_value and not bracket.min_inclusive
        ):
            return False
    if bracket.max_value is not None:
        if value > bracket.max_value or (
            value == bracket.max_value and not bracket.max_inclusive
        ):
            return False
    return True


def _threshold_matches(
    operator: Literal["GT", "GTE", "LT", "LTE", "EQ"],
    value: float,
    threshold: float,
) -> bool:
    return {
        "GT": value > threshold,
        "GTE": value >= threshold,
        "LT": value < threshold,
        "LTE": value <= threshold,
        "EQ": value == threshold,
    }[operator]


def _points_for_value(
    criterion: QuantitativeCriterion,
    value: float | bool | str,
) -> float | None:
    if _is_discrete_count_bracket(criterion) and (
        not _finite_integer(value) or value < 0
    ):
        return None
    if criterion.formula_type == "CASE_TABLE":
        if criterion.case_table is None:
            return None
        points = case_table_points(criterion.case_table, value)
        return _round_points(points) if points is not None else None
    if criterion.formula_type == "BOOLEAN":
        if not isinstance(value, bool):
            return None
        match = next(
            (item for item in criterion.brackets if item.boolean_value is value),
            None,
        )
    elif criterion.formula_type == "CATEGORICAL":
        if not isinstance(value, (str, bool)):
            return None
        points = category_points(tuple(criterion.categories), value)
        return _round_points(points) if points is not None else None
    elif criterion.formula_type == "THRESHOLD":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if (
            criterion.threshold_operator is None
            or criterion.threshold_value is None
            or criterion.threshold_points_if_met is None
            or criterion.threshold_points_if_not_met is None
        ):
            return None
        points = (
            criterion.threshold_points_if_met
            if _threshold_matches(
                criterion.threshold_operator,
                float(value),
                criterion.threshold_value,
            )
            else criterion.threshold_points_if_not_met
        )
        return _round_points(points)
    elif criterion.formula_type == "FORMULA":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or criterion.deterministic_formula is None
        ):
            return None
        try:
            return _round_points(
                evaluate_formula(criterion.deterministic_formula, float(value))
            )
        except (ValueError, ArithmeticError, OverflowError):
            return None
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        match = next(
            (item for item in criterion.brackets if _numeric_bracket_matches(item, float(value))),
            None,
        )
    return _round_points(match.points) if match else None


def _points_for_numeric_range(
    criterion: QuantitativeCriterion,
    lower: float,
    upper: float,
) -> tuple[float, float] | None:
    if criterion.formula_type == "CASE_TABLE":
        table = criterion.case_table
        if table is None or table.value_kind == "CATEGORICAL" or lower > upper:
            return None
        sample_values: set[float] = {lower, upper}
        for row in table.rows:
            if row.comparison_value is None:
                continue
            comparison = float(row.comparison_value)
            if lower <= comparison <= upper:
                sample_values.add(comparison)
                if table.value_kind == "DISCRETE" and comparison - 1 >= lower:
                    sample_values.add(comparison - 1)
            if row.comparison_upper_value is not None:
                upper_comparison = float(row.comparison_upper_value)
                if lower <= upper_comparison <= upper:
                    sample_values.add(upper_comparison)
                    if table.value_kind == "DISCRETE" and upper_comparison + 1 <= upper:
                        sample_values.add(upper_comparison + 1)
        resolved = [_points_for_value(criterion, value) for value in sample_values]
        # The CASE DSL has no implicit ELSE. If either endpoint, a cutoff, or
        # a representative discrete gap is undefined, the entire company
        # value range remains unscorable rather than discarding that sample.
        if any(points is None for points in resolved):
            return None
        values = [float(points) for points in resolved if points is not None]
        return _round_points(min(values)), _round_points(max(values))
    if criterion.formula_type == "FORMULA":
        if criterion.deterministic_formula is None:
            return None
        try:
            return tuple(
                _round_points(value)
                for value in evaluate_formula_range(
                    criterion.deterministic_formula,
                    lower,
                    upper,
                )
            )
        except (ValueError, ArithmeticError, OverflowError):
            return None
    if criterion.formula_type == "THRESHOLD":
        if (
            criterion.threshold_operator is None
            or criterion.threshold_value is None
            or criterion.threshold_points_if_met is None
            or criterion.threshold_points_if_not_met is None
        ):
            return None
        candidates = {
            _points_for_value(criterion, lower),
            _points_for_value(criterion, upper),
        }
        threshold = criterion.threshold_value
        if (
            criterion.threshold_operator == "EQ"
            and lower < threshold < upper
        ):
            candidates.add(criterion.threshold_points_if_met)
        values = [float(value) for value in candidates if value is not None]
        return (_round_points(min(values)), _round_points(max(values))) if values else None
    if criterion.formula_type != "BRACKET":
        return None
    if _is_discrete_count_bracket(criterion):
        if (
            not _finite_integer(lower) or not _finite_integer(upper)
            or lower < 0 or upper < lower
            or _count_bracket_rule_error(criterion) is not None
        ):
            return None
        points: list[float] = []
        for bracket in criterion.brackets:
            bounds = _count_bracket_bounds(bracket)
            if bounds is None:
                return None
            bracket_lower, bracket_upper = bounds
            intersection_lower = max(int(lower), bracket_lower)
            intersection_upper = int(upper) if bracket_upper is None else min(int(upper), bracket_upper)
            if intersection_lower <= intersection_upper:
                points.append(bracket.points)
        return (_round_points(min(points)), _round_points(max(points))) if points else None
    candidate_points: list[float] = []
    for bracket in criterion.brackets:
        bracket_lower = -math.inf if bracket.min_value is None else bracket.min_value
        bracket_upper = math.inf if bracket.max_value is None else bracket.max_value
        lower_intersects = upper > bracket_lower or (
            upper == bracket_lower and bracket.min_inclusive
        )
        upper_intersects = lower < bracket_upper or (
            lower == bracket_upper and bracket.max_inclusive
        )
        if lower_intersects and upper_intersects:
            candidate_points.append(bracket.points)
    if not candidate_points:
        return None
    return _round_points(min(candidate_points)), _round_points(max(candidate_points))


def _performance_lower_bound_saturates_max(
    criterion: QuantitativeCriterion,
    lower_value: float | None,
) -> bool:
    """Prove that every possible value above a performance lower bound is max.

    This deliberately supports only mechanically bounded numeric programs. It
    is used when some imported records remain uncertain but the independently
    validated records already reach a monotonic top band. No raw value is
    invented and non-monotonic/formula/categorical programs remain blocked.
    """

    if _is_discrete_count_bracket(criterion) and (
        not _finite_integer(lower_value) or lower_value < 0
    ):
        return False
    if (
        not criterion.metric_key.startswith("company.performance.")
        or lower_value is None
        or isinstance(lower_value, bool)
        or not math.isfinite(float(lower_value))
    ):
        return False
    lower = float(lower_value)
    maximum = _round_points(criterion.max_points)
    if _points_for_value(criterion, lower) != maximum:
        return False

    if criterion.formula_type == "CASE_TABLE":
        table = criterion.case_table
        if table is None or table.value_kind == "CATEGORICAL":
            return False
        if table.value_kind == "DISCRETE" and not lower.is_integer():
            return False
        if table.value_kind == "DISCRETE":
            gte_cutoffs = [
                float(row.comparison_value)
                for row in table.rows
                if row.operator == "GTE" and row.comparison_value is not None
            ]
            # Equality-only rows can leave unlisted integer gaps. Saturation is
            # safe only inside the tail covered by an explicit GTE row.
            if not gte_cutoffs or lower < min(gte_cutoffs):
                return False
        # Compiled numeric CASE tables are source-ordered descending cutoffs
        # with non-increasing points. If the lower value is already max, every
        # higher cutoff/value is necessarily the same max band.
        return all(
            row.points == maximum
            for row in table.rows
            if row.comparison_value is not None
            and float(row.comparison_value) >= lower
        )

    if criterion.formula_type == "BRACKET":
        intersecting = []
        for bracket in criterion.brackets:
            upper = math.inf if bracket.max_value is None else bracket.max_value
            intersects = lower < upper or (
                lower == upper and bracket.max_inclusive
            )
            if intersects:
                intersecting.append(bracket)
        return bool(intersecting) and all(
            _round_points(bracket.points) == maximum for bracket in intersecting
        )

    if criterion.formula_type == "THRESHOLD":
        if (
            criterion.threshold_operator is None
            or criterion.threshold_value is None
            or criterion.threshold_points_if_met is None
            or criterion.threshold_points_if_not_met is None
        ):
            return False
        threshold = criterion.threshold_value
        operator = criterion.threshold_operator
        if operator == "GTE":
            outcomes = {"met"} if lower >= threshold else {"met", "unmet"}
        elif operator == "GT":
            outcomes = {"met"} if lower > threshold else {"met", "unmet"}
        elif operator == "LTE":
            outcomes = {"unmet"} if lower > threshold else {"met", "unmet"}
        elif operator == "LT":
            outcomes = {"unmet"} if lower >= threshold else {"met", "unmet"}
        else:  # EQ
            outcomes = {"unmet"} if lower > threshold else {"met", "unmet"}
        points = {
            "met": _round_points(criterion.threshold_points_if_met),
            "unmet": _round_points(criterion.threshold_points_if_not_met),
        }
        return all(points[outcome] == maximum for outcome in outcomes)

    return False


def _criterion_unscored(
    criterion: QuantitativeCriterion,
    *,
    status: EstimateStatus,
    rationale: str,
    evidence_key: str | None = None,
    evidence_reference: str | None = None,
    evidence_sha256: str | None = None,
    assumptions: list[str] | None = None,
) -> CriterionEstimate:
    floor = (
        _round_points(criterion.rule_floor_points)
        if criterion.source_anchor is not None and criterion.floor_condition
        else 0
    )
    floor_assumptions = list(assumptions or [])
    if floor and criterion.floor_condition:
        floor_assumptions.append(
            f"원문상 최소점수 {floor:g}점은 다음 조건에서만 적용됩니다: {criterion.floor_condition}"
        )
    return CriterionEstimate(
        criterion_id=criterion.criterion_id,
        category=criterion.category,
        label=criterion.label,
        max_points=criterion.max_points,
        formula=criterion.formula,
        rule_floor_points=floor,
        floor_condition=criterion.floor_condition,
        rule_base_points=criterion.rule_base_points,
        base_condition=criterion.base_condition,
        source_anchor=criterion.source_anchor,
        evidence_key=evidence_key,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha256,
        fact_binding_sha256=criterion.fact_binding_sha256,
        estimated_points=None,
        lower_points=floor,
        upper_points=criterion.max_points,
        confidence=0,
        status=status,
        rationale=rationale,
        assumptions=floor_assumptions,
    )


def _estimate_criterion(
    criterion: QuantitativeCriterion,
    fact: QuantitativeFact | None,
) -> CriterionEstimate:
    error = _rule_error(criterion)
    if error:
        return _criterion_unscored(criterion, status="REVIEW", rationale=error)
    if fact is None:
        return _criterion_unscored(
            criterion,
            status="UNSCORABLE",
            rationale="연결된 회사 데이터가 없어 점수를 계산하지 않았습니다.",
            assumptions=["누락값을 0점 또는 만점으로 임의 가정하지 않았습니다."],
        )
    fact_audit = {
        "evidence_key": fact.evidence_key,
        "evidence_reference": fact.evidence_reference,
        "evidence_sha256": fact.evidence_sha256,
    }
    if fact.metric_key != criterion.metric_key or fact.evidence_key not in criterion.required_evidence_keys:
        return _criterion_unscored(
            criterion,
            status="REVIEW",
            rationale="회사 데이터의 증빙 키가 평가항목의 허용 증빙과 일치하지 않습니다.",
            **fact_audit,
        )
    if (
        criterion.fact_binding_sha256 is not None
        and fact.fact_binding_sha256 != criterion.fact_binding_sha256
    ):
        missing_binding = fact.fact_binding_sha256 is None
        return _criterion_unscored(
            criterion,
            status="UNSCORABLE" if missing_binding else "REVIEW",
            rationale=(
                "회사 사실의 평가항목 결합 정보가 없어 인정기간·범위·단위 조건을 "
                "확인할 수 없으므로 generic 값을 점수에 적용하지 않았습니다."
                if missing_binding
                else "회사 사실의 결합 정보가 현재 평가항목의 인정기간·범위·단위 조건과 "
                "다릅니다. 현재 조건으로 다시 확인하기 전에는 점수에 적용하지 않습니다."
            ),
            **fact_audit,
        )
    if fact.status in {"REVIEW", "UNSCORABLE"}:
        return _criterion_unscored(
            criterion,
            status=fact.status,
            rationale=fact.rationale or "증빙 상태상 점수를 계산할 수 없습니다.",
            **fact_audit,
        )

    if criterion.performance_scope is not None and (
        criterion.performance_scope.manual_verification_conditions
        or _unsupported_recognition_reason(criterion.performance_scope.source_literal) is not None
    ) and (
        fact.status != "CONFIRMED"
        or not (fact.evidence_reference or "").strip()
        or not fact.evidence_sha256
        or criterion.fact_binding_sha256 is None
        or fact.fact_binding_sha256 != criterion.fact_binding_sha256
    ):
        # These conditions require an attested aggregate for this exact source
        # rule. A register estimate or a saturated lower bound cannot prove
        # per-contract participants or annual contract amounts. Recheck the
        # original text for stored scopes that predate the explicit field.
        return _criterion_unscored(
            criterion,
            status="REVIEW",
            rationale=(
                "참여 인원·연간 계약금액 등 추가 실적 인정조건은 이 평가항목에 "
                "결합된 확인 증빙값이 필요하여 잠정값으로 점수를 계산하지 않았습니다."
            ),
            **fact_audit,
        )

    lower_bound_saturates_max = _performance_lower_bound_saturates_max(
        criterion, fact.lower_value
    )
    if fact.submission_status is not None:
        if (criterion.metric_key != "company.performance.count"
            or criterion.fact_binding_sha256 is None
            or criterion.formula_type != "CASE_TABLE" or criterion.case_table is None):
            return _criterion_unscored(criterion, status="REVIEW",
                rationale="미제출 상태에 대응하는 검증된 실적 평가항목이 없습니다.", **fact_audit)
        points = case_table_points(criterion.case_table, None, submission_status=fact.submission_status)
        if points is None:
            return _criterion_unscored(criterion, status="REVIEW",
                rationale="원문에 해당 미제출 상태의 배점이 명시되어 있지 않습니다.", **fact_audit)
        lower_points = upper_points = _round_points(points)
    elif criterion.formula_type in {"BRACKET", "THRESHOLD", "FORMULA", "CASE_TABLE"} and fact.status == "ESTIMATED" and (
        fact.lower_value is not None or fact.upper_value is not None
    ):
        if fact.lower_value is not None and fact.upper_value is None and lower_bound_saturates_max:
            lower_points = upper_points = _round_points(criterion.max_points)
        elif fact.lower_value is None or fact.upper_value is None:
            return _criterion_unscored(
                criterion,
                status="REVIEW",
                rationale="추정값 범위의 하한과 상한이 모두 필요합니다.",
                **fact_audit,
            )
        else:
            point_range = _points_for_numeric_range(
                criterion,
                fact.lower_value,
                fact.upper_value,
            )
            if point_range is None:
                return _criterion_unscored(
                    criterion,
                    status="REVIEW",
                    rationale="회사 데이터 범위를 포괄하는 배점 구간이 없습니다.",
                    **fact_audit,
                )
            lower_points, upper_points = point_range
    else:
        if fact.value is None:
            return _criterion_unscored(
                criterion,
                status="UNSCORABLE",
                rationale="산식에 적용할 회사 값이 없습니다.",
                **fact_audit,
            )
        points = _points_for_value(criterion, fact.value)
        if points is None:
            return _criterion_unscored(
                criterion,
                status="REVIEW",
                rationale="회사 값에 해당하는 유효한 배점 구간이 없습니다.",
                **fact_audit,
            )
        lower_points = upper_points = points

    case_range_requires_review = (
        criterion.formula_type == "CASE_TABLE"
        and fact.status == "ESTIMATED"
        and lower_points != upper_points
    )
    estimate_status: EstimateStatus = (
        "REVIEW" if case_range_requires_review else fact.status
    )
    if case_range_requires_review:
        rationale = (
            "원문 CASE_TABLE의 서로 다른 배점 행을 회사 데이터 범위가 가로질러 "
            "단일 점수를 확정할 수 없습니다."
        )
        estimate_assumptions = [
            "회사 데이터 범위에서 특정 값을 임의 선택하지 않고 확인 가능한 배점 범위만 표시했습니다."
        ]
    else:
        rationale = fact.rationale or (
            "유효 증빙값을 산식에 적용했습니다."
            if fact.status == "CONFIRMED"
            else "잠정 증빙 범위를 산식에 적용했습니다."
        )
        estimate_assumptions = (
            []
            if fact.status == "CONFIRMED"
            else ["증빙 확정 전 잠정 범위이며 최종점수가 아닙니다."]
        )

    return CriterionEstimate(
        criterion_id=criterion.criterion_id,
        category=criterion.category,
        label=criterion.label,
        max_points=criterion.max_points,
        formula=criterion.formula,
        rule_floor_points=_round_points(criterion.rule_floor_points),
        floor_condition=criterion.floor_condition,
        rule_base_points=criterion.rule_base_points,
        base_condition=criterion.base_condition,
        source_anchor=criterion.source_anchor,
        evidence_key=fact.evidence_key,
        evidence_reference=fact.evidence_reference,
        evidence_sha256=fact.evidence_sha256,
        fact_binding_sha256=criterion.fact_binding_sha256,
        estimated_points=lower_points if lower_points == upper_points else None,
        lower_points=lower_points,
        upper_points=upper_points,
        confidence=(
            0
            if case_range_requires_review
            else (1.0 if fact.status == "CONFIRMED" else min(fact.confidence, 0.8))
        ),
        status=estimate_status,
        rationale=rationale,
        assumptions=estimate_assumptions,
    )


def estimate_quantitative_score(
    request: QuantitativeEstimateRequest,
    *,
    evidence_observations: list[EvidenceObservation] | None = None,
) -> QuantitativeEstimateResult:
    separation_notice = (
        "정량 준비도는 참가자격과 GO/NO-GO 판단을 변경하지 않는 별도 보조지표입니다. "
        "최종 점수는 발주기관 평가 결과로만 확정됩니다."
    )
    assumptions = list(request.assumptions)
    activation_reasons = list(request.activation_reasons)
    if request.missing_reason:
        assumptions.append(request.missing_reason)
    source_is_validated = (
        request.rule_source_status == "AVAILABLE"
        and request.source_validation_status == "SOURCE_VALIDATED"
    )
    full_activation_is_safe = request.activation_status == "AUTO_ACTIVE"
    partial_activation_is_safe = (
        request.activation_status == "PARTIAL_ACTIVE"
        and request.rule_source_status == "AVAILABLE"
        and request.source_validation_status == "REVIEW_REQUIRED"
        and bool(request.criteria)
        and bool(request.review_criteria)
    )
    # Rows proven against their own attachment stay scoreable even when the
    # manifest as a whole is unresolved. The subtotal is reported as partial:
    # the unread remainder is never assumed to be zero, full or absent.
    partial_source_is_safe = (
        request.activation_status == "PARTIAL_SOURCE"
        and request.rule_source_status == "INCOMPLETE"
        and request.source_validation_status == "INCOMPLETE"
        and bool(request.criteria)
        and bool(request.activation_reasons)
        and not request.review_criteria
        and request.minimum_score is None
    )
    if (
        not request.criteria
        or not (
            full_activation_is_safe
            or partial_activation_is_safe
            or partial_source_is_safe
        )
        or (full_activation_is_safe and not source_is_validated)
    ):
        if request.rule_source_status == "AVAILABLE" and not activation_reasons:
            activation_reasons.append("AUTO_ACTIVATION_NOT_ESTABLISHED")
        return QuantitativeEstimateResult(
            engine_version=QUANTITATIVE_ENGINE_VERSION,
            ruleset_version=request.ruleset_version,
            source_anchor=request.source_anchor,
            rule_source_status=request.rule_source_status,
            source_validation_status=request.source_validation_status,
            activation_status=request.activation_status,
            activation_reasons=activation_reasons,
            overall_status="REVIEW",
            total_max_points=None,
            confirmed_points=None,
            estimated_points=None,
            lower_points=None,
            upper_points=None,
            unscorable_points=None,
            evidence_coverage_pct=0,
            readiness_pct=None,
            readiness_band="GRAY",
            minimum_score=request.minimum_score,
            meets_minimum=None,
            confidence=0,
            criteria=[],
            assumptions=assumptions,
            evidence_observations=evidence_observations or [],
            opinion=(
                "정량평가표의 원문 검증 또는 자동 활성화 조건이 충족되지 않아 점수를 "
                "표시하지 않습니다. 배점표·산식·회사 사실 연결을 검토한 뒤 다시 계산해야 합니다."
            ),
            separation_notice=separation_notice,
        )

    # A fact bound to one criterion's exact text belongs to that criterion, so
    # it is addressed by its binding and never competes for the metric key.
    # Two 자기자본비율 and 유동비율 rows in one table share
    # ``company.financial.ratio``; without this they would collide and both
    # would be withheld as ambiguous.
    facts_by_binding: dict[tuple[str, str], QuantitativeFact] = {}
    duplicate_bindings: set[tuple[str, str]] = set()
    facts: dict[str, QuantitativeFact] = {}
    duplicate_keys: set[str] = set()
    for fact in request.facts:
        if fact.fact_binding_sha256:
            identity = (fact.metric_key, fact.fact_binding_sha256)
            if identity in facts_by_binding:
                duplicate_bindings.add(identity)
            facts_by_binding[identity] = fact
            continue
        if fact.metric_key in facts:
            duplicate_keys.add(fact.metric_key)
        facts[fact.metric_key] = fact

    estimates: list[CriterionEstimate] = []
    for criterion in request.criteria:
        binding = criterion.fact_binding_sha256
        set_aside = out_of_scope_reason(
            label=criterion.label,
            criterion_literal=criterion.formula,
            metric_in_registry=criterion.category in _CANONICAL_METRIC_REGISTRY,
        )
        if set_aside is not None:
            estimates.append(_criterion_set_aside(criterion, set_aside))
            continue
        identity = (criterion.metric_key, binding) if binding else None
        if identity in duplicate_bindings:
            estimates.append(
                _criterion_unscored(
                    criterion,
                    status="REVIEW",
                    rationale="동일 평가항목에 결합된 회사 데이터가 중복되어 적용 대상을 확정할 수 없습니다.",
                )
            )
            continue
        bound = facts_by_binding.get(identity) if identity else None
        if bound is not None:
            estimates.append(_estimate_criterion(criterion, bound))
        elif criterion.metric_key in duplicate_keys:
            estimates.append(
                _criterion_unscored(
                    criterion,
                    status="REVIEW",
                    rationale="동일 metric_key의 회사 데이터가 중복되어 적용 대상을 확정할 수 없습니다.",
                )
            )
        else:
            estimates.append(_estimate_criterion(criterion, facts.get(criterion.metric_key)))

    for criterion in request.review_criteria:
        estimates.append(
            CriterionEstimate(
                criterion_id=criterion.criterion_id,
                category=criterion.category,
                label=criterion.label,
                max_points=_round_points(criterion.max_points),
                formula="원문 산식 검토 필요",
                rule_floor_points=0,
                floor_condition=None,
                rule_base_points=None,
                base_condition=None,
                source_anchor=None,
                evidence_key=None,
                evidence_reference=None,
                evidence_sha256=None,
                fact_binding_sha256=None,
                estimated_points=None,
                lower_points=0,
                upper_points=_round_points(criterion.max_points),
                confidence=0,
                status="REVIEW",
                rationale=(
                    "이 항목은 원문 검증이 끝나지 않아 점수를 넣지 않았습니다: "
                    + ", ".join(criterion.issue_codes)
                ),
                assumptions=[
                    "미검증 항목은 0점으로 판정한 것이 아니며 전체 범위의 상한에만 반영합니다."
                ],
            )
        )

    # Quantitative totals describe only the rows within this calculation's
    # scope. Keep excluded rows for audit, but never assume their full marks
    # in the quantitative upper bound. Unverified quantitative REVIEW rows
    # remain in scope and retain their unresolved 0..maximum range.
    scored = [item for item in estimates if item.status != "OUT_OF_SCOPE"]
    set_aside_rows = [item for item in estimates if item.status == "OUT_OF_SCOPE"]
    total_max = _round_points(sum(item.max_points for item in scored))
    confirmed = _round_points(
        sum(item.lower_points for item in scored if item.status == "CONFIRMED")
    )
    lower = _round_points(sum(item.lower_points for item in scored))
    upper = _round_points(sum(item.upper_points for item in scored))
    unscorable = _round_points(
        sum(
            item.max_points - item.lower_points
            for item in scored
            if item.status in {"UNSCORABLE", "REVIEW"}
        )
    )
    confirmed_weight = sum(
        item.max_points for item in scored if item.status == "CONFIRMED"
    )
    # Rows set aside are not failures of the company data, so they stay out of
    # the denominators. Counting 정성 배점 there would read a perfect
    # quantitative fit as a near-total miss.
    scored_max = total_max
    out_of_scope = _round_points(sum(item.max_points for item in set_aside_rows))
    coverage = _round_points((confirmed_weight / scored_max) * 100) if scored_max else 0
    readiness = _round_points((lower / scored_max) * 100) if scored_max else None
    if readiness is None:
        band: ReadinessBand = "GRAY"
    elif readiness < 70 or coverage < 60:
        band = "RED"
    elif readiness < 80 or coverage < 80:
        band = "YELLOW"
    else:
        band = "GREEN"

    statuses = {item.status for item in scored}
    if not scored:
        # 산정 대상이 없는 상태를 0점 확보로 확정하지 않는다.
        statuses = {"UNSCORABLE"}
    if partial_source_is_safe:
        # An unresolved manifest can still hold rules nobody has read. A
        # subtotal from the resolved part is never a confirmed notice total.
        overall: EstimateStatus = "REVIEW"
    elif "REVIEW" in statuses:
        overall = "REVIEW"
    elif "UNSCORABLE" in statuses:
        overall = "UNSCORABLE"
    elif "ESTIMATED" in statuses:
        overall = "ESTIMATED"
    else:
        overall = "CONFIRMED"

    meets_minimum: bool | None = None
    # A mixed request does not bind its minimum to the remaining quantitative
    # subtotal. Do not compare an overall technical/combined cutoff against it.
    if request.minimum_score is not None and not set_aside_rows:
        if lower >= request.minimum_score:
            meets_minimum = True
        elif upper < request.minimum_score:
            meets_minimum = False

    weighted_confidence = sum(item.confidence * item.max_points for item in scored)
    confidence = _round_points(weighted_confidence / scored_max) if scored_max else 0
    estimated_points = lower if lower == upper and overall in {"CONFIRMED", "ESTIMATED"} else None

    if set_aside_rows:
        assumptions = list(assumptions) + [
            "정성·총괄·가격 등 자동 산정 대상이 아닌 %g점은 별도로 보존하고, "
            "정량 합계·상하한·준비도·커버리지 계산에서는 제외했습니다."
            % out_of_scope
        ]
        if request.minimum_score is not None:
            assumptions.append(
                "최소점수의 적용 범위가 정량 소계인지 전체 평가인지 확인되지 않아 "
                "최소점수 충족 여부를 판단하지 않았습니다."
            )

    if partial_activation_is_safe:
        opinion = (
            "원문 검증이 끝난 항목만 부분 산정했습니다. 검토 항목에는 임의 점수를 "
            "넣지 않았으며 해당 배점은 0점부터 만점까지의 미확정 범위로 남겼습니다."
        )
    elif not scored:
        opinion = "정량 산정 대상이 없어 점수를 확정하지 않았습니다. 별도로 보존한 평가 항목을 확인하세요."
    elif band == "GREEN":
        opinion = "현재 하한과 검증 커버리지가 기본 GREEN 기준을 충족합니다. 공고별 최소점수와 최종 제출 증빙을 다시 확인하세요."
    elif band == "YELLOW":
        opinion = "점수 하한 또는 검증 커버리지가 보완 구간입니다. 잠정 항목의 증빙을 확정한 뒤 다시 계산하세요."
    else:
        opinion = "점수 하한 또는 검증 커버리지가 RED 구간입니다. 이는 자동 NO-GO가 아니며 누락·검토 항목을 우선 보완해야 합니다."

    return QuantitativeEstimateResult(
        engine_version=QUANTITATIVE_ENGINE_VERSION,
        ruleset_version=request.ruleset_version,
        source_anchor=request.source_anchor,
        rule_source_status=request.rule_source_status,
        source_validation_status=request.source_validation_status,
        activation_status=request.activation_status,
        activation_reasons=activation_reasons,
        overall_status=overall,
        total_max_points=total_max,
        confirmed_points=confirmed,
        estimated_points=estimated_points,
        lower_points=lower,
        upper_points=upper,
        unscorable_points=unscorable,
        out_of_scope_points=out_of_scope,
        evidence_coverage_pct=coverage,
        readiness_pct=readiness,
        readiness_band=band,
        minimum_score=request.minimum_score,
        meets_minimum=meets_minimum,
        confidence=confidence,
        criteria=estimates,
        assumptions=assumptions,
        evidence_observations=evidence_observations or [],
        opinion=opinion,
        separation_notice=separation_notice,
    )


@lru_cache(maxsize=1)
def _load_quantitative_profile_catalog() -> dict[str, Any]:
    resource = resources.files("pai_loop").joinpath(QUANTITATIVE_PROFILE_RESOURCE)
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuantitativeProfileError("quantitative notice profile could not be loaded") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "pai-loop-quantitative-notice-profiles-1.0.0":
        raise QuantitativeProfileError("quantitative notice profile schema is invalid")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise QuantitativeProfileError("quantitative notice profiles must be a list")
    if any(not isinstance(item, dict) for item in profiles):
        raise QuantitativeProfileError("each quantitative notice profile must be an object")
    ids = [str(item.get("notice_key") or "").strip() for item in profiles]
    if any(not item for item in ids):
        raise QuantitativeProfileError("quantitative notice profile keys must be non-empty")
    if len(ids) != len(set(ids)):
        raise QuantitativeProfileError("quantitative notice profile keys must be unique")
    try:
        for item in profiles:
            QuantitativeEstimateRequest(
                ruleset_version=item["ruleset_version"],
                rule_source_status=item.get("rule_source_status", "INCOMPLETE"),
                minimum_score=item.get("minimum_score"),
                criteria=item.get("criteria", []),
                facts=[],
                assumptions=item.get("assumptions", []),
                missing_reason=item.get("missing_reason"),
                source_anchor=item.get("source_reference"),
            )
    except (KeyError, ValidationError) as exc:
        raise QuantitativeProfileError("quantitative notice profile content is invalid") from exc
    return payload


def load_quantitative_profile_catalog() -> dict[str, Any]:
    return copy.deepcopy(_load_quantitative_profile_catalog())


def _normalize_notice_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _profile_identity_matches(notice: Notice, profile: dict[str, Any]) -> bool:
    normalized_title = _normalize_notice_title(notice.title)
    if profile.get("notice_key") == notice.notice_key:
        return True
    if profile.get("bid_notice_no") and profile.get("bid_notice_no") == notice.bid_notice_no:
        return True
    profile_title = profile.get("notice_title")
    return bool(
        profile_title
        and _normalize_notice_title(str(profile_title)) == normalized_title
    )


def _profile_source_digest(profile: dict[str, Any]) -> str | None:
    source = profile.get("source_reference")
    if not isinstance(source, dict):
        return None
    digest = source.get("document_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest.casefold()):
        return None
    return digest.casefold()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_dynamic_quantitative_profile(
    notice: Notice,
) -> QuantitativeCandidateProfile | None:
    """Merge only validated records bound to the exact current PPS manifest."""

    versions = sorted(notice.versions, key=lambda item: item.version_no, reverse=True)
    metadata = next(
        (
            version
            for version in versions
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == PPS_METADATA_KIND
        ),
        None,
    )
    if metadata is None or not isinstance(metadata.source_payload, dict):
        return None
    metadata_schema_current = (
        metadata.source_payload.get("schema_version") == PPS_METADATA_SCHEMA
    )
    raw_values = metadata.source_payload.get("attachment_manifest")
    if not isinstance(raw_values, list):
        return merge_validated_quantitative_records(
            [],
            expected_documents={},
            manifest_sha256=_canonical_digest(raw_values),
            incomplete_attachment_ids=["INVALID-MANIFEST-CONTAINER"],
        )
    raw_manifest_values = list(raw_values)
    raw_manifest = [
        dict(item)
        for item in raw_manifest_values
        if isinstance(item, dict)
    ]
    manifest_sha256 = _canonical_digest(raw_manifest_values)
    attachments, invalid_count = _validated_manifest_attachments(raw_manifest)
    invalid_count += len(raw_manifest_values) - len(raw_manifest)
    descriptors = {
        item["attachment_id"]: _canonical_digest(item) for item in attachments
    }
    _read_attachments, _read_invalid, attempts = _current_manifest_attempts(
        versions, validate_accepted=False, preserve_quantitative_proof=True,
    )

    expected_documents: dict[str, str] = {}
    attachment_profiles: dict[str, dict[str, object]] = {}
    records: list[ValidatedQuantitativeAttachmentRecord] = []
    incomplete: set[str] = set()
    for attachment in attachments:
        attachment_id = attachment["attachment_id"]
        attempt = attempts.get(attachment_id)
        if attempt is None:
            expected_documents[attachment_id] = _canonical_digest(
                {
                    "attachment_id": attachment_id,
                    "manifest_sha256": descriptors[attachment_id],
                    "state": "NOT_AUDITED",
                }
            )
            incomplete.add(attachment_id)
            continue
        expected_documents[attachment_id] = attempt.file_sha256.casefold()
        payload = attempt.source_payload if isinstance(attempt.source_payload, dict) else {}
        result = payload.get("result")
        raw_document_type = (
            result.get("document_type") if isinstance(result, dict) else None
        )
        raw_source_label = payload.get("source_label")
        attachment_profiles[attachment_id] = {
            "document_type": raw_document_type,
            "source_label": raw_source_label,
            "missing_or_unreadable": (
                result.get("missing_or_unreadable")
                if isinstance(result, dict)
                else None
            ),
        }
        if (
            payload.get("status") != "ACCEPTED"
            or attempt.extraction_status not in {"ACCEPTED", "COMPLETE"}
            or not attempt.document_complete
        ):
            incomplete.add(attachment_id)
        raw_record = payload.get("quantitative_validation_record")
        if not isinstance(raw_record, dict):
            incomplete.add(attachment_id)
            continue
        try:
            records.append(
                ValidatedQuantitativeAttachmentRecord.model_validate(raw_record)
            )
        except ValidationError:
            incomplete.add(attachment_id)

    if invalid_count:
        incomplete.update(
            f"INVALID-MANIFEST-SLOT-{index + 1}"
            for index in range(invalid_count)
        )
    if not metadata_schema_current:
        incomplete.add("PPS-METADATA-SCHEMA-STALE")
    if not raw_manifest_values:
        incomplete.add("EMPTY-MANIFEST")
    return merge_validated_quantitative_records(
        records,
        expected_documents=expected_documents,
        manifest_sha256=manifest_sha256,
        incomplete_attachment_ids=sorted(incomplete),
        attachment_profiles=attachment_profiles,
        source_payloads={
            attachment_id: attempt.source_payload
            for attachment_id, attempt in attempts.items()
        },
    )


_DISCRETE_COUNT_METRICS = frozenset({
    "PERFORMANCE_COUNT", "PERSONNEL_COUNT", "CERTIFICATION_COUNT",
    "FACILITY_EQUIPMENT_COUNT", "AWARD_COUNT",
})


_CANONICAL_METRIC_REGISTRY: dict[str, dict[str, Any]] = {
    "PERFORMANCE_AMOUNT": {
        "fact_key": "company.performance.amount",
        "canonical_unit": "KRW",
        "unit_scales": {
            "원": Decimal("1"),
            "krw": Decimal("1"),
            "천": Decimal("1000"),
            "천원": Decimal("1000"),
            "만": Decimal("10000"),
            "만원": Decimal("10000"),
            "백만": Decimal("1000000"),
            "백만원": Decimal("1000000"),
            "천만": Decimal("10000000"),
            "천만원": Decimal("10000000"),
            "억": Decimal("100000000"),
            "억원": Decimal("100000000"),
        },
    },
    "PERFORMANCE_COUNT": {
        "fact_key": "company.performance.count",
        "canonical_unit": "COUNT",
        # 교육여행 배점표는 학교 단위로 실적을 센다: 한 개교가 실적 한 건이다.
        "unit_scales": {
            "건": Decimal("1"),
            "회": Decimal("1"),
            "개": Decimal("1"),
            "개교": Decimal("1"),
        },
    },
    "PERSONNEL_COUNT": {
        "fact_key": "company.personnel.count",
        "canonical_unit": "PERSON",
        "unit_scales": {"명": Decimal("1"), "인": Decimal("1")},
    },
    "CERTIFICATION_COUNT": {
        "fact_key": "company.certification.count",
        "canonical_unit": "COUNT",
        "unit_scales": {"건": Decimal("1"), "개": Decimal("1")},
    },
    "CREDIT_RATING": {
        "fact_key": "company.credit_rating",
        "canonical_unit": "RATING",
        "value_kind": "CATEGORICAL",
        "unit_scales": {"등급": Decimal("1"), "신용등급": Decimal("1"), "rating": Decimal("1")},
    },
    "FINANCIAL_RATIO": {
        "fact_key": "company.financial.ratio",
        "canonical_unit": "PERCENT",
        "unit_scales": {"%": Decimal("1"), "퍼센트": Decimal("1")},
    },
    "BUSINESS_YEARS": {
        "fact_key": "company.business.years",
        "canonical_unit": "YEAR",
        "unit_scales": {"년": Decimal("1"), "year": Decimal("1")},
    },
    "FACILITY_EQUIPMENT_COUNT": {
        "fact_key": "company.facility_equipment.count",
        "canonical_unit": "COUNT",
        "unit_scales": {"대": Decimal("1"), "개": Decimal("1")},
    },
    "AWARD_COUNT": {
        "fact_key": "company.award.count",
        "canonical_unit": "COUNT",
        "unit_scales": {"건": Decimal("1"), "회": Decimal("1"), "개": Decimal("1")},
    },
    "LOCAL_PRESENCE": {
        "fact_key": "company.local_presence",
        "canonical_unit": "BOOLEAN",
        "value_kind": "BOOLEAN",
        "unit_scales": {"여부": Decimal("1"), "유무": Decimal("1"), "boolean": Decimal("1")},
    },
}

QUANTITATIVE_CANONICAL_FACT_KEYS = frozenset(
    str(item["fact_key"]) for item in _CANONICAL_METRIC_REGISTRY.values()
)
# Only the five existing count metrics with scale-one units use an integer
# BRACKET domain. Amount, ratio, years, unknown keys and other DSLs keep their
# existing numeric contracts.
_DISCRETE_COUNT_FACT_KEYS = frozenset(
    str(_CANONICAL_METRIC_REGISTRY[metric]["fact_key"])
    for metric in _DISCRETE_COUNT_METRICS
    if set(_CANONICAL_METRIC_REGISTRY[metric]["unit_scales"].values()) == {Decimal("1")}
)
_FACT_SPEC_BY_KEY = {
    str(item["fact_key"]): item for item in _CANONICAL_METRIC_REGISTRY.values()
}
# The extractor does not yet structure the recognition period, comparable-
# project definition, completion/VAT rules, single-vs-aggregate contract
# basis, or consortium share conditions needed by performance tables.  A
# document/binding digest cannot prove semantics that were never modeled.
_UNMODELED_FACT_DIMENSION_METRICS = frozenset(
    {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT"}
)
_SOURCE_UNIT_RE = re.compile(
    r"-?(?:\d[\d,]*)(?:\.\d+)?\s*"
    r"(?P<unit>천\s*만\s*원|천\s*만|백\s*만\s*원|백\s*만|억\s*원|억|"
    r"만\s*원|만|천\s*원|천|원|"
    r"퍼센트|%|건|회|개|명|인|대|년|등급|신용등급|여부|유무|KRW)",
    re.IGNORECASE,
)
_SOURCE_AMOUNT_CASE_BOUND_RE = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>천\s*만\s*원|백\s*만\s*원|억\s*원|"
    r"만\s*원|천\s*원|원)\s*"
    r"(?P<op>이상|초과|이하|미만)",
    re.IGNORECASE,
)


def _normalize_unit(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _metric_spec(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> dict[str, Any] | None:
    spec = _CANONICAL_METRIC_REGISTRY.get(candidate.metric)
    if spec is None:
        return None
    if _implicit_credit_unit_is_source_bound(candidate):
        return spec
    unit_scales = spec["unit_scales"]
    return spec if _normalize_unit(candidate.unit) in unit_scales else None


def _metric_scale(candidate: ImmutableQuantitativeRuleCandidate) -> Decimal | None:
    spec = _metric_spec(candidate)
    if spec is None or spec.get("value_kind", "NUMERIC") != "NUMERIC":
        return None
    return spec["unit_scales"][_normalize_unit(candidate.unit)]


def _amount_case_units_are_value_equivalent(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> bool:
    """Prove source currency cells equal the candidate's normalized values."""

    if (
        candidate.metric != "PERFORMANCE_AMOUNT"
        or candidate.scoring_method != "CASE_TABLE"
        or not candidate.cases
    ):
        return False
    spec = _metric_spec(candidate)
    if spec is None:
        return False
    candidate_scale = spec["unit_scales"].get(
        _normalize_unit(candidate.unit)
    )
    if candidate_scale is None:
        return False
    operator_map = {
        "이상": "GTE",
        "초과": "GT",
        "이하": "LTE",
        "미만": "LT",
    }
    for case in candidate.cases:
        comparison = (
            None
            if case.comparison_value is None
            else Decimal(str(case.comparison_value))
        )
        matches = list(_SOURCE_AMOUNT_CASE_BOUND_RE.finditer(case.literal))
        if (
            comparison is None
            or not comparison.is_finite()
            or len(matches) != 1
            or operator_map[matches[0].group("op")] != case.operator
        ):
            return False
        match = matches[0]
        try:
            source_number = Decimal(match.group("num").replace(",", ""))
        except InvalidOperation:
            return False
        source_scale = spec["unit_scales"].get(
            _normalize_unit(match.group("unit"))
        )
        if (
            source_scale is None
            or source_number * source_scale
            != comparison * candidate_scale
        ):
            return False
    return True


def _case_percent_award_input_literal(
    candidate: ImmutableQuantitativeRuleCandidate,
    case: ImmutableQuantitativeCase,
) -> str | None:
    """Separate an exact numeric condition from its source-bound percent award.

    This does not remove arbitrary percent tokens. The full literal must remain
    inside its persisted evidence, end in the exact declared award cell, and
    contain only its one structured comparison value before that cell.
    """
    if (
        case.award_kind != "PERCENT_OF_MAX"
        or case.operator not in {"GTE", "EQ", "LTE", "LT"}
    ):
        return None
    if _normalise_anchor_text(case.literal) not in _normalise_anchor_text(
        case.evidence.quote
    ):
        return None
    literal = unicodedata.normalize("NFKC", case.literal).strip()
    match = re.fullmatch(
        r"(?P<condition>.+?)(?:\s+|(?=배점))"
        r"(?P<award>(?:배점\s*(?:의\s*)?)?\d[\d,]*(?:\.\d+)?\s*(?:%|퍼센트))",
        literal,
        re.DOTALL,
    )
    if match is None or not _score_cell_matches(
        match.group("award"), value=case.award_value, percent=True,
    ):
        return None
    condition = match.group("condition").strip()
    if len(re.findall(r"-?\d[\d,]*(?:\.\d+)?", condition)) != 1:
        return None
    if not _case_condition_matches(candidate, case, condition):
        return None
    return condition


def _candidate_unit_is_source_bound(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> bool:
    if _implicit_credit_unit_is_source_bound(candidate):
        return True
    if _amount_case_units_are_value_equivalent(candidate):
        return True
    literals = [candidate.criterion_literal, candidate.evidence.quote]
    literals.extend(item.literal for item in candidate.brackets)
    literals.extend(item.evidence.quote for item in candidate.brackets)
    if candidate.threshold is not None:
        literals.extend(
            [candidate.threshold.literal, candidate.threshold.evidence.quote]
        )
    if candidate.formula_literal:
        literals.append(candidate.formula_literal)
    spec = _CANONICAL_METRIC_REGISTRY.get(candidate.metric)
    for case in candidate.cases:
        if (
            case.award_kind == "PERCENT_OF_MAX"
            and (spec or {}).get("value_kind", "NUMERIC") == "NUMERIC"
        ):
            condition = _case_percent_award_input_literal(candidate, case)
            if condition is None:
                return False
            literals.append(condition)
        else:
            literals.extend((case.literal, case.evidence.quote))
    observed = {
        _normalize_unit(match.group("unit"))
        for literal in literals
        for match in _SOURCE_UNIT_RE.finditer(literal)
    }
    expected = _normalize_unit(candidate.unit)
    if expected in observed:
        return True
    spec = _CANONICAL_METRIC_REGISTRY.get(candidate.metric)
    if spec is not None:
        value_kind = str(spec.get("value_kind", "NUMERIC"))
        if value_kind != "NUMERIC":
            normalized_literals = [
                re.sub(r"\s+", "", literal).casefold() for literal in literals
            ]
            observed.update(
                unit
                for unit in spec["unit_scales"]
                if unit and any(unit in literal for literal in normalized_literals)
            )
        # Numeric scale equivalence is meaningful only for currency aliases
        # (``억``/``억원`` etc.).  Treating every scale-1 unit as synonymous
        # would let YEAR bind to a source that explicitly says ``년`` and
        # similarly conflate 건/회/개, bypassing the source-unit gate.
        if value_kind != "NUMERIC" or candidate.metric == "PERFORMANCE_AMOUNT":
            expected_scale = spec["unit_scales"].get(expected)
            if expected_scale is not None and any(
                spec["unit_scales"].get(unit) == expected_scale for unit in observed
            ):
                return True
    # Some tables declare a unit once in the verified header and omit it from
    # every numeric row.  Accept only the explicit ``단위: X`` form here;
    # arbitrary free-text occurrence is not sufficient source binding.
    normalized_literals = [
        re.sub(r"\s+", "", literal).casefold() for literal in literals
    ]
    return any(
        f"단위:{expected}" in literal or f"단위：{expected}" in literal
        for literal in normalized_literals
    )


def _candidate_bound_unit_scales_are_consistent(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> bool:
    """Require each numeric bound row to prove one unambiguous unit scale."""

    spec = _CANONICAL_METRIC_REGISTRY.get(candidate.metric)
    if spec is None:
        return False
    if spec.get("value_kind", "NUMERIC") != "NUMERIC":
        return True
    if (
        candidate.metric == "PERFORMANCE_AMOUNT"
        and candidate.scoring_method == "CASE_TABLE"
        and _amount_case_units_are_value_equivalent(candidate)
    ):
        return True
    unit_scales = spec["unit_scales"]
    expected = unit_scales.get(_normalize_unit(candidate.unit))
    if expected is None:
        return False
    literals = [item.literal for item in candidate.brackets]
    if candidate.threshold is not None:
        literals.append(candidate.threshold.literal)
    if candidate.formula_literal:
        literals.append(candidate.formula_literal)
    for case in candidate.cases:
        if case.award_kind == "PERCENT_OF_MAX":
            condition = _case_percent_award_input_literal(candidate, case)
            if condition is None:
                return False
            literals.append(condition)
        else:
            literals.append(case.literal)
    if not literals:
        return False
    for literal in literals:
        observed = {
            _normalize_unit(match.group("unit"))
            for match in _SOURCE_UNIT_RE.finditer(literal)
        }
        if not observed:
            # A verified criterion/header unit may be inherited by unitless
            # numeric rows.  Only an explicit conflicting row is unsafe.
            continue
        scales = {unit_scales.get(unit) for unit in observed}
        if None in scales or scales != {expected}:
            return False
    return True


def _canonical_company_fact_value(
    fact: CompanyFact,
    criterion: QuantitativeCriterion,
) -> tuple[float | bool | str | None, str | None, str | None]:
    spec = _FACT_SPEC_BY_KEY.get(fact.fact_key)
    if spec is None:
        return None, None, "등록되지 않은 canonical 회사 사실 키입니다."
    raw = fact.value
    unit: str | None = None
    fact_binding_sha256: str | None = None
    if isinstance(raw, dict):
        allowed = {"value", "unit", "fact_binding_sha256"}
        if not raw or set(raw) - allowed or "value" not in raw:
            return (
                None,
                None,
                "회사 사실 값 구조가 canonical 계약과 일치하지 않습니다.",
            )
        unit = str(raw.get("unit") or "") or None
        raw_binding = raw.get("fact_binding_sha256")
        if raw_binding is not None:
            fact_binding_sha256 = str(raw_binding).casefold()
            if not re.fullmatch(r"[a-f0-9]{64}", fact_binding_sha256):
                return (
                    None,
                    None,
                    "회사 사실의 평가항목 binding digest가 유효하지 않습니다.",
                )
        raw = raw["value"]
    if (
        criterion.fact_binding_sha256 is not None
        and fact_binding_sha256 != criterion.fact_binding_sha256
    ):
        return (
            None,
            fact_binding_sha256,
            "회사 사실이 이 평가항목의 인정기간·유사범위·단위 조건에 결합되지 않았습니다.",
        )
    if criterion.fact_binding_sha256 is not None and unit is None:
        return (
            None,
            fact_binding_sha256,
            "공고별 평가항목에 결합된 회사 사실에는 명시적인 단위가 필요합니다.",
        )
    if unit:
        normalized_unit = _normalize_unit(unit)
        if (
            normalized_unit != _normalize_unit(str(spec["canonical_unit"]))
            and normalized_unit not in spec["unit_scales"]
        ):
            return (
                None,
                fact_binding_sha256,
                "회사 사실 단위를 canonical 단위로 변환할 수 없습니다.",
            )
    value_kind = str(spec.get("value_kind", "NUMERIC"))
    if value_kind == "CATEGORICAL":
        if not isinstance(raw, str) or not raw.strip():
            return None, fact_binding_sha256, "회사 사실의 범주 값이 비어 있습니다."
        return re.sub(r"\s+", " ", raw).strip(), fact_binding_sha256, None
    if value_kind == "BOOLEAN":
        if isinstance(raw, bool):
            return raw, fact_binding_sha256, None
        normalized = str(raw).strip().casefold()
        if normalized in {"y", "yes", "true", "1", "보유", "있음", "해당"}:
            return True, fact_binding_sha256, None
        if normalized in {"n", "no", "false", "0", "미보유", "없음", "비해당"}:
            return False, fact_binding_sha256, None
        return None, fact_binding_sha256, "회사 사실 값을 boolean으로 해석할 수 없습니다."
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None, fact_binding_sha256, "회사 사실 값이 유한 숫자가 아닙니다."
    try:
        numeric = Decimal(str(raw))
    except InvalidOperation:
        return None, fact_binding_sha256, "회사 사실 값을 숫자로 해석할 수 없습니다."
    if not numeric.is_finite():
        return None, fact_binding_sha256, "회사 사실 값이 유한 숫자가 아닙니다."
    scale = Decimal("1")
    if unit:
        normalized = _normalize_unit(unit)
        if normalized != _normalize_unit(str(spec["canonical_unit"])):
            scale = spec["unit_scales"].get(normalized)
            if scale is None:
                return (
                    None,
                    fact_binding_sha256,
                    "회사 사실 단위를 canonical 단위로 변환할 수 없습니다.",
                )
    return float(numeric * scale), fact_binding_sha256, None


def _dynamic_evidence_binding_error(
    fact: CompanyFact,
    criterion: QuantitativeCriterion,
) -> str | None:
    """Validate the immutable evidence row behind a dynamic company fact."""

    if criterion.fact_binding_sha256 is None:
        # Curated static profiles retain their existing explicit compatibility
        # contract; this bridge is mandatory for dynamically extracted rules.
        return None
    evidence = fact.evidence
    if (
        evidence is None
        or not fact.evidence_id
        or not evidence.id
        or fact.evidence_id != evidence.id
    ):
        return "회사 사실이 실제 증빙 행과 연결되지 않았습니다."
    if evidence.evidence_type != "QUANTITATIVE_FACT":
        return "정량 회사 사실에 허용되지 않은 증빙 유형입니다."
    if not str(evidence.source_location or "").strip():
        return "정량 회사 사실 증빙의 원본 위치가 없습니다."
    evidence_sha256 = str(evidence.sha256 or "").casefold()
    if not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256):
        return "정량 회사 사실 증빙의 불변 콘텐츠 해시가 없습니다."
    metadata = evidence.metadata_json
    if not isinstance(metadata, dict):
        return "정량 회사 사실 증빙의 binding 메타데이터가 없습니다."
    if metadata.get("quantitative_fact_key") != fact.fact_key:
        return "증빙의 canonical 회사 사실 키가 평가 값과 일치하지 않습니다."
    if (
        str(metadata.get("fact_binding_sha256") or "").casefold()
        != criterion.fact_binding_sha256
    ):
        return "증빙이 이 평가항목의 조건 binding과 일치하지 않습니다."
    payload_digest = str(
        metadata.get("company_fact_payload_sha256") or ""
    ).casefold()
    if (
        not re.fullmatch(r"[a-f0-9]{64}", payload_digest)
        or payload_digest != quantitative_company_fact_payload_sha256(fact)
    ):
        return "증빙이 현재 회사 사실 값·단위·유효기간 payload와 일치하지 않습니다."
    return None


def quantitative_company_fact_payload_sha256(fact: CompanyFact) -> str:
    """Digest the semantic CompanyFact payload linked by immutable evidence."""

    def timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    return _canonical_digest(
        {
            "binding_schema": "pai-loop-company-fact-evidence-binding-1.0.0",
            "fact_key": fact.fact_key,
            "value": fact.value,
            "value_label": fact.value_label,
            "effective_from": timestamp(fact.effective_from),
            "effective_to": timestamp(fact.effective_to),
            "source": fact.source,
        }
    )


def resolve_verified_quantitative_facts(
    criteria: Sequence[QuantitativeCriterion],
    company_facts: Iterable[CompanyFact],
    *,
    as_of: datetime,
) -> list[QuantitativeFact]:
    """Bridge only exact canonical, effective and evidence-verified facts."""

    stored_facts = tuple(company_facts)
    active_bindings = {
        (criterion.metric_key, criterion.fact_binding_sha256)
        for criterion in criteria if criterion.fact_binding_sha256
    }
    resolved: list[QuantitativeFact] = []
    for criterion in criteria:
        confirmed: list[QuantitativeFact] = []
        value_errors: list[tuple[str, str | None]] = []
        for fact in stored_facts:
            if fact.fact_key != criterion.metric_key or not fact.verified:
                continue
            raw_binding = (
                str(fact.value.get("fact_binding_sha256") or "").casefold()
                if isinstance(fact.value, dict) else ""
            )
            if (
                criterion.fact_binding_sha256 is not None
                and raw_binding != criterion.fact_binding_sha256
                and (fact.fact_key, raw_binding) in active_bindings
            ):
                # Another criterion's explicitly addressed value is neither
                # input nor an error for this row, even when the metric agrees.
                continue
            if not fact_is_effective(fact, as_of):
                continue
            evidence_valid, _ = evidence_state(fact, as_of)
            if not evidence_valid:
                continue
            value, binding, value_error = _canonical_company_fact_value(
                fact,
                criterion,
            )
            if value_error:
                value_errors.append((value_error, binding))
                continue
            evidence_binding_error = _dynamic_evidence_binding_error(
                fact,
                criterion,
            )
            if evidence_binding_error:
                value_errors.append((evidence_binding_error, binding))
                continue
            assert fact.evidence is not None
            confirmed.append(
                QuantitativeFact(
                    metric_key=fact.fact_key,
                    status="CONFIRMED",
                    value=value,
                    evidence_key=fact.fact_key,
                    evidence_reference=fact.evidence.evidence_key,
                    evidence_sha256=(
                        str(fact.evidence.sha256).casefold()
                        if re.fullmatch(
                            r"[a-fA-F0-9]{64}",
                            str(fact.evidence.sha256 or ""),
                        )
                        else None
                    ),
                    fact_binding_sha256=binding,
                    confidence=1,
                    rationale=(
                        "공고 마감일 기준 유효한 검증 증빙의 canonical 회사 사실을 적용했습니다."
                    ),
                )
            )
        if confirmed:
            resolved.extend(confirmed)
        elif value_errors:
            rationale, binding = sorted(
                set(value_errors),
                key=lambda item: (item[0], item[1] or ""),
            )[0]
            resolved.append(
                QuantitativeFact(
                    metric_key=criterion.metric_key,
                    status="UNSCORABLE",
                    evidence_key=criterion.metric_key,
                    fact_binding_sha256=criterion.fact_binding_sha256 or binding,
                    confidence=0,
                    rationale=rationale,
                )
            )
    return resolved


def resolve_performance_register_facts(
    criteria: Sequence[QuantitativeCriterion],
    performance_records: Iterable[CompanyPerformanceRecord],
    *,
    as_of: datetime,
    bid_notice_at: datetime | None = None,
) -> list[QuantitativeFact]:
    """Apply source-bound recognition scopes to operator-validated records.

    These values remain ESTIMATED until the ordering authority accepts the
    evidence.  They are nevertheless deterministic: drafts, archived rows,
    unknown VAT/completion/share conditions, and records outside the exact
    lookback/similarity scope never silently contribute to a score.
    """

    records = tuple(performance_records)
    active_private_records = tuple(
        record
        for record in records
        if str(getattr(record, "source", "")).startswith("PRIVATE_IMPORT")
        and str(getattr(record, "record_status", "")).upper() != "ARCHIVED"
    )
    if active_private_records:
        # One completed private workbook is the authoritative register. Manual
        # duplicates must not be summed or counted again; during a staged
        # replacement the pending DRAFT rows also keep scoring fail-closed.
        records = active_private_records
    resolved: list[QuantitativeFact] = []
    for criterion in criteria:
        scope = criterion.performance_scope
        if scope is None:
            continue
        evaluation_as_of = as_of
        evaluation_basis = "UNSPECIFIED"
        if scope.lookback_anchor_basis == "BID_NOTICE_DATE":
            if bid_notice_at is None:
                resolved.append(
                    QuantitativeFact(
                        metric_key=criterion.metric_key,
                        status="REVIEW",
                        evidence_key=criterion.metric_key,
                        fact_binding_sha256=criterion.fact_binding_sha256,
                        confidence=0,
                        rationale=(
                            "원문은 입찰공고일 기준 최근 실적을 요구하지만 현재 공고일을 "
                            "확정할 수 없어 자동 집계를 중지했습니다."
                        ),
                    )
                )
                continue
            evaluation_as_of = bid_notice_at
            evaluation_basis = "BID_NOTICE_DATE"
        elif scope.lookback_anchor_basis == "SUBMISSION_DEADLINE":
            evaluation_basis = "SUBMISSION_DEADLINE"
        derived = derive_performance_value(
            scope,
            records,
            as_of=evaluation_as_of,
            as_of_basis=evaluation_basis,
        )
        saturated_lower_bound = (
            derived.status == "REVIEW"
            and derived.upper_value is None
            and _performance_lower_bound_saturates_max(
                criterion, derived.lower_value
            )
        )
        effective_status = "ESTIMATED" if saturated_lower_bound else derived.status
        rationale = derived.rationale
        if saturated_lower_bound:
            rationale += (
                " 검증 완료 실적만으로 이미 원문 배점의 최상위 구간에 도달했고, "
                "추가 인정 실적은 점수를 낮출 수 없어 만점 구간을 안전하게 확정했습니다."
            )
        resolved.append(
            QuantitativeFact(
                metric_key=criterion.metric_key,
                status=effective_status,
                value=derived.value,
                lower_value=derived.lower_value,
                upper_value=derived.upper_value,
                evidence_key=criterion.metric_key,
                evidence_reference=derived.evidence_reference,
                evidence_sha256=derived.evidence_sha256,
                fact_binding_sha256=criterion.fact_binding_sha256,
                confidence=(0.8 if saturated_lower_bound else (0.85 if derived.status == "ESTIMATED" else 0)),
                rationale=rationale,
            )
        )
    return resolved


def _financial_statement_years_conflict(company_facts: Sequence[CompanyFact]) -> bool:
    years: dict[int, tuple[Decimal, ...]] = {}
    fields = ("total_assets", "equity", "current_assets", "current_liabilities",
              "non_current_liabilities")
    for fact in company_facts:
        raw = getattr(fact, "value", None)
        if getattr(fact, "fact_key", None) != "company.financial.statement" or not isinstance(raw, dict):
            continue
        for row in raw.get("years") or ():
            if not isinstance(row, dict):
                continue
            try:
                year = int(row["fiscal_year"])
                values = tuple(Decimal(str(row.get(field, 0))) for field in fields)
            except (KeyError, ValueError, TypeError, InvalidOperation):
                # The statement loader already rejects malformed years.
                continue
            if not all(value.is_finite() for value in values):
                continue
            if year in years and years[year] != values:
                return True
            years[year] = values
    return False


def resolve_financial_register_facts(
    criteria: Sequence[QuantitativeCriterion],
    company_facts: Iterable[CompanyFact],
    *,
    as_of: datetime,
    bid_notice_at: datetime | None = None,
) -> list[QuantitativeFact]:
    """Apply the operator statement to criteria that name the ratio they score.

    A ratio is a company-level scalar, so the value derived here is the value
    for this criterion and may carry its binding. Criteria whose source never
    names a ratio produce no scope and are skipped, leaving them manual.
    """

    stored_facts = list(company_facts)
    statement_conflict = _financial_statement_years_conflict(stored_facts)
    statement = load_financial_statement(stored_facts)
    resolved: list[QuantitativeFact] = []
    for criterion in criteria:
        scope = criterion.financial_scope
        if scope is None:
            continue
        if statement_conflict:
            resolved.append(QuantitativeFact(
                metric_key=criterion.metric_key, status="REVIEW",
                evidence_key=criterion.metric_key,
                fact_binding_sha256=criterion.fact_binding_sha256,
                confidence=0,
                rationale="동일 회계연도의 재무제표 값이 상충하여 적용할 재무비율을 확정할 수 없습니다.",
            ))
            continue
        derived = derive_financial_value(
            scope, statement, as_of=as_of, bid_notice_at=bid_notice_at,
        )
        resolved.append(
            QuantitativeFact(
                metric_key=criterion.metric_key,
                status=derived.status,
                value=derived.value,
                evidence_key=criterion.metric_key,
                evidence_reference=derived.evidence_reference,
                fact_binding_sha256=criterion.fact_binding_sha256,
                confidence=0.85 if derived.status == "ESTIMATED" else 0,
                rationale=derived.rationale,
            )
        )
    return resolved


def _top_bracket_threshold(criterion: QuantitativeCriterion) -> float | None:
    """The value at which a larger count can no longer improve the score."""

    top_brackets = [
        bracket
        for bracket in (criterion.brackets or [])
        if bracket.min_value is not None
        and bracket.max_value is None
        and bracket.points == criterion.max_points
    ]
    if len(top_brackets) != 1:
        return None
    top = top_brackets[0]
    return math.ceil(top.min_value) if top.min_inclusive else math.floor(top.min_value) + 1


def resolve_personnel_register_facts(
    criteria: Sequence[QuantitativeCriterion],
    company_facts: Iterable[CompanyFact],
    *,
    as_of: datetime,
    bid_notice_at: datetime | None = None,
) -> list[QuantitativeFact]:
    """Apply a reviewed roster, with separately attested assignment forecasts."""

    stored_facts = tuple(company_facts)
    resolved: list[QuantitativeFact] = []
    for criterion in criteria:
        scope = criterion.personnel_scope
        if scope is None:
            continue
        reference = personnel_reference_time(
            scope, as_of=as_of, bid_notice_at=bid_notice_at,
        )
        if reference is None:
            derived = DerivedPersonnelValue(
                status="REVIEW", rationale="인력 산정 기준 공고일이 없어 자동 계산을 중지했습니다.",
            )
        else:
            loaded = load_personnel_roster(stored_facts, as_of=reference)
            derived = (
                derive_personnel_value(
                    scope, loaded.roster.members, as_of=as_of,
                    bid_notice_at=bid_notice_at,
                    sufficiency_value=_top_bracket_threshold(criterion),
                    allow_assignment_capacity=(loaded.roster.assignment_assumption
                        == "ALL_QUALIFIED_ROSTER_MEMBERS_AVAILABLE"),
                )
                if loaded.roster is not None else
                DerivedPersonnelValue(status="REVIEW", rationale=loaded.reason)
            )
            if loaded.projected:
                derived = derived.model_copy(update={
                    "rationale": (derived.rationale + " " + loaded.reason)[:1_000],
                })
        resolved.append(
            QuantitativeFact(
                metric_key=criterion.metric_key,
                status=derived.status,
                value=derived.value,
                evidence_key=criterion.metric_key,
                evidence_reference=derived.evidence_reference,
                fact_binding_sha256=criterion.fact_binding_sha256,
                confidence=0.85 if derived.status == "ESTIMATED" else 0,
                rationale=derived.rationale,
            )
        )
    return resolved


def _scaled_value(value: float | None, scale: Decimal) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)) * scale)


def _candidate_fact_binding_sha256(
    candidate: ImmutableQuantitativeRuleCandidate,
    *,
    document_sha256: str,
) -> str:
    binding_candidate = candidate.model_dump(mode="json")
    for case in binding_candidate["cases"]:
        # No upper bound has the same meaning before and after the additive
        # interval schema. Preserve existing company-fact bindings; a populated
        # bound (and every new operator) still changes the identity.
        if case.get("comparison_upper_value") is None:
            case.pop("comparison_upper_value", None)
    binding_payload = {
        "binding_schema": "pai-loop-quantitative-fact-binding-1.0.0",
        "document_sha256": document_sha256,
        "candidate": binding_candidate,
    }
    if candidate.metric in {"PERFORMANCE_COUNT", "PERFORMANCE_AMOUNT"}:
        scope = parse_performance_recognition_scope(
            _performance_scope_literal(candidate),
            metric_key=("company.performance.count" if candidate.metric == "PERFORMANCE_COUNT"
                        else "company.performance.amount"),
        )
        binding_payload["performance_recognition_contract"] = "performance-recognition-2"
        binding_payload["performance_scope"] = (
            scope.model_dump(mode="json", exclude={"source_literal"}) if scope else None
        )
    return _canonical_digest(binding_payload)


def _candidate_brackets(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> list[ScoreBracket] | None:
    scale = _metric_scale(candidate)
    if scale is None:
        return None
    if candidate.scoring_method == "BRACKET":
        return [
            ScoreBracket(
                bracket_id=f"dyn-{index}-{_canonical_digest(item.model_dump(mode='json'))[:12]}",
                label=item.label,
                min_value=_scaled_value(item.min_value, scale),
                max_value=_scaled_value(item.max_value, scale),
                min_inclusive=item.min_inclusive,
                max_inclusive=item.max_inclusive,
                points=item.points,
            )
            for index, item in enumerate(candidate.brackets, start=1)
        ]
    threshold = candidate.threshold
    if candidate.scoring_method != "THRESHOLD" or threshold is None:
        return None
    # An equality complement cannot be represented as one contiguous numeric
    # interval. Keep the captured rule available for audit but do not invent a
    # deterministic scoring formula.
    if threshold.operator == "EQ":
        return None
    value = _scaled_value(threshold.threshold_value, scale)
    if value is None:
        return None
    met = threshold.points_if_met
    unmet = threshold.points_if_not_met
    if threshold.operator == "GTE":
        bounds = (
            (None, value, True, False, unmet, "임계값 미충족"),
            (value, None, True, False, met, "임계값 충족"),
        )
    elif threshold.operator == "GT":
        bounds = (
            (None, value, True, True, unmet, "임계값 미충족"),
            (value, None, False, False, met, "임계값 충족"),
        )
    elif threshold.operator == "LTE":
        bounds = (
            (None, value, True, True, met, "임계값 충족"),
            (value, None, False, False, unmet, "임계값 미충족"),
        )
    else:  # LT
        bounds = (
            (None, value, True, False, met, "임계값 충족"),
            (value, None, True, False, unmet, "임계값 미충족"),
        )
    return [
        ScoreBracket(
            bracket_id=f"dyn-threshold-{index}",
            label=label,
            min_value=minimum,
            max_value=maximum,
            min_inclusive=min_inclusive,
            max_inclusive=max_inclusive,
            points=points,
        )
        for index, (
            minimum,
            maximum,
            min_inclusive,
            max_inclusive,
            points,
            label,
        ) in enumerate(bounds, start=1)
    ]


def _compiled_formula_contract(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> tuple[DeterministicFormula | None, tuple[CategoryScore, ...] | None]:
    if candidate.scoring_method != "FORMULA" or not candidate.formula_literal:
        return None, None
    spec = _metric_spec(candidate)
    if spec is None:
        return None, None
    value_kind = str(spec.get("value_kind", "NUMERIC"))
    if value_kind in {"CATEGORICAL", "BOOLEAN"}:
        categories = compile_category_formula(
            candidate.formula_literal,
            maximum_points=candidate.max_points,
        )
        if value_kind == "BOOLEAN" and (
            categories is None or not boolean_categories_complete(categories)
        ):
            return None, None
        return None, categories
    scale = _metric_scale(candidate)
    if scale is None:
        return None, None
    return (
        compile_arithmetic_formula(
            candidate.formula_literal,
            maximum_points=candidate.max_points,
            source_unit_scale=float(scale),
        ),
        None,
    )


def _criterion_set_aside(
    criterion: QuantitativeCriterion, reason: str
) -> CriterionEstimate:
    """Preserve the uncomputed row's own range, outside quantitative totals."""

    return CriterionEstimate(
        criterion_id=criterion.criterion_id,
        category=criterion.category,
        label=criterion.label,
        max_points=_round_points(criterion.max_points),
        formula=criterion.formula,
        rule_floor_points=0,
        floor_condition=None,
        rule_base_points=None,
        base_condition=None,
        source_anchor=criterion.source_anchor,
        evidence_key=None,
        evidence_reference=None,
        evidence_sha256=None,
        fact_binding_sha256=None,
        estimated_points=None,
        lower_points=0,
        upper_points=_round_points(criterion.max_points),
        confidence=0,
        status="OUT_OF_SCOPE",
        rationale=reason,
        assumptions=[
            "이 항목의 배점과 미확정 범위는 별도로 보존하며, "
            "정량 합계와 상하한에는 반영하지 않습니다."
        ],
    )


def _implicit_credit_unit_is_source_bound(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> bool:
    """A complete, source-bound credit category table establishes rating units.

    Only an absent unit can use this proof. Explicit incompatible units, numeric
    metrics, unbound rows and incomplete/overlapping rating domains still fail.
    The extracted candidate and its evidence are never rewritten.
    """
    if (
        candidate.metric != "CREDIT_RATING"
        or candidate.unit is not None
        or candidate.scoring_method != "CASE_TABLE"
        or tuple(candidate.required_evidence) != ("company.credit_rating",)
        or not candidate.cases
        or candidate.brackets
        or candidate.threshold is not None
        or candidate.formula_literal is not None
    ):
        return False
    if any(
        case.evidence.attachment_id != candidate.source_attachment_id
        or not evidence_quote_matches_source(case.literal, case.evidence.quote)
        or not _case_award_matches_literal(candidate, case, case.literal)
        or case.comparison_value is not None
        or case.comparison_upper_value is not None
        for case in candidate.cases
    ):
        return False
    rows = tuple(CaseTableRowLiteral(
        operator=case.operator, comparison_value=None, comparison_upper_value=None,
        category_values=case.category_values, source_literal=case.literal,
        award_kind=case.award_kind, award_value=case.award_value,
    ) for case in sorted(candidate.cases, key=lambda item: item.row_order))
    return compile_case_table(
        rows, value_kind="CREDIT_RATING", maximum_points=candidate.max_points,
    ) is not None


def _compiled_case_table_contract(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> CompiledCaseTable | None:
    if candidate.scoring_method != "CASE_TABLE" or not candidate.cases:
        return None
    if any(
        (
            not evidence_quote_matches_source(case.literal, case.evidence.quote)
            and not _short_count_literal_has_owned_context(candidate, case)
        )
        or not _case_award_matches_literal(candidate, case, case.literal)
        for case in candidate.cases
    ):
        return None
    spec = _metric_spec(candidate)
    if spec is None:
        return None
    if candidate.metric in _DISCRETE_COUNT_METRICS:
        value_kind: Literal[
            "NUMERIC", "DISCRETE", "CATEGORICAL", "CREDIT_RATING"
        ] = "DISCRETE"
    elif candidate.metric == "CREDIT_RATING":
        value_kind = "CREDIT_RATING"
    elif str(spec.get("value_kind", "NUMERIC")) in {"CATEGORICAL", "BOOLEAN"}:
        value_kind = "CATEGORICAL"
    else:
        value_kind = "NUMERIC"
    scale = Decimal("1")
    if value_kind not in {"CATEGORICAL", "CREDIT_RATING"}:
        candidate_scale = _metric_scale(candidate)
        if candidate_scale is None:
            return None
        scale = candidate_scale
    rows = tuple(
        CaseTableRowLiteral(
            operator=item.operator,
            comparison_value=(
                _scaled_value(item.comparison_value, scale)
                if item.comparison_value is not None
                else None
            ),
            comparison_upper_value=(
                _scaled_value(item.comparison_upper_value, scale)
                if item.comparison_upper_value is not None else None
            ),
            category_values=item.category_values,
            source_literal=item.literal,
            award_kind=item.award_kind,
            award_value=item.award_value,
        )
        for item in sorted(candidate.cases, key=lambda value: value.row_order)
    )
    return compile_case_table(
        rows,
        value_kind=value_kind,
        maximum_points=candidate.max_points,
    )


_LogicalTableKey = tuple[str, str]
_MAX_LOGICAL_PROGRAM_TABLES = 16
_MAX_LOGICAL_RESOLVER_WORK = 50_000
_LogicalDocumentRole = Literal["NOTICE", "RFP", "SCOPE"]


@dataclass
class _LogicalResolverBudget:
    remaining: int = _MAX_LOGICAL_RESOLVER_WORK
    exhausted: bool = False

    def consume(self, units: int = 1) -> bool:
        if units < 0 or self.remaining < units:
            self.exhausted = True
            return False
        self.remaining -= units
        return True


@dataclass(frozen=True)
class _LogicalQuantitativeProgram:
    tables: tuple[ImmutableQuantitativeTable, ...]
    candidates: tuple[ImmutableQuantitativeRuleCandidate, ...]
    reasons: tuple[str, ...] = ()


def _logical_table_key(table: ImmutableQuantitativeTable) -> _LogicalTableKey:
    return (table.source_attachment_id, table.table_id)


def _normalize_semantic_text(value: str | None) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value or "").split()
    ).casefold()


_SOURCE_LABEL_ROLE_PATTERNS: dict[_LogicalDocumentRole, re.Pattern[str]] = {
    "NOTICE": re.compile(r"(?:입찰\s*공고|공고문|notice)", re.IGNORECASE),
    "RFP": re.compile(r"(?:제안\s*요청서|제안요청서|\brfp\b)", re.IGNORECASE),
    "SCOPE": re.compile(
        r"(?:과업\s*(?:지시서|내용서|설명서)|\bscope\b)",
        re.IGNORECASE,
    ),
}


def _attachment_document_role(
    binding: AttachmentDocumentBinding,
) -> _LogicalDocumentRole | None:
    explicit = (
        binding.document_type
        if binding.document_type in {"NOTICE", "RFP", "SCOPE"}
        else None
    )
    label = unicodedata.normalize("NFKC", binding.source_label or "")
    inferred = {
        role
        for role, pattern in _SOURCE_LABEL_ROLE_PATTERNS.items()
        if pattern.search(label)
    }
    if len(inferred) > 1:
        return None
    inferred_role = next(iter(inferred), None)
    if explicit is not None and inferred_role is not None and explicit != inferred_role:
        return None
    return explicit or inferred_role


def _profile_attachment_roles(
    profile: QuantitativeCandidateProfile,
) -> dict[str, _LogicalDocumentRole]:
    return {
        binding.attachment_id: role
        for binding in profile.document_bindings
        if (role := _attachment_document_role(binding)) is not None
    }


def _performance_scope_literal(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> str:
    return _normalize_semantic_text(
        " ".join(
            value
            for value in (
                candidate.criterion_literal,
                candidate.formula_literal or "",
                *(item.literal for item in candidate.cases),
                *(item.literal for item in candidate.recognition_conditions),
            )
            if value
        )
    )


@lru_cache(maxsize=4_096)
def _performance_scope_semantic_fingerprint(
    metric: str,
    literal: str,
) -> str | None:
    spec = _CANONICAL_METRIC_REGISTRY.get(metric)
    if metric not in _UNMODELED_FACT_DIMENSION_METRICS or spec is None:
        return None
    scope = parse_performance_recognition_scope(
        literal,
        metric_key=str(spec["fact_key"]),
    )
    if scope is None:
        return "UNMODELED"
    return _canonical_digest(
        scope.model_dump(mode="json", exclude={"source_literal"})
    )


@lru_cache(maxsize=4_096)
def _candidate_semantic_signature(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> str:
    threshold = candidate.threshold
    return _canonical_digest(
        {
            "criterion_literal": _normalize_semantic_text(
                candidate.criterion_literal
            ),
            "label": _normalize_semantic_text(candidate.label),
            "evidence_section": _normalize_semantic_text(
                candidate.evidence.section
            ),
            "metric": candidate.metric,
            "unit": _normalize_unit(candidate.unit),
            "max_points": str(Decimal(str(candidate.max_points))),
            "scoring_method": candidate.scoring_method,
            "brackets": [
                {
                    "min_value": item.min_value,
                    "max_value": item.max_value,
                    "min_inclusive": item.min_inclusive,
                    "max_inclusive": item.max_inclusive,
                    "points": item.points,
                }
                for item in candidate.brackets
            ],
            "threshold": (
                None
                if threshold is None
                else {
                    "operator": threshold.operator,
                    "threshold_value": threshold.threshold_value,
                    "points_if_met": threshold.points_if_met,
                    "points_if_not_met": threshold.points_if_not_met,
                }
            ),
            "cases": [
                {
                    "operator": item.operator,
                    "comparison_value": item.comparison_value,
                    "category_values": item.category_values,
                    "award_kind": item.award_kind,
                    "award_value": item.award_value,
                    "row_order": item.row_order,
                }
                for item in candidate.cases
            ],
            "formula_literal": " ".join(
                (candidate.formula_literal or "").split()
            ).casefold(),
            "recognition_conditions": sorted(
                (
                    _normalize_semantic_text(item.literal),
                    _normalize_semantic_text(item.evidence.section),
                )
                for item in candidate.recognition_conditions
            ),
            "performance_scope_fingerprint": (
                _performance_scope_semantic_fingerprint(
                    candidate.metric,
                    _performance_scope_literal(candidate),
                )
            ),
            "required_evidence": sorted(candidate.required_evidence),
        }
    )


def _logical_representation_signature(
    table_keys: Iterable[_LogicalTableKey],
    candidates_by_table: dict[
        _LogicalTableKey, tuple[ImmutableQuantitativeRuleCandidate, ...]
    ],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            _candidate_semantic_signature(candidate)
            for table_key in table_keys
            for candidate in candidates_by_table[table_key]
        )
    )


def _candidate_anchor_signature(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> tuple[str, int | None, str | None, str]:
    anchor = candidate.evidence
    return (anchor.attachment_id, anchor.page, anchor.section, anchor.quote.strip())


def _table_page(table: ImmutableQuantitativeTable) -> int | None:
    return table.total_evidence.page if table.total_evidence is not None else None


def _table_can_refine(
    root: ImmutableQuantitativeTable,
    detail: ImmutableQuantitativeTable,
    attachment_roles: dict[str, _LogicalDocumentRole],
) -> bool:
    if detail.source_attachment_id == root.source_attachment_id:
        root_page = _table_page(root)
        detail_page = _table_page(detail)
        return root_page is None or detail_page is None or detail_page >= root_page
    return (
        attachment_roles.get(root.source_attachment_id) == "NOTICE"
        and attachment_roles.get(detail.source_attachment_id) in {"RFP", "SCOPE"}
    )


def _refinement_order_is_valid(
    summary: ImmutableQuantitativeTable,
    detail_keys: Iterable[_LogicalTableKey],
    *,
    tables_by_key: dict[_LogicalTableKey, ImmutableQuantitativeTable],
    attachment_roles: dict[str, _LogicalDocumentRole],
) -> bool:
    details = [tables_by_key[key] for key in detail_keys]
    if not details:
        return False
    if all(
        item.source_attachment_id == summary.source_attachment_id
        for item in details
    ):
        summary_page = _table_page(summary)
        detail_pages = [
            page for item in details if (page := _table_page(item)) is not None
        ]
        return (
            summary_page is None
            or not detail_pages
            or max(detail_pages) > summary_page
        )
    return (
        attachment_roles.get(summary.source_attachment_id) == "NOTICE"
        and all(
            attachment_roles.get(item.source_attachment_id) in {"RFP", "SCOPE"}
            for item in details
        )
    )


def _point_partition_is_preserved(
    summary: Sequence[ImmutableQuantitativeRuleCandidate],
    detail: Sequence[ImmutableQuantitativeRuleCandidate],
    *,
    budget: _LogicalResolverBudget,
) -> bool:
    """Return whether detail rows form whole, positive parts of summary rows."""
    summary_points = sorted(
        (Decimal(str(item.max_points)) for item in summary), reverse=True
    )
    detail_points = sorted(
        (Decimal(str(item.max_points)) for item in detail), reverse=True
    )
    if (
        not summary_points
        or len(detail_points) < len(summary_points)
        or sum(summary_points, Decimal("0"))
        != sum(detail_points, Decimal("0"))
    ):
        return False

    # Each canonical remaining-capacity tuple is one memoized state. This
    # avoids factorial recursion while the shared budget bounds adversarial
    # partitions across the whole resolver invocation.
    states: set[tuple[Decimal, ...]] = {tuple(summary_points)}
    for point in detail_points:
        next_states: set[tuple[Decimal, ...]] = set()
        for remaining in states:
            tried: set[Decimal] = set()
            for slot, capacity in enumerate(remaining):
                if capacity in tried or point > capacity:
                    continue
                if not budget.consume():
                    return False
                tried.add(capacity)
                updated = list(remaining)
                updated[slot] -= point
                next_states.add(tuple(sorted(updated, reverse=True)))
        if not next_states:
            return False
        states = next_states
    return any(all(value == 0 for value in remaining) for remaining in states)


def _best_strict_detail_cover(
    root: ImmutableQuantitativeTable,
    *,
    tables: Sequence[ImmutableQuantitativeTable],
    candidates_by_table: dict[
        _LogicalTableKey, tuple[ImmutableQuantitativeRuleCandidate, ...]
    ],
    attachment_roles: dict[str, _LogicalDocumentRole],
    budget: _LogicalResolverBudget,
) -> tuple[tuple[_LogicalTableKey, ...] | None, bool, tuple[_LogicalTableKey, ...]]:
    """Find one maximally granular, semantically unique subtotal-preserving cover."""
    if root.total_points is None:
        return None, False, ()
    root_total = Decimal(str(root.total_points))
    eligible = [
        table
        for table in tables
        if table is not root
        and table.total_points is not None
        and Decimal("0") < Decimal(str(table.total_points)) < root_total
        and _table_can_refine(root, table, attachment_roles)
    ]
    eligible.sort(key=_logical_table_key)
    if len(eligible) > _MAX_LOGICAL_PROGRAM_TABLES:
        return None, True, tuple(_logical_table_key(item) for item in eligible)

    for size in range(len(eligible), 1, -1):
        covers_by_signature: dict[
            tuple[str, ...], tuple[_LogicalTableKey, ...]
        ] = {}
        for selected in combinations(eligible, size):
            if not budget.consume():
                return None, True, tuple(
                    _logical_table_key(item) for item in eligible
                )
            if sum(
                (Decimal(str(item.total_points)) for item in selected),
                Decimal("0"),
            ) != root_total:
                continue
            keys = tuple(_logical_table_key(item) for item in selected)
            signature = _logical_representation_signature(keys, candidates_by_table)
            covers_by_signature.setdefault(signature, keys)
        if not covers_by_signature:
            continue
        conflict_keys = tuple(
            sorted(
                {
                    key
                    for cover in covers_by_signature.values()
                    for key in cover
                }
            )
        )
        if len(covers_by_signature) != 1:
            return None, True, conflict_keys
        return next(iter(covers_by_signature.values())), False, conflict_keys
    return None, False, ()


def _logical_conflict_reason(
    table: ImmutableQuantitativeTable,
    code: str,
) -> str:
    return (
        f"LOGICAL_TABLE_CONFLICT|{table.source_attachment_id}|"
        f"{table.table_id}|{code}"
    )


def _logical_candidate_conflict_reasons(
    candidates: Sequence[ImmutableQuantitativeRuleCandidate],
) -> set[str]:
    reasons: set[str] = set()
    by_id: dict[str, ImmutableQuantitativeRuleCandidate] = {}
    by_anchor: dict[
        tuple[str, int | None, str | None, str],
        ImmutableQuantitativeRuleCandidate,
    ] = {}
    for candidate in candidates:
        prior_id = by_id.setdefault(candidate.criterion_id, candidate)
        prior_anchor = by_anchor.setdefault(
            _candidate_anchor_signature(candidate), candidate
        )
        conflicts = []
        if prior_id is not candidate:
            conflicts.extend((prior_id, candidate))
        if prior_anchor is not candidate:
            conflicts.extend((prior_anchor, candidate))
        for item in conflicts:
            reasons.add(
                "LOGICAL_CRITERION_CONFLICT|"
                f"{item.source_attachment_id}|{item.table_id}|"
                f"{item.criterion_id}"
            )
    if reasons:
        reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
    return reasons


def _restated_rule_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _restated_rule_payload(item)
            for key, item in value.items()
            if key not in {"attachment_id", "page", "confidence"}
        }
    if isinstance(value, (list, tuple)):
        return [_restated_rule_payload(item) for item in value]
    return " ".join(value.split()) if isinstance(value, str) else value


def _restated_table_signature(
    table: ImmutableQuantitativeTable,
    candidates: Sequence[ImmutableQuantitativeRuleCandidate],
) -> str | None:
    ids = [item.criterion_id for item in candidates]
    if (
        table.status != "AVAILABLE"
        or not candidates
        or len(ids) != len(set(ids))
        or set(ids) != set(table.criterion_ids)
        or set(ids) != set(table.available_criterion_ids)
        or table.review_criterion_ids
        or table.total_points is None
        or table.total_evidence is None
        or table.total_evidence.attachment_id != table.source_attachment_id
        or not table.total_evidence.quote.strip()
        or not (table.total_evidence.section or "").strip()
        or (table.minimum_score is not None and table.minimum_evidence is None)
        or sum(Decimal(str(item.max_points)) for item in candidates)
        != Decimal(str(table.total_points))
    ):
        return None
    if any(
        not item.evidence.quote.strip()
        or not (item.evidence.section or "").strip()
        or item.evidence.attachment_id != table.source_attachment_id
        for item in candidates
    ):
        return None
    rules = []
    for candidate in candidates:
        payload = candidate.model_dump(mode="json")
        for key in ("source_attachment_id", "table_id", "criterion_id"):
            payload.pop(key)
        # Labels and every condition/anchor section carry scoring scope.
        rules.append(_canonical_digest(_restated_rule_payload(payload)))
    payload = table.model_dump(mode="json")
    for key in (
        "source_attachment_id", "table_id", "criterion_ids",
        "available_criterion_ids", "review_criterion_ids",
    ):
        payload.pop(key)
    return _canonical_digest({
        "table": _restated_rule_payload(payload), "rules": sorted(rules),
    })


def _same_restated_document(
    left: AttachmentDocumentBinding, right: AttachmentDocumentBinding,
) -> bool:
    role = _attachment_document_role(left)
    if role is None or role != _attachment_document_role(right):
        return False
    if left.document_sha256 == right.document_sha256:
        return True
    labels = [
        re.fullmatch(r"(.+)\.(hwp|hwpx|pdf)", _normalize_semantic_text(item.source_label))
        for item in (left, right)
    ]
    # A filename is insufficient on its own. The caller also proves the full
    # validated table program equal; same-format revisions never qualify.
    return bool(
        all(labels)
        and labels[0].group(1) == labels[1].group(1)
        and labels[0].group(2) != labels[1].group(2)
    )


def _fold_restated_table_candidates(
    profile: QuantitativeCandidateProfile,
    candidates: tuple[ImmutableQuantitativeRuleCandidate, ...],
) -> tuple[ImmutableQuantitativeRuleCandidate, ...]:
    """Fold complete alternate representations, never similar individual rows."""
    if (
        profile.manifest_sha256 is None
        or len(profile.tables) > _MAX_LOGICAL_PROGRAM_TABLES
        or any(issue.attachment_id is None for issue in profile.issues)
    ):
        return candidates
    bindings = {item.attachment_id: item for item in profile.document_bindings}
    if len(bindings) != len(profile.document_bindings):
        return candidates
    expected = set(profile.expected_attachment_ids) & set(profile.processed_attachment_ids)
    programs: dict[str, dict[str, _LogicalTableKey]] = {}
    invalid: set[str] = set()
    for table in profile.tables:
        attachment_id = table.source_attachment_id
        local = tuple(
            item for item in profile.available_candidates
            if (item.source_attachment_id, item.table_id) == _logical_table_key(table)
        )
        signature = _restated_table_signature(table, local)
        program = programs.setdefault(attachment_id, {})
        if signature is None or signature in program:
            invalid.add(attachment_id)
        else:
            program[signature] = _logical_table_key(table)
    invalid.update(item.source_attachment_id for item in profile.review_candidates)
    invalid.update(issue.attachment_id for issue in profile.issues)
    invalid.update(item.attachment_id for item in profile.not_applicable_evidence)
    table_keys = {_logical_table_key(table) for table in profile.tables}
    invalid.update(
        item.source_attachment_id for item in profile.available_candidates
        if (item.source_attachment_id, item.table_id) not in table_keys
    )
    groups: dict[tuple[str, ...], list[str]] = {}
    for attachment_id, program in programs.items():
        if attachment_id not in invalid and attachment_id in bindings and attachment_id in expected:
            groups.setdefault(tuple(sorted(program)), []).append(attachment_id)
    selected_keys = {(item.source_attachment_id, item.table_id) for item in candidates}
    removed: set[_LogicalTableKey] = set()
    for group in groups.values():
        if len(group) < 2 or not all(
            _same_restated_document(bindings[left], bindings[right])
            for left, right in combinations(group, 2)
        ):
            continue
        # An earlier resolver may already have selected a representation.
        # Keep its original candidate and fact binding, without synthesizing one.
        for signature in programs[group[0]]:
            keys = sorted(programs[attachment_id][signature] for attachment_id in group)
            active = [key for key in keys if key in selected_keys]
            removed.update(active[1:])
    return tuple(
        item for item in candidates
        if (item.source_attachment_id, item.table_id) not in removed
    )


def _logical_quantitative_program(
    profile: QuantitativeCandidateProfile,
) -> _LogicalQuantitativeProgram:
    """Assemble one source-bound program from compatible physical tables."""
    tables = tuple(profile.tables)
    all_candidates = tuple(profile.available_candidates)
    reasons: set[str] = set()
    if not tables:
        # No table was established in the extracted/validated profile; this
        # does not prove that the source document has no physical score table.
        # Preserve the legacy blocker and add the specific diagnostic code so
        # presentation can distinguish this gap from competing-table ambiguity.
        return _LogicalQuantitativeProgram(
            tables=(),
            candidates=all_candidates,
            reasons=(
                "ALTERNATIVE_TABLE_AMBIGUOUS",
                "QUANTITATIVE_TABLE_NOT_ESTABLISHED",
            ),
        )
    if len(tables) > _MAX_LOGICAL_PROGRAM_TABLES:
        reasons.update(
            {"ALTERNATIVE_TABLE_AMBIGUOUS", "LOGICAL_PROGRAM_TABLE_LIMIT_EXCEEDED"}
        )
        reasons.update(
            _logical_conflict_reason(table, "PROGRAM_TABLE_LIMIT_EXCEEDED")
            for table in tables
        )
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=all_candidates,
            reasons=tuple(sorted(reasons)),
        )

    table_keys = [_logical_table_key(table) for table in tables]
    if len(table_keys) != len(set(table_keys)):
        reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
        for table in tables:
            if table_keys.count(_logical_table_key(table)) > 1:
                reasons.add(
                    _logical_conflict_reason(table, "DUPLICATE_PHYSICAL_TABLE")
                )
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=all_candidates,
            reasons=tuple(sorted(reasons)),
        )

    tables_by_key = {_logical_table_key(table): table for table in tables}
    candidates_by_table = {
        key: tuple(
            candidate
            for candidate in all_candidates
            if (candidate.source_attachment_id, candidate.table_id) == key
        )
        for key in tables_by_key
    }
    structure_invalid = False
    multi_table = len(tables) > 1
    attachment_ids = {table.source_attachment_id for table in tables}
    cross_attachment = len(attachment_ids) > 1
    attachment_roles = _profile_attachment_roles(profile)
    if cross_attachment:
        notice_attachments = {
            attachment_id
            for attachment_id in attachment_ids
            if attachment_roles.get(attachment_id) == "NOTICE"
        }
        roles_verified = (
            len(notice_attachments) == 1
            and all(
                attachment_roles.get(attachment_id) in {"RFP", "SCOPE"}
                for attachment_id in attachment_ids - notice_attachments
            )
        )
        if not roles_verified:
            reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
            reasons.update(
                _logical_conflict_reason(
                    table, "CROSS_ATTACHMENT_ROLE_UNVERIFIED"
                )
                for table in tables
            )
            structure_invalid = True
    for table in tables:
        key = _logical_table_key(table)
        local = candidates_by_table[key]
        candidate_ids = [item.criterion_id for item in local]
        if table.status != "AVAILABLE":
            reasons.add("TABLE_NOT_SOURCE_VALIDATED")
            if multi_table:
                reasons.add(
                    _logical_conflict_reason(table, "TABLE_NOT_SOURCE_VALIDATED")
                )
            structure_invalid = True
        if table.total_points is None or table.total_evidence is None:
            reasons.add("TABLE_TOTAL_INCOMPLETE")
            if multi_table:
                reasons.add(_logical_conflict_reason(table, "TABLE_TOTAL_INCOMPLETE"))
            structure_invalid = True
        elif (
            not table.total_evidence.quote.strip()
            or table.total_evidence.attachment_id != table.source_attachment_id
        ):
            reasons.add("TABLE_TOTAL_ANCHOR_INCOMPLETE")
            if multi_table:
                reasons.add(
                    _logical_conflict_reason(table, "TABLE_TOTAL_ANCHOR_INCOMPLETE")
                )
            structure_invalid = True
        if (
            not local
            or len(candidate_ids) != len(set(candidate_ids))
            or set(candidate_ids) != set(table.criterion_ids)
            or set(candidate_ids) != set(table.available_criterion_ids)
            or table.review_criterion_ids
        ):
            reasons.add("TABLE_CRITERIA_LINKAGE_INCOMPLETE")
            if multi_table:
                reasons.add(
                    _logical_conflict_reason(table, "TABLE_CRITERIA_LINKAGE_INCOMPLETE")
                )
            structure_invalid = True
        if table.total_points is not None:
            local_total = sum(
                (Decimal(str(item.max_points)) for item in local), Decimal("0")
            )
            if local_total != Decimal(str(table.total_points)):
                reasons.add("TABLE_TOTAL_MISMATCH")
                if multi_table:
                    reasons.add(_logical_conflict_reason(table, "TABLE_TOTAL_MISMATCH"))
                structure_invalid = True

    unlinked = [
        candidate
        for candidate in all_candidates
        if (candidate.source_attachment_id, candidate.table_id) not in tables_by_key
    ]
    if unlinked:
        reasons.add("TABLE_CRITERIA_LINKAGE_INCOMPLETE")
        structure_invalid = True
        for candidate in unlinked:
            reasons.add(
                "LOGICAL_CRITERION_CONFLICT|"
                f"{candidate.source_attachment_id}|{candidate.table_id}|"
                f"{candidate.criterion_id}"
            )

    if len(tables) == 1:
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=candidates_by_table[table_keys[0]],
            reasons=tuple(sorted(reasons)),
        )
    if structure_invalid:
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=all_candidates,
            reasons=tuple(sorted(reasons)),
        )

    budget = _LogicalResolverBudget()
    totals = {Decimal(str(table.total_points)) for table in tables}
    if len(totals) == 1:
        max_rows = max(len(candidates_by_table[key]) for key in table_keys)
        most_detailed = [
            key for key in table_keys if len(candidates_by_table[key]) == max_rows
        ]
        representations = {
            _logical_representation_signature((key,), candidates_by_table)
            for key in most_detailed
        }
        if len(representations) != 1:
            reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
            reasons.update(
                _logical_conflict_reason(
                    tables_by_key[key], "EQUAL_SUBTOTAL_ALTERNATIVE"
                )
                for key in most_detailed
            )
            return _LogicalQuantitativeProgram(
                tables=tables,
                candidates=all_candidates,
                reasons=tuple(sorted(reasons)),
            )
        selected_key = sorted(
            most_detailed,
            key=lambda key: (
                0
                if attachment_roles.get(key[0]) in {"RFP", "SCOPE"}
                else 1,
                key,
            ),
        )[0]
        selected = candidates_by_table[selected_key]
        selected_signature = _logical_representation_signature(
            (selected_key,), candidates_by_table
        )
        for key in table_keys:
            if key == selected_key:
                continue
            summary = candidates_by_table[key]
            exact = (
                _logical_representation_signature((key,), candidates_by_table)
                == selected_signature
            )
            chronology_ok = exact or _refinement_order_is_valid(
                tables_by_key[key],
                (selected_key,),
                tables_by_key=tables_by_key,
                attachment_roles=attachment_roles,
            )
            if not exact and (
                not chronology_ok
                or not _point_partition_is_preserved(
                    summary, selected, budget=budget
                )
            ):
                reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
                reasons.add(
                    _logical_conflict_reason(
                        tables_by_key[key], "EQUAL_SUBTOTAL_NOT_REFINED"
                    )
                )
        if budget.exhausted:
            reasons.update(
                {"ALTERNATIVE_TABLE_AMBIGUOUS", "LOGICAL_RESOLVER_BUDGET_EXCEEDED"}
            )
            reasons.update(
                _logical_conflict_reason(table, "RESOLVER_WORK_BUDGET_EXCEEDED")
                for table in tables
            )
        if (
            cross_attachment
            and attachment_roles.get(selected_key[0]) not in {"RFP", "SCOPE"}
        ):
            reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
            reasons.add(
                _logical_conflict_reason(
                    tables_by_key[selected_key],
                    "CROSS_ATTACHMENT_REFINEMENT_INVALID",
                )
            )
        reasons.update(_logical_candidate_conflict_reasons(selected))
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=selected,
            reasons=tuple(sorted(reasons)),
        )

    root_covers: dict[_LogicalTableKey, tuple[_LogicalTableKey, ...]] = {}
    for root in tables:
        cover, ambiguous, conflict_keys = _best_strict_detail_cover(
            root,
            tables=tables,
            candidates_by_table=candidates_by_table,
            attachment_roles=attachment_roles,
            budget=budget,
        )
        if budget.exhausted:
            reasons.update(
                {"ALTERNATIVE_TABLE_AMBIGUOUS", "LOGICAL_RESOLVER_BUDGET_EXCEEDED"}
            )
            reasons.add(
                _logical_conflict_reason(root, "RESOLVER_WORK_BUDGET_EXCEEDED")
            )
            break
        if ambiguous:
            reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
            reasons.add(_logical_conflict_reason(root, "SUBTOTAL_COVER_AMBIGUOUS"))
            reasons.update(
                _logical_conflict_reason(
                    tables_by_key[key], "SUBTOTAL_COVER_AMBIGUOUS"
                )
                for key in conflict_keys
            )
        elif cover is not None:
            detail_candidates = tuple(
                candidate
                for key in cover
                for candidate in candidates_by_table[key]
            )
            if not _point_partition_is_preserved(
                candidates_by_table[_logical_table_key(root)],
                detail_candidates,
                budget=budget,
            ):
                reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
                reasons.add(
                    _logical_conflict_reason(
                        root,
                        (
                            "RESOLVER_WORK_BUDGET_EXCEEDED"
                            if budget.exhausted
                            else "SUBTOTAL_PARTITION_MISMATCH"
                        ),
                    )
                )
                if budget.exhausted:
                    reasons.add("LOGICAL_RESOLVER_BUDGET_EXCEEDED")
                    break
            else:
                root_covers[_logical_table_key(root)] = cover
    if reasons:
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=all_candidates,
            reasons=tuple(sorted(reasons)),
        )
    if not root_covers:
        reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
        reasons.update(
            _logical_conflict_reason(table, "SUBTOTAL_NOT_PRESERVED")
            for table in tables
        )
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=all_candidates,
            reasons=tuple(sorted(reasons)),
        )

    outer_roots: list[_LogicalTableKey] = []
    for root_key, cover in root_covers.items():
        root_total = Decimal(str(tables_by_key[root_key].total_points))
        leaf_keys = set(cover)
        nested = any(
            root_total < Decimal(str(tables_by_key[other_key].total_points))
            and leaf_keys.issubset(set(other_cover))
            for other_key, other_cover in root_covers.items()
            if other_key != root_key
        )
        if not nested:
            outer_roots.append(root_key)

    outer_programs = {
        (
            Decimal(str(tables_by_key[root_key].total_points)),
            _logical_representation_signature(
                root_covers[root_key], candidates_by_table
            ),
        )
        for root_key in outer_roots
    }
    if len(outer_programs) != 1:
        reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
        reasons.update(
            _logical_conflict_reason(
                tables_by_key[key], "MULTIPLE_LOGICAL_PROGRAM_ROOTS"
            )
            for key in outer_roots
        )
        return _LogicalQuantitativeProgram(
            tables=tables,
            candidates=all_candidates,
            reasons=tuple(sorted(reasons)),
        )

    if cross_attachment:
        notice_roots = [
            key
            for key in outer_roots
            if attachment_roles.get(key[0]) == "NOTICE"
        ]
        if len(notice_roots) != 1:
            reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
            reasons.update(
                _logical_conflict_reason(
                    tables_by_key[key], "CROSS_ATTACHMENT_REFINEMENT_INVALID"
                )
                for key in outer_roots
            )
            return _LogicalQuantitativeProgram(
                tables=tables,
                candidates=all_candidates,
                reasons=tuple(sorted(reasons)),
            )
        chosen_root = notice_roots[0]
    else:
        chosen_root = sorted(outer_roots)[0]
    selected_keys = set(root_covers[chosen_root])
    selected = tuple(
        candidate
        for key in sorted(selected_keys)
        for candidate in candidates_by_table[key]
    )
    if cross_attachment and any(
        attachment_roles.get(key[0]) not in {"RFP", "SCOPE"}
        for key in selected_keys
    ):
        reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
        reasons.add(
            _logical_conflict_reason(
                tables_by_key[chosen_root], "CROSS_ATTACHMENT_REFINEMENT_INVALID"
            )
        )
    target = Decimal(str(tables_by_key[chosen_root].total_points))
    selected_total = sum(
        (Decimal(str(item.max_points)) for item in selected), Decimal("0")
    )
    if selected_total != target:
        reasons.add("TABLE_TOTAL_MISMATCH")
        reasons.add(
            _logical_conflict_reason(
                tables_by_key[chosen_root], "LOGICAL_SUBTOTAL_MISMATCH"
            )
        )

    participating = set(selected_keys)
    participating.add(chosen_root)
    for root_key, cover in root_covers.items():
        if set(cover).issubset(selected_keys):
            participating.add(root_key)
            participating.update(cover)

    selected_signature = _logical_representation_signature(
        sorted(selected_keys), candidates_by_table
    )
    for key in table_keys:
        if key in participating:
            continue
        table = tables_by_key[key]
        local = candidates_by_table[key]
        local_signature = _logical_representation_signature((key,), candidates_by_table)
        exact = local_signature == selected_signature
        partition_preserved = exact or _point_partition_is_preserved(
            local,
            selected,
            budget=budget,
        )
        same_subtotal_summary = (
            Decimal(str(table.total_points)) == target
            and partition_preserved
            and (
                exact
                or _refinement_order_is_valid(
                    table,
                    sorted(selected_keys),
                    tables_by_key=tables_by_key,
                    attachment_roles=attachment_roles,
                )
            )
        )
        if same_subtotal_summary:
            participating.add(key)
            continue
        reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
        reasons.add(_logical_conflict_reason(table, "UNLINKED_PHYSICAL_TABLE"))

    if budget.exhausted:
        reasons.update(
            {"ALTERNATIVE_TABLE_AMBIGUOUS", "LOGICAL_RESOLVER_BUDGET_EXCEEDED"}
        )
        reasons.update(
            _logical_conflict_reason(table, "RESOLVER_WORK_BUDGET_EXCEEDED")
            for table in tables
        )

    reasons.update(_logical_candidate_conflict_reasons(selected))
    return _LogicalQuantitativeProgram(
        tables=tables,
        candidates=selected,
        reasons=tuple(sorted(reasons)),
    )


def _candidate_financial_scope(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> FinancialRecognitionScope | None:
    spec = _CANONICAL_METRIC_REGISTRY.get(candidate.metric)
    if spec is None:
        return None
    return parse_financial_recognition_scope(
        " ".join(value for value in (
            candidate.criterion_literal, candidate.formula_literal or "",
            *(item.literal for item in candidate.cases),
            *(item.literal for item in candidate.recognition_conditions),
        ) if value),
        metric_key=str(spec["fact_key"]),
    )


def _shared_fact_key_is_explicitly_scoped(
    candidates: Sequence[ImmutableQuantitativeRuleCandidate],
) -> bool:
    # A distinct digest alone does not explain duplicate metrics. Permit only
    # separately named financial inputs that the register bridge understands;
    # unnamed/composite ratios and repeated copies of one scope stay ambiguous.
    scopes = [_candidate_financial_scope(candidate) for candidate in candidates]
    if any(scope is None for scope in scopes):
        return False
    identities = {
        (scope.ratio_kind, scope.fiscal_basis, scope.reference_basis, scope.benchmark_pct)
        for scope in scopes if scope is not None
    }
    return len(identities) == len(candidates)


@dataclass(frozen=True)
class _ActivationReasonPartition:
    notice_reasons: tuple[str, ...]
    row_reasons: tuple[tuple[tuple[str, str, str], tuple[str, ...]], ...]


def _profile_activation_reasons(profile: QuantitativeCandidateProfile) -> list[str]:
    """Preserve the existing sorted diagnostic contract, including row failures."""
    partition = _profile_activation_reason_partition(profile)
    return sorted(set(partition.notice_reasons).union(
        *(set(codes) for _identity, codes in partition.row_reasons)))


def _profile_activation_reason_partition(profile: QuantitativeCandidateProfile) -> _ActivationReasonPartition:
    """Return stable fail-closed codes for the machine activation contract."""

    reasons: set[str] = set()
    expected = set(profile.expected_attachment_ids)
    processed = set(profile.processed_attachment_ids)
    bound = {item.attachment_id for item in profile.document_bindings}
    if (
        not profile.manifest_sha256
        or not expected
        or len(expected) != len(profile.expected_attachment_ids)
        or expected != processed
        or expected != bound
    ):
        reasons.add("CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE")
    if profile.issues or profile.review_candidates:
        reasons.add("SOURCE_VALIDATION_ISSUES_PRESENT")
    program = _logical_quantitative_program(profile)
    reasons.update(program.reasons)
    table_candidates = list(program.candidates)

    binding_ids = {item.attachment_id for item in profile.document_bindings}
    candidates_by_fact: dict[str, list[ImmutableQuantitativeRuleCandidate]] = {}
    for candidate in table_candidates:
        if (spec := _CANONICAL_METRIC_REGISTRY.get(candidate.metric)) is not None:
            candidates_by_fact.setdefault(str(spec["fact_key"]), []).append(candidate)
    if any(
        len(candidates) > 1 and not _shared_fact_key_is_explicitly_scoped(candidates)
        for candidates in candidates_by_fact.values()
    ):
        reasons.add("FACT_KEY_AMBIGUOUS")
    rows = []
    for candidate in table_candidates:
        candidate_reasons: set[str] = set()
        scoring_anchors = [item.evidence for item in candidate.brackets]
        if candidate.threshold is not None:
            scoring_anchors.append(candidate.threshold.evidence)
        scoring_anchors.extend(item.evidence for item in candidate.cases)
        scoring_anchors.extend(
            item.evidence for item in candidate.recognition_conditions
        )
        if (
            candidate.source_attachment_id not in binding_ids
            or candidate.evidence.attachment_id != candidate.source_attachment_id
            or not candidate.evidence.quote.strip()
            or any(
                item.attachment_id != candidate.source_attachment_id
                or not item.quote.strip()
                for item in scoring_anchors
            )
        ):
            candidate_reasons.add("SOURCE_ANCHOR_INCOMPLETE")
        spec = _CANONICAL_METRIC_REGISTRY.get(candidate.metric)
        if candidate.metric in _UNMODELED_FACT_DIMENSION_METRICS:
            metric_key = str((spec or {}).get("fact_key") or "")
            performance_literal = " ".join(
                value
                for value in (
                    candidate.criterion_literal,
                    candidate.formula_literal or "",
                    *(item.literal for item in candidate.cases),
                    *(item.literal for item in candidate.recognition_conditions),
                )
                if value
            )
            if parse_performance_recognition_scope(
                performance_literal,
                metric_key=metric_key,
            ) is None:
                candidate_reasons.add("FACT_DIMENSIONS_UNMODELED")
        if spec is None:
            candidate_reasons.add("FACT_KEY_UNREGISTERED")
        elif (
            tuple(candidate.required_evidence) != (spec["fact_key"],)
            or len(candidate.required_evidence) != 1
        ):
            candidate_reasons.add("FACT_EVIDENCE_KEY_UNREGISTERED")
        if _metric_spec(candidate) is None:
            candidate_reasons.add("UNSUPPORTED_UNIT")
        elif not _candidate_unit_is_source_bound(candidate):
            candidate_reasons.add("UNIT_NOT_SOURCE_BOUND")
        elif not _candidate_bound_unit_scales_are_consistent(candidate):
            candidate_reasons.add("BOUND_UNIT_INCONSISTENT")
        if candidate.scoring_method not in {
            "BRACKET",
            "THRESHOLD",
            "FORMULA",
            "CASE_TABLE",
        }:
            candidate_reasons.add("UNSUPPORTED_SCORING_DSL")
        elif candidate.scoring_method == "THRESHOLD" and candidate.threshold is None:
            candidate_reasons.add("UNSUPPORTED_SCORING_DSL")
        elif candidate.scoring_method == "FORMULA":
            compiled_formula, compiled_categories = _compiled_formula_contract(candidate)
            if compiled_formula is None and compiled_categories is None:
                candidate_reasons.add("UNSUPPORTED_SCORING_DSL")
        elif (
            candidate.scoring_method == "CASE_TABLE"
            and _compiled_case_table_contract(candidate) is None
        ):
            candidate_reasons.add("UNSUPPORTED_SCORING_DSL")
        if candidate.scoring_method == "BRACKET":
            brackets = _candidate_brackets(candidate)
            if not brackets:
                candidate_reasons.add("UNSUPPORTED_SCORING_DSL")
            else:
                draft = QuantitativeCriterion(
                    criterion_id="activation-check",
                    category=candidate.metric,
                    label=candidate.label,
                    max_points=candidate.max_points,
                    metric_key=str((spec or {}).get("fact_key") or "unregistered"),
                    unit=str((spec or {}).get("canonical_unit") or candidate.unit),
                    formula_type="BRACKET",
                    formula=candidate.criterion_literal,
                    brackets=brackets,
                    source_anchor=SourceAnchor(
                        document_label=candidate.source_attachment_id,
                        section=candidate.evidence.section or candidate.table_id,
                        quote=candidate.evidence.quote,
                    ),
                    required_evidence_keys=list(candidate.required_evidence),
                )
                error = _rule_error(draft)
                if error and ("겹" in error or "공백" in error or "최솟값" in error or "최댓값" in error):
                    candidate_reasons.add("BRACKETS_NOT_EXHAUSTIVE_OR_OVERLAPPING")
                elif error:
                    candidate_reasons.add("UNSUPPORTED_SCORING_DSL")
        if candidate_reasons:
            rows.append(((candidate.source_attachment_id, candidate.table_id, candidate.criterion_id),
                         tuple(sorted(candidate_reasons))))
    return _ActivationReasonPartition(tuple(sorted(reasons)), tuple(rows))


def _partial_profile_review_criteria(
    profile: QuantitativeCandidateProfile,
) -> list[QuantitativeReviewCriterion] | None:
    """Return review-only rows when every other table invariant is safe.

    Partial activation is deliberately narrower than the extractor's REVIEW
    status. It is allowed only when every issue belongs to a row that is
    wholly excluded from calculation, while the manifest, table total and all
    remaining rows satisfy the ordinary AUTO_ACTIVE contract.
    """

    if profile.status != "REVIEW" or len(profile.tables) > _MAX_LOGICAL_PROGRAM_TABLES:
        return None
    review_tables = [table for table in profile.tables if table.status == "REVIEW"]
    if len(review_tables) != 1:
        return None
    table = review_tables[0]
    keys = [_logical_table_key(item) for item in profile.tables]
    if len(keys) != len(set(keys)):
        return None
    expected = set(profile.expected_attachment_ids)
    processed = set(profile.processed_attachment_ids)
    bound = {item.attachment_id for item in profile.document_bindings}
    # A nonempty second table may be an alternative, summary or another stage.
    # Do not compare it only after deleting review rows or reducing their total.
    # Only source-validated, explicitly zero-point empty companions are inert.
    for companion in profile.tables:
        if companion is table:
            continue
        source_points = re.findall(
            r"([+-]?\d[\d,.]*)\s*점",
            companion.total_evidence.quote if companion.total_evidence else "",
        )
        if (
            companion.status != "AVAILABLE"
            or companion.source_attachment_id not in bound
            or companion.total_points != 0
            or companion.criterion_ids or companion.available_criterion_ids
            or companion.review_criterion_ids
            or companion.minimum_score is not None or companion.minimum_evidence is not None
            or companion.total_evidence is None
            or companion.total_evidence.attachment_id != companion.source_attachment_id
            or not source_points
            or any(not re.fullmatch(r"0+(?:\.0+)?", value) for value in source_points)
        ):
            return None
    if (
        not profile.manifest_sha256
        or not expected
        or len(expected) != len(profile.expected_attachment_ids)
        or expected != processed
        or expected != bound
        or table.status != "REVIEW"
        or table.total_points is None
        or table.total_evidence is None
        or not table.total_evidence.quote.strip()
        or table.total_evidence.attachment_id != table.source_attachment_id
        or not profile.available_candidates
        or not profile.review_candidates
    ):
        return None

    available = tuple(profile.available_candidates)
    review = tuple(profile.review_candidates)
    available_ids = [item.criterion_id for item in available]
    review_ids = [item.criterion_id for item in review]
    all_ids = available_ids + review_ids
    if (
        len(all_ids) != len(set(all_ids))
        or set(table.criterion_ids) != set(all_ids)
        or set(table.available_criterion_ids) != set(available_ids)
        or set(table.review_criterion_ids) != set(review_ids)
        or any(
            item.source_attachment_id != table.source_attachment_id
            or item.table_id != table.table_id
            for item in (*available, *review)
        )
        or any(
            item.status != "REVIEW"
            or not math.isfinite(item.max_points)
            or item.max_points <= 0
            or not item.issue_codes
            for item in review
        )
    ):
        return None

    review_id_set = set(review_ids)
    issue_codes_by_criterion: dict[str, set[str]] = {
        criterion_id: set() for criterion_id in review_ids
    }
    for issue in profile.issues:
        if (
            issue.disposition != "REVIEW"
            or issue.criterion_id not in review_id_set
            or issue.attachment_id != table.source_attachment_id
            or issue.table_id != table.table_id
        ):
            return None
        issue_codes_by_criterion[issue.criterion_id].add(issue.code)
    for candidate in review:
        if set(candidate.issue_codes) != issue_codes_by_criterion[candidate.criterion_id]:
            return None

    candidate_total = sum(
        (Decimal(str(item.max_points)) for item in (*available, *review)),
        Decimal("0"),
    )
    if candidate_total != Decimal(str(table.total_points)):
        return None

    available_total = sum(
        (Decimal(str(item.max_points)) for item in available), Decimal("0")
    )
    machine_table = table.model_copy(
        update={
            "status": "AVAILABLE",
            "total_points": float(available_total),
            "criterion_ids": tuple(available_ids),
            "available_criterion_ids": tuple(available_ids),
            "review_criterion_ids": (),
        }
    )
    machine_profile = profile.model_copy(
        update={
            "status": "AVAILABLE",
            "tables": (machine_table,),
            "review_candidates": (),
            "issues": (),
        }
    )
    if _profile_activation_reasons(machine_profile):
        return None

    return [
        QuantitativeReviewCriterion(
            criterion_id=(
                "review-"
                + _canonical_digest(
                    {
                        "attachment_id": item.source_attachment_id,
                        "table_id": item.table_id,
                        "criterion_id": item.criterion_id,
                    }
                )[:28]
            ),
            category=item.metric,
            label=item.label,
            max_points=item.max_points,
            issue_codes=sorted(set(item.issue_codes)),
        )
        for item in review
    ]


@dataclass(frozen=True)
class _RowPartialActivationPlan:
    candidates: tuple[ImmutableQuantitativeRuleCandidate, ...]
    tables: tuple[ImmutableQuantitativeTable, ...]
    review_criteria: tuple[QuantitativeReviewCriterion, ...]
    reasons: tuple[str, ...]


def _available_row_partial_plan(
    profile: QuantitativeCandidateProfile,
) -> _RowPartialActivationPlan | None:
    """Quarantine compiler failures only after the full source program is valid.

    Resolve totals/alternatives with every original row still present. Removing
    rows must never manufacture a new logical subtotal or hide fact ambiguity.
    This projection is ephemeral; raw, records and fact bindings are unchanged.
    """
    if profile.status != "AVAILABLE":
        return None
    partition = _profile_activation_reason_partition(profile)
    if partition.notice_reasons or not partition.row_reasons:
        return None
    program = _logical_quantitative_program(profile)
    bad = dict(partition.row_reasons)
    if len({identity[:2] for identity in bad}) != 1:
        # Multiple affected tables need explicit stage/alternative ownership.
        return None
    good, review = [], []
    for candidate in program.candidates:
        identity = (candidate.source_attachment_id, candidate.table_id, candidate.criterion_id)
        codes = bad.get(identity)
        if codes is None:
            good.append(candidate)
            continue
        if not math.isfinite(candidate.max_points) or candidate.max_points <= 0:
            return None
        review.append(QuantitativeReviewCriterion(
            criterion_id="review-" + _canonical_digest({
                "attachment_id": identity[0], "table_id": identity[1],
                "criterion_id": identity[2]})[:28],
            category=candidate.metric, label=candidate.label[:300],
            max_points=candidate.max_points, issue_codes=list(codes)))
    if not good or len(review) != len(bad):
        return None
    # The full logical resolver checked the source table totals. Check the
    # projection partition too; review maxima remain in the score denominator.
    original_total = sum((Decimal(str(c.max_points)) for c in program.candidates), Decimal(0))
    retained_total = sum((Decimal(str(c.max_points)) for c in (*good, *review)), Decimal(0))
    if retained_total != original_total:
        return None
    return _RowPartialActivationPlan(tuple(good), program.tables, tuple(review),
        tuple(sorted({code for codes in bad.values() for code in codes})))


def _criterion_from_rule_candidate(
    candidate: ImmutableQuantitativeRuleCandidate,
    bindings: dict[str, str],
    *,
    case_table: CompiledCaseTable | None = None,
) -> QuantitativeCriterion | None:
    """Convert one source rule row into an engine criterion, or refuse.

    ``case_table`` is only for a human-reviewed row whose case program was
    compiled by its own bounded grammar; every other row compiles here.
    """

    spec = _metric_spec(candidate)
    if spec is None:
        return None
    performance_scope = (
        parse_performance_recognition_scope(
            " ".join(
                value
                for value in (
                    candidate.criterion_literal,
                    candidate.formula_literal or "",
                    *(item.literal for item in candidate.cases),
                    *(item.literal for item in candidate.recognition_conditions),
                )
                if value
            ),
            metric_key=str(spec["fact_key"]),
        )
        if candidate.metric in _UNMODELED_FACT_DIMENSION_METRICS
        else None
    )
    financial_scope = _candidate_financial_scope(candidate)
    personnel_scope = parse_personnel_recognition_scope(
        " ".join(
            value
            for value in (
                # Preserve assignment wording even if it appears only in
                # the label; an attested capacity forecast is not staffing.
                candidate.label,
                candidate.criterion_literal,
                candidate.formula_literal or "",
                *(item.literal for item in candidate.cases),
            )
            if value
        ),
        metric_key=str(spec["fact_key"]),
        recognition_literal=" ".join(
            item.literal for item in candidate.recognition_conditions if item.literal
        ),
        allow_assignment_capacity=True,
    )
    scoring_fields: dict[str, Any]
    if candidate.scoring_method == "BRACKET":
        brackets = _candidate_brackets(candidate)
        if not brackets:
            return None
        scoring_fields = {"formula_type": "BRACKET", "brackets": brackets}
    elif candidate.scoring_method == "THRESHOLD" and candidate.threshold is not None:
        scale = _metric_scale(candidate)
        threshold_value = _scaled_value(candidate.threshold.threshold_value, scale) if scale is not None else None
        if threshold_value is None:
            return None
        scoring_fields = {
            "formula_type": "THRESHOLD",
            "threshold_operator": candidate.threshold.operator,
            "threshold_value": threshold_value,
            "threshold_points_if_met": candidate.threshold.points_if_met,
            "threshold_points_if_not_met": candidate.threshold.points_if_not_met,
        }
    elif candidate.scoring_method == "FORMULA":
        compiled_formula, compiled_categories = _compiled_formula_contract(candidate)
        if compiled_formula is not None:
            scoring_fields = {
                "formula_type": "FORMULA",
                "deterministic_formula": compiled_formula,
            }
        elif compiled_categories is not None:
            scoring_fields = {
                "formula_type": "CATEGORICAL",
                "categories": list(compiled_categories),
            }
        else:
            return None
    elif candidate.scoring_method == "CASE_TABLE":
        compiled_case_table = (
            case_table if case_table is not None
            else _compiled_case_table_contract(candidate)
        )
        if compiled_case_table is None:
            return None
        scoring_fields = {
            "formula_type": "CASE_TABLE",
            "case_table": compiled_case_table,
        }
    else:
        return None
    criterion_identity = _canonical_digest(
        {
            "attachment_id": candidate.source_attachment_id,
            "table_id": candidate.table_id,
            "criterion_id": candidate.criterion_id,
        }
    )[:28]
    return QuantitativeCriterion(
        criterion_id=f"dyn-{criterion_identity}",
        category=candidate.metric,
        label=candidate.label,
        max_points=candidate.max_points,
        metric_key=str(spec["fact_key"]),
        unit=str(spec["canonical_unit"]),
        formula=candidate.criterion_literal,
        **scoring_fields,
        performance_scope=performance_scope,
        financial_scope=financial_scope,
        personnel_scope=personnel_scope,
        source_anchor=SourceAnchor(
            document_label=candidate.source_attachment_id,
            document_sha256=bindings.get(candidate.source_attachment_id),
            section=(
                candidate.evidence.section
                or f"{candidate.table_id}/{candidate.criterion_id}"
            ),
            page=candidate.evidence.page,
            quote=candidate.evidence.quote,
        ),
        required_evidence_keys=list(candidate.required_evidence),
        fact_binding_sha256=_candidate_fact_binding_sha256(
            candidate,
            document_sha256=bindings[candidate.source_attachment_id],
        ),
    )


_KST = timezone(timedelta(hours=9))


def _kst_date(value: datetime) -> date:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(_KST).date()


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ReviewRowSource:
    """One current REVIEW row paired with its exact raw extracted row."""

    attachment_id: str
    table_id: str
    criterion_id: str
    document_sha256: str
    source_label: str | None
    raw: Any
    issue_codes: frozenset[str]
    state: str = "REVIEW"


def review_row_sources(
    notice: Notice, profile: QuantitativeCandidateProfile,
) -> dict[tuple[str, str, str], ReviewRowSource]:
    """Pair REVIEW rows with the raw rows of the same attempts the profile read.

    Codes are the row's own review codes plus every profile issue on its row,
    its table and its attachment's unlocated issues, so an approval must name
    every blocker that applies.
    """

    versions = sorted(notice.versions, key=lambda item: item.version_no, reverse=True)
    _attachments, _invalid, attempts = _current_manifest_attempts(
        versions, validate_accepted=False, preserve_quantitative_proof=True,
    )
    bindings = {item.attachment_id: item.document_sha256 for item in profile.document_bindings}
    labels = {item.attachment_id: item.source_label for item in profile.document_bindings}
    identities = [
        (item.source_attachment_id, item.table_id, item.criterion_id)
        for item in profile.review_candidates
    ]
    sources: dict[tuple[str, str, str], ReviewRowSource] = {}
    for review, identity in zip(profile.review_candidates, identities):
        if identities.count(identity) != 1:
            continue
        attachment_id, table_id, criterion_id = identity
        attempt = attempts.get(attachment_id)
        payload = (
            attempt.source_payload
            if attempt is not None and isinstance(attempt.source_payload, dict) else {}
        )
        document_sha256 = bindings.get(attachment_id)
        if (
            attempt is None or document_sha256 is None
            or str(attempt.file_sha256 or "").casefold() != document_sha256
            or payload.get("status") != "ACCEPTED"
        ):
            continue
        try:
            extraction = ExtractionPayload.model_validate(payload.get("result"))
        except (TypeError, ValueError):
            continue
        tables = [item for item in extraction.quantitative_tables if item.table_id == table_id]
        rows = [row for item in tables for row in item.criteria if row.criterion_id == criterion_id]
        if len(tables) != 1 or len(rows) != 1:
            continue
        codes = frozenset(review.issue_codes) | frozenset(
            issue.code for issue in profile.issues
            if issue.attachment_id == attachment_id
            and issue.table_id in {table_id, None}
            and issue.criterion_id in {criterion_id, None}
        )
        sources[identity] = ReviewRowSource(
            attachment_id=attachment_id, table_id=table_id, criterion_id=criterion_id,
            document_sha256=document_sha256, source_label=labels.get(attachment_id),
            raw=rows[0], issue_codes=codes,
        )
    return sources


def _approved_hyphen_case_table(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> CompiledCaseTable | None:
    """Compile a reviewed hyphen row; its printed-literal proof ran separately."""

    spec = _metric_spec(candidate)
    if spec is None or candidate.metric == "CREDIT_RATING" or str(
        spec.get("value_kind", "NUMERIC")
    ) in {"CATEGORICAL", "BOOLEAN"}:
        return None
    value_kind: Literal["NUMERIC", "DISCRETE"] = (
        "DISCRETE" if candidate.metric in _DISCRETE_COUNT_METRICS else "NUMERIC"
    )
    scale = _metric_scale(candidate)
    if scale is None:
        return None
    rows = []
    for item in sorted(candidate.cases, key=lambda value: value.row_order):
        comparison = _scaled_value(item.comparison_value, scale)
        if comparison is None:
            return None
        rows.append(CaseTableRowLiteral(
            operator=item.operator, comparison_value=comparison,
            comparison_upper_value=None, category_values=(),
            source_literal=item.literal, award_kind=item.award_kind,
            award_value=item.award_value,
        ))
    return compile_case_table(
        tuple(rows), value_kind=value_kind, maximum_points=candidate.max_points,
    )


def approved_row_criterion(
    notice: Notice,
    profile: QuantitativeCandidateProfile,
    approval: QuantitativeRowApproval,
    *,
    sources: dict[tuple[str, str, str], ReviewRowSource] | None = None,
) -> QuantitativeCriterion | None:
    """Rebuild one approved row only while every bound identity is unchanged."""

    source = (sources if sources is not None else review_row_sources(notice, profile)).get(
        approval.row_key()
    )
    if (
        source is None
        or approval.notice_key != notice.notice_key
        or approval.manifest_sha256 != profile.manifest_sha256
        or approval.document_sha256 != source.document_sha256
        or approval.raw_candidate_sha256 != raw_candidate_sha256(source.raw)
        or set(approval.waived_issue_codes) != source.issue_codes
        or not source.issue_codes <= WAIVABLE_ISSUE_CODES
        or approval.program.model_dump(mode="json") != row_program(source.raw)
        or not hyphen_case_rows_match(source.raw)
        or not approval.accepted_on <= _kst_date(notice.deadline) <= approval.effective_through
    ):
        return None
    try:
        candidate = ImmutableQuantitativeRuleCandidate.model_validate({
            **source.raw.model_dump(mode="python", exclude={"ambiguity_reason"}),
            "source_attachment_id": source.attachment_id,
            "table_id": source.table_id,
        })
    except ValidationError:
        return None
    case_table = _approved_hyphen_case_table(candidate)
    if case_table is None:
        return None
    return _criterion_from_rule_candidate(
        candidate, {source.attachment_id: source.document_sha256}, case_table=case_table,
    )


def _apply_row_approvals(
    request: QuantitativeEstimateRequest,
    *,
    notice: Notice,
    profile: QuantitativeCandidateProfile,
    company_facts: Sequence[object],
) -> tuple[QuantitativeEstimateRequest, frozenset[str]]:
    """Add approved REVIEW rows to an incomplete-manifest partial subtotal.

    Duplicate, inactive, unverified or stale approvals are ignored, leaving the
    row in REVIEW. The notice total, minimum and every other blocker stay open.
    """

    raw_facts = [
        fact for fact in company_facts
        if getattr(fact, "fact_key", None) == ROW_APPROVAL_FACT_KEY
    ]
    if not raw_facts or profile.status != "INCOMPLETE" or not (
        request.activation_status == "PARTIAL_SOURCE"
        or (request.activation_status == "REVIEW_REQUIRED" and not request.criteria)
    ):
        return request, frozenset()
    reference = _aware_utc(notice.deadline)
    grouped: dict[tuple[str, str, str], list[tuple[object, QuantitativeRowApproval]]] = {}
    for fact in raw_facts:
        try:
            approval = QuantitativeRowApproval.model_validate(getattr(fact, "value", None))
        except ValidationError:
            continue
        if approval.notice_key == notice.notice_key:
            grouped.setdefault(approval.row_key(), []).append((fact, approval))
    if not grouped:
        return request, frozenset()
    sources = review_row_sources(notice, profile)
    existing = {item.criterion_id for item in request.criteria}
    approved: list[QuantitativeCriterion] = []
    for items in grouped.values():
        if len(items) != 1:
            continue
        fact, approval = items[0]
        start = getattr(fact, "effective_from", None)
        end = getattr(fact, "effective_to", None)
        if (
            getattr(fact, "verified", None) is not True
            or getattr(fact, "source", None) != ROW_APPROVAL_SOURCE
            or not isinstance(start, datetime)
            or _aware_utc(start) > reference
            or (end is not None and (not isinstance(end, datetime) or _aware_utc(end) < reference))
        ):
            continue
        criterion = approved_row_criterion(notice, profile, approval, sources=sources)
        if criterion is None or criterion.criterion_id in existing:
            continue
        existing.add(criterion.criterion_id)
        approved.append(criterion)
    if not approved:
        return request, frozenset()
    reasons = list(dict.fromkeys([*request.activation_reasons[:99], ROW_APPROVAL_REASON]))
    assumptions = (
        list(request.assumptions)
        if request.activation_status == "PARTIAL_SOURCE"
        else [
            "현재 PPS manifest의 일부 첨부 또는 행이 미해소 상태입니다. 사람이 승인한 행을 포함한 "
            "항목만 부분 소계로 보여주며, 이 합계는 공고 총점이 아닙니다.",
            "회사 증빙값이 없는 항목은 0점이나 만점으로 가정하지 않습니다.",
        ]
    )
    return QuantitativeEstimateRequest(
        ruleset_version=request.ruleset_version,
        rule_source_status="INCOMPLETE",
        source_validation_status="INCOMPLETE",
        activation_status="PARTIAL_SOURCE",
        activation_reasons=reasons,
        minimum_score=None,
        criteria=[*request.criteria, *approved],
        facts=list(request.facts),
        assumptions=[*assumptions, ROW_APPROVAL_ASSUMPTION][:50],
        source_anchor=request.source_anchor,
    ), frozenset(item.fact_binding_sha256 for item in approved if item.fact_binding_sha256)


def _cap_approved_row_facts(
    request: QuantitativeEstimateRequest, bindings: frozenset[str],
) -> QuantitativeEstimateRequest:
    """An approved reading never becomes a confirmed score, whatever the evidence."""

    if not bindings:
        return request
    facts = [
        fact.model_copy(update={
            "status": "ESTIMATED" if fact.status == "CONFIRMED" else fact.status,
            "rationale": f"{fact.rationale} {ROW_APPROVAL_WARNING}".strip()[:1_000],
        })
        if fact.fact_binding_sha256 in bindings else fact
        for fact in request.facts
    ]
    return request.model_copy(update={"facts": facts})


def _partial_subtotal_statement(reasons: Sequence[object]) -> str:
    if COMPANY_FIRST_REASON in reasons:
        return COMPANY_FIRST_STATEMENT
    if ROW_APPROVAL_REASON in reasons:
        return "원문 검증이 끝났거나 사람이 행 해석을 승인한 항목만의 부분 소계이며 공고 총점이 아닙니다."
    return "원문 검증이 끝난 첨부만의 부분 소계이며 공고 총점이 아닙니다."


SELECTED_PERFORMANCE_COUNT_KEY = "company.performance.count.reviewer_selected"


def available_row_sources(
    notice: Notice, profile: QuantitativeCandidateProfile,
) -> dict[tuple[str, str, str], ReviewRowSource]:
    """Pair AVAILABLE rows with their raw rows, like review_row_sources."""

    versions = sorted(notice.versions, key=lambda item: item.version_no, reverse=True)
    _attachments, _invalid, attempts = _current_manifest_attempts(
        versions, validate_accepted=False, preserve_quantitative_proof=True,
    )
    bindings = {item.attachment_id: item.document_sha256 for item in profile.document_bindings}
    labels = {item.attachment_id: item.source_label for item in profile.document_bindings}
    identities = [
        (item.source_attachment_id, item.table_id, item.criterion_id)
        for item in profile.available_candidates
    ]
    sources: dict[tuple[str, str, str], ReviewRowSource] = {}
    for identity in identities:
        if identities.count(identity) != 1:
            continue
        attachment_id, table_id, criterion_id = identity
        attempt = attempts.get(attachment_id)
        payload = (
            attempt.source_payload
            if attempt is not None and isinstance(attempt.source_payload, dict) else {}
        )
        document_sha256 = bindings.get(attachment_id)
        if (
            attempt is None or document_sha256 is None
            or str(attempt.file_sha256 or "").casefold() != document_sha256
            or payload.get("status") != "ACCEPTED"
        ):
            continue
        try:
            extraction = ExtractionPayload.model_validate(payload.get("result"))
        except (TypeError, ValueError):
            continue
        tables = [item for item in extraction.quantitative_tables if item.table_id == table_id]
        rows = [row for item in tables for row in item.criteria if row.criterion_id == criterion_id]
        if len(tables) != 1 or len(rows) != 1:
            continue
        codes = frozenset(
            issue.code for issue in profile.issues
            if issue.attachment_id == attachment_id
            and issue.table_id in {table_id, None}
            and issue.criterion_id in {criterion_id, None}
        )
        sources[identity] = ReviewRowSource(
            attachment_id=attachment_id, table_id=table_id, criterion_id=criterion_id,
            document_sha256=document_sha256, source_label=labels.get(attachment_id),
            raw=rows[0], issue_codes=codes, state="AVAILABLE",
        )
    return sources


def _sufficient_row_literal(raw: Any, approval: QuantitativeSufficientRowApproval) -> dict[str, Any] | None:
    """The one confirmed row as a single case, or None when it cannot stand alone."""

    matches = [
        choice for choice in sufficient_row_choices(raw)
        if choice["row_kind"] == approval.row_kind and choice["row_index"] == approval.row_index
    ]
    if (
        len(matches) != 1
        or matches[0]["row_literal"] != approval.row_literal
        or abs(matches[0]["award_points"] - approval.award_points) > 1e-9
        or approval.award_points > raw.max_points
    ):
        return None
    discrete = raw.metric in _DISCRETE_COUNT_METRICS
    if approval.row_kind == "CASE":
        case = sorted(raw.cases, key=lambda item: item.row_order)[approval.row_index]
        if case.operator == "IN":
            if raw.metric != "CREDIT_RATING" or not case.category_values:
                return None
        elif case.operator == "GTE":
            # A lone GTE row keeps no upper bound, so it may stand alone only as
            # the table's top band: no other row may start above its cutoff.
            others = [
                bound for index, other in enumerate(sorted(raw.cases, key=lambda item: item.row_order))
                if index != approval.row_index
                for bound in (other.comparison_value, other.comparison_upper_value)
                if bound is not None
            ]
            if raw.metric == "CREDIT_RATING" or case.comparison_value is None or any(
                bound >= case.comparison_value for bound in others
            ):
                return None
        elif case.operator in {"EQ", "BETWEEN"}:
            if not discrete:
                return None
        else:
            return None
        return {"literal": case.literal, "operator": case.operator,
                "comparison_value": case.comparison_value,
                "comparison_upper_value": case.comparison_upper_value,
                "category_values": tuple(case.category_values), "evidence": case.evidence}
    bracket = raw.brackets[approval.row_index]
    if raw.metric == "CREDIT_RATING" or bracket.min_value is None:
        return None
    if bracket.max_value is None:
        if any(other.min_value is not None and other.min_value >= bracket.min_value
               for index, other in enumerate(raw.brackets) if index != approval.row_index):
            return None
        if bracket.min_inclusive:
            lower = bracket.min_value
        elif discrete and float(bracket.min_value).is_integer():
            lower = bracket.min_value + 1
        else:
            return None
        return {"literal": bracket.literal, "operator": "GTE", "comparison_value": lower,
                "comparison_upper_value": None, "category_values": (), "evidence": bracket.evidence}
    if not discrete or not all(float(value).is_integer() for value in (bracket.min_value, bracket.max_value)):
        return None
    lower = int(bracket.min_value) + (0 if bracket.min_inclusive else 1)
    upper = int(bracket.max_value) - (0 if bracket.max_inclusive else 1)
    if lower < 0 or upper < lower:
        return None
    if lower == upper:
        return {"literal": bracket.literal, "operator": "EQ", "comparison_value": float(lower),
                "comparison_upper_value": None, "category_values": (), "evidence": bracket.evidence}
    return {"literal": bracket.literal, "operator": "BETWEEN", "comparison_value": float(lower),
            "comparison_upper_value": float(upper), "category_values": (), "evidence": bracket.evidence}


def approved_sufficient_criterion(
    notice: Notice,
    profile: QuantitativeCandidateProfile,
    approval: QuantitativeSufficientRowApproval,
    *,
    sources: dict[tuple[str, str, str], ReviewRowSource] | None = None,
) -> QuantitativeCriterion | None:
    """Rebuild one confirmed REVIEW row as a one-row program, or refuse."""

    if approval.row_state != "REVIEW":
        return None
    source = (sources if sources is not None else review_row_sources(notice, profile)).get(
        approval.row_key()
    )
    raw = source.raw if source is not None else None
    if (
        source is None
        or source.state != "REVIEW"
        or approval.notice_key != notice.notice_key
        or approval.manifest_sha256 != profile.manifest_sha256
        or approval.document_sha256 != source.document_sha256
        or approval.raw_candidate_sha256 != raw_candidate_sha256(raw)
        or set(approval.waived_issue_codes) != source.issue_codes
        or not source.issue_codes <= SUFFICIENT_WAIVABLE_ISSUE_CODES
        or raw.metric != approval.metric
        or not approval.accepted_on <= _kst_date(notice.deadline) <= approval.effective_through
        or (approval.credit is not None and approval.credit.conditions_sha256 != credit_conditions_digest(
            [item.literal for item in raw.recognition_conditions]))
    ):
        return None
    row = _sufficient_row_literal(raw, approval)
    spec = _CANONICAL_METRIC_REGISTRY.get(raw.metric)
    if row is None or spec is None:
        return None
    unit = raw.unit
    if raw.metric == "CREDIT_RATING":
        # A lone grade row cannot prove the implicit rating unit of a complete
        # table. The confirmed row does; an explicit foreign unit still fails.
        if unit is not None and _normalize_unit(unit) not in spec["unit_scales"]:
            return None
        unit = unit or "등급"
    required = list(raw.required_evidence)
    if "REQUIRED_EVIDENCE_INCOMPLETE" in approval.waived_issue_codes or not required:
        required = [str(spec["fact_key"])]
    try:
        candidate = ImmutableQuantitativeRuleCandidate.model_validate({
            **raw.model_dump(mode="python", exclude={"ambiguity_reason", "cases", "brackets", "threshold", "scoring_method", "unit", "required_evidence"}),
            "unit": unit,
            "required_evidence": required,
            "scoring_method": "CASE_TABLE",
            "brackets": [], "threshold": None,
            "cases": [{
                "literal": row["literal"], "operator": row["operator"],
                "comparison_value": row["comparison_value"],
                "comparison_upper_value": row["comparison_upper_value"],
                "category_values": row["category_values"], "award_kind": "POINTS",
                "award_value": approval.award_points, "row_order": 1,
                "evidence": row["evidence"].model_dump(mode="python"),
            }],
            "source_attachment_id": source.attachment_id,
            "table_id": source.table_id,
        })
    except ValidationError:
        return None
    if raw.metric == "CREDIT_RATING":
        # The raw row may print several instrument columns; bind each extracted
        # enterprise expression to the printed row, then canonicalize only it.
        printed = re.sub(r"\s+", "", unicodedata.normalize("NFKC", row["literal"]))
        if any(re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)) not in printed
               for value in row["category_values"]):
            return None
        categories = compile_credit_rating_values(
            row["category_values"], source_literal=chr(10).join(row["category_values"]))
        if not categories:
            return None
        literal_row = CaseTableRowLiteral(operator="IN", category_values=categories,
                                          source_literal=row["literal"], award_value=approval.award_points)
        value_kind: Literal["NUMERIC", "DISCRETE", "CATEGORICAL"] = "CATEGORICAL"
    else:
        scale = _metric_scale(candidate)
        comparison = _scaled_value(row["comparison_value"], scale) if scale is not None else None
        upper = (_scaled_value(row["comparison_upper_value"], scale)
                 if scale is not None and row["comparison_upper_value"] is not None else None)
        if comparison is None or (row["comparison_upper_value"] is not None and upper is None):
            return None
        try:
            literal_row = CaseTableRowLiteral(operator=row["operator"], comparison_value=comparison,
                                              comparison_upper_value=upper, source_literal=row["literal"],
                                              award_value=approval.award_points)
        except ValidationError:
            return None
        value_kind = "DISCRETE" if raw.metric in _DISCRETE_COUNT_METRICS else "NUMERIC"
    case_table = compile_case_table((literal_row,), value_kind=value_kind, maximum_points=raw.max_points)
    if case_table is None:
        return None
    criterion = _criterion_from_rule_candidate(
        candidate, {source.attachment_id: source.document_sha256}, case_table=case_table,
    )
    if criterion is not None and raw.metric == "PERFORMANCE_COUNT":
        # The reviewer's record selection replaces register recognition, so the
        # row is evaluated on that count only and never re-derived by keywords.
        criterion = criterion.model_copy(update={
            "metric_key": SELECTED_PERFORMANCE_COUNT_KEY,
            "required_evidence_keys": [SELECTED_PERFORMANCE_COUNT_KEY],
            "performance_scope": None,
        })
    return criterion


def approved_available_credit_binding(
    notice: Notice,
    profile: QuantitativeCandidateProfile,
    approval: QuantitativeSufficientRowApproval,
    *,
    sources: dict[tuple[str, str, str], ReviewRowSource] | None = None,
) -> str | None:
    """Criterion id of a validated credit row whose company fact is bound here."""

    if approval.row_state != "AVAILABLE" or approval.metric != "CREDIT_RATING" or approval.credit is None:
        return None
    source = (sources if sources is not None else available_row_sources(notice, profile)).get(
        approval.row_key()
    )
    raw = source.raw if source is not None else None
    if (
        source is None
        or source.state != "AVAILABLE"
        or approval.notice_key != notice.notice_key
        or approval.manifest_sha256 != profile.manifest_sha256
        or approval.document_sha256 != source.document_sha256
        or approval.raw_candidate_sha256 != raw_candidate_sha256(raw)
        or set(approval.waived_issue_codes) != source.issue_codes
        or not source.issue_codes <= SUFFICIENT_WAIVABLE_ISSUE_CODES
        or raw.metric != "CREDIT_RATING"
        or not approval.accepted_on <= _kst_date(notice.deadline) <= approval.effective_through
        or approval.credit.conditions_sha256 != credit_conditions_digest(
            [item.literal for item in raw.recognition_conditions])
        or _sufficient_row_literal(raw, approval) is None
    ):
        return None
    return "dyn-" + _canonical_digest({
        "attachment_id": source.attachment_id, "table_id": source.table_id,
        "criterion_id": source.criterion_id,
    })[:28]


def _partial_source_request(
    request: QuantitativeEstimateRequest,
    added: Sequence[QuantitativeCriterion],
) -> QuantitativeEstimateRequest:
    """Join human-approved rows to an incomplete-manifest partial subtotal."""

    reasons = list(dict.fromkeys([*request.activation_reasons[:99], ROW_APPROVAL_REASON]))
    assumptions = (
        list(request.assumptions)
        if request.activation_status == "PARTIAL_SOURCE"
        else [
            "현재 PPS manifest의 일부 첨부 또는 행이 미해소 상태입니다. 사람이 승인한 행을 포함한 "
            "항목만 부분 소계로 보여주며, 이 합계는 공고 총점이 아닙니다.",
            "회사 증빙값이 없는 항목은 0점이나 만점으로 가정하지 않습니다.",
        ]
    )
    if ROW_APPROVAL_ASSUMPTION not in assumptions:
        assumptions.append(ROW_APPROVAL_ASSUMPTION)
    return QuantitativeEstimateRequest(
        ruleset_version=request.ruleset_version,
        rule_source_status="INCOMPLETE",
        source_validation_status="INCOMPLETE",
        activation_status="PARTIAL_SOURCE",
        activation_reasons=reasons,
        minimum_score=None,
        criteria=[*request.criteria, *added],
        facts=list(request.facts),
        assumptions=assumptions[:50],
        source_anchor=request.source_anchor,
    )


def _active_approval_facts(
    company_facts: Sequence[object], *, fact_key: str, source: str, model: Any, notice: Notice,
) -> dict[tuple[str, str, str], list[tuple[object, Any]]]:
    candidates = [fact for fact in company_facts if getattr(fact, "fact_key", None) == fact_key]
    if not candidates:
        return {}
    reference = _aware_utc(notice.deadline)
    grouped: dict[tuple[str, str, str], list[tuple[object, Any]]] = {}
    for fact in candidates:
        try:
            approval = model.model_validate(getattr(fact, "value", None))
        except ValidationError:
            continue
        if approval.notice_key != notice.notice_key:
            continue
        start = getattr(fact, "effective_from", None)
        end = getattr(fact, "effective_to", None)
        active = (
            getattr(fact, "verified", None) is True
            and getattr(fact, "source", None) == source
            and isinstance(start, datetime) and _aware_utc(start) <= reference
            and (end is None or (isinstance(end, datetime) and _aware_utc(end) >= reference))
        )
        # An inactive duplicate still makes the row ambiguous: fail closed.
        grouped.setdefault(approval.row_key(), []).append((fact if active else None, approval))
    return grouped


def _apply_sufficient_rows(
    request: QuantitativeEstimateRequest,
    *,
    notice: Notice,
    profile: QuantitativeCandidateProfile,
    company_facts: Sequence[object],
) -> tuple[QuantitativeEstimateRequest, list[tuple[QuantitativeCriterion, QuantitativeSufficientRowApproval, object]]]:
    grouped = _active_approval_facts(
        company_facts, fact_key=SUFFICIENT_ROW_FACT_KEY, source=SUFFICIENT_ROW_SOURCE,
        model=QuantitativeSufficientRowApproval, notice=notice,
    )
    if not grouped or profile.status != "INCOMPLETE" or not (
        request.activation_status == "PARTIAL_SOURCE"
        or (request.activation_status == "REVIEW_REQUIRED" and not request.criteria)
    ):
        return request, []
    reviews = review_row_sources(notice, profile)
    availables = available_row_sources(notice, profile)
    existing = {item.criterion_id: item for item in request.criteria}
    added: list[QuantitativeCriterion] = []
    plan: list[tuple[QuantitativeCriterion, QuantitativeSufficientRowApproval, object]] = []
    for items in grouped.values():
        if len(items) != 1 or items[0][0] is None:
            continue
        fact, approval = items[0]
        if approval.row_state == "AVAILABLE":
            criterion_id = approved_available_credit_binding(notice, profile, approval, sources=availables)
            criterion = existing.get(criterion_id) if criterion_id else None
            if criterion is not None:
                plan.append((criterion, approval, fact))
            continue
        criterion = approved_sufficient_criterion(notice, profile, approval, sources=reviews)
        if criterion is None or criterion.criterion_id in existing:
            continue
        existing[criterion.criterion_id] = criterion
        added.append(criterion)
        plan.append((criterion, approval, fact))
    if not plan:
        return request, []
    return (_partial_source_request(request, added) if added else request), plan


def _sufficient_performance_fact(
    criterion: QuantitativeCriterion,
    approval: QuantitativeSufficientRowApproval,
    performance_records: Sequence[CompanyPerformanceRecord],
    notice: Notice,
) -> QuantitativeFact:
    review = QuantitativeFact(
        metric_key=criterion.metric_key, status="REVIEW", evidence_key=criterion.metric_key,
        fact_binding_sha256=criterion.fact_binding_sha256,
        rationale="선택한 실적 계약이 현재 대장에서 검증·기간·금액 조건을 충족하는지 다시 확인해야 합니다.",
    )
    selection = approval.performance
    if selection is None or selection.window_end > _kst_date(notice.deadline):
        return review
    by_id = {str(getattr(record, "id", "")): record for record in performance_records}
    chosen = [by_id.get(record_id) for record_id in selection.record_ids]
    for record in chosen:
        if record is None:
            return review
        amount = getattr(record, "gross_contract_amount_krw", None) or getattr(record, "contract_amount", None)
        end = getattr(record, "end_date", None)
        if (
            getattr(record, "record_status", None) != "VALIDATED"
            or getattr(record, "completed", None) is not True
            or end is None or not selection.window_start <= end <= selection.window_end
            or (selection.min_amount_krw is not None and (amount is None or amount < selection.min_amount_krw))
            or (selection.vat_included_required and getattr(record, "vat_basis", None) != "INCLUDED")
        ):
            return review
    return QuantitativeFact(
        metric_key=criterion.metric_key, status="ESTIMATED", value=float(len(chosen)),
        evidence_key=criterion.metric_key, fact_binding_sha256=criterion.fact_binding_sha256,
        confidence=0.8,
        rationale=(
            f"사람이 인정 대상으로 고른 실적 {len(chosen)}건을 현재 대장에서 검증 완료·완료 여부·"
            "기간·금액 조건으로 다시 확인해 셌습니다."
        ),
    )


def _bind_sufficient_row_facts(
    request: QuantitativeEstimateRequest,
    plan: Sequence[tuple[QuantitativeCriterion, QuantitativeSufficientRowApproval, object]],
    *,
    notice: Notice,
    performance_records: Sequence[CompanyPerformanceRecord],
) -> tuple[QuantitativeEstimateRequest, frozenset[str]]:
    """Replace credit/performance facts of confirmed rows; keep roster/financial facts."""

    if not plan:
        return request, frozenset()
    from .private_company_evidence import sufficient_credit_fact

    replaced: dict[str, QuantitativeFact] = {}
    for criterion, approval, fact in plan:
        if approval.metric == "CREDIT_RATING":
            replaced[criterion.fact_binding_sha256] = sufficient_credit_fact(notice, criterion, approval, fact)
        elif approval.metric == "PERFORMANCE_COUNT":
            replaced[criterion.fact_binding_sha256] = _sufficient_performance_fact(
                criterion, approval, performance_records, notice)
    facts = [item for item in request.facts if item.fact_binding_sha256 not in replaced]
    facts.extend(replaced.values())
    bindings = frozenset(item.fact_binding_sha256 for item, _approval, _fact in plan if item.fact_binding_sha256)
    return request.model_copy(update={"facts": facts}), bindings


def _cap_sufficient_row_facts(
    request: QuantitativeEstimateRequest, bindings: frozenset[str],
) -> QuantitativeEstimateRequest:
    if not bindings:
        return request
    facts = [
        fact.model_copy(update={
            "status": "ESTIMATED" if fact.status == "CONFIRMED" else fact.status,
            "rationale": f"{fact.rationale} {SUFFICIENT_ROW_WARNING}".strip()[:1_000],
        })
        if fact.fact_binding_sha256 in bindings else fact
        for fact in request.facts
    ]
    return request.model_copy(update={"facts": facts})


def _row_criterion_id(attachment_id: str, table_id: str, criterion_id: str) -> str:
    identity = _canonical_digest({
        "attachment_id": attachment_id, "table_id": table_id, "criterion_id": criterion_id,
    })[:28]
    return f"dyn-{identity}"


def _strict_row_probe(
    source: ReviewRowSource,
    request: QuantitativeEstimateRequest,
    *,
    notice: Notice,
    company_facts: Sequence[object],
    performance_records: Sequence[CompanyPerformanceRecord],
) -> BetaScore | None:
    """Score one row with its own compiled rule when only its table was blocked."""

    try:
        candidate = ImmutableQuantitativeRuleCandidate.model_validate({
            **source.raw.model_dump(mode="python", exclude={"ambiguity_reason"}),
            "source_attachment_id": source.attachment_id,
            "table_id": source.table_id,
        })
        criterion = _criterion_from_rule_candidate(
            candidate, {source.attachment_id: source.document_sha256},
        )
        if criterion is None or _rule_error(criterion) is not None:
            return None
        probe = QuantitativeEstimateRequest(
            ruleset_version=request.ruleset_version, rule_source_status="INCOMPLETE",
            source_validation_status="INCOMPLETE", activation_status="PARTIAL_SOURCE",
            activation_reasons=[COMPANY_FIRST_REASON], criteria=[criterion],
        )
        probe = bind_quantitative_company_inputs(
            probe, company_facts, performance_records, as_of=notice.deadline,
            bid_notice_at=getattr(notice, "published_at", None), notice=notice,
        )
        [estimate] = estimate_quantitative_score(probe).criteria
    except (ValidationError, ValueError, TypeError, KeyError, ArithmeticError):
        return None
    if estimate.status not in {"CONFIRMED", "ESTIMATED"} or estimate.estimated_points is None:
        return None
    return BetaScore(
        estimate.estimated_points, "원문 배점 규칙 그대로 계산", estimate.rationale, False,
    )


def _extracted_row_sources(notice: Notice) -> dict[tuple[str, str, str], ReviewRowSource]:
    """Every row of every accepted current extraction, for the beta only.

    A table the validator could not link (summary/detail conflict, total
    mismatch) drops its rows from the profile altogether; the beta still reads
    them from the stored extraction, bound to that attachment's document hash.
    """

    versions = sorted(notice.versions, key=lambda item: item.version_no, reverse=True)
    attachments, _invalid, attempts = _current_manifest_attempts(
        versions, validate_accepted=False, preserve_quantitative_proof=True,
    )
    sources: dict[tuple[str, str, str], ReviewRowSource] = {}
    for attachment in attachments:
        attachment_id = attachment["attachment_id"]
        attempt = attempts.get(attachment_id)
        payload = attempt.source_payload if attempt is not None and isinstance(attempt.source_payload, dict) else {}
        document_sha256 = str(getattr(attempt, "file_sha256", "") or "").casefold()
        if re.fullmatch(r"[a-f0-9]{64}", document_sha256) is None:
            continue
        state = "EXTRACTED"
        try:
            if payload.get("status") == "ACCEPTED":
                tables = ExtractionPayload.model_validate(payload.get("result")).quantitative_tables
            elif payload.get("status") == "REVIEW" and payload.get("error_code") == "UNVERIFIED_QUOTE":
                # The response's quotes failed exact verification; its printed
                # scoring rows still bound a labeled estimate.
                tables = [QuantitativeTableCandidate.model_validate(item)
                          for item in payload.get("unverified_quantitative_tables") or []]
                state = "UNVERIFIED"
            else:
                continue
        except (TypeError, ValueError):
            continue
        for table in tables:
            for row in table.criteria:
                identity = (attachment_id, table.table_id, row.criterion_id)
                if identity in sources:
                    sources.pop(identity)  # repeated identity: ambiguous, skip it
                    continue
                sources[identity] = ReviewRowSource(
                    attachment_id=attachment_id, table_id=table.table_id, criterion_id=row.criterion_id,
                    document_sha256=document_sha256, source_label=attachment.get("file_name"),
                    raw=row, issue_codes=frozenset(), state=state,
                )
    return sources


def _apply_company_first_beta(
    request: QuantitativeEstimateRequest,
    *,
    notice: Notice,
    profile: QuantitativeCandidateProfile,
    company_facts: Sequence[object],
    performance_records: Sequence[CompanyPerformanceRecord],
) -> QuantitativeEstimateRequest | None:
    """Fill every unscored printed row from company facts (beta lower bound).

    Rows the strict path already scored are kept untouched. Each other current
    row is scored by its own compiled rule when that works, else by applying
    company facts to its printed rows, else by its lowest printed award. The
    result is always a PARTIAL_SOURCE subtotal labeled as a beta lower bound.
    """

    if not company_first_enabled():
        return None
    try:
        sources = {
            **_extracted_row_sources(notice),
            **review_row_sources(notice, profile), **available_row_sources(notice, profile),
        }
    except (ValidationError, ValueError, TypeError, KeyError):
        return None
    if not sources:
        return None
    current = estimate_quantitative_score(request)
    settled = {
        item.criterion_id for item in current.criteria
        if item.status == "OUT_OF_SCOPE"
        or (item.status in {"CONFIRMED", "ESTIMATED"} and item.estimated_points is not None)
    }
    deadline = _aware_utc(notice.deadline)
    published_at = getattr(notice, "published_at", None)
    published_at = _aware_utc(published_at) if isinstance(published_at, datetime) else None
    roster_load = load_personnel_roster(company_facts, as_of=deadline)
    if roster_load.roster is None:
        roster_load = load_personnel_roster(company_facts, as_of=datetime.now(timezone.utc))
    roster = roster_load.roster.members if roster_load.roster is not None else None
    credit_grade = company_credit_grade(company_facts)
    statement = financial_ratios(company_facts)
    kept_ids = {item.criterion_id for item in request.criteria}
    seen: set[tuple[str, str, str, float]] = set()
    for item in request.criteria:
        seen.add((item.category, re.sub(r"\s+", "", item.label), re.sub(r"\s+", "", item.formula)[:200], item.max_points))
    chosen: list[tuple[str, str, float, str]] = [
        (item.source_anchor.document_label if item.source_anchor else "", item.category, item.max_points,
         re.sub(r"\s+", "", item.label))
        for item in request.criteria
    ]
    replaced: set[str] = set()
    criteria: list[QuantitativeCriterion] = []
    facts: list[QuantitativeFact] = []
    for (attachment_id, table_id, row_id), source in sorted(sources.items()):
        raw = source.raw
        criterion_id = _row_criterion_id(attachment_id, table_id, row_id)
        if criterion_id in settled:
            continue
        key = (raw.metric, re.sub(r"\s+", "", raw.label), re.sub(r"\s+", "", raw.criterion_literal)[:200], raw.max_points)
        compact_label = re.sub(r"\s+", "", raw.label)
        if criterion_id not in kept_ids and (key in seen or any(
            other_attachment != attachment_id and metric == raw.metric and maximum == raw.max_points
            and (label in compact_label or compact_label in label)
            for other_attachment, metric, maximum, label in chosen
        )):
            # The same printed row read twice: a summary and a detail table, or
            # one announcement uploaded as both HWP and PDF.
            continue
        score = None
        if raw.metric != "CREDIT_RATING" or credit_grade is None:
            score = _strict_row_probe(
                source, request, notice=notice, company_facts=company_facts,
                performance_records=performance_records,
            )
        if score is None:
            score = beta_score(
                raw, deadline=deadline, published_at=published_at,
                credit_grade=credit_grade, roster=roster, records=performance_records,
                statement=statement,
                notice_title=getattr(notice, "title", None),
                estimated_amount=getattr(notice, "estimated_amount", None),
            )
        if score is None:
            continue
        seen.add(key)
        chosen.append((attachment_id, raw.metric, raw.max_points, compact_label))
        metric_key = f"beta.company_first.{criterion_id}"
        points = _round_points(min(score.points, raw.max_points))
        try:
            criteria.append(QuantitativeCriterion(
                criterion_id=criterion_id, category=raw.metric, label=raw.label[:300],
                max_points=raw.max_points, metric_key=metric_key, unit=None,
                formula_type="BRACKET", formula=raw.criterion_literal[:1_000],
                brackets=[ScoreBracket(
                    bracket_id="company-first", label=(score.row_literal or COMPANY_FIRST_LABEL)[:300],
                    points=points,
                )],
                source_anchor=SourceAnchor(
                    document_label=attachment_id[:200], document_sha256=source.document_sha256,
                    section=(raw.evidence.section or f"{table_id}/{row_id}")[:300],
                    page=raw.evidence.page, quote=(raw.evidence.quote or None),
                ),
                required_evidence_keys=[metric_key],
            ))
        except ValidationError:
            continue
        rationale = (
            f"[{COMPANY_FIRST_LABEL}] {score.basis} → 적용 행: {score.row_literal} = {points:g}점"
            + (f" · {COMPANY_FIRST_FLOOR_NOTE}" if score.floor_applied else "")
            + (" · 원문 인용 미검증 표" if source.state == "UNVERIFIED" else "")
        )
        facts.append(QuantitativeFact(
            metric_key=metric_key, status="ESTIMATED", value=1.0, evidence_key=metric_key,
            confidence=0.3 if score.floor_applied else 0.6, rationale=rationale[:1_000],
        ))
        replaced.add(criterion_id)
    if not criteria:
        return None
    kept = [item for item in request.criteria if item.criterion_id not in replaced]
    all_criteria = [*kept, *criteria][:100]
    reasons = list(dict.fromkeys([
        *request.activation_reasons[:98],
        *([] if request.activation_reasons else ["CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE"]),
        COMPANY_FIRST_REASON,
    ]))
    assumptions = [*request.assumptions[:48], COMPANY_FIRST_STATEMENT]
    return QuantitativeEstimateRequest(
        ruleset_version=request.ruleset_version,
        rule_source_status="INCOMPLETE",
        source_validation_status="INCOMPLETE",
        activation_status="PARTIAL_SOURCE",
        activation_reasons=reasons,
        minimum_score=None,
        criteria=all_criteria,
        facts=[*request.facts, *facts][:200],
        assumptions=assumptions,
        source_anchor=request.source_anchor,
    )


def quantitative_request_from_candidate_profile(
    profile: QuantitativeCandidateProfile,
    *,
    facts: list[QuantitativeFact] | None = None,
    allow_partial_source: bool = False,
) -> QuantitativeEstimateRequest:
    """Convert verified source rules, never model output, into engine inputs.

    ``allow_partial_source`` lets notice scoring report a subtotal from the
    attachments whose rules are already source-validated while the manifest as
    a whole is unresolved. It stays off by default; the reviewed-input preview
    keeps that default, while credit binding applies additional source and
    activation checks before opting in.
    """

    ruleset_version = (
        "dynamic-quantitative-rules-"
        f"{_canonical_digest(profile.model_dump(mode='json'))[:24]}"
    )
    row_partial = _available_row_partial_plan(profile)
    partial_review_criteria = (
        list(row_partial.review_criteria) if row_partial is not None
        else _partial_profile_review_criteria(profile)
    )
    # An incomplete manifest can still hold attachment-local rules that passed
    # their own source validation. Report those as a partial subtotal instead
    # of discarding them; the unresolved remainder stays visible as reasons.
    partial_source = (
        allow_partial_source
        and profile.status == "INCOMPLETE"
        and bool(profile.available_candidates)
        and row_partial is None
        and partial_review_criteria is None
        and bool(profile.issues)
    )
    if (
        profile.status != "AVAILABLE"
        and partial_review_criteria is None
        and not partial_source
    ):
        issue_codes = sorted({item.code for item in profile.issues})
        not_applicable = profile.status == "NOT_APPLICABLE"
        return QuantitativeEstimateRequest(
            ruleset_version=ruleset_version,
            rule_source_status="NOT_APPLICABLE" if not_applicable else "INCOMPLETE",
            source_validation_status=(
                "NOT_APPLICABLE"
                if not_applicable
                else "REVIEW_REQUIRED"
                if profile.status == "REVIEW"
                else "INCOMPLETE"
            ),
            activation_status="NOT_APPLICABLE" if not_applicable else "REVIEW_REQUIRED",
            activation_reasons=([] if not_applicable else issue_codes[:100]),
            criteria=[],
            facts=[],
            missing_reason=(
                "정량평가 비적용 문구가 현재 첨부 원문에서 확인되었습니다."
                if not_applicable
                else "현재 첨부의 정량 규칙 검증이 완료되지 않았습니다: "
                + ", ".join(issue_codes[:12])
            ),
        )

    activation_reasons = (
        list(row_partial.reasons) if row_partial is not None else
        sorted({item.code for item in profile.issues})
        if partial_review_criteria is not None or partial_source
        else _profile_activation_reasons(profile)
    )
    if activation_reasons and partial_review_criteria is None and not partial_source:
        return QuantitativeEstimateRequest(
            ruleset_version=ruleset_version,
            rule_source_status="AVAILABLE",
            source_validation_status="SOURCE_VALIDATED",
            activation_status="REVIEW_REQUIRED",
            activation_reasons=activation_reasons,
            criteria=[],
            facts=[],
            missing_reason=(
                "원문은 검증되었지만 자동 점수 계산 안전조건을 모두 통과하지 못했습니다: "
                + ", ".join(activation_reasons[:12])
            ),
        )

    logical_program = (
        None
        if partial_review_criteria is not None or partial_source
        else _logical_quantitative_program(profile)
    )
    logical_candidates = (
        row_partial.candidates if row_partial is not None else
        tuple(profile.available_candidates)
        if logical_program is None
        else logical_program.candidates
    )
    logical_tables = (row_partial.tables if row_partial is not None else
        tuple(profile.tables) if logical_program is None else logical_program.tables)
    logical_candidates = _fold_restated_table_candidates(profile, logical_candidates)

    bindings = {
        item.attachment_id: item.document_sha256
        for item in profile.document_bindings
    }
    criteria: list[QuantitativeCriterion] = []
    conversion_errors: list[str] = []
    for candidate in logical_candidates:
        criterion = _criterion_from_rule_candidate(candidate, bindings)
        if criterion is None:
            conversion_errors.append(
                f"{candidate.source_attachment_id}:{candidate.table_id}:{candidate.criterion_id}"
            )
            continue
        criteria.append(criterion)
    if conversion_errors or len(criteria) != len(logical_candidates):
        return QuantitativeEstimateRequest(
            ruleset_version=ruleset_version,
            rule_source_status="INCOMPLETE" if partial_source else "AVAILABLE",
            source_validation_status=(
                "INCOMPLETE" if partial_source else "SOURCE_VALIDATED"
            ),
            activation_status="REVIEW_REQUIRED",
            activation_reasons=["DETERMINISTIC_CONVERSION_FAILED"],
            criteria=[],
            facts=[],
            missing_reason=(
                "원문 규칙은 보존했지만 현재 결정론적 점수 엔진으로 안전하게 변환할 수 "
                "없는 산식이 있습니다: " + ", ".join(conversion_errors[:12])
            ),
        )
    minimums = {
        table.minimum_score
        for table in logical_tables
        if table.minimum_score is not None
    }
    if len(minimums) > 1:
        return QuantitativeEstimateRequest(
            ruleset_version=ruleset_version,
            rule_source_status="INCOMPLETE" if partial_source else "AVAILABLE",
            source_validation_status=(
                "INCOMPLETE" if partial_source else "SOURCE_VALIDATED"
            ),
            activation_status="REVIEW_REQUIRED",
            activation_reasons=["ALTERNATIVE_MINIMUM_SCORE_AMBIGUOUS"],
            criteria=[],
            facts=[],
            missing_reason="여러 정량평가표의 최저점 기준이 달라 합산 기준을 확정할 수 없습니다.",
        )
    return QuantitativeEstimateRequest(
        ruleset_version=ruleset_version,
        rule_source_status="INCOMPLETE" if partial_source else "AVAILABLE",
        source_validation_status=(
            "INCOMPLETE"
            if partial_source
            else "REVIEW_REQUIRED"
            if partial_review_criteria is not None
            else "SOURCE_VALIDATED"
        ),
        activation_status=(
            "PARTIAL_SOURCE"
            if partial_source
            else "PARTIAL_ACTIVE"
            if partial_review_criteria is not None
            else "AUTO_ACTIVE"
        ),
        activation_reasons=(
            activation_reasons if (partial_review_criteria or partial_source) else []
        ),
        # A minimum printed on one table cannot judge a subtotal drawn from an
        # unresolved manifest.
        minimum_score=None if partial_source else next(iter(minimums), None),
        criteria=criteria,
        review_criteria=list(partial_review_criteria or []),
        facts=list(facts or []),
        assumptions=[
            (
                "현재 PPS manifest의 일부 첨부가 미해소 상태입니다. 원문 검증이 끝난 "
                "첨부의 항목만 부분 소계로 보여주며, 이 합계는 공고 총점이 아닙니다."
                if partial_source
                else "현재 PPS manifest의 모든 첨부를 확인했고, 원문 검증이 끝난 항목만 부분 산정합니다."
                if partial_review_criteria is not None
                else "현재 PPS manifest의 모든 첨부에서 원문 규칙을 검증했습니다."
            ),
            "회사 증빙값이 없는 항목은 0점이나 만점으로 가정하지 않습니다.",
        ],
    )


def _current_authoritative_document_state(notice: Notice) -> tuple[set[str], str | None]:
    """Return current-manifest-bound accepted document digests.

    A corrected PPS manifest supersedes every older attachment/extraction. For
    curated notices without a PPS manifest, the newest reviewed public
    document reference is authoritative. Analysis/materialisation versions do
    not change this binding and therefore cannot resurrect a stale profile.
    """

    versions = sorted(notice.versions, key=lambda item: item.version_no, reverse=True)
    metadata = next(
        (
            version
            for version in versions
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == PPS_METADATA_KIND
        ),
        None,
    )
    if metadata is not None:
        if metadata.source_payload.get("schema_version") != PPS_METADATA_SCHEMA:
            return set(), "현재 PPS 첨부 manifest 스키마가 갱신되지 않아 정량 배점을 확정할 수 없습니다."
        raw_values = metadata.source_payload.get("attachment_manifest")
        if not isinstance(raw_values, list):
            return set(), "현재 PPS 첨부 manifest 형식이 올바르지 않아 정량 배점을 확정할 수 없습니다."
        raw_manifest_values = list(raw_values)
        manifest = [
            dict(item)
            for item in raw_manifest_values
            if isinstance(item, dict)
        ]
        attachments, invalid_count, attempts = _current_manifest_attempts(versions)
        expected_ids = {attachment["attachment_id"] for attachment in attachments}
        if (
            not attachments
            or invalid_count
            or len(attachments) != len(raw_manifest_values)
            or set(attempts) != expected_ids
        ):
            return set(), "현재 공고의 모든 공개 첨부에 대한 분석 감사가 완료되지 않았습니다."
        if any(
            not isinstance(version.source_payload, dict)
            or version.source_payload.get("status") != "ACCEPTED"
            or version.extraction_status not in {"ACCEPTED", "COMPLETE"}
            or not version.document_complete
            for version in attempts.values()
        ):
            return set(), "현재 공고 첨부 중 읽지 못했거나 검토가 필요한 파일이 있어 정량 배점을 확정할 수 없습니다."
        return {
            str(version.file_sha256).casefold()
            for version in attempts.values()
        }, None

    reference = next(
        (
            version
            for version in versions
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == "PUBLIC_DOCUMENT_REFERENCE"
        ),
        None,
    )
    digest = reference.file_sha256 if reference is not None else None
    return ({str(digest).casefold()}, None) if isinstance(digest, str) else (set(), None)


def _profile_for_notice(notice: Notice) -> tuple[dict[str, Any] | None, str | None]:
    """Return an identity match only when its cited document is in the audit.

    Notice number/title matching alone is not a sufficient scoring basis: a
    corrected notice can retain both while replacing its RFP.  The packaged
    profile therefore becomes applicable only after the exact cited document
    digest is present in a persisted NoticeVersion.
    """

    catalog = _load_quantitative_profile_catalog()
    for item in catalog["profiles"]:
        if not _profile_identity_matches(notice, item):
            continue
        # A MISSING registry entry carries no scoring rules; it only records
        # that the reviewed public source did not contain a quantitative
        # table. It must remain distinguishable from a stale AVAILABLE table.
        if item.get("rule_source_status") != "AVAILABLE":
            return copy.deepcopy(item), None
        expected_digest = _profile_source_digest(item)
        if expected_digest is None:
            return None, "연결된 정량 프로필에 검증 가능한 원문 문서 해시가 없습니다."
        current_digests, coverage_error = _current_authoritative_document_state(notice)
        if coverage_error:
            return None, coverage_error
        if expected_digest not in current_digests:
            return None, (
                "정량 프로필의 원문 문서 해시가 현재 권위 공고 문서와 일치하지 않습니다. "
                "정정공고 또는 첨부 변경 여부를 확인해야 합니다."
            )
        return copy.deepcopy(item), None
    return None, None


def _keyword_in_text(keyword: str, haystack: str) -> bool:
    folded = keyword.casefold().strip()
    if not folded:
        return False
    if folded == "ai":
        return bool(re.search(r"(?<![a-z0-9])ai(?![a-z0-9])", haystack))
    return folded in haystack


def _public_performance_candidates(
    config: dict[str, Any],
) -> tuple[dict[str, Any], int, Decimal]:
    seed = load_public_performance_seed()
    try:
        start = date.fromisoformat(str(config["date_from"]))
        end = date.fromisoformat(str(config["date_to"]))
        minimum_amount = Decimal(str(config["minimum_single_contract_amount_krw"]))
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise QuantitativeProfileError("public performance candidate configuration is invalid") from exc
    if start > end or minimum_amount < 0:
        raise QuantitativeProfileError("public performance candidate range is invalid")

    all_keywords = [str(item) for item in config.get("all_keywords", []) if str(item).strip()]
    any_keywords = [str(item) for item in config.get("any_keywords", []) if str(item).strip()]
    if not all_keywords or not any_keywords:
        raise QuantitativeProfileError("public performance candidate keywords are incomplete")

    count = 0
    total = Decimal(0)
    for record in seed["records"]:
        try:
            contract_date = date.fromisoformat(str(record.get("contract_date") or ""))
            amount = Decimal(str(record.get("contract_amount_krw")))
        except (ValueError, InvalidOperation):
            continue
        if not start <= contract_date <= end or amount < minimum_amount:
            continue
        haystack = " ".join(
            [
                str(record.get("project_name") or ""),
                str(record.get("overview") or ""),
                " ".join(record.get("keywords") or []),
            ]
        ).casefold()
        if not all(_keyword_in_text(item, haystack) for item in all_keywords):
            continue
        if not any(_keyword_in_text(item, haystack) for item in any_keywords):
            continue
        count += 1
        total += amount
    return seed, count, total


def _performance_evidence_key(seed: dict[str, Any]) -> str:
    return (
        f"PUBLIC-PERFORMANCE:{seed['dataset_version']}:"
        f"{seed['provenance']['records_sha256']}"
    )


def _public_performance_fact(profile: dict[str, Any]) -> QuantitativeFact | None:
    config = profile.get("public_performance_fact")
    if not isinstance(config, dict):
        return None
    seed, count, total = _public_performance_candidates(config)
    total_krw = int(total.to_integral_value(rounding=ROUND_CEILING))
    return QuantitativeFact(
        metric_key=str(config["metric_key"]),
        status="ESTIMATED",
        lower_value=0,
        upper_value=total_krw,
        evidence_key=_performance_evidence_key(seed),
        confidence=0.35,
        rationale=(
            f"공개·비식별 실적에서 원문 후보조건을 기계 적용한 {count}건, "
            f"후보금액 합계 {total_krw:,}원입니다. 인정실적 여부·완료·유사성·VAT 포함 여부와 "
            "증빙 원본은 확정되지 않아 0원부터 후보합계까지만 잠정 범위로 사용합니다."
        ),
    )


def _public_evidence_observations(profile: dict[str, Any] | None) -> list[EvidenceObservation]:
    if not profile:
        return []
    observations: list[EvidenceObservation] = []
    fact_config = profile.get("public_performance_fact")
    performance_config = profile.get("public_performance_observation")
    if isinstance(fact_config, dict):
        seed, count, total = _public_performance_candidates(fact_config)
        evidence_key = _performance_evidence_key(seed)
        observations.extend(
            [
                EvidenceObservation(
                    observation_key="PUBLIC-PERFORMANCE-CANDIDATES",
                    label="공개 유사실적 후보",
                    value=count,
                    unit="건",
                    status="CANDIDATE_ONLY",
                    evidence_key=evidence_key,
                    rationale=str(
                        fact_config.get("note")
                        or "원문 후보조건을 기계 적용한 결과이며 인정실적 건수가 아닙니다."
                    ),
                ),
                EvidenceObservation(
                    observation_key="PUBLIC-PERFORMANCE-CANDIDATE-AMOUNT",
                    label="공개 유사실적 후보금액",
                    value=int(total.to_integral_value(rounding=ROUND_CEILING)),
                    unit="원",
                    status="CANDIDATE_ONLY",
                    evidence_key=evidence_key,
                    rationale=(
                        "후보금액 합계는 점수 산정의 상한 후보일 뿐이며, 발주기관 인정금액이나 "
                        "확정 실적금액이 아닙니다."
                    ),
                ),
            ]
        )
    elif isinstance(performance_config, dict):
        keywords = [str(item).casefold() for item in performance_config.get("keywords", []) if str(item).strip()]
        mode = str(performance_config.get("match_mode") or "ALL").upper()
        seed = load_public_performance_seed()
        count = 0
        for record in seed["records"]:
            haystack = " ".join(
                [
                    str(record.get("project_name") or ""),
                    str(record.get("overview") or ""),
                    " ".join(record.get("keywords") or []),
                ]
            ).casefold()
            matched = all(item in haystack for item in keywords) if mode == "ALL" else any(item in haystack for item in keywords)
            count += int(bool(keywords) and matched)
        observations.append(
            EvidenceObservation(
                observation_key="PUBLIC-PERFORMANCE-CANDIDATES",
                label="공개 유사실적 후보",
                value=count,
                unit="건",
                status="CANDIDATE_ONLY",
                evidence_key=f"PUBLIC-PERFORMANCE:{seed['dataset_version']}:{seed['provenance']['records_sha256']}",
                rationale=str(performance_config.get("note") or "공개 실적 후보 집계이며 점수로 적용하지 않습니다."),
            )
        )

    company = load_public_company_profile()
    observations.append(
        EvidenceObservation(
            observation_key="PUBLIC-COMPANY-PROFILE",
            label="공개 회사 자격 프로필",
            value=len(company.get("facts", {})),
            unit="개 사실",
            status="NOT_APPLIED",
            evidence_key=f"PUBLIC-COMPANY-PROFILE:{company.get('profile_version', 'unknown')}",
            rationale="공개 회사 프로필은 자격 판정 근거이며, 공고별 정량 배점 산식이 없으므로 정량점수에 적용하지 않았습니다.",
        )
    )
    return observations


def bind_quantitative_company_inputs(
    request: QuantitativeEstimateRequest,
    company_facts: Iterable[CompanyFact] = (),
    performance_records: Iterable[CompanyPerformanceRecord] = (),
    *,
    as_of: datetime,
    bid_notice_at: datetime | None = None,
    notice: Notice | None = None,
) -> QuantitativeEstimateRequest:
    """Resolve company evidence for an already selected, validated rule request.

    This is not source authorization: source validation, coverage and authority
    selection remain the caller's responsibility. Inactive requests are returned
    unchanged without reading company inputs. Active requests replace any supplied
    scoring facts with resolver outputs; neither source nor company inputs mutate.
    """

    # PARTIAL_SOURCE rows carry the same per-criterion source binding as an
    # active request, so verified company evidence resolves against them too.
    # Only the notice-level total stays unresolved, never an individual row.
    if request.activation_status not in {
        "AUTO_ACTIVE", "PARTIAL_ACTIVE", "PARTIAL_SOURCE",
    }:
        return request

    # Every company resolver must see the same facts, including when the caller
    # supplies a one-shot iterator rather than a list.
    stored_facts = tuple(company_facts)
    stored_records = tuple(performance_records)
    verified_facts = resolve_verified_quantitative_facts(
        request.criteria, stored_facts, as_of=as_of,
    )
    register_facts = resolve_performance_register_facts(
        request.criteria, stored_records, as_of=as_of,
        bid_notice_at=bid_notice_at,
    )
    register_facts.extend(resolve_financial_register_facts(
        request.criteria, stored_facts, as_of=as_of, bid_notice_at=bid_notice_at,
    ))
    register_facts.extend(resolve_personnel_register_facts(
        request.criteria, stored_facts, as_of=as_of, bid_notice_at=bid_notice_at,
    ))
    if notice is not None:
        from .private_company_evidence import resolve_credit_scenario_facts
        register_facts.extend(resolve_credit_scenario_facts(notice, request.criteria, stored_facts))
    register_identities = {
        (item.metric_key, item.fact_binding_sha256) for item in register_facts
    }
    register_metrics = {item.metric_key for item in register_facts}
    # An exact immutable CompanyFact remains authoritative. A generic fact that
    # failed the dynamic binding contract must not suppress the notice-scoped
    # value derived from a validated register.
    merged_facts = [
        item
        for item in verified_facts
        if item.status == "CONFIRMED"
        or (
            (item.metric_key, item.fact_binding_sha256) not in register_identities
            and (item.fact_binding_sha256 is not None or item.metric_key not in register_metrics)
        )
    ]
    confirmed_identities = {
        (item.metric_key, item.fact_binding_sha256)
        for item in verified_facts
        if item.status == "CONFIRMED"
    }
    merged_facts.extend(
        item
        for item in register_facts
        if (item.metric_key, item.fact_binding_sha256) not in confirmed_identities
    )
    return request.model_copy(update={"facts": merged_facts})


def estimate_for_notice(
    notice: Notice,
    company_facts: Iterable[CompanyFact] = (),
    performance_records: Iterable[CompanyPerformanceRecord] = (),
) -> QuantitativeEstimateResult:
    dynamic_profile = _current_dynamic_quantitative_profile(notice)
    if dynamic_profile is not None:
        stored_facts = tuple(company_facts)
        stored_records = tuple(performance_records)
        request = quantitative_request_from_candidate_profile(
            dynamic_profile, allow_partial_source=True,
        )
        request, approved_bindings = _apply_row_approvals(
            request, notice=notice, profile=dynamic_profile, company_facts=stored_facts,
        )
        request, sufficient_plan = _apply_sufficient_rows(
            request, notice=notice, profile=dynamic_profile, company_facts=stored_facts,
        )
        request = bind_quantitative_company_inputs(
            request, stored_facts, stored_records,
            as_of=notice.deadline,
            bid_notice_at=getattr(notice, "published_at", None),
            notice=notice,
        )
        request = _cap_approved_row_facts(request, approved_bindings)
        request, sufficient_bindings = _bind_sufficient_row_facts(
            request, sufficient_plan, notice=notice, performance_records=stored_records,
        )
        request = _cap_sufficient_row_facts(request, sufficient_bindings)
        beta_request = _apply_company_first_beta(
            request, notice=notice, profile=dynamic_profile,
            company_facts=stored_facts, performance_records=stored_records,
        )
        return estimate_quantitative_score(beta_request or request)

    profile, profile_binding_error = _profile_for_notice(notice)
    if profile is None:
        request = QuantitativeEstimateRequest(
            ruleset_version="unmapped-notice-review-v1",
            rule_source_status=("INCOMPLETE" if profile_binding_error else "MISSING"),
            source_validation_status=(
                "INCOMPLETE" if profile_binding_error else "MISSING"
            ),
            activation_status="REVIEW_REQUIRED",
            activation_reasons=[
                "SOURCE_BINDING_INCOMPLETE"
                if profile_binding_error
                else "QUANTITATIVE_PROFILE_MISSING"
            ],
            criteria=[],
            facts=[],
            missing_reason=(
                profile_binding_error
                or "이 공고에 연결된 정량평가표 프로필이 없습니다."
            ),
        )
        return estimate_quantitative_score(request)
    source_status = str(profile.get("rule_source_status") or "INCOMPLETE").upper()
    if source_status not in {"AVAILABLE", "MISSING", "INCOMPLETE"}:
        source_status = "INCOMPLETE"
    facts = [fact] if (fact := _public_performance_fact(profile)) else []
    request = QuantitativeEstimateRequest(
        ruleset_version=profile["ruleset_version"],
        rule_source_status=source_status,
        source_validation_status=(
            "SOURCE_VALIDATED"
            if source_status == "AVAILABLE"
            else "MISSING"
            if source_status == "MISSING"
            else "INCOMPLETE"
        ),
        activation_status=(
            "AUTO_ACTIVE" if source_status == "AVAILABLE" else "REVIEW_REQUIRED"
        ),
        activation_reasons=(
            [] if source_status == "AVAILABLE" else ["CURATED_PROFILE_NOT_AVAILABLE"]
        ),
        minimum_score=profile.get("minimum_score"),
        criteria=profile.get("criteria", []),
        facts=facts,
        assumptions=profile.get("assumptions", []),
        missing_reason=profile.get("missing_reason"),
        source_anchor=profile.get("source_reference"),
    )
    return estimate_quantitative_score(
        request,
        evidence_observations=_public_evidence_observations(profile),
    )


def _get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(_get_session)]

quantitative_scoring_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_key)],
    tags=["quantitative scoring"],
)


def _public_criterion_display_code(category: str) -> PublicCriterionDisplayCode:
    return _PUBLIC_CRITERION_DISPLAY_CODE_BY_CATEGORY.get(category, "OTHER")


def _public_number_matches(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    canonical_left = _canonical_public_points(left)
    return canonical_left is not None and left == canonical_left == right


def _public_criteria_match_aggregate(
    items: Sequence[PublicQuantitativeCriterionSnapshot],
    *,
    total_max_points: float | None,
    confirmed_points: float | None,
    estimated_points: float | None,
    lower_points: float | None,
    upper_points: float | None,
    evidence_coverage_pct: float,
    overall_status: EstimateStatus,
    out_of_scope_points: float | None = None,
    partial_source: bool = False,
) -> bool:
    scored = [item for item in items if item.status != "OUT_OF_SCOPE"]
    excluded_total = _sum_public_points(
        item.max_points for item in items if item.status == "OUT_OF_SCOPE"
    )
    if excluded_total is None or (
        out_of_scope_points is not None
        and not _public_number_matches(out_of_scope_points, excluded_total)
    ):
        return False
    if not items:
        return (
            total_max_points is None
            and confirmed_points is None
            and estimated_points is None
            and lower_points is None
            and upper_points is None
            and _public_number_matches(evidence_coverage_pct, 0)
            and overall_status == "REVIEW"
        )
    if (
        total_max_points is None
        or confirmed_points is None
        or lower_points is None
        or upper_points is None
    ):
        return False

    expected_total = _sum_public_points(item.max_points for item in scored)
    expected_confirmed = _sum_public_points(
        item.lower_points for item in scored if item.status == "CONFIRMED"
    )
    expected_lower = _sum_public_points(item.lower_points for item in scored)
    expected_upper = _sum_public_points(item.upper_points for item in scored)
    confirmed_weight = _sum_public_points(
        item.max_points for item in scored if item.status == "CONFIRMED"
    )
    if (
        expected_total is None
        or expected_confirmed is None
        or expected_lower is None
        or expected_upper is None
        or confirmed_weight is None
    ):
        return False
    expected_coverage = _canonical_public_points(
        (confirmed_weight / expected_total) * 100 if expected_total else 0
    )
    if expected_coverage is None:
        return False
    statuses = {item.status for item in scored}
    if partial_source:
        # Row scores cannot resolve the unread remainder of the notice.
        expected_status: EstimateStatus = "REVIEW"
    elif not scored:
        expected_status = "UNSCORABLE"
    elif "REVIEW" in statuses:
        expected_status = "REVIEW"
    elif "UNSCORABLE" in statuses:
        expected_status = "UNSCORABLE"
    elif "ESTIMATED" in statuses:
        expected_status = "ESTIMATED"
    else:
        expected_status = "CONFIRMED"
    expected_estimated = (
        expected_lower
        if expected_lower == expected_upper
        and expected_status in {"CONFIRMED", "ESTIMATED"}
        else None
    )
    return (
        _public_number_matches(total_max_points, expected_total)
        and _public_number_matches(confirmed_points, expected_confirmed)
        and _public_number_matches(lower_points, expected_lower)
        and _public_number_matches(upper_points, expected_upper)
        and _public_number_matches(estimated_points, expected_estimated)
        and _public_number_matches(evidence_coverage_pct, expected_coverage)
        and overall_status == expected_status
    )


def build_public_quantitative_criteria_snapshot(
    result: QuantitativeEstimateResult,
) -> dict[str, Any] | None:
    """Build a versioned public-only row snapshot or omit it fail closed."""

    partial_source = result.activation_status == "PARTIAL_SOURCE"
    if partial_source and (
        result.rule_source_status != "INCOMPLETE"
        or result.source_validation_status != "INCOMPLETE"
        or not result.activation_reasons
        or not result.criteria
        or result.overall_status != "REVIEW"
        or result.estimated_points is not None
        or result.minimum_score is not None
        or result.meets_minimum is not None
    ):
        return None
    try:
        snapshot = PublicQuantitativeCriteriaSnapshot(
            schema_version=PUBLIC_QUANTITATIVE_CRITERIA_SCHEMA_VERSION,
            items=[
                PublicQuantitativeCriterionSnapshot(
                    display_code=_public_criterion_display_code(
                        criterion.category
                    ),
                    max_points=criterion.max_points,
                    estimated_points=criterion.estimated_points,
                    lower_points=criterion.lower_points,
                    upper_points=criterion.upper_points,
                    status=criterion.status,
                )
                for criterion in result.criteria
            ],
        )
    except ValidationError:
        return None
    if not _public_criteria_match_aggregate(
        snapshot.items,
        total_max_points=result.total_max_points,
        confirmed_points=result.confirmed_points,
        estimated_points=result.estimated_points,
        lower_points=result.lower_points,
        upper_points=result.upper_points,
        evidence_coverage_pct=result.evidence_coverage_pct,
        overall_status=result.overall_status,
        out_of_scope_points=result.out_of_scope_points,
        partial_source=partial_source,
    ):
        return None
    return snapshot.model_dump(mode="json")


def _restore_public_quantitative_criteria_snapshot(
    value: object,
    *,
    total_max_points: float | None,
    confirmed_points: float | None,
    estimated_points: float | None,
    lower_points: float | None,
    upper_points: float | None,
    evidence_coverage_pct: float,
    overall_status: EstimateStatus,
    out_of_scope_points: float | None = None,
    partial_source: bool = False,
) -> list[CriterionEstimate] | None:
    try:
        snapshot = PublicQuantitativeCriteriaSnapshot.model_validate(value)
    except ValidationError:
        return None
    if not _public_criteria_match_aggregate(
        snapshot.items,
        total_max_points=total_max_points,
        confirmed_points=confirmed_points,
        estimated_points=estimated_points,
        lower_points=lower_points,
        upper_points=upper_points,
        evidence_coverage_pct=evidence_coverage_pct,
        overall_status=overall_status,
        out_of_scope_points=out_of_scope_points,
        partial_source=partial_source,
    ):
        return None

    rationale_by_status = {
        "CONFIRMED": "저장된 최신 분석에서 확정된 항목 점수입니다.",
        "ESTIMATED": "저장된 최신 분석의 잠정 점수 또는 범위입니다.",
        "UNSCORABLE": "현재 공개 요약만으로 산정할 수 없는 항목입니다.",
        "REVIEW": "저장된 최신 분석에서 자동 산정을 보류한 항목입니다.",
        "OUT_OF_SCOPE": "정량 합계에서 제외하고 별도로 보존한 평가 항목입니다. 이 항목의 점수는 산정하지 않았습니다.",
    }
    criteria: list[CriterionEstimate] = []
    for index, item in enumerate(snapshot.items, start=1):
        label = _PUBLIC_CRITERION_LABEL_BY_DISPLAY_CODE[item.display_code]
        if item.status == "OUT_OF_SCOPE":
            label = f"별도 평가 항목 {index}"
        elif item.display_code == "OTHER":
            label = f"{label} {index}"
        criteria.append(
            CriterionEstimate(
                criterion_id=f"PUBLIC-CRITERION-{index:03d}",
                category=("PUBLIC_OUT_OF_SCOPE" if item.status == "OUT_OF_SCOPE" else "PUBLIC_QUANTITATIVE"),
                label=label,
                max_points=item.max_points,
                formula="공개 화면에서는 세부 원문 산식을 제외합니다.",
                rule_floor_points=0,
                floor_condition=None,
                rule_base_points=None,
                base_condition=None,
                source_anchor=None,
                evidence_key=None,
                evidence_reference=None,
                evidence_sha256=None,
                fact_binding_sha256=None,
                estimated_points=item.estimated_points,
                lower_points=item.lower_points,
                upper_points=item.upper_points,
                confidence=0,
                status=item.status,
                rationale=(
                    "현재 명부 기반 추정이며, 미래 기준일은 현재 명부 유지 가정과 기준일 재확인이 필요합니다. "
                    "투입인력 평가의 경우 투입 가능 가정이며 실제 배정을 뜻하지 않습니다."
                    if item.display_code == "PERSONNEL_COUNT" and item.status == "ESTIMATED"
                    else "현재 신용등급과 단독입찰을 가정한 추정치이며, 공고 조건과 제출 시점의 증빙 재확인이 필요합니다."
                    if item.display_code == "CREDIT_RATING" and item.status == "ESTIMATED"
                    else rationale_by_status[item.status]
                ),
                assumptions=[],
            )
        )
    return criteria


def _public_quantitative_projection(
    result: QuantitativeEstimateResult,
) -> QuantitativeEstimateResult:
    """Remove company/evidence bindings from the anonymous read-only view."""

    snapshot = build_public_quantitative_criteria_snapshot(result)
    criteria = (
        _restore_public_quantitative_criteria_snapshot(
            snapshot,
            total_max_points=result.total_max_points,
            confirmed_points=result.confirmed_points,
            estimated_points=result.estimated_points,
            lower_points=result.lower_points,
            upper_points=result.upper_points,
            evidence_coverage_pct=result.evidence_coverage_pct,
            overall_status=result.overall_status,
            out_of_scope_points=result.out_of_scope_points,
            partial_source=result.activation_status == "PARTIAL_SOURCE",
        )
        if snapshot is not None
        else None
    )
    return result.model_copy(
        update={
            "ruleset_version": "public-quantitative-summary-v1",
            "source_anchor": None,
            "activation_reasons": (
                []
                if result.activation_status == "AUTO_ACTIVE"
                else ["PUBLIC_ANALYSIS_REVIEW_REQUIRED"]
            ),
            "criteria": criteria or [],
            "assumptions": [
                "공개 화면에서는 회사 사실값과 원문·내부 증빙 식별자를 제외합니다."
            ] + ([_partial_subtotal_statement(result.activation_reasons)]
                 if result.activation_status == "PARTIAL_SOURCE" else []),
            "evidence_observations": [],
        }
    )


def _stored_public_quantitative_projection(
    session: Session,
    notice: Notice,
) -> QuantitativeEstimateResult | None:
    """Return the latest current aggregate snapshot without private bindings.

    The latest current analysis run is selected first. We never search back
    through older successful runs when that run lacks a quantitative snapshot,
    and a newer PPS metadata version invalidates every older basis.
    """

    run_query = select(AnalysisRun).where(AnalysisRun.notice_id == notice.id)
    pps_metadata = [
        version
        for version in notice.versions
        if isinstance(version.source_payload, dict)
        and version.source_payload.get("kind") == PPS_METADATA_KIND
    ]
    if pps_metadata:
        current_version_no = max(item.version_no for item in pps_metadata)
        current_basis_ids = [
            item.id
            for item in notice.versions
            if item.version_no >= current_version_no
        ]
        if not current_basis_ids:
            return None
        run_query = run_query.where(
            AnalysisRun.notice_version_id.in_(current_basis_ids)
        )
    analysis_run = session.scalar(
        run_query.order_by(
            AnalysisRun.generated_at.desc(),
            AnalysisRun.created_at.desc(),
            AnalysisRun.id.desc(),
        ).limit(1)
    )
    if analysis_run is None:
        return None
    scores = list(
        session.scalars(
            select(ScoreSnapshot).where(
                ScoreSnapshot.analysis_run_id == analysis_run.id,
                ScoreSnapshot.score_key == "quantitative.total",
            )
        ).all()
    )
    if len(scores) != 1:
        return None
    return public_quantitative_snapshot_projection(analysis_run, scores[0])


def public_quantitative_snapshot_projection(
    analysis_run: AnalysisRun,
    score: ScoreSnapshot,
) -> QuantitativeEstimateResult | None:
    """Validate one aggregate using the same contract as the public detail.

    Callers must first select the latest current run; this function performs
    no database reads and never searches older snapshots for a success.
    """
    if score.score_key != "quantitative.total" or score.analysis_run_id != analysis_run.id:
        return None
    basis_versions = analysis_run.basis_versions
    basis = score.basis_json
    if (
        score.score_type != "QUANTITATIVE_ESTIMATE"
        or score.unit != "POINTS"
        or score.method_version != QUANTITATIVE_ENGINE_VERSION
        or not isinstance(basis_versions, dict)
        or basis_versions.get("quantitative_engine")
        != QUANTITATIVE_ENGINE_VERSION
        or not isinstance(basis, dict)
        or basis.get("input_sha256") != analysis_run.input_sha256
        or re.fullmatch(r"[a-f0-9]{64}", analysis_run.input_sha256 or "") is None
        or re.fullmatch(
            r"[a-f0-9]{64}", str(basis.get("profile_output_sha256") or "")
        )
        is None
    ):
        return None

    def public_number(
        value: object,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("snapshot aggregate must be numeric")
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("snapshot aggregate must be finite") from exc
        if not math.isfinite(number):
            raise ValueError("snapshot aggregate must be finite")
        if minimum is not None and number < minimum:
            raise ValueError("snapshot aggregate is below its public bound")
        if maximum is not None and number > maximum:
            raise ValueError("snapshot aggregate is above its public bound")
        return number

    try:
        estimated = public_number(score.value, minimum=0)
        lower = public_number(score.lower_value, minimum=0)
        upper = public_number(score.upper_value, minimum=0)
        total_max = public_number(basis.get("total_max_points"), minimum=0)
        confirmed = public_number(basis.get("confirmed_points"), minimum=0)
        coverage = public_number(
            basis.get("evidence_coverage_pct"), minimum=0, maximum=100
        )
        confidence = public_number(score.confidence, minimum=0, maximum=1)
        out_of_scope = public_number(basis.get("out_of_scope_points"), minimum=0)
    except ValueError:
        return None

    if coverage is None or confidence is None:
        return None
    if "out_of_scope_points" in basis and out_of_scope is None:
        return None
    if any(
        number is not None and _canonical_public_points(number) != number
        for number in (
            estimated,
            lower,
            upper,
            total_max,
            confirmed,
            coverage,
            confidence,
            out_of_scope,
        )
    ):
        return None
    if (lower is None) != (upper is None):
        return None
    if lower is not None and upper is not None:
        if total_max is None or lower > upper or upper > total_max:
            return None
    elif estimated is not None:
        return None
    if estimated is not None and (
        lower is None
        or upper is None
        or estimated != lower
        or estimated != upper
        or score.status not in {"CONFIRMED", "ESTIMATED"}
    ):
        return None
    if estimated is None and lower is not None and lower == upper and score.status in {
        "CONFIRMED",
        "ESTIMATED",
    }:
        return None
    if confirmed is not None and (
        total_max is None
        or confirmed > total_max
        or (lower is not None and confirmed > lower)
    ):
        return None
    if lower is None:
        if (
            score.status != "REVIEW"
            or total_max is not None
            or confirmed is not None
            or estimated is not None
            or coverage != 0
        ):
            return None
    elif total_max is None or confirmed is None:
        return None
    if score.status == "CONFIRMED" and (
        estimated is None
        or lower != upper
        or estimated != lower
        or confirmed != lower
        or coverage != 100
    ):
        return None
    if score.status == "ESTIMATED" and lower is None:
        return None

    rule_source_status = basis.get("rule_source_status")
    source_validation_status = basis.get("source_validation_status")
    activation_status = basis.get("activation_status")
    if rule_source_status not in {
        "AVAILABLE",
        "MISSING",
        "INCOMPLETE",
        "NOT_APPLICABLE",
    }:
        return None
    if source_validation_status not in {
        "SOURCE_VALIDATED",
        "REVIEW_REQUIRED",
        "INCOMPLETE",
        "MISSING",
        "NOT_APPLICABLE",
    }:
        return None
    if activation_status not in {
        "AUTO_ACTIVE",
        "PARTIAL_ACTIVE",
        "PARTIAL_SOURCE",
        "REVIEW_REQUIRED",
        "NOT_APPLICABLE",
    }:
        return None
    if activation_status == "AUTO_ACTIVE" and (
        rule_source_status != "AVAILABLE"
        or source_validation_status != "SOURCE_VALIDATED"
    ):
        return None
    if activation_status == "PARTIAL_ACTIVE" and (
        rule_source_status != "AVAILABLE"
        or source_validation_status != "REVIEW_REQUIRED"
    ):
        return None
    partial_source = activation_status == "PARTIAL_SOURCE"
    if partial_source and (
        rule_source_status != "INCOMPLETE"
        or source_validation_status != "INCOMPLETE"
        or score.status != "REVIEW"
        or estimated is not None
        or not isinstance(basis.get("activation_reasons"), list)
        or not basis["activation_reasons"]
        or any(not isinstance(reason, str) or not reason.strip()
               for reason in basis["activation_reasons"])
        or "public_criteria" not in basis
    ):
        return None
    if lower is None and (
        activation_status not in {"REVIEW_REQUIRED", "NOT_APPLICABLE"}
        or confidence != 0
    ):
        return None
    if lower is not None and activation_status not in {
        "AUTO_ACTIVE",
        "PARTIAL_ACTIVE",
        "PARTIAL_SOURCE",
    }:
        return None
    if score.status not in {"CONFIRMED", "ESTIMATED", "UNSCORABLE", "REVIEW"}:
        return None
    if score.band not in {"GREEN", "YELLOW", "RED", "GRAY"}:
        return None

    readiness = (
        _round_points((lower / total_max) * 100)
        if lower is not None and total_max not in {None, 0}
        else None
    )
    expected_band: ReadinessBand
    if readiness is None:
        expected_band = "GRAY"
    elif readiness < 70 or coverage < 60:
        expected_band = "RED"
    elif readiness < 80 or coverage < 80:
        expected_band = "YELLOW"
    else:
        expected_band = "GREEN"
    if score.band != expected_band:
        return None

    public_criteria: list[CriterionEstimate] = []
    if "public_criteria" in basis:
        restored_criteria = _restore_public_quantitative_criteria_snapshot(
            basis.get("public_criteria"),
            total_max_points=total_max,
            confirmed_points=confirmed,
            estimated_points=estimated,
            lower_points=lower,
            upper_points=upper,
            evidence_coverage_pct=coverage,
            overall_status=score.status,
            out_of_scope_points=out_of_scope,
            partial_source=partial_source,
        )
        if restored_criteria is None or (partial_source and not restored_criteria):
            return None
        public_criteria = restored_criteria
        # The bounded public rows preserve excluded weights even for a stored
        # snapshot without the additive aggregate field. Never infer these
        # weights from private text or count them as a quantitative score.
        out_of_scope = _sum_public_points(
            item.max_points for item in public_criteria if item.status == "OUT_OF_SCOPE"
        )
    elif out_of_scope not in {None, 0}:
        return None
    if total_max == 0 and (
        not public_criteria
        or not out_of_scope
        or confidence != 0
    ):
        return None

    activation_reasons = (
        []
        if activation_status == "AUTO_ACTIVE"
        else ["PUBLIC_ANALYSIS_REVIEW_REQUIRED"]
    )
    if partial_source:
        opinion = (
            _partial_subtotal_statement(basis["activation_reasons"])
            + " 미해소 첨부의 배점과 최소점수 충족 여부는 판단하지 않았습니다."
        )
    elif total_max == 0:
        opinion = "저장된 최신 분석에 정량 산정 대상이 없어 점수를 확정하지 않았습니다. 별도 평가 항목을 확인하세요."
    elif estimated is not None:
        opinion = "저장된 최신 분석에서 확정 가능한 정량 합계를 계산했습니다."
    elif lower is not None:
        opinion = (
            "저장된 최신 분석의 정량 점수는 범위로 계산되었습니다. "
            "미확정 항목을 보완한 뒤 다시 분석하세요."
        )
    else:
        opinion = "저장된 최신 분석에서 공개 가능한 정량 점수를 확정하지 못했습니다."

    try:
        return QuantitativeEstimateResult(
            engine_version=QUANTITATIVE_ENGINE_VERSION,
            ruleset_version="public-quantitative-summary-v1",
            source_anchor=None,
            rule_source_status=rule_source_status,
            source_validation_status=source_validation_status,
            activation_status=activation_status,
            activation_reasons=activation_reasons,
            overall_status=score.status,
            total_max_points=total_max,
            confirmed_points=confirmed,
            estimated_points=estimated,
            lower_points=lower,
            upper_points=upper,
            unscorable_points=None,
            out_of_scope_points=out_of_scope or 0,
            evidence_coverage_pct=coverage,
            readiness_pct=readiness,
            readiness_band=score.band,
            minimum_score=None,
            meets_minimum=None,
            confidence=confidence,
            criteria=public_criteria,
            assumptions=[
                "저장된 최신 분석 스냅샷에서 공개 가능한 배점·범위·상태만 표시합니다."
            ] + ([_partial_subtotal_statement(basis["activation_reasons"])]
                 if partial_source else []),
            evidence_observations=[],
            opinion=opinion,
            separation_notice=(
                "정량 준비도는 참가자격과 GO/NO-GO 판단을 변경하지 않는 별도 "
                "보조지표입니다. 최종 점수는 발주기관 평가 결과로만 확정됩니다."
            ),
        )
    except ValidationError:
        return None


@quantitative_scoring_router.get(
    "/notices/{notice_key}/quantitative-estimate",
    response_model=QuantitativeEstimateResult,
)
def get_notice_quantitative_estimate(
    notice_key: str,
    request: Request,
    response: Response,
    session: DbSession,
) -> QuantitativeEstimateResult:
    response.headers["Cache-Control"] = "no-store"
    notice = session.scalar(
        select(Notice)
        .options(selectinload(Notice.versions))
        .where(Notice.notice_key == notice_key)
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    public_view = public_read_allowed(request)
    if public_view:
        stored_public_result = _stored_public_quantitative_projection(
            session, notice
        )
        if stored_public_result is not None:
            return stored_public_result
    company_facts = (
        []
        if public_view
        else list(
            session.scalars(
                select(CompanyFact).options(selectinload(CompanyFact.evidence))
            ).all()
        )
    )
    performance_records = (
        []
        if public_view
        else list(
            session.scalars(
                select(CompanyPerformanceRecord).where(
                    CompanyPerformanceRecord.record_status != "ARCHIVED"
                )
            ).all()
        )
    )
    result = (
        estimate_for_notice(notice, company_facts, performance_records)
        if performance_records
        else estimate_for_notice(notice, company_facts)
    )
    return _public_quantitative_projection(result) if public_view else result
