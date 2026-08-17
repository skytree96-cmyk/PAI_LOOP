from __future__ import annotations

import copy
import json
import math
import re
from datetime import date
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from functools import lru_cache
from importlib import resources
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_api_key
from .eligibility_policy import load_public_company_profile
from .models import Notice
from .public_performance import load_public_performance_seed


QUANTITATIVE_ENGINE_VERSION = "pai-loop-quantitative-engine-1.0.0"
QUANTITATIVE_PROFILE_RESOURCE = "data/quantitative_notice_profiles.json"

EstimateStatus = Literal["CONFIRMED", "ESTIMATED", "UNSCORABLE", "REVIEW"]
ReadinessBand = Literal["GREEN", "YELLOW", "RED", "GRAY"]


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
    boolean_value: bool | None = None
    points: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ScoreBracket":
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value >= self.max_value
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


class QuantitativeFact(QuantModel):
    metric_key: str = Field(min_length=1, max_length=160)
    status: EstimateStatus
    value: float | bool | None = None
    lower_value: float | None = None
    upper_value: float | None = None
    evidence_key: str | None = Field(default=None, max_length=240)
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
    rule_source_status: Literal["AVAILABLE", "MISSING", "INCOMPLETE"] = "AVAILABLE"
    minimum_score: float | None = Field(default=None, ge=0)
    criteria: list[QuantitativeCriterion] = Field(default_factory=list, max_length=100)
    facts: list[QuantitativeFact] = Field(default_factory=list, max_length=200)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    missing_reason: str | None = Field(default=None, max_length=2_000)
    source_anchor: SourceAnchor | None = None


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
    rule_source_status: Literal["AVAILABLE", "MISSING", "INCOMPLETE"]
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
    for index, item in enumerate(numeric):
        if index and previous_max is None:
            return "열린 상한 구간 뒤에 다른 구간을 둘 수 없습니다."
        if previous_max is not None and item.min_value is not None and item.min_value < previous_max:
            return "배점 구간이 서로 겹칩니다."
        previous_max = item.max_value
    return None


def _numeric_bracket_matches(bracket: ScoreBracket, value: float) -> bool:
    if bracket.min_value is not None and value < bracket.min_value:
        return False
    if bracket.max_value is not None and value >= bracket.max_value:
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
        # Facts use an inclusive uncertainty range; score brackets use an
        # inclusive lower and exclusive upper bound.
        if upper >= bracket_lower and lower < bracket_upper:
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
    if fact.evidence_key not in criterion.required_evidence_keys:
        return _criterion_unscored(
            criterion,
            status="REVIEW",
            rationale="회사 데이터의 증빙 키가 평가항목의 허용 증빙과 일치하지 않습니다.",
            evidence_key=fact.evidence_key,
        )
    if fact.status in {"REVIEW", "UNSCORABLE"}:
        return _criterion_unscored(
            criterion,
            status=fact.status,
            rationale=fact.rationale or "증빙 상태상 점수를 계산할 수 없습니다.",
            evidence_key=fact.evidence_key,
        )

    if criterion.formula_type == "BRACKET" and fact.status == "ESTIMATED" and (
        fact.lower_value is not None or fact.upper_value is not None
    ):
        if fact.lower_value is None or fact.upper_value is None:
            return _criterion_unscored(
                criterion,
                status="REVIEW",
                rationale="추정값 범위의 하한과 상한이 모두 필요합니다.",
                evidence_key=fact.evidence_key,
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
                evidence_key=fact.evidence_key,
            )
        lower_points, upper_points = point_range
    else:
        if fact.value is None:
            return _criterion_unscored(
                criterion,
                status="UNSCORABLE",
                rationale="산식에 적용할 회사 값이 없습니다.",
                evidence_key=fact.evidence_key,
            )
        points = _points_for_value(criterion, fact.value)
        if points is None:
            return _criterion_unscored(
                criterion,
                status="REVIEW",
                rationale="회사 값에 해당하는 유효한 배점 구간이 없습니다.",
                evidence_key=fact.evidence_key,
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
    if request.missing_reason:
        assumptions.append(request.missing_reason)
    if request.rule_source_status != "AVAILABLE" or not request.criteria:
        return QuantitativeEstimateResult(
            engine_version=QUANTITATIVE_ENGINE_VERSION,
            ruleset_version=request.ruleset_version,
            source_anchor=request.source_anchor,
            rule_source_status=request.rule_source_status,
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
                "정량평가표 또는 핵심 배점 산식이 확보되지 않아 점수를 표시하지 않습니다. "
                "제안요청서의 정량 배점표와 인정 기준을 연결한 뒤 다시 계산해야 합니다."
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


def _profile_for_notice(notice: Notice) -> dict[str, Any] | None:
    catalog = _load_quantitative_profile_catalog()
    normalized_title = _normalize_notice_title(notice.title)
    for item in catalog["profiles"]:
        if item.get("notice_key") == notice.notice_key:
            return copy.deepcopy(item)
        if item.get("bid_notice_no") and item.get("bid_notice_no") == notice.bid_notice_no:
            return copy.deepcopy(item)
        profile_title = item.get("notice_title")
        if profile_title and _normalize_notice_title(str(profile_title)) == normalized_title:
            return copy.deepcopy(item)
    return None


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


def estimate_for_notice(notice: Notice) -> QuantitativeEstimateResult:
    profile = _profile_for_notice(notice)
    if profile is None:
        request = QuantitativeEstimateRequest(
            ruleset_version="unmapped-notice-review-v1",
            rule_source_status="MISSING",
            criteria=[],
            facts=[],
            missing_reason="이 공고에 연결된 정량평가표 프로필이 없습니다.",
        )
        return estimate_quantitative_score(request)
    source_status = str(profile.get("rule_source_status") or "INCOMPLETE").upper()
    if source_status not in {"AVAILABLE", "MISSING", "INCOMPLETE"}:
        source_status = "INCOMPLETE"
    facts = [fact] if (fact := _public_performance_fact(profile)) else []
    request = QuantitativeEstimateRequest(
        ruleset_version=profile["ruleset_version"],
        rule_source_status=source_status,
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


@quantitative_scoring_router.get(
    "/notices/{notice_key}/quantitative-estimate",
    response_model=QuantitativeEstimateResult,
)
def get_notice_quantitative_estimate(
    notice_key: str,
    session: DbSession,
) -> QuantitativeEstimateResult:
    notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return estimate_for_notice(notice)
