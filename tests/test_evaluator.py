from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pai_loop.enums import Eligibility, ReadinessStatus, RiskBand
from pai_loop.evaluator import compare, evaluate_notice
from pai_loop.models import AtomicRequirement, CompanyFact, Evidence, Notice, NoticeVersion

DEADLINE = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


def evidence(
    *,
    key: str = "E-1",
    issued_at: datetime | None = datetime(2025, 1, 1, tzinfo=timezone.utc),
    valid_from: datetime | None = datetime(2025, 1, 1, tzinfo=timezone.utc),
    valid_until: datetime | None = datetime(2027, 1, 1, tzinfo=timezone.utc),
    status: str = "VERIFIED",
) -> Evidence:
    return Evidence(
        evidence_key=key,
        name="synthetic",
        evidence_type="TEST",
        status=status,
        issued_at=issued_at,
        valid_from=valid_from,
        valid_until=valid_until,
    )


def fact(
    key: str,
    value: object,
    *,
    linked_evidence: Evidence | None = None,
    effective_from: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_to: datetime | None = None,
) -> CompanyFact:
    return CompanyFact(
        fact_key=key,
        value=value,
        effective_from=effective_from,
        effective_to=effective_to,
        evidence=linked_evidence,
        verified=linked_evidence is not None,
    )


def requirement(
    key: str,
    fact_key: str,
    expected: object,
    *,
    group: str = "G-1",
    path: str = "P-1",
    operator: str = "eq",
    pass_rule: str = "P-ENTITY",
    review: str | None = None,
    review_trigger: object | None = None,
    evidence_required: bool = True,
    confidence: float = 1.0,
) -> AtomicRequirement:
    return AtomicRequirement(
        requirement_key=key,
        group_key=group,
        path_key=path,
        sequence=1,
        label=f"condition {key}",
        fact_key=fact_key,
        operator=operator,
        required_value=expected,
        evidence_required=evidence_required,
        mandatory=True,
        pass_rule_id=pass_rule,
        linked_review_code=review,
        review_trigger_value=review_trigger,
        parse_confidence=confidence,
        active=True,
    )


def notice_and_version(*, complete: bool = True, confidence: float = 1.0) -> tuple[Notice, NoticeVersion]:
    notice = Notice(
        notice_key="T-SYN",
        bid_notice_no="SYN-1",
        revision_no="00",
        title="synthetic",
        agency="synthetic",
        deadline=DEADLINE,
        status="OPEN",
        risk_dimensions={
            "qualification": 20,
            "execution": 20,
            "competition": 20,
            "profitability": 20,
            "operation": 20,
            "document": 20,
        },
    )
    version = NoticeVersion(
        version_no=1,
        file_sha256="a" * 64,
        document_complete=complete,
        extraction_status="COMPLETE" if complete else "INCOMPLETE",
        extraction_confidence=confidence,
    )
    notice.versions.append(version)
    return notice, version


@pytest.mark.parametrize(
    ("actual", "operator", "expected", "result"),
    [
        ("Active  Company", "eq", "active company", True),
        ("3144", "in", ["3144", "3244"], True),
        (["A", "B"], "contains", "b", True),
        (10, "gte", 9, True),
        (10, "lte", 9, False),
        (None, "exists", True, False),
    ],
)
def test_operator_comparison(actual: object, operator: str, expected: object, result: bool) -> None:
    assert compare(actual, operator, expected) is result


def test_pass_requires_matching_fact_and_valid_deadline_evidence() -> None:
    notice, version = notice_and_version()
    req = requirement("Q-1", "entity", "active")
    result = evaluate_notice(notice, version, [req], [fact("entity", "active", linked_evidence=evidence())])

    assert result.eligibility == Eligibility.PASS
    assert result.reason_code == "PASS_MATCH"
    assert result.readiness_status == ReadinessStatus.GREEN
    assert result.evidence_coverage == 100
    assert result.risk_score == 20
    assert result.risk_band == RiskBand.GO


def test_current_fact_is_not_applied_retroactively_to_old_deadline() -> None:
    notice, version = notice_and_version()
    req = requirement(
        "T-002",
        "industry_code",
        "3144",
        review="R06",
        review_trigger="__MISSING__",
    )
    acquired_after_deadline = fact(
        "industry_code",
        "3144",
        linked_evidence=evidence(),
        effective_from=DEADLINE + timedelta(days=1),
    )
    result = evaluate_notice(notice, version, [req], [acquired_after_deadline])

    assert result.eligibility == Eligibility.REVIEW
    assert result.atomic_results[0]["reason_code"] == "R06"
    assert result.atomic_results[0]["actual_value"] is None


def test_matching_fact_without_evidence_is_r04_not_unsupported_pass() -> None:
    notice, version = notice_and_version()
    req = requirement("Q-1", "license", "valid")
    result = evaluate_notice(notice, version, [req], [fact("license", "valid")])

    assert result.eligibility == Eligibility.REVIEW
    assert result.atomic_results[0]["reason_code"] == "R04"
    assert result.evidence_coverage == 0
    assert result.readiness_status == ReadinessStatus.RED


def test_unknown_evidence_date_is_r06() -> None:
    notice, version = notice_and_version()
    req = requirement("Q-1", "license", "valid")
    undated = evidence(issued_at=None, valid_from=None, valid_until=None)
    result = evaluate_notice(notice, version, [req], [fact("license", "valid", linked_evidence=undated)])

    assert result.eligibility == Eligibility.REVIEW
    assert result.atomic_results[0]["reason_code"] == "R06"


def test_expired_evidence_never_passes() -> None:
    notice, version = notice_and_version()
    req = requirement("Q-1", "license", "valid")
    expired = evidence(valid_until=DEADLINE - timedelta(days=1))
    result = evaluate_notice(notice, version, [req], [fact("license", "valid", linked_evidence=expired)])

    assert result.eligibility == Eligibility.REVIEW
    assert result.atomic_results[0]["reason_code"] == "R04"


def test_document_incomplete_or_low_confidence_is_r07_and_gray() -> None:
    notice, version = notice_and_version(complete=False, confidence=0.5)
    req = requirement("T-003", "industry_code", "3144")
    result = evaluate_notice(
        notice, version, [req], [fact("industry_code", "3144", linked_evidence=evidence())]
    )

    assert result.eligibility == Eligibility.REVIEW
    assert result.atomic_results[0]["reason_code"] == "R07"
    assert result.readiness_status == ReadinessStatus.GRAY


def test_atomic_order_uses_only_linked_review_then_default_fail() -> None:
    notice, version = notice_and_version()
    linked = requirement(
        "T-018-A", "direct_cert", "held", review="R01", review_trigger="not_held"
    )
    no_link = requirement("T-011", "industry_code", "3144", group="G-2")
    facts = [
        fact("direct_cert", "not_held", linked_evidence=evidence(key="E-1")),
        fact("industry_code", "9999", linked_evidence=evidence(key="E-2")),
    ]
    result = evaluate_notice(notice, version, [linked, no_link], facts)

    assert result.atomic_results[0]["result"] == "REVIEW"
    assert result.atomic_results[0]["reason_code"] == "R01"
    assert result.atomic_results[1]["result"] == "FAIL"
    assert result.eligibility == Eligibility.FAIL  # REVIEW must not hide another mandatory FAIL.
    assert result.evidence_coverage == 100  # Negative evidence coverage is separate from eligibility.
    assert result.readiness_status == ReadinessStatus.GREEN
    detail = result.explanation["default_fail_details"][0]
    assert set(detail) == {
        "failed_condition",
        "required_value",
        "current_value",
        "unmatched_pass_paths",
        "review_not_matched_reason",
    }


def test_or_group_accepts_one_complete_pass_path() -> None:
    notice, version = notice_and_version()
    failed_path = requirement("Q-A", "code_a", "A", group="G-OR", path="PATH-A")
    passed_path = requirement("Q-B", "code_b", "B", group="G-OR", path="PATH-B")
    facts = [
        fact("code_a", "X", linked_evidence=evidence(key="E-A")),
        fact("code_b", "B", linked_evidence=evidence(key="E-B")),
    ]
    result = evaluate_notice(notice, version, [failed_path, passed_path], facts)

    assert result.eligibility == Eligibility.PASS
    group = result.explanation["group_results"][0]
    assert group["result"] == "PASS"


def test_and_path_fail_precedes_review_and_pass() -> None:
    notice, version = notice_and_version()
    reqs = [
        requirement("Q-P", "pass", True, path="PATH-AND"),
        requirement("Q-R", "review", "yes", path="PATH-AND", review="R05", review_trigger="ambiguous"),
        requirement("Q-F", "fail", True, path="PATH-AND"),
    ]
    facts = [
        fact("pass", True, linked_evidence=evidence(key="E-P")),
        fact("review", "ambiguous", linked_evidence=evidence(key="E-R")),
        fact("fail", False, linked_evidence=evidence(key="E-F")),
    ]
    result = evaluate_notice(notice, version, reqs, facts)

    assert result.eligibility == Eligibility.FAIL
    assert result.explanation["group_results"][0]["paths"][0]["result"] == "FAIL"


@pytest.mark.parametrize("code", ["R01", "R02", "R03", "R05", "R09"])
def test_linked_business_review_codes_are_expressible(code: str) -> None:
    notice, version = notice_and_version()
    req = requirement(
        f"Q-{code}", "state", "pass", review=code, review_trigger=f"trigger-{code}"
    )
    result = evaluate_notice(
        notice,
        version,
        [req],
        [fact("state", f"trigger-{code}", linked_evidence=evidence())],
    )
    assert result.eligibility == Eligibility.REVIEW
    assert result.atomic_results[0]["reason_code"] == code
