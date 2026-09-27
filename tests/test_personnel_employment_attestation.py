from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from pai_loop.quantitative_personnel import (
    CompanyPersonnelMember, derive_personnel_value, parse_personnel_recognition_scope,
)
from tools.prepare_personnel_roster import normalize_roster_rows, roster_summary
from test_personnel_roster_preparation import _kwargs, _rows


AS_OF = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _member(key, regular, **updates):
    return CompanyPersonnelMember.model_validate({
        "member_key": key, "joined_on": "2020-01-01", "degrees": [],
        "credentials": [], "credentials_recorded": True,
        "regular_employee": regular, **updates,
    })


def _scope(head="SYN 보유 인력", conditions="정규 직원의 재직증명서 제출"):
    return parse_personnel_recognition_scope(
        head, metric_key="company.personnel.count", recognition_literal=conditions,
    )


@pytest.mark.parametrize("label", ["정규직", "정규 직원", "정규 근로자"])
def test_regular_employment_requirement_is_retained(label):
    scope = _scope(conditions=f"{label} 재직증명서 제출")
    assert scope is not None and scope.regular_employment_required


@pytest.mark.parametrize("conditions", [
    "정규직 및 비정규직의 재직증명서", "비정규직의 재직증명서",
    "정규직 또는 계약직의 재직증명서", "정규직 여부 무관 재직증명서",
    "정규직 제외, 재직증명서 제출", "정규직 외 인력의 재직증명서",
    "정규직 여부 관계없이 재직증명서 제출", "정규직을 제외한 재직 인력",
    "정규직이 아닌 인력의 재직증명서",
])
def test_mixed_or_unrestricted_employment_is_not_treated_as_regular_only(conditions):
    assert _scope(conditions=conditions) is None


def test_only_attested_regular_members_are_counted():
    result = derive_personnel_value(_scope(), [
        _member("SYN-REGULAR", True), _member("SYN-NONREGULAR", False),
    ], as_of=AS_OF)
    assert result.status == "ESTIMATED" and result.value == 1
    assert "정규직" in result.rationale


def test_unknown_employment_is_not_inferred_from_employment_or_insurance():
    result = derive_personnel_value(_scope(), [_member("SYN-UNKNOWN", None)], as_of=AS_OF)
    assert result.status == "REVIEW" and result.value is None


def test_employment_unknown_does_not_block_an_independently_excluded_member():
    scope = _scope("SYN 석사 이상 인력")
    result = derive_personnel_value(scope, [_member("SYN-NO-DEGREE", None)], as_of=AS_OF)
    assert result.status == "ESTIMATED" and result.value == 0


def test_degree_and_regular_requirement_both_apply():
    scope = _scope("SYN 석사 이상 인력")
    degree = [{"level": "MASTER", "major": "SYN 경영"}]
    assert derive_personnel_value(scope, [
        _member("SYN-UNKNOWN", None, degrees=degree),
    ], as_of=AS_OF).status == "REVIEW"
    result = derive_personnel_value(scope, [
        _member("SYN-REGULAR", True, degrees=degree),
        _member("SYN-NONREGULAR", False, degrees=degree),
    ], as_of=AS_OF)
    assert result.value == 1


def test_payroll_without_regular_restriction_does_not_invent_one():
    scope = _scope(conditions="재직증명서 제출")
    assert not scope.regular_employment_required
    assert derive_personnel_value(scope, [_member("SYN-UNKNOWN", None)], as_of=AS_OF).value == 1


@pytest.mark.parametrize("value", ["true", "false", 1, 0])
def test_regular_status_requires_a_real_boolean(value):
    with pytest.raises(ValidationError):
        _member("SYN-BAD", value)


def test_optional_preparation_attestations_do_not_change_default_unknowns():
    result = normalize_roster_rows(_rows(), **_kwargs())
    assert all(row["regular_employee"] is None for row in result["members"])
    assert not result["members"][1]["credentials_recorded"]


def test_explicit_attestations_record_regular_status_and_empty_credentials():
    result = normalize_roster_rows(
        _rows(), **_kwargs(), confirm_all_regular_employees=True,
        confirm_blank_credentials_none=True,
    )
    assert all(row["regular_employee"] is True for row in result["members"])
    assert all(row["credentials_recorded"] for row in result["members"])
    assert result["members"][1]["credentials"] == []
    assert result["members"][0]["credentials"] == ["평생교육사", "SYN 자격"]
    summary = roster_summary(result)
    assert summary["regular_employee_count"] == 2
    assert summary["unknown_employment_type_count"] == 0
    assert summary["unknown_credential_count"] == 0
