"""REVIEW clauses that only point elsewhere, describe scoring, or restate a confirmed clearance (2026-10-05)."""
import pytest

from pai_loop.eligibility_policy import classify_requirements, load_public_company_profile


def item(condition, category):
    return classify_requirements(
        [{"requirement_id": "SYN-REVIEW", "category": category,
          "normalized_condition": condition, "mandatory": True}],
        profile=load_public_company_profile(), deadline="2026-10-12",
    )["items"][0]


@pytest.mark.parametrize("condition", [
    "제안업체의 자격조건은 입찰공고문에 따름",
    "입찰참가자격은 조달청 공고서에 따름",
    "입찰참가자격은 별도 입찰공고문 제4항을 따름(본 문서에 세부기준 미포함)",
    "입찰참가자격은 별도의 입찰공고문 제4항에 따름",
])
def test_referral_only_clauses_are_information(condition):
    assert item(condition, "ENTITY")["policy_class"] == "INFORMATION"


@pytest.mark.parametrize("condition,category", [
    ("고용노동부장관으로부터 체불사업주로 명단이 공개중인 자 여부가 사회적 책임 평가에 반영됨", "SANCTION"),
    ("신용평가등급확인서가 확인되지 않은 경우 최저등급으로 평가함", "CERTIFICATION"),
    ("해당 평가대상 사업과 관련한 용역·자문·연구 수행자, 이해당사자가 되는 자는 평가위원 후보에서 제외됨", "SANCTION"),
])
def test_scoring_and_evaluator_clauses_are_information(condition, category):
    assert item(condition, category)["policy_class"] == "INFORMATION"


@pytest.mark.parametrize("condition,category,fact", [
    ("국가계약법 제27조 부정당업자 제재 및 관련 시행령·시행규칙에 따른 유자격자여야 함", "SANCTION", "sanction_clear"),
    ("조세포탈세액 등이 5억원 이상인 자 등 국가계약법 제27조의5 제1항 각 호에 해당하여 유죄판결 확정일로부터 "
     "2년이 지나지 않은 자가 아님을 서약해야 하며, 위반 시 계약 해제·해지 및 부정당업자 입찰참가자격 제한을 받을 수 있음",
     "SANCTION", "conviction_clear"),
    ("공동수급체 대표자가 부도, 부정당업자 제재, 영업정지 등 결격사유가 있는 경우 해당 공동수급체는 결격 처리됨",
     "CONSORTIUM", "sanction_clear"),
])
def test_restated_clearances_pass_on_confirmed_facts(condition, category, fact):
    result = item(condition, category)
    assert result["outcome"] == "PASS_CURRENT"
    assert result["company_fact_key"] == fact


def test_status_report_form_is_a_checklist():
    result = item("최근 2년 이내 부정당업자 제재(1년 이상 또는 1년 미만), 계약해지, 부도상태 여부를 업체현황조사서에 기재해야 함",
                  "SANCTION")
    assert result["policy_class"] == "CHECKLIST"


@pytest.mark.parametrize("condition,category", [
    # A referral that also adds its own requirement is not referral-only.
    ("입찰참가자격은 입찰공고문에 따르며, 직접생산확인증명서를 소지해야 함", "ENTITY"),
    # A scoring note that also excludes bidders is still a gate.
    ("체불사업주로 명단이 공개중인 자는 입찰 참가 불가하며 평가에 반영됨", "SANCTION"),
    # The representative clause with an unconfirmed predicate stays closed.
    ("공동수급체 대표자가 소송 중이거나 부도 등 결격사유가 있는 경우 결격 처리됨", "CONSORTIUM"),
])
def test_mixed_clauses_are_not_reclassified(condition, category):
    result = item(condition, category)
    assert result["policy_class"] != "INFORMATION"
    assert result["outcome"] != "PASS_CURRENT"
