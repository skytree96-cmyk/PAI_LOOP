"""Personnel derivation, pinned against the operator roster.

Every criterion literal here is verbatim production text. The roster fixture
reproduces the real distribution of KMA_인력정보_260810 (154 members): 학사급
118 / 석사급 34 / 박사 1 / 고졸 1, 학술연구용역등급 책임연구원 26 · 연구원 28 ·
연구보조원 100, 자격증 기재 97.

Note 석사 이상 is 35, not 37: ``석사(수료)`` holds a bachelor's and
``박사(수료)`` holds a master's. Counting a candidate as a degree holder would
overstate the company in a bid document.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pai_loop.quantitative_personnel import (
    PERSONNEL_METRIC_KEY,
    CompanyPersonnelMember,
    derive_personnel_value,
    parse_personnel_recognition_scope,
)

AS_OF = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _member(
    degree: str,
    *,
    grade: str | None = None,
    majors: tuple[str, ...] = (),
    credentials: tuple[str, ...] = (),
    recorded: bool = True,
    tenure: int = 24,
) -> CompanyPersonnelMember:
    return CompanyPersonnelMember(
        degree_level=degree,
        research_grade=grade,
        majors=majors,
        credentials=credentials,
        credentials_recorded=recorded,
        tenure_months=tenure,
    )


def _roster() -> list[CompanyPersonnelMember]:
    """154 members, matching the real distribution."""

    members: list[CompanyPersonnelMember] = []
    # 학위: 118 bachelor-level (116 학사 + 2 석사수료), 34 master-level
    # (30 석사 + 4 박사수료), 1 doctorate, 1 고졸.
    levels = ["BACHELOR"] * 118 + ["MASTER"] * 34 + ["DOCTORATE"] + ["NONE"]
    # 학술연구용역등급: 26 / 28 / 100.
    grades = (
        ["LEAD_RESEARCHER"] * 26 + ["RESEARCHER"] * 28 + ["RESEARCH_ASSISTANT"] * 100
    )
    for index, (level, grade) in enumerate(zip(levels, grades)):
        # 15 of the master-level members hold an 교육 major.
        majors = ("교육공학",) if 118 <= index < 133 else ("경영학",)
        # 97 members have a credential on file; 9 of them 평생교육사.
        recorded = index < 97
        credentials = ("평생교육사 2급",) if index < 9 else ("컴퓨터활용능력 2급",)
        members.append(
            _member(
                level,
                grade=grade,
                majors=majors,
                credentials=credentials if recorded else (),
                recorded=recorded,
                # 143 members have 6 months or more of tenure.
                tenure=12 if index < 143 else 2,
            )
        )
    return members


def scope_for(literal: str, conditions: str = ""):
    return parse_personnel_recognition_scope(
        literal, metric_key=PERSONNEL_METRIC_KEY, recognition_literal=conditions
    )


def test_the_roster_fixture_matches_the_real_headcount() -> None:
    roster = _roster()
    assert len(roster) == 154
    assert sum(1 for m in roster if m.degree_level != "NONE") == 153
    assert sum(1 for m in roster if m.degree_level in ("MASTER", "DOCTORATE")) == 35
    assert sum(1 for m in roster if m.degree_level == "DOCTORATE") == 1
    assert sum(1 for m in roster if m.research_grade == "LEAD_RESEARCHER") == 26
    assert sum(1 for m in roster if m.credentials_recorded) == 97


def test_a_bachelor_floor_counts_every_degree_holder() -> None:
    """교육전문인력 보유현황(학사학위이상) Ⓐ15명이상 — the company clears it."""

    scope = scope_for("교육전문인력 보유현황(학사학위이상) Ⓐ15명이상 Ⓑ15명미만~12명이상")
    assert scope is not None and scope.basis == "DEGREE"
    assert scope.degree_floor == "BACHELOR"
    derived = derive_personnel_value(scope, _roster(), as_of=AS_OF)
    assert derived.status == "ESTIMATED"
    assert derived.value == 153


def test_a_master_floor_with_a_major_filter_narrows_the_count() -> None:
    """전문기술인력 확보 현황 : 교육 및 관련학과 학위소유자(석사 이상)."""

    scope = scope_for(
        "‣ 전문기술인력 확보 현황 : 교육 및 관련학과 학위소유자(석사 이상)"
        " (5명이상: 7점 / 4-3명: 6점 / 2명이하: 5점)",
        "- 고용사실을 증명할 수 있는 서류 (4대보험 중 1개 이상 가입 증빙자료)",
    )
    assert scope is not None and scope.degree_floor == "MASTER"
    assert scope.major_keywords == ("교육",)
    derived = derive_personnel_value(scope, _roster(), as_of=AS_OF)
    # 15 master-level members hold an 교육 major, not all 35.
    assert derived.value == 15


def test_a_candidate_is_not_counted_as_a_degree_holder() -> None:
    """박사(수료) holds a master's, so a doctorate floor sees one person."""

    scope = scope_for("박사 학위 소지자 보유 현황")
    assert scope is not None and scope.degree_floor == "DOCTORATE"
    derived = derive_personnel_value(scope, _roster(), as_of=AS_OF)
    assert derived.value == 1


def test_a_row_accepting_either_degree_takes_the_lower_floor() -> None:
    scope = scope_for("박사 또는 석사 학위소지자 보유현황")
    assert scope is not None and scope.degree_floor == "MASTER"
    assert derive_personnel_value(scope, _roster(), as_of=AS_OF).value == 35


def test_a_research_grade_row_counts_that_grade() -> None:
    """``참여연구원`` would be the assigned team; ``보유`` is the payroll."""

    scope = scope_for("학술연구용역등급 기준 책임연구원 보유 현황")
    assert scope is not None and scope.basis == "RESEARCH_GRADE"
    assert scope.research_grade_floor == "LEAD_RESEARCHER"
    assert derive_personnel_value(scope, _roster(), as_of=AS_OF).value == 26


def test_researcher_or_above_includes_the_lead_grade() -> None:
    scope = scope_for("연구원 이상 인력 보유현황")
    assert scope is not None and scope.research_grade_floor == "RESEARCHER"
    assert derive_personnel_value(scope, _roster(), as_of=AS_OF).value == 54


def test_payroll_evidence_makes_an_unconditional_row_derivable() -> None:
    """제안업체 인력 보유상태 with 4대보험 명부 asks about the payroll."""

    scope = scope_for(
        "3. 제안업체 인력 보유상태(5점)", "※ 4대보험 가입자 명부 제출 시에만 인정"
    )
    assert scope is not None and scope.basis == "PAYROLL"
    assert derive_personnel_value(scope, _roster(), as_of=AS_OF).value == 154


def test_a_tenure_condition_excludes_recent_joiners() -> None:
    scope = scope_for(
        "제안업체 인력 보유상태",
        "입찰 공고일 기준 4대 보험 증빙자료 기준 3개월 이상 근무한 자에 한함",
    )
    assert scope is not None and scope.minimum_tenure_months == 3
    assert derive_personnel_value(scope, _roster(), as_of=AS_OF).value == 143


@pytest.mark.parametrize(
    ("literal", "conditions"),
    [
        # Scores the team assigned to this one contract.
        ("제안업체 인력 보유상태", "[서식 2] 사업수행인력 투입계획에 따라 평가함"),
        ("제안업체 인력 보유상태", "붙임 2) 참여인원 총괄표에 작성된 인원만 산정함."),
        ("본 과업에 직접 참여하는 인력 수", ""),
        ("사업수행 참여인력 투입계획", ""),
        # Caught scoring the corpus: each of these read as a payroll row until
        # the wording of its own assignment phrase was added.
        ("(3) 인력 보유 현황 (6점) ∙ 사업참여 전문 인력 보유 현황", "4대보험 가입증명"),
        (
            "제안사 1년 이상 재직 중인 인력 중, 유사 경력 2년 이상인 참여인원 현황",
            "재직증명서",
        ),
        (
            "제안업체의 수행인력 참여현황 - 총괄책임자(책임연구원급) 각 1.5점",
            "",
        ),
        # No recognition rule binds the count to the payroll.
        ("3. 제안업체 인력 보유 상태(5점)", ""),
        ("전문인력 보유 현황", "(상주인력만 해당)"),
        ("", "※ 4대보험 가입자 명부 제출 시에만 인정"),
    ],
)
def test_a_bid_side_or_unbound_row_stays_manual(literal: str, conditions: str) -> None:
    assert scope_for(literal, conditions) is None


def test_a_row_naming_both_a_credential_and_a_degree_stays_manual() -> None:
    assert scope_for("석사 이상 학위소지자 또는 평생교육사 보유 현황") is None


def test_a_foreign_metric_key_is_refused() -> None:
    assert (
        parse_personnel_recognition_scope(
            "교육전문인력 보유현황(학사학위이상)",
            metric_key="company.financial.ratio",
        )
        is None
    )


def test_a_credential_short_of_the_top_bracket_is_only_a_lower_bound() -> None:
    """국외여행인솔자 자격증 소지자 — 57 blank cells make 0 a floor, not a count."""

    scope = scope_for(
        "2026.7.1. 기준으로 6개월 이상 근무한 자 중, 국외여행인솔자자격증 소지인원수"
    )
    assert scope is not None and scope.basis == "CREDENTIAL"
    assert scope.credential_keywords == ("국외여행인솔자",)
    assert scope.minimum_tenure_months == 6
    derived = derive_personnel_value(
        scope, _roster(), as_of=AS_OF, sufficiency_value=5
    )
    assert derived.status == "REVIEW"
    assert derived.value is None
    assert "미기재" in derived.rationale


def test_a_credential_already_at_the_top_bracket_is_scored() -> None:
    """평생교육사 9명 clears a 5명 이상 top bracket whatever the blanks hold."""

    scope = scope_for("평생교육사 자격 소지 인원수 (5명 이상 5점)")
    assert scope is not None and scope.credential_keywords == ("평생교육사",)
    derived = derive_personnel_value(
        scope, _roster(), as_of=AS_OF, sufficiency_value=5
    )
    assert derived.status == "ESTIMATED"
    assert derived.value == 9


def test_no_roster_refuses_rather_than_guesses() -> None:
    scope = scope_for("교육전문인력 보유현황(학사학위이상)")
    assert scope is not None
    derived = derive_personnel_value(scope, [], as_of=AS_OF)
    assert derived.status == "REVIEW"
    assert derived.value is None
