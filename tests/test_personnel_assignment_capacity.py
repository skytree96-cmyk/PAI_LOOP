"""An operator capacity scenario is never an actual team assignment."""

import pytest

from pai_loop.quantitative_personnel import (
    CompanyPersonnelMember, derive_personnel_value, parse_personnel_recognition_scope,
)
from pai_loop.quantitative_scoring import resolve_personnel_register_facts
from tools.prepare_personnel_roster import normalize_roster_rows
from test_personnel_roster_preparation import _kwargs, _rows
from test_quantitative_personnel_binding import AS_OF, _envelope, _fact, _member, _request


def _scope(extra="", **kwargs):
    return parse_personnel_recognition_scope(
        "SYN 학사학위이상 참여인력", metric_key="company.personnel.count",
        recognition_literal="정규직 재직증명서 제출 " + extra, **kwargs,
    )


def test_assignment_is_not_enabled_by_default():
    assert _scope() is None


def test_capacity_requires_separate_derivation_opt_in_and_keeps_qualifications():
    scope = _scope(allow_assignment_capacity=True)
    assert scope.population_basis == "ASSIGNED_CAPACITY"
    members = [CompanyPersonnelMember.model_validate(_member(regular_employee=True))]
    assert derive_personnel_value(scope, members, as_of=AS_OF).status == "REVIEW"
    members.append(CompanyPersonnelMember.model_validate(
        _member("SYN-NO-DEGREE", degrees=[], regular_employee=True),
    ))
    result = derive_personnel_value(scope, members, as_of=AS_OF, allow_assignment_capacity=True)
    assert result.status == "ESTIMATED" and result.value == 1
    assert "투입 가능 가정" in result.rationale and "실제 배정 아님" in result.rationale


@pytest.mark.parametrize("extra", [
    "관련 경력자", "투입률 50%", "중복참여 불가", "상주 인원", "공고일 전일 기준",
    "만 40세 이하만 인정", "최근 3년간 유사사업 2건 수행자만 인정",
    "별첨 특별요건 충족자만 인정", "SYN 추가 미모델 조건",
])
def test_assignment_opt_in_does_not_waive_other_unmodeled_conditions(extra):
    assert _scope(extra, allow_assignment_capacity=True) is None


def test_unknown_restriction_in_heading_is_also_rejected():
    assert parse_personnel_recognition_scope(
        "SYN 학사학위이상 만 40세 이하 참여인력",
        metric_key="company.personnel.count", allow_assignment_capacity=True,
    ) is None


@pytest.mark.parametrize("degree", ["박사 미만", "박사 이하", "박사 초과", "책임연구원 미만"])
def test_band_comparators_cannot_reverse_degree_or_grade_population(degree):
    assert parse_personnel_recognition_scope(
        f"SYN {degree} 참여인력", metric_key="company.personnel.count",
        allow_assignment_capacity=True,
    ) is None


def test_roster_assumption_is_optional_and_explicit():
    assert normalize_roster_rows(_rows(), **_kwargs())["assignment_assumption"] is None
    payload = normalize_roster_rows(
        _rows(), **_kwargs(), confirm_all_qualified_staff_available=True,
    )
    assert payload["assignment_assumption"] == "ALL_QUALIFIED_ROSTER_MEMBERS_AVAILABLE"


def test_resolver_requires_current_verified_roster_assumption():
    request = _request(label="SYN 학사학위이상 참여인력")
    assert request.criteria[0].personnel_scope.population_basis == "ASSIGNED_CAPACITY"
    result = resolve_personnel_register_facts(request.criteria, [_fact()], as_of=AS_OF)
    assert result[0].status == "REVIEW" and result[0].value is None
    fact = _fact(_envelope(assignment_assumption="ALL_QUALIFIED_ROSTER_MEMBERS_AVAILABLE"))
    result = resolve_personnel_register_facts(request.criteria, [fact], as_of=AS_OF)
    assert result[0].status == "ESTIMATED" and result[0].value == 1
    fact.verified = False
    assert resolve_personnel_register_facts(request.criteria, [fact], as_of=AS_OF)[0].status == "REVIEW"
