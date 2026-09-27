from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pai_loop.models import CompanyFact
from pai_loop.quantitative_personnel import (
    PERSONNEL_METRIC_KEY,
    PERSONNEL_ROSTER_FACT_KEY,
    CompanyPersonnelMember,
    PersonnelRosterEnvelope,
    derive_personnel_value,
    load_personnel_roster,
    parse_personnel_recognition_scope,
    personnel_reference_time,
)
from pai_loop.quantitative_scoring import (
    bind_quantitative_company_inputs,
    estimate_for_notice,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
    resolve_personnel_register_facts,
    _public_quantitative_projection,
    _top_bracket_threshold,
)
from test_quantitative_auto_activation import _validated_profile


AS_OF = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)


def _member(key="SYN-1", **updates):
    row = {
        "member_key": key,
        "joined_on": "2025-03-10",
        "degrees": [{"level": "BACHELOR", "major": "SYN 경영"}],
        "credentials": [],
        "credentials_recorded": True,
        "research_grade": "RESEARCHER",
        "regular_employee": None,
    }
    row.update(updates)
    return row


def _envelope(**updates):
    value = {
        "schema_version": "pai-loop-personnel-roster-1.0",
        "source_sha256": "a" * 64,
        "snapshot_date": "2026-08-01",
        "verified_through": "2026-09-10",
        "verification_attestation": "HUMAN_REVIEWED_ROSTER_SNAPSHOT",
        "research_grade_basis": "CORRECTED_FINAL",
        "members": [_member()],
    }
    value.update(updates)
    return value


def _fact(value=None, **updates):
    attrs = {
        "fact_key": PERSONNEL_ROSTER_FACT_KEY,
        "value": _envelope() if value is None else value,
        "verified": True,
        "effective_from": datetime(2026, 7, 31, 15, tzinfo=timezone.utc),
        "effective_to": None,
    }
    attrs.update(updates)
    return SimpleNamespace(**attrs)


def _scope(literal="SYN 학사학위이상 보유인력", conditions=""):
    scope = parse_personnel_recognition_scope(
        literal, metric_key=PERSONNEL_METRIC_KEY, recognition_literal=conditions
    )
    assert scope is not None
    return scope


def _request(label="SYN 학사학위이상 보유인력"):
    return quantitative_request_from_candidate_profile(_validated_profile(
        metric="PERSONNEL_COUNT", unit="명", fact_key=PERSONNEL_METRIC_KEY, label=label
    ))


def test_json_envelope_preserves_degree_major_pairs_and_unknown_credentials():
    raw = _envelope(members=[_member(
        degrees=[{"level": "BACHELOR", "major": "SYN 교육"}, {"level": "MASTER", "major": None}],
        credentials_recorded=False,
    )])
    parsed = PersonnelRosterEnvelope.model_validate(raw)
    assert parsed.model_dump(mode="json")["members"] == raw["members"]
    assert parsed.members[0].degrees[1].major is None


@pytest.mark.parametrize("field", ["joined_on", "degrees", "credentials", "credentials_recorded", "member_key"])
def test_missing_member_fields_are_not_silently_defaulted(field):
    row = _member()
    del row[field]
    with pytest.raises(ValidationError):
        PersonnelRosterEnvelope.model_validate(_envelope(members=[row]))


@pytest.mark.parametrize("updates", [
    {"credentials_recorded": "false"},
    {"credentials_recorded": 1},
    {"joined_on": "2026-08-02"},
    {"joined_on": 0},
    {"degrees": [{"level": "MASTER"}]},
    {"degrees": [{"level": "MASTER", "major": " "}]},
    {"degrees": [{"level": "NONE", "major": None}]},
    {"degrees": [{"level": "MASTER", "major": "SYN"}] * 2},
    {"credentials": [""]},
    {"credentials": ["SYN", "SYN"]},
    {"birthdate": "1990-01-01"},
    {"tenure_months": 24},
])
def test_invalid_or_legacy_member_values_are_rejected(updates):
    with pytest.raises(ValidationError):
        PersonnelRosterEnvelope.model_validate(_envelope(members=[_member(**updates)]))


@pytest.mark.parametrize("updates", [
    {"snapshot_date": "2026-09-11"},
    {"source_sha256": "not-a-hash"},
    {"verification_attestation": "AUTOMATIC"},
    {"research_grade_basis": "ORIGINAL"},
    {"members": []},
    {"members": [_member(), _member()]},
    {"projection_through": "2026-09-20"},
    {"projection_assumption": "CURRENT_ROSTER_UNCHANGED"},
    {"projection_through": "2026-09-10", "projection_assumption": "CURRENT_ROSTER_UNCHANGED"},
])
def test_invalid_envelope_is_rejected(updates):
    with pytest.raises(ValidationError):
        PersonnelRosterEnvelope.model_validate(_envelope(**updates))


def test_one_attested_snapshot_loads_without_claiming_independent_proof():
    loaded = load_personnel_roster([_fact()], as_of=AS_OF)
    assert loaded.roster is not None
    assert loaded.projected is False
    derived = derive_personnel_value(_scope(), loaded.roster.members, as_of=AS_OF)
    assert derived.status == "ESTIMATED" and derived.value == 1
    assert "확정 점수가 아닙니다" in derived.rationale


@pytest.mark.parametrize("updates", [
    {"verified": False}, {"verified": 1}, {"effective_from": None},
    {"effective_from": AS_OF.replace(year=2027)},
    {"effective_to": AS_OF.replace(day=9)},
    {"effective_from": AS_OF.replace(day=11), "effective_to": AS_OF},
    {"effective_from": "2026-08-01"},
    {"value": {"members": [{"degree_level": "BACHELOR"}]}},
    {"value": _envelope(verified_through="2026-09-09")},
    {"value": _envelope(snapshot_date="2026-09-11", verified_through="2026-09-12")},
])
def test_untrusted_stale_future_or_legacy_roster_fails_closed(updates):
    loaded = load_personnel_roster([_fact(**updates)], as_of=AS_OF)
    assert loaded.roster is None
    assert loaded.reason
    assert "SYN-1" not in loaded.reason


def test_no_roster_or_foreign_fact_cannot_create_zero_count():
    assert load_personnel_roster([], as_of=AS_OF).roster is None
    assert load_personnel_roster([_fact(fact_key="SYN.other")], as_of=AS_OF).roster is None


@pytest.mark.parametrize("second", [_fact(), _fact(verified=False), _fact(value={"members": []})])
def test_overlapping_versions_never_merge_or_fall_back(second):
    result = load_personnel_roster([_fact(), second], as_of=AS_OF)
    assert result.roster is None and "중복" in result.reason


def test_expired_and_future_effective_versions_are_not_merged():
    expired = _fact(value={"legacy": True}, effective_to=AS_OF.replace(day=9))
    future = _fact(verified=False, effective_from=AS_OF.replace(day=11))
    loaded = load_personnel_roster([expired, _fact(), future], as_of=AS_OF)
    assert loaded.roster is not None and len(loaded.roster.members) == 1


def test_cutoff_uses_korean_calendar_and_naive_database_times_are_utc():
    instant = datetime(2026, 9, 9, 15)
    fact = _fact(_envelope(snapshot_date="2026-09-10"), effective_from=instant, effective_to=instant)
    assert load_personnel_roster([fact], as_of=instant).roster is not None
    assert load_personnel_roster([fact], as_of=instant.replace(hour=14, minute=59)).roster is None
    assert load_personnel_roster([fact], as_of=instant.replace(minute=1)).roster is None


def test_future_projection_is_opt_in_bounded_and_does_not_extend_attestation():
    future = AS_OF.replace(day=20)
    assert load_personnel_roster([_fact()], as_of=future).roster is None
    fact = _fact(_envelope(projection_through="2026-09-20", projection_assumption="CURRENT_ROSTER_UNCHANGED"))
    loaded = load_personnel_roster([fact], as_of=future)
    assert loaded.projected and loaded.roster is not None
    assert loaded.roster.verified_through == date(2026, 9, 10)
    assert "가정" in loaded.reason and "다시 확인" in loaded.reason
    assert load_personnel_roster([fact], as_of=future.replace(day=21)).roster is None
    fact.effective_to = AS_OF
    assert load_personnel_roster([fact], as_of=future).roster is None


def test_degree_floor_and_major_must_belong_to_same_completed_degree():
    scope = _scope("SYN 교육 관련학과 석사 이상 인력")
    mismatched = CompanyPersonnelMember.model_validate(_member(degrees=[
        {"level": "BACHELOR", "major": "교육학"},
        {"level": "MASTER", "major": "경영학"},
    ]))
    assert derive_personnel_value(scope, [mismatched], as_of=AS_OF).value == 0
    matched = mismatched.model_copy(update={"degrees": tuple(reversed(mismatched.degrees))})
    assert derive_personnel_value(scope, [matched], as_of=AS_OF).value == 0
    correct = CompanyPersonnelMember.model_validate(_member(degrees=[{"level": "MASTER", "major": "교육학"}]))
    assert derive_personnel_value(scope, [correct], as_of=AS_OF).value == 1


def test_recognition_conditions_cannot_lose_their_major_constraint():
    scope = _scope("SYN 석사 이상 보유 인력", "교육학 전공자, 4대보험 가입자 명부 제출")
    assert scope.major_keywords == ("교육학",)
    member = CompanyPersonnelMember.model_validate(_member(degrees=[{"level": "MASTER", "major": "경영학"}]))
    assert derive_personnel_value(scope, [member], as_of=AS_OF).value == 0
    for head, condition in [
        ("SYN 교육학 전공자 석사 이상 인력", "경영학 전공자"),
        ("SYN 학사학위이상 인력", "석사 이상"),
        ("SYN 석사 이상 인력", "연구원 이상"),
    ]:
        assert parse_personnel_recognition_scope(head, metric_key=PERSONNEL_METRIC_KEY, recognition_literal=condition) is None
    assert _scope("SYN 보유인력", "석사 이상 4대보험 가입자").degree_floor == "MASTER"


def test_unknown_major_or_grade_requires_review_even_above_top_bracket():
    unknown = CompanyPersonnelMember.model_validate(_member(degrees=[{"level": "MASTER", "major": None}]))
    known = CompanyPersonnelMember.model_validate(_member("SYN-2", degrees=[{"level": "MASTER", "major": "교육학"}]))
    result = derive_personnel_value(_scope("SYN 교육 관련학과 석사 이상 인력"), [unknown, known], as_of=AS_OF, sufficiency_value=1)
    assert result.status == "REVIEW" and result.value is None
    no_grade = CompanyPersonnelMember.model_validate(_member(research_grade=None))
    assert derive_personnel_value(_scope("SYN 연구원 이상 인력"), [no_grade], as_of=AS_OF, sufficiency_value=0).status == "REVIEW"


def test_explicit_no_completed_degree_is_known_zero_not_missing():
    member = CompanyPersonnelMember.model_validate(_member(degrees=[]))
    result = derive_personnel_value(_scope(), [member], as_of=AS_OF)
    assert result.status == "ESTIMATED" and result.value == 0


def test_tenure_is_completed_calendar_months_at_korean_cutoff():
    member = CompanyPersonnelMember.model_validate(_member(joined_on="2026-06-10"))
    scope = _scope(conditions="3개월 이상 재직")
    assert derive_personnel_value(scope, [member], as_of=datetime(2026, 9, 9, 14, 59, tzinfo=timezone.utc)).value == 0
    assert derive_personnel_value(scope, [member], as_of=datetime(2026, 9, 9, 15, tzinfo=timezone.utc)).value == 1
    assert derive_personnel_value(scope, [member], as_of=datetime(2026, 6, 9, tzinfo=timezone.utc)).status == "REVIEW"


def test_direct_derivation_also_rejects_duplicate_member_keys():
    member = CompanyPersonnelMember.model_validate(_member())
    assert derive_personnel_value(_scope(), [member, member], as_of=AS_OF).status == "REVIEW"


@pytest.mark.parametrize(("literal", "basis"), [
    ("입찰 공고일 기준", "PUBLICATION"), ("공고일 현재", "PUBLICATION"),
    ("입찰 마감일 기준", "DEADLINE"), ("", "DEADLINE"),
    ("2026.7.1. 기준", "FIXED_DATE"), ("2026년 7월 1일 기준", "FIXED_DATE"),
])
def test_explicit_date_basis_is_preserved(literal, basis):
    scope = _scope(conditions=literal)
    assert scope.reference_basis == basis
    if basis == "FIXED_DATE":
        assert scope.reference_date == date(2026, 7, 1)
        assert personnel_reference_time(scope, as_of=AS_OF).isoformat().startswith("2026-07-01T23:59:59")


@pytest.mark.parametrize("conditions", [
    "공고일 기준 및 마감일 기준", "2026.7.1. 기준 및 공고일 기준",
    "2026.7.1. 기준 또는 2026.8.1. 기준", "2026.2.30. 기준",
    "2026년 기준", "제출일 기준", "평가일 기준", "공고일 전일 기준", "공고일로부터",
    "연수 및 교육 관련 업무 경력자", "현재 기준",
])
def test_ambiguous_or_unmodelled_constraints_do_not_activate(conditions):
    assert parse_personnel_recognition_scope("SYN 학사학위이상 인력", metric_key=PERSONNEL_METRIC_KEY, recognition_literal=conditions) is None


def test_specialist_only_payroll_and_assigned_team_are_not_general_headcounts():
    for label, conditions in [
        ("SYN 교육전문 인력보유", "공고일 현재 고용 전문인력 재직증명"),
        ("SYN 보유인력", "4대보험과 사업수행인력 투입계획으로 평가"),
    ]:
        assert parse_personnel_recognition_scope(label, metric_key=PERSONNEL_METRIC_KEY, recognition_literal=conditions) is None


def test_publication_basis_resolves_tenure_and_requires_publication_time():
    request = _request("SYN 학사학위이상 보유인력 공고일 기준 3개월 이상 재직")
    fact = _fact(_envelope(members=[_member(joined_on="2026-06-10")]))
    missing = resolve_personnel_register_facts(request.criteria, [fact], as_of=AS_OF)
    assert missing[0].status == "REVIEW" and missing[0].value is None
    at_publication = resolve_personnel_register_facts(request.criteria, [fact], as_of=AS_OF, bid_notice_at=AS_OF.replace(day=9))
    assert at_publication[0].status == "ESTIMATED" and at_publication[0].value == 0
    deadline_request = _request("SYN 학사학위이상 보유인력 3개월 이상 재직")
    assert resolve_personnel_register_facts(deadline_request.criteria, [fact], as_of=AS_OF)[0].value == 1


def test_fixed_date_also_selects_the_snapshot_at_its_own_reference_time():
    request = _request("SYN 학사학위이상 보유인력 2026.7.1. 기준")
    assert resolve_personnel_register_facts(request.criteria, [_fact()], as_of=AS_OF)[0].status == "REVIEW"
    fact = _fact(_envelope(snapshot_date="2026-07-01"), effective_from=datetime(2026, 6, 30, 15, tzinfo=timezone.utc))
    assert resolve_personnel_register_facts(request.criteria, [fact], as_of=AS_OF)[0].value == 1


def test_validated_source_and_operator_roster_produce_estimated_not_confirmed_points(monkeypatch):
    profile = _validated_profile(metric="PERSONNEL_COUNT", unit="명", fact_key=PERSONNEL_METRIC_KEY, label="SYN 학사학위이상 보유인력")
    monkeypatch.setattr("pai_loop.quantitative_scoring._current_dynamic_quantitative_profile", lambda notice: profile)
    notice = SimpleNamespace(deadline=AS_OF, published_at=AS_OF.replace(day=1))
    raw = _envelope(members=[_member(f"SYN-{index}") for index in range(12)])
    fact = CompanyFact(fact_key=PERSONNEL_ROSTER_FACT_KEY, value=raw, verified=True, effective_from=datetime(2026, 8, 1, tzinfo=timezone.utc))
    result = estimate_for_notice(notice, [fact])
    assert result.confirmed_points == 0 and result.estimated_points == 20
    assert result.criteria[0].status == "ESTIMATED"
    assert "12명" in result.criteria[0].rationale
    assert fact.value == raw
    missing = estimate_for_notice(notice, [])
    assert missing.confirmed_points == 0 and missing.estimated_points is None
    assert missing.criteria[0].status == "REVIEW"


def test_projected_roster_stays_estimated_and_exposes_assumption():
    request = _request()
    raw = _envelope(projection_through="2026-09-20", projection_assumption="CURRENT_ROSTER_UNCHANGED", members=[_member(f"SYN-{i}") for i in range(12)])
    before = deepcopy(raw)
    bound = bind_quantitative_company_inputs(request, [_fact(raw)], as_of=AS_OF.replace(day=20))
    assert bound.facts[0].status == "ESTIMATED"
    assert "가정" in bound.facts[0].rationale and "다시 확인" in bound.facts[0].rationale
    result = estimate_quantitative_score(bound)
    assert result.confirmed_points == 0 and result.estimated_points == 20
    public = _public_quantitative_projection(result)
    assert public.criteria[0].status == "ESTIMATED"
    assert "현재 명부 유지 가정" in public.criteria[0].rationale
    assert "재확인" in public.criteria[0].rationale
    rendered = public.model_dump_json()
    assert "SYN-" not in rendered and "2026-09-10" not in rendered
    assert "12명" not in rendered and "source_sha256" not in rendered
    assert raw == before


@pytest.mark.parametrize(("updates", "expected"), [
    ({"min_inclusive": False}, 11),
    ({"min_value": 10.5}, 11),
    ({"max_value": 20}, None),
    ({"points": 15}, None),
])
def test_credential_lower_bound_requires_an_unbounded_maximum_points_bracket(updates, expected):
    criterion = _request().criteria[0]
    brackets = list(criterion.brackets)
    top_index = next(i for i, bracket in enumerate(brackets) if bracket.max_value is None)
    brackets[top_index] = brackets[top_index].model_copy(update=updates)
    altered = criterion.model_copy(update={"brackets": brackets})
    assert _top_bracket_threshold(altered) == expected


def test_inactive_source_does_not_gain_points_from_a_complete_roster():
    request = _request().model_copy(update={"activation_status": "REVIEW_REQUIRED"})
    assert bind_quantitative_company_inputs(request, [_fact()], as_of=AS_OF) is request
