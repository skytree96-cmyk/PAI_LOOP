from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from functools import lru_cache
from importlib import resources
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .auth import public_read_allowed, require_api_key
from .evaluator import evidence_state, fact_is_effective
from .eligibility_policy import load_public_company_profile
from .integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
)
from .models import CompanyFact, Notice
from .pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_METADATA_KIND,
    PPS_METADATA_SCHEMA,
    PPS_PROCESSING_VERSION,
    _current_manifest_attempts,
    _validated_manifest_attachments,
)
from .quantitative_rule_extraction import (
    ImmutableQuantitativeRuleCandidate,
    QuantitativeCandidateProfile,
    ValidatedQuantitativeAttachmentRecord,
    merge_validated_quantitative_records,
)
from .public_performance import load_public_performance_seed


QUANTITATIVE_ENGINE_VERSION = "pai-loop-quantitative-engine-1.2.0"
QUANTITATIVE_PROFILE_RESOURCE = "data/quantitative_notice_profiles.json"

EstimateStatus = Literal["CONFIRMED", "ESTIMATED", "UNSCORABLE", "REVIEW"]
ReadinessBand = Literal["GREEN", "YELLOW", "RED", "GRAY"]
SourceValidationStatus = Literal[
    "SOURCE_VALIDATED",
    "REVIEW_REQUIRED",
    "INCOMPLETE",
    "MISSING",
    "NOT_APPLICABLE",
]
ActivationStatus = Literal["AUTO_ACTIVE", "REVIEW_REQUIRED", "NOT_APPLICABLE"]


class QuantModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    formula_type: Literal["BRACKET", "BOOLEAN"]
    formula: str = Field(min_length=1, max_length=1_000)
    brackets: list[ScoreBracket] = Field(default_factory=list)
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
    value: float | bool | None = None
    lower_value: float | None = None
    upper_value: float | None = None
    evidence_key: str | None = Field(default=None, max_length=240)
    evidence_reference: str | None = Field(default=None, max_length=240)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    fact_binding_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    confidence: float = Field(default=0, ge=0, le=1)
    rationale: str = Field(default="", max_length=1_000)

    @model_validator(mode="after")
    def validate_range(self) -> "QuantitativeFact":
        if (
            self.lower_value is not None
            and self.upper_value is not None
            and self.lower_value > self.upper_value
        ):
            raise ValueError("fact lower_value must not exceed upper_value")
        return self


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
        ):
            raise ValueError(
                "AUTO_ACTIVE requires source-validated AVAILABLE rules without activation reasons"
            )
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
    status: EstimateStatus
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
    return round(float(value), 2)


def _rule_error(criterion: QuantitativeCriterion) -> str | None:
    if criterion.source_anchor is None:
        return "평가표 원문 위치가 연결되지 않았습니다."
    if not criterion.required_evidence_keys:
        return "필요 증빙 키가 정의되지 않았습니다."
    if criterion.rule_floor_points > criterion.max_points:
        return "원문상 최소점수가 항목 만점을 초과합니다."
    if criterion.rule_floor_points and not criterion.floor_condition:
        return "원문상 최소점수의 적용 조건이 정의되지 않았습니다."
    if criterion.rule_base_points is not None and criterion.rule_base_points > criterion.max_points:
        return "원문상 기본점수가 항목 만점을 초과합니다."
    if criterion.rule_base_points is not None and not criterion.base_condition:
        return "원문상 기본점수의 적용 조건이 정의되지 않았습니다."
    if not criterion.brackets:
        return "배점 구간이 정의되지 않았습니다."
    if any(bracket.points > criterion.max_points for bracket in criterion.brackets):
        return "배점 구간 점수가 항목 만점을 초과합니다."
    if any(bracket.points < criterion.rule_floor_points for bracket in criterion.brackets):
        return "배점 구간 점수가 원문상 최소점수보다 낮습니다."
    if criterion.formula_type == "BOOLEAN":
        values = [bracket.boolean_value for bracket in criterion.brackets]
        if sorted(value for value in values if value is not None) != [False, True]:
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


def _points_for_value(criterion: QuantitativeCriterion, value: float | bool) -> float | None:
    if criterion.formula_type == "BOOLEAN":
        if not isinstance(value, bool):
            return None
        match = next(
            (item for item in criterion.brackets if item.boolean_value is value),
            None,
        )
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
    if fact.evidence_key not in criterion.required_evidence_keys:
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
        return _criterion_unscored(
            criterion,
            status="UNSCORABLE",
            rationale=(
                "회사 사실이 이 평가항목의 인정기간·범위·단위 조건에 결합되지 않아 "
                "generic 값을 점수에 적용하지 않았습니다."
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

    if criterion.formula_type == "BRACKET" and fact.status == "ESTIMATED" and (
        fact.lower_value is not None or fact.upper_value is not None
    ):
        if fact.lower_value is None or fact.upper_value is None:
            return _criterion_unscored(
                criterion,
                status="REVIEW",
                rationale="추정값 범위의 하한과 상한이 모두 필요합니다.",
                **fact_audit,
            )
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
        confidence=1.0 if fact.status == "CONFIRMED" else min(fact.confidence, 0.8),
        status=fact.status,
        rationale=fact.rationale
        or ("유효 증빙값을 산식에 적용했습니다." if fact.status == "CONFIRMED" else "잠정 증빙 범위를 산식에 적용했습니다."),
        assumptions=[] if fact.status == "CONFIRMED" else ["증빙 확정 전 잠정 범위이며 최종점수가 아닙니다."],
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
    activation_is_safe = request.activation_status == "AUTO_ACTIVE"
    if not source_is_validated or not activation_is_safe or not request.criteria:
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

    facts: dict[str, QuantitativeFact] = {}
    duplicate_keys: set[str] = set()
    for fact in request.facts:
        if fact.metric_key in facts:
            duplicate_keys.add(fact.metric_key)
        facts[fact.metric_key] = fact

    estimates: list[CriterionEstimate] = []
    for criterion in request.criteria:
        if criterion.metric_key in duplicate_keys:
            estimates.append(
                _criterion_unscored(
                    criterion,
                    status="REVIEW",
                    rationale="동일 metric_key의 회사 데이터가 중복되어 적용 대상을 확정할 수 없습니다.",
                )
            )
        else:
            estimates.append(_estimate_criterion(criterion, facts.get(criterion.metric_key)))

    total_max = _round_points(sum(item.max_points for item in estimates))
    confirmed = _round_points(
        sum(item.lower_points for item in estimates if item.status == "CONFIRMED")
    )
    lower = _round_points(sum(item.lower_points for item in estimates))
    upper = _round_points(sum(item.upper_points for item in estimates))
    unscorable = _round_points(
        sum(
            item.max_points - item.lower_points
            for item in estimates
            if item.status in {"UNSCORABLE", "REVIEW"}
        )
    )
    confirmed_weight = sum(
        item.max_points for item in estimates if item.status == "CONFIRMED"
    )
    coverage = _round_points((confirmed_weight / total_max) * 100) if total_max else 0
    readiness = _round_points((lower / total_max) * 100) if total_max else None
    if readiness is None:
        band: ReadinessBand = "GRAY"
    elif readiness < 70 or coverage < 60:
        band = "RED"
    elif readiness < 80 or coverage < 80:
        band = "YELLOW"
    else:
        band = "GREEN"

    statuses = {item.status for item in estimates}
    if "REVIEW" in statuses:
        overall: EstimateStatus = "REVIEW"
    elif "UNSCORABLE" in statuses:
        overall = "UNSCORABLE"
    elif "ESTIMATED" in statuses:
        overall = "ESTIMATED"
    else:
        overall = "CONFIRMED"

    meets_minimum: bool | None = None
    if request.minimum_score is not None:
        if lower >= request.minimum_score:
            meets_minimum = True
        elif upper < request.minimum_score:
            meets_minimum = False

    weighted_confidence = sum(item.confidence * item.max_points for item in estimates)
    confidence = _round_points(weighted_confidence / total_max) if total_max else 0
    estimated_points = lower if lower == upper and overall in {"CONFIRMED", "ESTIMATED"} else None

    if band == "GREEN":
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
            and isinstance(version.source_payload.get("attachment_manifest"), list)
        ),
        None,
    )
    if metadata is None or not isinstance(metadata.source_payload, dict):
        return None
    metadata_schema_current = (
        metadata.source_payload.get("schema_version") == PPS_METADATA_SCHEMA
    )
    raw_manifest_values = list(metadata.source_payload.get("attachment_manifest", []))
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
    attempts: dict[str, Any] = {}
    for version in reversed(versions):
        payload = version.source_payload
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION"
            or payload.get("source_kind") != PPS_ATTACHMENT_SOURCE
            or not metadata_schema_current
            or payload.get("prompt_version") != PROMPT_VERSION
            or payload.get("processing_version") != PPS_PROCESSING_VERSION
            or payload.get("current_manifest_sha256") != manifest_sha256
        ):
            continue
        attachment_id = str(payload.get("attachment_id") or "")
        if payload.get("manifest_sha256") == descriptors.get(attachment_id):
            attempts[attachment_id] = version

    expected_documents: dict[str, str] = {}
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
    )


_CANONICAL_METRIC_REGISTRY: dict[str, dict[str, Any]] = {
    "PERFORMANCE_AMOUNT": {
        "fact_key": "company.performance.amount",
        "canonical_unit": "KRW",
        "unit_scales": {
            "원": Decimal("1"),
            "krw": Decimal("1"),
            "천원": Decimal("1000"),
            "만원": Decimal("10000"),
            "백만원": Decimal("1000000"),
            "천만원": Decimal("10000000"),
            "억원": Decimal("100000000"),
        },
    },
    "PERFORMANCE_COUNT": {
        "fact_key": "company.performance.count",
        "canonical_unit": "COUNT",
        "unit_scales": {"건": Decimal("1"), "회": Decimal("1"), "개": Decimal("1")},
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
}

QUANTITATIVE_CANONICAL_FACT_KEYS = frozenset(
    str(item["fact_key"]) for item in _CANONICAL_METRIC_REGISTRY.values()
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
    r"(?P<unit>천\s*만\s*원|백\s*만\s*원|억\s*원|만\s*원|천\s*원|원|"
    r"퍼센트|%|건|회|개|명|인|대|년|KRW)",
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
    unit_scales = spec["unit_scales"]
    return spec if _normalize_unit(candidate.unit) in unit_scales else None


def _metric_scale(candidate: ImmutableQuantitativeRuleCandidate) -> Decimal | None:
    spec = _metric_spec(candidate)
    if spec is None:
        return None
    return spec["unit_scales"][_normalize_unit(candidate.unit)]


def _candidate_unit_is_source_bound(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> bool:
    literals = [candidate.criterion_literal, candidate.evidence.quote]
    literals.extend(item.literal for item in candidate.brackets)
    literals.extend(item.evidence.quote for item in candidate.brackets)
    if candidate.threshold is not None:
        literals.extend(
            [candidate.threshold.literal, candidate.threshold.evidence.quote]
        )
    observed = {
        _normalize_unit(match.group("unit"))
        for literal in literals
        for match in _SOURCE_UNIT_RE.finditer(literal)
    }
    expected = _normalize_unit(candidate.unit)
    if expected in observed:
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
    unit_scales = spec["unit_scales"]
    expected = unit_scales.get(_normalize_unit(candidate.unit))
    if expected is None:
        return False
    literals = [item.literal for item in candidate.brackets]
    if candidate.threshold is not None:
        literals.append(candidate.threshold.literal)
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
) -> tuple[float | None, str | None, str | None]:
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
    resolved: list[QuantitativeFact] = []
    for criterion in criteria:
        confirmed: list[QuantitativeFact] = []
        value_errors: list[tuple[str, str | None]] = []
        for fact in stored_facts:
            if fact.fact_key != criterion.metric_key or not fact.verified:
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
                    fact_binding_sha256=binding,
                    confidence=0,
                    rationale=rationale,
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
    return _canonical_digest(
        {
            "binding_schema": "pai-loop-quantitative-fact-binding-1.0.0",
            "document_sha256": document_sha256,
            "candidate": candidate.model_dump(mode="json"),
        }
    )


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


def _profile_activation_reasons(profile: QuantitativeCandidateProfile) -> list[str]:
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
    if len(profile.tables) != 1:
        reasons.add("ALTERNATIVE_TABLE_AMBIGUOUS")
        return sorted(reasons)

    table = profile.tables[0]
    if table.status != "AVAILABLE":
        reasons.add("TABLE_NOT_SOURCE_VALIDATED")
    if table.total_points is None or table.total_evidence is None:
        reasons.add("TABLE_TOTAL_INCOMPLETE")
    elif (
        not table.total_evidence.quote.strip()
        or table.total_evidence.attachment_id != table.source_attachment_id
    ):
        reasons.add("TABLE_TOTAL_ANCHOR_INCOMPLETE")

    table_candidates = [
        item
        for item in profile.available_candidates
        if item.source_attachment_id == table.source_attachment_id
        and item.table_id == table.table_id
    ]
    candidate_ids = [item.criterion_id for item in table_candidates]
    if (
        not table_candidates
        or len(candidate_ids) != len(set(candidate_ids))
        or set(candidate_ids) != set(table.criterion_ids)
        or set(candidate_ids) != set(table.available_criterion_ids)
        or table.review_criterion_ids
        or len(table_candidates) != len(profile.available_candidates)
    ):
        reasons.add("TABLE_CRITERIA_LINKAGE_INCOMPLETE")
    if table.total_points is not None:
        candidate_total = sum(
            (Decimal(str(item.max_points)) for item in table_candidates),
            Decimal("0"),
        )
        if candidate_total != Decimal(str(table.total_points)):
            reasons.add("TABLE_TOTAL_MISMATCH")

    binding_ids = {item.attachment_id for item in profile.document_bindings}
    canonical_fact_keys = [
        str(spec["fact_key"])
        for candidate in table_candidates
        if (spec := _CANONICAL_METRIC_REGISTRY.get(candidate.metric)) is not None
    ]
    if len(canonical_fact_keys) != len(set(canonical_fact_keys)):
        reasons.add("FACT_KEY_AMBIGUOUS")
    for candidate in table_candidates:
        scoring_anchors = [item.evidence for item in candidate.brackets]
        if candidate.threshold is not None:
            scoring_anchors.append(candidate.threshold.evidence)
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
            reasons.add("SOURCE_ANCHOR_INCOMPLETE")
        spec = _CANONICAL_METRIC_REGISTRY.get(candidate.metric)
        if candidate.metric in _UNMODELED_FACT_DIMENSION_METRICS:
            reasons.add("FACT_DIMENSIONS_UNMODELED")
        if spec is None:
            reasons.add("FACT_KEY_UNREGISTERED")
        elif (
            tuple(candidate.required_evidence) != (spec["fact_key"],)
            or len(candidate.required_evidence) != 1
        ):
            reasons.add("FACT_EVIDENCE_KEY_UNREGISTERED")
        if _metric_spec(candidate) is None:
            reasons.add("UNSUPPORTED_UNIT")
        elif not _candidate_unit_is_source_bound(candidate):
            reasons.add("UNIT_NOT_SOURCE_BOUND")
        elif not _candidate_bound_unit_scales_are_consistent(candidate):
            reasons.add("BOUND_UNIT_INCONSISTENT")
        if candidate.scoring_method not in {"BRACKET", "THRESHOLD"} or (
            candidate.scoring_method == "THRESHOLD"
            and (
                candidate.threshold is None
                or candidate.threshold.operator == "EQ"
            )
        ):
            reasons.add("UNSUPPORTED_SCORING_DSL")
        if candidate.scoring_method == "BRACKET":
            brackets = _candidate_brackets(candidate)
            if not brackets:
                reasons.add("UNSUPPORTED_SCORING_DSL")
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
                    reasons.add("BRACKETS_NOT_EXHAUSTIVE_OR_OVERLAPPING")
                elif error:
                    reasons.add("UNSUPPORTED_SCORING_DSL")
    return sorted(reasons)


def quantitative_request_from_candidate_profile(
    profile: QuantitativeCandidateProfile,
    *,
    facts: list[QuantitativeFact] | None = None,
) -> QuantitativeEstimateRequest:
    """Convert verified source rules, never model output, into engine inputs."""

    ruleset_version = (
        "dynamic-quantitative-rules-"
        f"{_canonical_digest(profile.model_dump(mode='json'))[:24]}"
    )
    if profile.status != "AVAILABLE":
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

    activation_reasons = _profile_activation_reasons(profile)
    if activation_reasons:
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

    bindings = {
        item.attachment_id: item.document_sha256
        for item in profile.document_bindings
    }
    criteria: list[QuantitativeCriterion] = []
    conversion_errors: list[str] = []
    for candidate in profile.available_candidates:
        spec = _metric_spec(candidate)
        brackets = _candidate_brackets(candidate)
        if spec is None or not brackets:
            conversion_errors.append(
                f"{candidate.source_attachment_id}:{candidate.table_id}:{candidate.criterion_id}"
            )
            continue
        anchor = candidate.evidence
        criterion_identity = _canonical_digest(
            {
                "attachment_id": candidate.source_attachment_id,
                "table_id": candidate.table_id,
                "criterion_id": candidate.criterion_id,
            }
        )[:28]
        criteria.append(
            QuantitativeCriterion(
                criterion_id=f"dyn-{criterion_identity}",
                category=candidate.metric,
                label=candidate.label,
                max_points=candidate.max_points,
                metric_key=str(spec["fact_key"]),
                unit=str(spec["canonical_unit"]),
                formula_type="BRACKET",
                formula=candidate.criterion_literal,
                brackets=brackets,
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
        )
    if conversion_errors or len(criteria) != len(profile.available_candidates):
        return QuantitativeEstimateRequest(
            ruleset_version=ruleset_version,
            rule_source_status="AVAILABLE",
            source_validation_status="SOURCE_VALIDATED",
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
        for table in profile.tables
        if table.minimum_score is not None
    }
    if len(minimums) > 1:
        return QuantitativeEstimateRequest(
            ruleset_version=ruleset_version,
            rule_source_status="AVAILABLE",
            source_validation_status="SOURCE_VALIDATED",
            activation_status="REVIEW_REQUIRED",
            activation_reasons=["ALTERNATIVE_MINIMUM_SCORE_AMBIGUOUS"],
            criteria=[],
            facts=[],
            missing_reason="여러 정량평가표의 최저점 기준이 달라 합산 기준을 확정할 수 없습니다.",
        )
    return QuantitativeEstimateRequest(
        ruleset_version=ruleset_version,
        rule_source_status="AVAILABLE",
        source_validation_status="SOURCE_VALIDATED",
        activation_status="AUTO_ACTIVE",
        activation_reasons=[],
        minimum_score=next(iter(minimums), None),
        criteria=criteria,
        facts=list(facts or []),
        assumptions=[
            "현재 PPS manifest의 모든 첨부에서 원문 규칙을 검증했습니다.",
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
            and isinstance(version.source_payload.get("attachment_manifest"), list)
        ),
        None,
    )
    if metadata is not None:
        if metadata.source_payload.get("schema_version") != PPS_METADATA_SCHEMA:
            return set(), "현재 PPS 첨부 manifest 스키마가 갱신되지 않아 정량 배점을 확정할 수 없습니다."
        raw_manifest_values = list(
            metadata.source_payload.get("attachment_manifest", [])
        )
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


def estimate_for_notice(
    notice: Notice,
    company_facts: Iterable[CompanyFact] = (),
) -> QuantitativeEstimateResult:
    dynamic_profile = _current_dynamic_quantitative_profile(notice)
    if dynamic_profile is not None:
        request = quantitative_request_from_candidate_profile(dynamic_profile)
        if request.activation_status == "AUTO_ACTIVE":
            request = request.model_copy(
                update={
                    "facts": resolve_verified_quantitative_facts(
                        request.criteria,
                        company_facts,
                        as_of=notice.deadline,
                    )
                }
            )
        return estimate_quantitative_score(request)

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


def _public_quantitative_projection(
    result: QuantitativeEstimateResult,
) -> QuantitativeEstimateResult:
    """Remove company/evidence bindings from the anonymous read-only view."""

    criteria = [
        criterion.model_copy(
            update={
                "criterion_id": f"PUBLIC-CRITERION-{index:03d}",
                "formula": "공개 화면에서는 세부 원문 산식을 제외합니다.",
                "source_anchor": None,
                "evidence_key": None,
                "evidence_reference": None,
                "evidence_sha256": None,
                "fact_binding_sha256": None,
            }
        )
        for index, criterion in enumerate(result.criteria, start=1)
    ]
    return result.model_copy(
        update={
            "ruleset_version": "public-quantitative-summary-v1",
            "source_anchor": None,
            "criteria": criteria,
            "assumptions": [
                "공개 화면에서는 회사 사실값과 원문·내부 증빙 식별자를 제외합니다."
            ],
            "evidence_observations": [],
        }
    )


@quantitative_scoring_router.get(
    "/notices/{notice_key}/quantitative-estimate",
    response_model=QuantitativeEstimateResult,
)
def get_notice_quantitative_estimate(
    notice_key: str,
    request: Request,
    session: DbSession,
) -> QuantitativeEstimateResult:
    notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    public_view = public_read_allowed(request)
    company_facts = (
        []
        if public_view
        else list(
            session.scalars(
                select(CompanyFact).options(selectinload(CompanyFact.evidence))
            ).all()
        )
    )
    result = estimate_for_notice(notice, company_facts)
    return _public_quantitative_projection(result) if public_view else result
