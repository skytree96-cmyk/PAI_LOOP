"""SYN company-first beta: company facts applied to printed rows, lowest row when unmet."""
from datetime import date, datetime, timezone

import pytest

from pai_loop import quantitative_company_first as beta
from pai_loop.integrations.openai_extraction import QuantitativeRuleCandidate
from pai_loop.quantitative_personnel import CompanyPersonnelMember, PersonnelDegree
from test_quantitative_row_approval import _payload as hyphen_payload
from test_quantitative_sufficient_row import (
    _estimate, _performance_payload, _records, _store,
)

DEADLINE = datetime(2030, 1, 31, 9, tzinfo=timezone.utc)


@pytest.fixture()
def beta_on(monkeypatch):
    monkeypatch.setenv("PAI_QUANT_COMPANY_FIRST_BETA", "true")


def _ev(quote):
    return {"attachment_id": "PPS-ATT-" + "f" * 24, "page": 1, "section": "SYN", "quote": quote[:500], "confidence": 1}


def _raw(label, metric, cases=(), brackets=(), *, max_points=10, unit=None, literal=None, conditions=()):
    literal = literal or label
    return QuantitativeRuleCandidate.model_validate({
        "criterion_id": "SYN-ROW", "label": label, "criterion_literal": literal, "max_points": max_points,
        "scoring_method": "CASE_TABLE" if cases else "BRACKET", "metric": metric, "unit": unit,
        "brackets": [{"label": text, "literal": text, "min_value": low, "max_value": high, "min_inclusive": True,
                      "max_inclusive": False, "points": points, "evidence": _ev(text)}
                     for text, low, high, points in brackets],
        "threshold": None, "formula_literal": None,
        "cases": [{"literal": text, "operator": op, "comparison_value": value, "comparison_upper_value": upper,
                   "category_values": list(categories), "award_kind": "POINTS", "award_value": points,
                   "row_order": order, "evidence": _ev(text)}
                  for order, (text, op, value, upper, categories, points) in enumerate(cases, start=1)],
        "recognition_conditions": [{"literal": text, "evidence": _ev(text)} for text in conditions],
        "required_evidence": [], "evidence": _ev(literal), "ambiguity_reason": None,
    })


CREDIT_ROWS = (
    ("AAA ~ -A 4점", "IN", None, None, ("AAA ~ -A",), 4.0),
    ("BBB+ ~ BB0 3점", "IN", None, None, ("BBB+ ~ BB0",), 3.0),
    ("CCC+ 이하 1점", "IN", None, None, ("CCC+ 이하",), 1.0),
    ("A1 ~ A2- 4점", "IN", None, None, ("A1 ~ A2-",), 4.0),
)


def _score(raw, **kwargs):
    defaults = dict(deadline=DEADLINE, published_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
                    credit_grade="A0", roster=None, records=(), statement=None)
    return beta.beta_score(raw, **{**defaults, **kwargs})


def test_credit_grade_takes_the_row_holding_it_even_when_the_source_is_loosely_printed():
    score = _score(_raw("경영상태 - 기업신용평가등급", "CREDIT_RATING", CREDIT_ROWS, max_points=4))
    assert score.points == 4 and not score.floor_applied
    low = _score(_raw("경영상태", "CREDIT_RATING", CREDIT_ROWS, max_points=4), credit_grade="CCC0")
    assert low.points == 1 and not low.floor_applied


def test_commercial_paper_rows_are_not_read_as_enterprise_grades():
    rows = (("A1 ~ A2- 4점", "IN", None, None, ("A1 ~ A2-",), 4.0),
            ("C 이하 1점", "IN", None, None, ("C 이하",), 1.0))
    score = _score(_raw("경영상태", "CREDIT_RATING", rows, max_points=4))
    assert score.points == 1 and score.floor_applied
    assert beta.COMPANY_FIRST_FLOOR_NOTE == "미충족 → 최하점 적용(하한)"


def test_sanction_clear_row_or_full_points_when_only_penalties_are_printed():
    clear = _raw("부정당제재", "UNKNOWN", (("해당 없을 시 2", "IN", None, None, ("해당 없음",), 2.0),
                                         ("제재 1회 0", "IN", None, None, ("1회",), 0.0)), max_points=2)
    assert _score(clear).points == 2
    penalties = _raw("신인도", "UNKNOWN", (
        ("입찰 참가자격제한 사실이 있는 경우 0", "IN", None, None, ("제한",), 0.0),), max_points=2)
    score = _score(penalties)
    assert score.points == 2 and not score.floor_applied and "만점" in score.row_literal


def test_value_beyond_an_ascending_lumped_table_takes_the_top_row():
    rows = tuple((f"구분(회) {n}", "EQ", float(n), None, (), 6.0 + n) for n in range(5))
    raw = _raw("사업실적", "PERFORMANCE_COUNT", rows, unit="건")
    records = [type("R", (), dict(record_status="VALIDATED", completed=True, end_date=date(2029, 6, 1),
                                  gross_contract_amount_krw=200_000_000, contract_amount=None, share_pct=100.0))()
               for _ in range(12)]
    score = _score(raw, records=records)
    assert score.points == 10 and "12건" in score.basis


def test_performance_counts_only_the_printed_period_and_single_contract_amount():
    rows = (("5건 이상 10", "GTE", 5.0, None, (), 10.0), ("3건 미만 1", "LT", 3.0, None, (), 1.0))
    raw = _raw("유사사업 수행실적", "PERFORMANCE_COUNT", rows, unit="건",
               literal="최근 3년 이내 단일 계약 1억원 이상 실적")

    def record(amount, ended):
        return type("R", (), dict(record_status="VALIDATED", completed=True, end_date=ended,
                                  gross_contract_amount_krw=amount, contract_amount=None, share_pct=100.0))()

    records = [record(150_000_000, date(2029, 1, 1))] * 2 + [record(50_000_000, date(2029, 1, 1))] * 5 \
        + [record(150_000_000, date(2020, 1, 1))] * 5
    score = _score(raw, records=records)
    assert "2건" in score.basis and score.points == 1 and not score.floor_applied  # printed "3건 미만" row


def _member(key, *, joined, degree=None, regular=True):
    degrees = (PersonnelDegree(level=degree, major="경영학"),) if degree else ()
    return CompanyPersonnelMember(member_key=key, joined_on=joined, degrees=degrees, credentials=(),
                                  credentials_recorded=True, research_grade=None, regular_employee=regular)


def test_personnel_applies_every_roster_condition_and_unverifiable_ones_count_nobody():
    roster = [_member("M1", joined=date(2020, 1, 1), degree="MASTER"),
              _member("M2", joined=date(2020, 1, 1), degree="BACHELOR"),
              _member("M3", joined=date(2029, 12, 1), degree="DOCTORATE"),
              _member("M4", joined=date(2020, 1, 1), degree="DOCTORATE", regular=False)]
    rows = (("2명 이상 5", "GTE", 2.0, None, (), 5.0), ("1명 3", "EQ", 1.0, None, (), 3.0),
            ("0명 1", "EQ", 0.0, None, (), 1.0))
    raw = _raw("석사 이상 보유 인력", "PERSONNEL_COUNT", rows, unit="명",
               literal="석사 이상 학위 소지자로 1년 이상 재직한 정규직 인력")
    count, basis = beta.personnel_count(raw, roster, deadline=DEADLINE, published_at=None)
    assert count == 1 and "학위" in basis and "정규직" in basis
    assert _score(raw, roster=roster).points == 3
    unverifiable = _raw("청년 인력", "PERSONNEL_COUNT", rows, unit="명", literal="만 34세 이하 청년 인력 수")
    score = _score(unverifiable, roster=roster)
    assert score.points == 1 and "미충족" in score.basis  # nobody counted: the printed 0명 row


def test_financial_ratio_uses_the_registered_statement_and_refuses_averages():
    statement = (2029, {"자기자본비율": 52.2, "유동비율": 190.9, "부채비율": 91.6})
    rows = (("50% 이상", "GTE", 50.0, None, (), 5.0), ("30% 이상", "GTE", 30.0, None, (), 4.0),
            ("5% 미만", "LT", 5.0, None, (), 1.0))
    raw = _raw("최근 연도의 자기자본 비율", "FINANCIAL_RATIO", rows, max_points=5)
    assert _score(raw, statement=statement).points == 5
    average = _raw("최근 3년 자기자본비율 평균", "FINANCIAL_RATIO", rows, max_points=5)
    assert _score(average, statement=statement).floor_applied


def test_bonus_tables_floor_at_zero_and_proposal_thresholds_are_not_company_rows():
    bonus = _raw("[가점] 상생협력 지표", "UNKNOWN", (("자활기업 1점", "IN", None, None, ("자활기업",), 1.0),),
                 max_points=5)
    score = _score(bonus)
    assert score.points == 0 and score.floor_applied
    proposal = _raw("제안서 평가점수 기준", "UNKNOWN", (("85점 이상", "GTE", 85.0, None, (), 85.0),),
                    max_points=100)
    assert _score(proposal) is None


def test_personnel_row_is_scored_end_to_end_only_with_the_beta_on(client, beta_on, monkeypatch):
    payload, source = hyphen_payload()
    attachment_id = payload.quantitative_tables[0].criteria[0].evidence.attachment_id
    _store(client, "SYN-BETA-PERSONNEL", attachment_id, payload, source, roster_members=6)
    estimate = _estimate(client, "SYN-BETA-PERSONNEL")
    assert estimate["activation_status"] == "PARTIAL_SOURCE"
    assert beta.COMPANY_FIRST_REASON in estimate["activation_reasons"]
    assert beta.COMPANY_FIRST_STATEMENT in estimate["assumptions"]
    [item] = estimate["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 10
    assert beta.COMPANY_FIRST_LABEL in item["rationale"]

    monkeypatch.setenv("PAI_QUANT_COMPANY_FIRST_BETA", "false")
    strict = _estimate(client, "SYN-BETA-PERSONNEL")
    assert beta.COMPANY_FIRST_REASON not in strict["activation_reasons"]
    assert all(row["estimated_points"] is None for row in strict["criteria"])


def test_missing_company_value_takes_the_lowest_printed_row_with_the_floor_label(client, beta_on):
    payload, source = hyphen_payload()
    attachment_id = payload.quantitative_tables[0].criteria[0].evidence.attachment_id
    _store(client, "SYN-BETA-NO-ROSTER", attachment_id, payload, source)
    [item] = _estimate(client, "SYN-BETA-NO-ROSTER")["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 6
    assert beta.COMPANY_FIRST_FLOOR_NOTE in item["rationale"]


def test_performance_row_counts_register_contracts_by_period_and_amount(client, beta_on):
    attachment_id, payload, source = _performance_payload()
    _store(client, "SYN-BETA-PERFORMANCE", attachment_id, payload, source, records=_records(6))
    [item] = _estimate(client, "SYN-BETA-PERFORMANCE")["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 6
    assert "6건" in item["rationale"] and "유사성 필터 없음" in item["rationale"]


def test_printed_alternatives_each_count_and_technician_tiers_need_the_tier_on_record():
    roster = [_member("M1", joined=date(2020, 1, 1), degree="BACHELOR"),
              _member("M2", joined=date(2029, 6, 1), degree="MASTER"),
              _member("M3", joined=date(2029, 6, 1), degree="BACHELOR")]
    rows = (("3명 이상 6", "GTE", 3.0, None, (), 6.0), ("2명 4", "EQ", 2.0, None, (), 4.0),
            ("1명 이하 2", "LTE", 1.0, None, (), 2.0))
    either = _raw("전문인력 보유상태", "PERSONNEL_COUNT", rows, unit="명", max_points=6, conditions=(
        "4년제 이상 관련 학과 졸업자이면서 관련분야 3년 이상 경력자이거나 혹은 석사학위 소지자 이상",))
    count, basis = beta.personnel_count(either, roster, deadline=DEADLINE, published_at=None)
    assert count == 2 and "2가지" in basis  # M1 by tenure, M2 by degree; M3 meets neither
    tiers = _raw("기술인력 보유 현황", "PERSONNEL_COUNT", rows, unit="인", max_points=6,
                 literal="중급 이상 3인 이상 6, 중급 이상 3인 미만 2")
    roster[0] = roster[0].model_copy(update={"credentials": ("고급기술자",)})
    assert beta.personnel_count(tiers, roster, deadline=DEADLINE, published_at=None)[0] == 1


def test_one_item_holding_two_ratios_scores_each_on_its_own_rows():
    statement = (2029, {"자기자본비율": 52.2, "유동비율": 190.9, "부채비율": 91.6})
    rows = (("자기자본 비율(3점) : 기준비율 50% 이상 3점", "GTE", 50.0, None, (), 3.0),
            ("자기자본 비율(3점) : 기준비율 50% 미만 2점", "LT", 50.0, None, (), 2.0),
            ("유동비율(2점) : 기준비율 200% 이상 2점", "GTE", 200.0, None, (), 2.0),
            ("유동비율(2점) : 기준비율 200% 미만 1점", "LT", 200.0, None, (), 1.0))
    raw = _raw("재무구조(경영상태) 5점", "FINANCIAL_RATIO", rows, max_points=5,
               literal="최근연도 자기자본 비율(3점) : 기준비율 50% 이상 3점 / 최근연도 유동비율(2점)")
    score = _score(raw, statement=statement)
    assert score.points == 4 and not score.floor_applied
    relative = _raw("자기자본 비율", "FINANCIAL_RATIO", (("기준비율의 50% 미만 2", "LT", 50.0, None, (), 2.0),
                                                   ("기준비율의 100% 이상 5", "GTE", 100.0, None, (), 5.0)),
                    max_points=5)
    assert _score(relative, statement=statement).floor_applied  # industry benchmark not printed


def test_careers_read_as_company_tenure_and_residence_assumes_every_member_is_available():
    roster = [_member("M1", joined=date(2020, 1, 1)), _member("M2", joined=date(2026, 1, 1)),
              _member("M3", joined=date(2029, 12, 1))]
    rows = (("2명 이상 10", "GTE", 2.0, None, (), 10.0), ("1명 이하 4", "LTE", 1.0, None, (), 4.0))
    career = _raw("인력경력(최근 5년간 유사사업 수행 경력)", "PERSONNEL_COUNT", rows, unit="명", conditions=(
        "최근 5년 내 회계·세무 분야 감사 지원 등 관련분야 경력 1년 이상 참여 인력 수",))
    count, basis = beta.personnel_count(career, roster, deadline=DEADLINE, published_at=None)
    assert count == 2 and "근속" in basis  # M3 joined a month before the deadline
    resident = _raw("상주 직원 수", "PERSONNEL_COUNT", rows, unit="명", literal="현장 상주 직원 수")
    assert _score(resident, roster=roster).points == 10


def _named(name, overview=""):
    return type("R", (), dict(record_status="VALIDATED", completed=True, end_date=date(2029, 6, 1),
                              gross_contract_amount_krw=200_000_000, contract_amount=None, share_pct=100.0,
                              project_name=name, overview=overview, keywords=[]))()


def test_performance_counts_only_contracts_in_the_field_the_row_or_title_names():
    rows = (("3건 이상 10", "GTE", 3.0, None, (), 10.0), ("2건 6", "EQ", 2.0, None, (), 6.0),
            ("1건 이하 2", "LTE", 1.0, None, (), 2.0))
    records = [_named("SYN 박람회 운영"), _named("SYN 위탁", "지역 축제 기획·운영"), _named("SYN 직무교육"),
               _named("SYN 경기도교육청 컨설팅")]
    event = _raw("행사 관련 용역 수행실적", "PERFORMANCE_COUNT", rows, unit="건")
    score = _score(event, records=records)
    assert score.points == 6 and "2건" in score.basis and "행사" in score.basis  # name or overview
    similar = _raw("유사 용역 수행실적", "PERFORMANCE_COUNT", rows, unit="건")
    by_title = beta.beta_score(similar, deadline=DEADLINE, published_at=None, credit_grade=None, roster=None,
                               records=records, notice_title="2026 SYN 직무 교육 운영 용역")
    assert by_title.points == 2 and "공고명" in by_title.basis  # 교육청 is an institution, not training
    twin = _raw("디지털트윈 개발 수행실적", "PERFORMANCE_COUNT", rows, unit="건")
    twin_score = _score(twin, records=records + [_named("SYN 정보시스템 구축")])
    assert twin_score.points == 2 and " 0건" in twin_score.basis  # a system contract is not a digital twin


def test_a_field_word_nested_in_a_narrower_one_is_not_asked_for():
    assert beta._fields_in("2027학년도 소규모테마형교육여행 수행경험")[0] == "해외연수·여행"
    assert beta._fields_in("최근 5년간 해외연수 실적 건수")[0] == "해외연수·여행"
    assert beta._fields_in("교원 직무연수 운영")[0] == "교육·연수"
    assert beta._fields_in("경기도교육청 박람회")[0] == "행사"
