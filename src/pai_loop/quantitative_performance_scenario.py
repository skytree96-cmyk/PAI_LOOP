"""Non-persistable count scenarios under an explicit exclusion policy.

This does not derive company facts or relax the production scoring engine.
An included record is a caller's explicit, evidenced review of every condition
for the exact criterion. Unverified records contribute nothing to this scenario;
their exclusion never asserts that the company's actual performance count is 0.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .quantitative_performance import parse_performance_recognition_scope
from .quantitative_scoring import (
    QuantitativeEstimateRequest, _points_for_value, _rule_error,
    estimate_quantitative_score,
)


class _ScenarioModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always",
    )


class VerifiedPerformanceScenarioRecord(_ScenarioModel):
    """Explicit review attestation, never populated from register status alone."""

    record_key: str = Field(min_length=1, max_length=180)
    evidence_reference: str = Field(min_length=1, max_length=500)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fact_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recognition_conditions_verified: Literal[True]

    @field_validator("record_key", "evidence_reference")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("verified record requires nonblank evidence identity")
        return value.strip()


class PerformanceCountExclusionScenario(_ScenarioModel):
    purpose: Literal["UNVERIFIED_PERFORMANCE_EXCLUDED"] = "UNVERIFIED_PERFORMANCE_EXCLUDED"
    persistence_eligible: Literal[False] = False
    company_actual_count_confirmed: Literal[False] = False
    eligibility_assessed: Literal[False] = False
    status: Literal["ESTIMATED_SCENARIO", "REVIEW_REQUIRED"]
    criterion_id: str
    fact_binding_sha256: str | None
    scenario_count: int = Field(ge=0)
    included_records: tuple[VerifiedPerformanceScenarioRecord, ...]
    excluded_record_keys: tuple[str, ...]
    estimated_points: float | None = Field(default=None, ge=0)
    max_points: float | None = Field(default=None, gt=0)
    review_code: str | None = None
    exclusion_reason: str
    rationale: str

    @model_validator(mode="after")
    def validate_scenario_points(self) -> "PerformanceCountExclusionScenario":
        if (self.status == "ESTIMATED_SCENARIO") != (self.estimated_points is not None):
            raise ValueError("only an estimated scenario may carry scenario points")
        if self.estimated_points is not None and (
            self.max_points is None or self.estimated_points > self.max_points
        ):
            raise ValueError("scenario points exceed the source maximum")
        return self


def estimate_performance_count_exclusion_scenario(
    request: QuantitativeEstimateRequest,
    *,
    criterion_id: str,
    included_records: Sequence[VerifiedPerformanceScenarioRecord],
    excluded_record_keys: Sequence[str],
    exclusion_reason: str,
) -> PerformanceCountExclusionScenario:
    """Apply source count bands to an explicitly verified subset, including 0.

    Callers must review all recognition conditions before including an ID.
    A 0-sized subset is permitted only as this non-final exclusion scenario;
    a missing 0-count source band is never replaced by a non-submission award.
    Non-performance facts and the input request remain unchanged.
    """
    included = tuple(sorted(
        (VerifiedPerformanceScenarioRecord.model_validate(item) for item in included_records),
        key=lambda item: item.record_key,
    ))
    if not isinstance(exclusion_reason, str) or not exclusion_reason.strip():
        raise ValueError("SCENARIO_EXCLUSION_REASON_REQUIRED")
    if any(not isinstance(key, str) or not key.strip() for key in excluded_record_keys):
        raise ValueError("SCENARIO_RECORD_IDENTITIES_REQUIRED")
    excluded = tuple(sorted(key.strip() for key in excluded_record_keys))
    included_keys = [item.record_key for item in included]
    if (len(set(included_keys)) != len(included_keys)
        or len(set(excluded)) != len(excluded)
        or set(included_keys).intersection(excluded)):
        raise ValueError("SCENARIO_RECORD_IDENTITIES_OVERLAP")

    candidates = [item for item in request.criteria if item.criterion_id == criterion_id]
    criterion = candidates[0] if len(candidates) == 1 else None
    metadata = dict(
        criterion_id=criterion_id,
        fact_binding_sha256=criterion.fact_binding_sha256 if criterion else None,
        scenario_count=len(included), included_records=included,
        excluded_record_keys=excluded, exclusion_reason=exclusion_reason.strip(),
        max_points=criterion.max_points if criterion else None,
    )

    def review(code: str, rationale: str) -> PerformanceCountExclusionScenario:
        return PerformanceCountExclusionScenario(
            **metadata, status="REVIEW_REQUIRED", review_code=code, rationale=rationale,
        )

    # Reuse the existing activation gate, without changing facts or statuses.
    baseline = estimate_quantitative_score(request)
    if criterion is None or baseline.total_max_points is None:
        return review("SOURCE_RULE_NOT_VALIDATED", "원문 배점의 검증 또는 항목 식별이 완료되지 않았습니다.")
    if criterion.metric_key != "company.performance.count":
        return review("NOT_PERFORMANCE_COUNT", "이 시나리오는 실적 건수에만 적용하며 재무·신용 항목은 변경하지 않습니다.")
    if not criterion.fact_binding_sha256 or _rule_error(criterion):
        return review("SOURCE_RULE_NOT_VALIDATED", "원문에 결합된 유효한 실적 건수 배점이 필요합니다.")
    scope = criterion.performance_scope
    if scope is None or parse_performance_recognition_scope(
        scope.source_literal, metric_key="company.performance.count",
    ) != scope:
        return review("PERFORMANCE_SCOPE_UNVERIFIED", "실적 인정조건 자체의 원문 해석이 확정되지 않아 시나리오를 계산하지 않았습니다.")
    if any(item.fact_binding_sha256 != criterion.fact_binding_sha256 for item in included):
        return review("SCENARIO_RECORD_BINDING_MISMATCH", "포함 실적의 확인 증빙이 현재 평가항목의 모든 인정조건에 결합되지 않았습니다.")
    # Compiled discrete CASE rows enforce monotonic awards. Brackets must also
    # be non-decreasing so excluding a record cannot improve its scenario score.
    if criterion.formula_type == "CASE_TABLE":
        if criterion.case_table is None or criterion.case_table.value_kind != "DISCRETE":
            return review("UNSUPPORTED_SCENARIO_RULE", "검증된 정수 건수 배점표가 필요합니다.")
    elif criterion.formula_type == "BRACKET":
        ordered = sorted(criterion.brackets, key=lambda item: float("-inf") if item.min_value is None else item.min_value)
        if any(right.points < left.points for left, right in zip(ordered, ordered[1:])):
            return review("NON_MONOTONIC_SCENARIO_RULE", "실적 제외가 점수를 높일 수 있는 배점표에는 보수적 시나리오를 적용하지 않습니다.")
    else:
        return review("UNSUPPORTED_SCENARIO_RULE", "이 시나리오는 검증된 건수 구간형 또는 CASE 배점표만 지원합니다.")
    points = _points_for_value(criterion, len(included))
    if points is None:
        return review("SCENARIO_COUNT_NOT_SOURCE_SCORED", "제외 후 인정집합의 건수에 대응하는 원문 배점이 없습니다. 실제 0건이나 미제출로 대체하지 않았습니다.")
    return PerformanceCountExclusionScenario(
        **metadata, status="ESTIMATED_SCENARIO", estimated_points=points,
        rationale=(
            f"미확인 실적 {len(excluded)}건을 제외하고 모든 인정조건을 확인한 "
            f"{len(included)}건의 집합만 원문 배점에 적용한 잠정 시나리오입니다. "
            "회사의 실제 총 실적 건수·확정점수·참가자격 판정이 아닙니다."
        ),
    )
