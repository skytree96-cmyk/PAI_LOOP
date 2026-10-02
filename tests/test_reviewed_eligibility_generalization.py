"""Synthetic boundaries for the September reviewer-policy continuation."""

import copy

import pytest

from pai_loop.eligibility_policy import (
    classify_requirements,
    expand_statutory_qualification_requirements,
    load_public_company_profile,
)


def row(text, category="CERTIFICATION", key="SYN-REVIEW", **extra):
    return dict(requirement_id=key, category=category, normalized_condition=text,
                mandatory=True, **extra)


def classify(rows, profile=None):
    return classify_requirements(
        rows, profile=profile or load_public_company_profile(),
        deadline="2026-10-01", evaluation_date="2026-09-30",
    )["items"]


EXCEPTION = "소기업·소상공인 확인서를 보유해야 함(비영리법인은 예외적으로 참가 가능)"


@pytest.mark.parametrize("text", [
    "소기업·소상공인 확인서를 보유해야 함.",
    "소기업 또는 소상공인으로서 소기업·소상공인 확인서를 소지해야 함",
    "소기업·소상공인 확인서는 유효기간 내에 있어야 함.",
])
def test_repeated_certificate_inherits_notice_local_exception(text):
    result = classify([row(EXCEPTION, key="SYN-SOURCE"), row(text)])[-1]
    assert result["outcome"] == "PASS_EXCEPTION"
    assert result["nonprofit_route_source"] == "NOTICE_SIBLING_CLAUSE"
    assert result["nonprofit_route_requirement_ids"] == ["SYN-SOURCE"]
    assert result["condition"] == text
    assert classify([row(text)])[0]["outcome"] == "FAIL_CONFIRMED"


@pytest.mark.parametrize("text", [
    "중소기업확인서를 예외 없이 반드시 보유해야 함.",
    "중소기업확인서를 보유해야 하며 별도 등록도 완료해야 함.",
    "중소기업확인서는 비영리법인도 보유해야 함.",
    "직접생산확인증명서를 보유해야 함.",
    "중소기업확인서 및 직접생산확인증명서를 보유해야 함.",
])
def test_exception_never_waives_independent_or_compound_duty(text):
    item = classify([row(EXCEPTION, key="SYN-SOURCE"), row(text)])[-1]
    assert not item["outcome"].startswith("PASS")
    assert "nonprofit_route_source" not in item


@pytest.mark.parametrize("source", [
    row(EXCEPTION, key="SYN-OPTIONAL") | {"mandatory": False},
    row(EXCEPTION, key="SYN-AMBIGUOUS", ambiguity_reason="SYN-unclear scope"),
])
def test_optional_or_ambiguous_source_cannot_supply_exception(source):
    item = classify([source, row("중소기업확인서를 보유해야 함.")])[-1]
    assert not item["outcome"].startswith("PASS")


@pytest.mark.parametrize("qualifier", ["특정 요건의", "학술연구를 위한", "일부"])
def test_statutory_fact_does_not_prove_unnamed_subset_or_purpose(qualifier):
    text = f"소기업·소상공인 확인서 소지자 또는 {qualifier} 비영리법인은 참가 가능"
    assert classify([row(text)])[0]["outcome"] == "REVIEW"


def test_missing_statutory_confirmation_preserves_review():
    profile = load_public_company_profile()
    profile["facts"].pop("nonprofit_priority_procurement_exception")
    item = classify([row("중소기업 확인서 소지자 또는 중소기업자로 간주되는 비영리법인이어야 함")], profile)[0]
    assert item["outcome"] == "REVIEW"


def test_unresolved_legal_subset_cannot_pass_its_sibling():
    source = "소기업·소상공인 확인서를 소지한 업체 또는 관계 법령상 비영리법인 중 하나에 해당"
    items = classify([row(source, key="SYN-LEGAL-SUBSET"), row("중소기업확인서를 보유해야 함.")])
    assert all(not item["outcome"].startswith("PASS") for item in items)


def test_positive_financial_status_is_not_a_clearance_requirement():
    item = classify([row("회생 절차를 진행 중인 기업만 지원 가능", "ENTITY")])[0]
    assert item["outcome"] == "REVIEW"


REGISTRATION = "국가계약법 시행령 제12조 및 시행규칙 제14조에 따른 자격을 갖추고 부정당업자에 해당하지 않을 것"


@pytest.mark.parametrize("missing", [None, "bidder_registration", "sanction_clear"])
def test_registration_and_clearance_require_both_facts(missing):
    profile = load_public_company_profile()
    if missing:
        profile["facts"].pop(missing)
    items = classify([row(REGISTRATION, "ENTITY")], profile)
    assert len(items) == 2
    assert all(i["outcome"].startswith("PASS") for i in items) is (missing is None)


def test_registration_split_keeps_evidence_and_source_intact():
    source = row(REGISTRATION, "ENTITY", evidence=[{"quote": REGISTRATION, "page": 2}])
    before = copy.deepcopy(source)
    parts = expand_statutory_qualification_requirements([source])
    assert source == before
    assert len(parts) == 2
    assert all(p["evidence"] == source["evidence"] for p in parts)
    assert all(p["normalized_condition"] in REGISTRATION for p in parts)
    assert expand_statutory_qualification_requirements(parts) == parts


@pytest.mark.parametrize("text", [
    REGISTRATION + ". 별도 허가증을 보유해야 함",
    REGISTRATION.replace("갖추고", "갖추거나"),
    REGISTRATION + " 및 실적을 보유해야 함",
])
def test_registration_compounds_do_not_drop_other_gates(text):
    assert all(not i["outcome"].startswith("PASS") for i in classify([row(text, "ENTITY")]))


@pytest.mark.parametrize("missing", [None, "business_continuity_clear", "court_receivership_clear", "sanction_clear"])
def test_company_status_compound_requires_every_fact(missing):
    profile = load_public_company_profile()
    if missing:
        profile["facts"].pop(missing)
    text = "청산·합병·매각·법정관리 및 부정당업자 제재에 해당하지 않는 업체"
    item = classify([row(text, "SANCTION")], profile)[0]
    assert item["outcome"].startswith("PASS") is (missing is None)


def test_company_status_confirmed_failure_remains_failure():
    profile = load_public_company_profile()
    profile["facts"]["business_continuity_clear"]["value"] = False
    item = classify([row("청산·합병·매각·법정관리 중이 아닌 업체", "SANCTION")], profile)[0]
    assert item["outcome"] == "FAIL_CONFIRMED"


@pytest.mark.parametrize("text", [
    "국세 체납이 없거나 부정당 제재를 받지 않는 업체",
    "법정관리 중이 아니거나 부정당업자로 지정되지 않은 업체",
    "법정관리 중인 업체이면서 국세 체납이 없는 업체",
    "회생 중이며 부정당업자로 지정되지 않은 업체",
])
@pytest.mark.parametrize("clear", [True, False, None])
def test_alternative_or_positive_status_is_not_an_and_of_clearances(text, clear):
    profile = load_public_company_profile()
    for key in ("public_dues_arrears_clear", "court_receivership_clear", "business_continuity_clear"):
        profile["facts"][key]["value"] = clear
    assert classify([row(text, "SANCTION")], profile)[0]["outcome"] == "REVIEW"


def test_different_status_components_keep_distinct_display_rows():
    result = classify_requirements([
        row("청산·합병·매각 중이 아닌 업체", "SANCTION", key="SYN-STATUS-A"),
        row("청산·합병·매각 및 법정관리 중이 아닌 업체", "SANCTION", key="SYN-STATUS-B"),
    ], profile=load_public_company_profile(), deadline="2026-10-01", evaluation_date="2026-09-30")
    assert len(result["display_items"]) == 2


@pytest.mark.parametrize("text", [
    "특수 인증서를 보유하고 확인서를 제출해야 함",
    "기업·대학·협회 등으로 실적을 보유해야 함",
    "특수 인증서를 보유해야 하며 가점 대상임",
    "종합여행업 또는 국외여행업 등록 및 실적을 보유해야 함",
])
def test_checklist_and_information_fallbacks_do_not_erase_gates(text):
    item = classify([row(text)])[0]
    assert item["policy_class"] == "ELIGIBILITY"
    assert item["outcome"] == "REVIEW"


@pytest.mark.parametrize("text,category,policy_class", [
    ("담합 적발 시 입찰참가자격을 제한한다", "SANCTION", "CHECKLIST"),
    ("신용평가등급확인서를 제출해야 함", "CERTIFICATION", "CHECKLIST"),
    ("기술평가 점수가 85% 미만이면 협상 대상자에서 제외", "CERTIFICATION", "INFORMATION"),
])
def test_pure_contract_and_submission_rules_are_separate(text, category, policy_class):
    item = classify([row(text, category)])[0]
    assert item["policy_class"] == policy_class


def test_unknown_head_office_cannot_be_failed_by_empty_branch_inventory():
    profile = load_public_company_profile()
    profile["facts"].pop("head_office_region_codes")
    item = classify([row("본점 또는 지점 소재지가 대구광역시인 업체", "REGION")], profile)[0]
    assert item["outcome"] == "REVIEW"


@pytest.mark.parametrize("text,outcome", [
    ("본점 소재지가 부산광역시인 업체", "FAIL_CONFIRMED"),
    ("본점 소재지가 서울특별시인 업체", "PASS_CURRENT"),
    # The company confirmed (2026-10-03) that it owns training facilities in Seoul only.
    ("경기도에 교육시설을 보유해야 함", "FAIL_CONFIRMED"),
])
def test_region_rules_distinguish_head_office_and_facility(text, outcome):
    assert classify([row(text, "REGION")])[0]["outcome"] == outcome
