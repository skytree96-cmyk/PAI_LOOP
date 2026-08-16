from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .enums import Eligibility, EvidenceStatus, ReadinessStatus, RiskBand
from .models import AtomicRequirement, CompanyFact, Notice, NoticeVersion

RULESET_VERSION = "2026.08-v1"
MIN_EXTRACTION_CONFIDENCE = 0.90


@dataclass(slots=True)
class EvaluationResult:
    eligibility: Eligibility
    reason_code: str
    readiness_score: float
    readiness_status: ReadinessStatus
    evidence_coverage: float
    risk_score: float | None
    risk_band: RiskBand
    atomic_results: list[dict[str, Any]]
    explanation: dict[str, Any]


def _normalise(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    return value


def _as_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def compare(actual: Any, operator: str, expected: Any) -> bool:
    actual_n = _normalise(actual)
    expected_n = _normalise(expected)
    if operator == "exists":
        return actual is not None
    if operator == "eq":
        return actual_n == expected_n
    if operator == "neq":
        return actual_n != expected_n
    if operator == "in":
        return isinstance(expected_n, list) and actual_n in expected_n
    if operator == "contains":
        if isinstance(actual_n, (list, str)):
            return expected_n in actual_n
        return False
    if operator in {"gte", "lte"}:
        actual_number, expected_number = _as_decimal(actual), _as_decimal(expected)
        if actual_number is None or expected_number is None:
            return False
        return actual_number >= expected_number if operator == "gte" else actual_number <= expected_number
    raise ValueError(f"unsupported operator: {operator}")


def _utc_naive(value: datetime) -> datetime:
    """Make database datetimes comparable across SQLite and PostgreSQL.

    SQLite discards timezone metadata while PostgreSQL preserves it. The system
    contract stores instants in UTC; comparisons therefore normalise both to a
    naive UTC representation without altering the instant.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def fact_is_effective(fact: CompanyFact, as_of: datetime) -> bool:
    basis = _utc_naive(as_of)
    start = _utc_naive(fact.effective_from)
    end = _utc_naive(fact.effective_to) if fact.effective_to is not None else None
    return start <= basis and (end is None or end >= basis)


def evidence_state(fact: CompanyFact, as_of: datetime) -> tuple[bool, str]:
    evidence = fact.evidence
    if evidence is None:
        return False, "Evidence_ID가 연결되지 않았습니다."
    if evidence.status not in {EvidenceStatus.VERIFIED.value, EvidenceStatus.OVERRIDDEN.value}:
        return False, f"증빙 상태가 {evidence.status}입니다."
    basis = evidence.valid_from or evidence.issued_at
    if basis is None:
        return False, "증빙 취득일 또는 유효시작일이 없어 기준일 유효성을 확인할 수 없습니다."
    as_of_normalised = _utc_naive(as_of)
    if _utc_naive(basis) > as_of_normalised:
        return False, "증빙이 공고 마감일 이후에 취득되었습니다."
    if evidence.valid_until is not None and _utc_naive(evidence.valid_until) < as_of_normalised:
        return False, "증빙이 공고 마감일 전에 만료되었습니다."
    return True, "공고 마감일 기준 유효한 검증 증빙입니다."


def _select_facts(facts: Iterable[CompanyFact], as_of: datetime) -> dict[str, CompanyFact]:
    effective: dict[str, CompanyFact] = {}
    for fact in facts:
        if not fact_is_effective(fact, as_of):
            continue
        previous = effective.get(fact.fact_key)
        if previous is None or previous.effective_from < fact.effective_from:
            effective[fact.fact_key] = fact
    return effective


def _review_triggered(requirement: AtomicRequirement, actual: Any, *, missing: bool) -> bool:
    if not requirement.linked_review_code:
        return False
    trigger = requirement.review_trigger_value
    if missing:
        return trigger == "__MISSING__"
    return trigger is not None and _normalise(actual) == _normalise(trigger)


def _evaluate_atomic(
    requirement: AtomicRequirement,
    facts: dict[str, CompanyFact],
    as_of: datetime,
    *,
    document_quality_ok: bool,
) -> dict[str, Any]:
    base = {
        "requirement_key": requirement.requirement_key,
        "group_key": requirement.group_key,
        "path_key": requirement.path_key,
        "label": requirement.label,
        "pass_rule_id": requirement.pass_rule_id,
        "linked_review_code": requirement.linked_review_code,
        "required_value": requirement.required_value,
        "source_excerpt": requirement.source_excerpt,
        "source_location": requirement.source_location,
    }

    if not document_quality_ok or requirement.parse_confidence < MIN_EXTRACTION_CONFIDENCE:
        return {
            **base,
            "result": Eligibility.REVIEW.value,
            "reason_code": "R07",
            "actual_value": None,
            "evidence_key": None,
            "evidence_valid": False,
            "message": "문서 추출 품질 Gate를 통과하지 못해 원문 확인이 필요합니다.",
        }

    fact = facts.get(requirement.fact_key)
    if fact is None:
        if _review_triggered(requirement, None, missing=True):
            return {
                **base,
                "result": Eligibility.REVIEW.value,
                "reason_code": requirement.linked_review_code,
                "actual_value": None,
                "evidence_key": None,
                "evidence_valid": False,
                "message": "연결된 REVIEW 예외의 확인이 필요합니다.",
            }
        return {
            **base,
            "result": Eligibility.FAIL.value,
            "reason_code": "DF-000",
            "actual_value": None,
            "evidence_key": None,
            "evidence_valid": False,
            "message": "마감일 당시 유효한 회사 사실값이 없어 PASS 경로와 일치하지 않습니다.",
        }

    fact_evidence_valid = True
    fact_evidence_message = "증빙이 요구되지 않는 조건입니다."
    if requirement.evidence_required:
        fact_evidence_valid, fact_evidence_message = evidence_state(fact, as_of)

    matches = compare(fact.value, requirement.operator, requirement.required_value)
    if not matches:
        if _review_triggered(requirement, fact.value, missing=False):
            return {
                **base,
                "result": Eligibility.REVIEW.value,
                "reason_code": requirement.linked_review_code,
                "actual_value": fact.value,
                "evidence_key": fact.evidence.evidence_key if fact.evidence else None,
                "evidence_valid": fact_evidence_valid,
                "message": (
                    "PASS 값은 아니지만 연결된 REVIEW 예외 조건과 일치합니다. "
                    f"{fact_evidence_message}"
                ),
            }
        return {
            **base,
            "result": Eligibility.FAIL.value,
            "reason_code": "DF-000",
            "actual_value": fact.value,
            "evidence_key": fact.evidence.evidence_key if fact.evidence else None,
            "evidence_valid": fact_evidence_valid,
            "message": f"회사 사실값이 요구조건과 일치하지 않습니다. {fact_evidence_message}",
        }

    evidence_valid = fact_evidence_valid
    evidence_message = fact_evidence_message
    if requirement.evidence_required:
        if not evidence_valid:
            code = "R06" if "유효" in evidence_message and "없어" in evidence_message else "R04"
            return {
                **base,
                "result": Eligibility.REVIEW.value,
                "reason_code": code,
                "actual_value": fact.value,
                "evidence_key": fact.evidence.evidence_key if fact.evidence else None,
                "evidence_valid": False,
                "message": evidence_message,
            }

    return {
        **base,
        "result": Eligibility.PASS.value,
        "reason_code": requirement.pass_rule_id,
        "actual_value": fact.value,
        "evidence_key": fact.evidence.evidence_key if fact.evidence else None,
        "evidence_valid": evidence_valid,
        "message": evidence_message,
    }


def _path_result(items: list[dict[str, Any]]) -> Eligibility:
    results = {item["result"] for item in items}
    if Eligibility.FAIL.value in results:
        return Eligibility.FAIL
    if Eligibility.REVIEW.value in results:
        return Eligibility.REVIEW
    return Eligibility.PASS


def _group_result(paths: list[Eligibility]) -> Eligibility:
    if Eligibility.PASS in paths:
        return Eligibility.PASS
    if Eligibility.REVIEW in paths:
        return Eligibility.REVIEW
    return Eligibility.FAIL


def _risk(dimensions: dict[str, float] | None) -> tuple[float | None, RiskBand]:
    if not dimensions:
        return None, RiskBand.UNKNOWN
    weights = {
        "qualification": 0.25,
        "execution": 0.20,
        "competition": 0.20,
        "profitability": 0.15,
        "operation": 0.10,
        "document": 0.10,
    }
    weighted_sum = sum(float(dimensions.get(key, 0)) * weight for key, weight in weights.items())
    supplied_weight = sum(weight for key, weight in weights.items() if key in dimensions)
    if supplied_weight == 0:
        return None, RiskBand.UNKNOWN
    score = round(weighted_sum / supplied_weight, 1)
    if score < 30:
        return score, RiskBand.GO
    if score < 60:
        return score, RiskBand.CONDITIONAL_GO
    return score, RiskBand.NO_GO


def evaluate_notice(
    notice: Notice,
    version: NoticeVersion,
    requirements: Iterable[AtomicRequirement],
    company_facts: Iterable[CompanyFact],
) -> EvaluationResult:
    active_requirements = [requirement for requirement in requirements if requirement.active]
    facts = _select_facts(company_facts, notice.deadline)
    document_quality_ok = (
        version.document_complete
        and version.extraction_status == "COMPLETE"
        and version.extraction_confidence >= MIN_EXTRACTION_CONFIDENCE
    )
    atomics = [
        _evaluate_atomic(
            requirement,
            facts,
            notice.deadline,
            document_quality_ok=document_quality_ok,
        )
        for requirement in active_requirements
    ]

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    mandatory_by_group: dict[str, bool] = {}
    for requirement, atomic in zip(active_requirements, atomics, strict=True):
        grouped[requirement.group_key][requirement.path_key].append(atomic)
        mandatory_by_group[requirement.group_key] = mandatory_by_group.get(requirement.group_key, False) or requirement.mandatory

    group_summaries: list[dict[str, Any]] = []
    overall_groups: list[Eligibility] = []
    for group_key, paths in grouped.items():
        path_summaries = []
        path_results = []
        for path_key, items in paths.items():
            result = _path_result(items)
            path_results.append(result)
            path_summaries.append({"path_key": path_key, "result": result.value})
        group_result = _group_result(path_results)
        if mandatory_by_group[group_key]:
            overall_groups.append(group_result)
        group_summaries.append(
            {"group_key": group_key, "result": group_result.value, "paths": path_summaries}
        )

    if not active_requirements:
        eligibility, reason_code = Eligibility.REVIEW, "R07"
    elif Eligibility.FAIL in overall_groups:
        eligibility, reason_code = Eligibility.FAIL, "DF-000"
    elif Eligibility.REVIEW in overall_groups:
        eligibility, reason_code = Eligibility.REVIEW, "REVIEW_MATCH"
    else:
        eligibility, reason_code = Eligibility.PASS, "PASS_MATCH"

    evidence_items = [
        item for requirement, item in zip(active_requirements, atomics, strict=True) if requirement.evidence_required
    ]
    evidence_coverage = round(
        100 * sum(bool(item["evidence_valid"]) for item in evidence_items) / len(evidence_items), 1
    ) if evidence_items else 100.0

    fact_coverage = round(
        100 * sum(item["actual_value"] is not None for item in atomics) / len(atomics), 1
    ) if atomics else 0.0
    document_quality_score = (
        round(version.extraction_confidence * 100, 1) if document_quality_ok else 0.0
    )
    # Readiness measures preparation, not eligibility: a well-evidenced mismatch
    # can correctly be FAIL and still have high evidence/readiness coverage.
    readiness_score = round(
        (evidence_coverage * 0.5) + (fact_coverage * 0.3) + (document_quality_score * 0.2),
        1,
    )

    if not atomics or not document_quality_ok:
        readiness_status = ReadinessStatus.GRAY
    elif readiness_score >= 80 and evidence_coverage >= 80:
        readiness_status = ReadinessStatus.GREEN
    elif readiness_score < 70 or evidence_coverage < 60:
        readiness_status = ReadinessStatus.RED
    else:
        readiness_status = ReadinessStatus.YELLOW

    risk_score, risk_band = _risk(notice.risk_dimensions)
    failed = [item for item in atomics if item["result"] == Eligibility.FAIL.value]
    default_fail_details = [
        {
            "failed_condition": item["label"],
            "required_value": item["required_value"],
            "current_value": item["actual_value"],
            "unmatched_pass_paths": f"{item['group_key']}/{item['path_key']} ({item['pass_rule_id']})",
            "review_not_matched_reason": (
                "연결된 REVIEW 규칙 없음"
                if not item["linked_review_code"]
                else f"{item['linked_review_code']} 트리거와 불일치"
            ),
        }
        for item in failed
    ]
    review_codes = sorted(
        {
            item["reason_code"]
            for item in atomics
            if item["result"] == Eligibility.REVIEW.value
        }
    )
    explanation = {
        "decision_order": ["PASS_RULE", "LINKED_REVIEW", "DF-000"],
        "deadline_snapshot": notice.deadline.isoformat(),
        "document_quality_ok": document_quality_ok,
        "group_results": group_summaries,
        "review_codes": review_codes,
        "default_fail_details": default_fail_details,
        "readiness_components": {
            "evidence_coverage": evidence_coverage,
            "fact_coverage": fact_coverage,
            "document_quality": document_quality_score,
            "weights": {"evidence": 0.5, "fact": 0.3, "document": 0.2},
        },
        "separation_notice": "참가자격, 정량 준비도, 증빙 커버리지, 사업 리스크는 서로 독립된 지표입니다.",
    }
    return EvaluationResult(
        eligibility=eligibility,
        reason_code=reason_code,
        readiness_score=readiness_score,
        readiness_status=readiness_status,
        evidence_coverage=evidence_coverage,
        risk_score=risk_score,
        risk_band=risk_band,
        atomic_results=atomics,
        explanation=explanation,
    )
