"""정량 점수 계산 사슬을 합성 데이터로 종단 검증하는 로컬 테스트베드.

검증 구간은 이렇다.

    회사 사실(CompanyFact + Evidence)
      → resolve_verified_quantitative_facts   (키·검증·유효기간·증빙·결속 해시)
      → QuantitativeEstimateRequest           (기준 + 결합된 사실)
      → estimate_quantitative_score           (결정론적 채점)
      → 항목별 점수와 총점 상태

지표를 늘릴 때 채점기를 복제하지 않는다. ``METRIC_PROFILES`` 에 등록 정보와 증빙
변환 규칙을 한 줄 더하면 같은 하네스가 그 지표를 그대로 채점한다.

운영 데이터베이스, 외부 서비스, 유료 제공자를 부르지 않는다. 식별자와 증빙은 전부
``SYN`` 합성이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Sequence

from pai_loop.models import CompanyFact, Evidence
from pai_loop.quantitative_formula import (
    CREDIT_RATING_ORDER,
    CaseTableRowLiteral,
    CategoryScore,
    DeterministicFormula,
    compile_case_table,
)
from pai_loop.quantitative_performance import PerformanceRecognitionScope
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeEstimateRequest,
    QuantitativeEstimateResult,
    ScoreBracket,
    estimate_quantitative_score,
    quantitative_company_fact_payload_sha256,
    resolve_verified_quantitative_facts,
)

AS_OF = datetime(2026, 9, 30, tzinfo=timezone.utc)
BINDING = "b" * 64
OTHER_BINDING = "c" * 64

FormulaKind = Literal["BRACKET", "BOOLEAN", "THRESHOLD", "FORMULA", "CATEGORICAL", "CASE_TABLE"]


@dataclass(frozen=True)
class MetricProfile:
    """한 지표를 채점 가능한 형태로 만드는 데 필요한 등록 정보.

    ``value_kind`` 는 저장소의 canonical 등록부를 따른다. 새 지표는 이 표에 한 줄을
    더하면 되고, 채점기나 하네스를 고칠 필요가 없다.
    """

    #: 저장소 canonical 등록부의 지표 이름. 평가항목의 ``category`` 로 쓰인다.
    metric: str
    fact_key: str
    unit: str
    value_kind: Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"]
    # 정상값 / 구간 경계에 정확히 걸친 값 / 경계 바로 아래 값
    nominal: Any
    boundary: Any
    below: Any = None
    threshold: float = 0
    formulas: tuple[FormulaKind, ...] = ()


METRIC_PROFILES: dict[str, MetricProfile] = {
    "PERFORMANCE_AMOUNT": MetricProfile(
        "PERFORMANCE_AMOUNT", "company.performance.amount", "원", "NUMERIC",
        nominal=300_000_000, boundary=200_000_000, below=199_999_999,
        threshold=200_000_000,
        formulas=("BRACKET", "THRESHOLD", "FORMULA"),
    ),
    "PERFORMANCE_COUNT": MetricProfile(
        "PERFORMANCE_COUNT", "company.performance.count", "건", "NUMERIC",
        nominal=7, boundary=5, below=4, threshold=5,
        formulas=("BRACKET", "THRESHOLD"),
    ),
    "PERSONNEL_COUNT": MetricProfile(
        "PERSONNEL_COUNT", "company.personnel.count", "명", "NUMERIC",
        nominal=12, boundary=5, below=4, threshold=5,
        formulas=("BRACKET", "THRESHOLD"),
    ),
    "CERTIFICATION_COUNT": MetricProfile(
        "CERTIFICATION_COUNT", "company.certification.count", "건", "NUMERIC",
        nominal=3, boundary=1, below=0, threshold=1,
        formulas=("BRACKET", "THRESHOLD"),
    ),
    "AWARD_COUNT": MetricProfile(
        "AWARD_COUNT", "company.award.count", "건", "NUMERIC",
        nominal=2, boundary=1, below=0, threshold=1,
        formulas=("BRACKET", "THRESHOLD"),
    ),
    "FACILITY_EQUIPMENT_COUNT": MetricProfile(
        "FACILITY_EQUIPMENT_COUNT", "company.facility_equipment.count", "대", "NUMERIC",
        nominal=4, boundary=2, below=1, threshold=2,
        formulas=("BRACKET", "THRESHOLD"),
    ),
    "BUSINESS_YEARS": MetricProfile(
        "BUSINESS_YEARS", "company.business.years", "년", "NUMERIC",
        nominal=11, boundary=5, below=4, threshold=5,
        formulas=("BRACKET", "THRESHOLD", "FORMULA"),
    ),
    "FINANCIAL_RATIO": MetricProfile(
        "FINANCIAL_RATIO", "company.financial.ratio", "%", "NUMERIC",
        nominal=150.0, boundary=100.0, below=99.9, threshold=100.0,
        formulas=("BRACKET", "THRESHOLD", "FORMULA"),
    ),
    "CREDIT_RATING": MetricProfile(
        "CREDIT_RATING", "company.credit_rating", "등급", "CATEGORICAL",
        # 최상위 등급대의 마지막 등급이 경계, 그 바로 아래 등급이 경계 미만이다.
        nominal="AA0", boundary=CREDIT_RATING_ORDER[6], below=CREDIT_RATING_ORDER[7],
        formulas=("CATEGORICAL", "CASE_TABLE"),
    ),
    "LOCAL_PRESENCE": MetricProfile(
        "LOCAL_PRESENCE", "company.local_presence", "여부", "BOOLEAN",
        nominal=True, boundary=False,
        formulas=("BOOLEAN",),
    ),
}


def _brackets(profile: MetricProfile, max_points: float) -> list[ScoreBracket]:
    # 구간은 가능한 최솟값부터 빠짐없이 이어져야 한다. 하한을 열어 두면 지표마다
    # 다른 정의역(음수 비율 등)을 하나의 하네스로 덮을 수 있다.
    edge = float(profile.threshold)
    return [
        ScoreBracket(bracket_id="SYN-LOW", label="SYN 하위 구간",
                     max_value=edge, points=0),
        ScoreBracket(bracket_id="SYN-HIGH", label="SYN 상위 구간",
                     min_value=edge, points=max_points),
    ]


#: 합성 신용평가 등급대. 세 구간이 공개된 등급 순서를 빠짐없이, 순서대로 덮는다.
CREDIT_BANDS = (CREDIT_RATING_ORDER[:7], CREDIT_RATING_ORDER[7:13], CREDIT_RATING_ORDER[13:])


def _credit_awards(max_points: float) -> tuple[float, float, float]:
    return (max_points, max_points / 2, 0.0)


def _credit_case_rows(max_points: float) -> tuple[CaseTableRowLiteral, ...]:
    """등급 도메인 전체를 순서대로 덮는 합성 신용평가표.

    저장소는 CASE_TABLE 의 CREDIT_RATING 프로그램이 공개된 등급 순서를 빠짐없이,
    그 순서 그대로, 배점이 내려가는 형태로 덮을 것을 요구한다. 일부 등급만 적은 표는
    결정론적이지 않으므로 컴파일되지 않는다.
    """

    return tuple(
        CaseTableRowLiteral(
            operator="IN",
            category_values=band,
            source_literal=f"{', '.join(band)} {award}점",
            award_kind="POINTS",
            award_value=award,
        )
        for band, award in zip(CREDIT_BANDS, _credit_awards(max_points), strict=True)
    )


def _performance_scope(profile: MetricProfile):
    """실적 평가항목은 원문 인정조건 없이는 채점되지 않는다."""

    return PerformanceRecognitionScope(
        metric_key=profile.fact_key,
        lookback_years=3,
        similarity_keywords=("교육",),
        match_mode="ANY",
        counterparty_scope="PUBLIC_SECTOR",
        # 공공부문 범위는 원문에 근거한 발주처 표현을 함께 요구한다.
        counterparty_keywords=("공공기관",),
        lookback_anchor_basis="SUBMISSION_DEADLINE",
        vat_basis="INCLUDED",
        completion_required=True,
        aggregation="SUM_AMOUNT" if profile.metric == "PERFORMANCE_AMOUNT" else "COUNT",
        consortium_share_rule="APPLY_SHARE",
        source_literal="SYN 최근 3년간 공공기관 교육 용역 수행완료 실적",
    )


def _scoring_fields(profile: MetricProfile, kind: FormulaKind, max_points: float) -> dict[str, Any]:
    if kind == "BRACKET":
        return {"formula_type": "BRACKET", "brackets": _brackets(profile, max_points)}
    if kind == "BOOLEAN":
        return {
            "formula_type": "BOOLEAN",
            "brackets": [
                ScoreBracket(bracket_id="SYN-TRUE", label="SYN 충족",
                             boolean_value=True, points=max_points),
                ScoreBracket(bracket_id="SYN-FALSE", label="SYN 미충족",
                             boolean_value=False, points=0),
            ],
        }
    if kind == "THRESHOLD":
        return {
            "formula_type": "THRESHOLD",
            "threshold_operator": "GTE",
            "threshold_value": float(profile.threshold),
            "threshold_points_if_met": max_points,
            "threshold_points_if_not_met": 0,
        }
    if kind == "FORMULA":
        return {
            "formula_type": "FORMULA",
            "deterministic_formula": DeterministicFormula(
                expression="x", source_unit_scale=float(profile.threshold) or 1,
                minimum_points=0, maximum_points=max_points,
            ),
        }
    if kind == "CATEGORICAL":
        # CASE_TABLE 와 같은 등급대를 쓴다. 두 계산식이 같은 원문을 다르게 채점하면
        # 경계값 검증이 무엇을 재는지 알 수 없게 된다.
        return {
            "formula_type": "CATEGORICAL",
            "categories": [
                CategoryScore(values=tuple(band), points=award)
                for band, award in zip(CREDIT_BANDS, _credit_awards(max_points), strict=True)
            ],
        }
    if kind == "CASE_TABLE":
        compiled = compile_case_table(
            _credit_case_rows(max_points),
            value_kind="CREDIT_RATING",
            maximum_points=max_points,
        )
        if compiled is None:
            raise AssertionError("합성 CASE_TABLE 이 컴파일되지 않았습니다")
        return {"formula_type": "CASE_TABLE", "case_table": compiled}
    raise AssertionError(f"지원하지 않는 계산식 유형: {kind}")


def profile_of(metric: str | MetricProfile) -> MetricProfile:
    """등록된 지표 이름이나 즉석에서 만든 등록 정보 한 줄을 받는다.

    새 지표를 붙일 때 하네스를 고치지 않아도 되는 것은 이 입구 덕분이다.
    """

    return metric if isinstance(metric, MetricProfile) else METRIC_PROFILES[metric]


def build_criterion(
    metric: str | MetricProfile,
    kind: FormulaKind,
    *,
    max_points: float = 10,
    binding: str | None = BINDING,
    criterion_id: str | None = None,
) -> QuantitativeCriterion:
    """등록된 지표 하나를 지정한 계산식 유형으로 채점 가능하게 만든다."""

    profile = profile_of(metric)
    return QuantitativeCriterion(
        criterion_id=criterion_id or f"SYN-{profile.metric}",
        category=profile.metric,
        # "평가항목"·"총점" 같은 말은 표의 머리말 판정에 걸린다. 등록된 지표는
        # 그 판정에서 보호되지만, 하네스가 그 보호에 기대지 않도록 라벨을 비운다.
        label=f"SYN {profile.metric} 배점",
        max_points=max_points,
        metric_key=profile.fact_key,
        unit=profile.unit,
        formula=f"SYN {profile.metric} {kind} 원문 근거",
        # 채점기는 결합된 사실의 canonical 키로 허용 증빙을 확인한다.
        # 원문 추출도 ``("company.credit_rating",)`` 같은 형태로 저장한다.
        required_evidence_keys=[profile.fact_key],
        fact_binding_sha256=binding,
        source_anchor={"document_label": "SYN 제안요청서", "section": "SYN-배점표"},
        performance_scope=(
            _performance_scope(profile)
            if profile.metric in {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT"}
            else None
        ),
        **_scoring_fields(profile, kind, max_points),
    )


def build_company_fact(
    metric: str | MetricProfile,
    value: Any = None,
    *,
    binding: str | None = BINDING,
    unit: str | None = None,
    verified: bool = True,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    evidence_status: str = "VERIFIED",
    evidence_issued_at: datetime | None = None,
    evidence_valid_until: datetime | None = None,
    with_evidence: bool = True,
    fact_key: str | None = None,
) -> CompanyFact:
    """합성 회사 사실과 그 증빙. 저장하지 않고 메모리에서만 쓴다."""

    profile = profile_of(metric)
    raw = profile.nominal if value is None else value
    payload: dict[str, Any] = {"value": raw, "unit": unit or profile.unit}
    if binding is not None:
        payload["fact_binding_sha256"] = binding
    fact = CompanyFact(
        id=f"SYN-FACT-{profile.metric}",
        fact_key=fact_key or profile.fact_key,
        value=payload,
        value_label=f"SYN {profile.metric}",
        effective_from=effective_from or AS_OF - timedelta(days=365),
        effective_to=effective_to,
        verified=verified,
        source="SYN_TESTBED",
    )
    if with_evidence:
        evidence_id = f"SYN-EVIDENCE-{profile.metric}"
        issued = evidence_issued_at or AS_OF - timedelta(days=200)
        fact.evidence_id = evidence_id
        fact.evidence = Evidence(
            id=evidence_id,
            evidence_key=evidence_id,
            name=f"SYN {profile.metric} 증빙",
            # 결속을 가진 동적 평가항목은 이 증빙 유형만 인정한다.
            evidence_type="QUANTITATIVE_FACT",
            status=evidence_status,
            issued_at=issued,
            valid_from=issued,
            valid_until=evidence_valid_until,
            source_location="SYN://testbed/evidence",
            sha256="d" * 64,
            metadata_json={
                "quantitative_fact_key": fact.fact_key,
                "fact_binding_sha256": binding,
                "company_fact_payload_sha256": quantitative_company_fact_payload_sha256(fact),
            },
        )
    return fact


@dataclass
class ScoreRun:
    """한 번의 채점과 그 중간 산출물."""

    request: QuantitativeEstimateRequest
    result: QuantitativeEstimateResult
    resolved_metric_keys: tuple[str, ...] = field(default_factory=tuple)

    def criterion(self, criterion_id: str):
        return next(item for item in self.result.criteria if item.criterion_id == criterion_id)

    def status_of(self, metric: str) -> str:
        return self.criterion(f"SYN-{metric}").status


def score(
    criteria: Sequence[QuantitativeCriterion],
    company_facts: Sequence[CompanyFact] = (),
    *,
    as_of: datetime = AS_OF,
    activation: Literal["AUTO_ACTIVE", "PARTIAL_SOURCE"] = "AUTO_ACTIVE",
) -> ScoreRun:
    """회사 사실을 결합해 채점한다. 결합 규칙은 운영 코드를 그대로 쓴다."""

    facts = resolve_verified_quantitative_facts(list(criteria), list(company_facts), as_of=as_of)
    if activation == "AUTO_ACTIVE":
        request = QuantitativeEstimateRequest(
            ruleset_version="syn-testbed-v1",
            rule_source_status="AVAILABLE",
            source_validation_status="SOURCE_VALIDATED",
            activation_status="AUTO_ACTIVE",
            criteria=list(criteria),
            facts=facts,
        )
    else:
        request = QuantitativeEstimateRequest(
            ruleset_version="syn-testbed-v1",
            rule_source_status="INCOMPLETE",
            source_validation_status="INCOMPLETE",
            activation_status="PARTIAL_SOURCE",
            activation_reasons=["SYN_ATTACHMENT_UNRESOLVED"],
            criteria=list(criteria),
            facts=facts,
        )
    return ScoreRun(
        request=request,
        result=estimate_quantitative_score(request),
        resolved_metric_keys=tuple(item.metric_key for item in facts),
    )
