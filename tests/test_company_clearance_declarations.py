from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from pai_loop.analysis_pipeline import _prototype_eligibility_facts
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.eligibility_policy import classify_requirements, load_public_company_profile
from pai_loop.evaluator import evaluate_notice
from pai_loop.models import AtomicRequirement, CompanyFact, Notice, NoticeVersion
from pai_loop.reference_registry import sync_public_company_profile


CASES = [
    ("공고일 현재 국세 및 지방세, 과태료 등 체납이 없는 업체", "public_dues_arrears_clear"),
    ("공고일 현재 국세, 지방세, 과태료 등 체납이 없는 업체여야 함", "public_dues_arrears_clear"),
    ("체납이 없어야 함", "public_dues_arrears_clear"),
    ("입찰공고일 현재 법정관리 중이 아닌 업체", "court_receivership_clear"),
    ("법정관리 이력이 없는 업체", "court_receivership_clear"),
    ("계약 불이행 이력이 없는 업체", "contract_nonperformance_clear"),
    ("공고일 기준 최근 2년 이내 협회와 체결한 계약을 정당한 이유없이 불이행한 사실이 확정판결·중재판정·해지통보 등으로 확인된 업체는 참가 불가", "contract_nonperformance_clear"),
    ("공고일 기준 최근 2년 이내 협회와 체결한 계약을 정당한 이유없이 이행하지 않은 사실이 법원 확정판결, 중재판정 또는 협회의 공식 계약해지 통보 등으로 확인된 업체는 참가 불가", "contract_nonperformance_clear"),
    ("공고일 기준 최근 2년 이내 협회와의 계약을 정당한 이유 없이 불이행한 사실이 확인된 업체는 참가 불가", "contract_nonperformance_clear"),
]


def item(condition, profile=None, **kwargs):
    return classify_requirements(
        [{"requirement_id": "SYN-CLEARANCE", "category": "SANCTION",
          "normalized_condition": condition, "mandatory": True}],
        profile=profile or load_public_company_profile(),
        deadline=kwargs.pop("deadline", "2026-10-01"), **kwargs,
    )["items"][0]


@pytest.mark.parametrize("condition,key", CASES)
def test_company_declaration_resolves_a_single_historical_clearance(condition, key):
    result = item(condition)
    assert result["outcome"] == "PASS_CURRENT"
    assert result["company_fact_key"] == key
    assert result["evaluation_fact_key"] == f"prototype.{key}"
    assert result["assessment_basis"] == "PROTOTYPE_CURRENT_FACTS"
    assert result["evidence_state"] == "COMPANY_CONFIRMED"
    assert result["evidence"] is None


@pytest.mark.parametrize("condition,key", CASES[::3])
@pytest.mark.parametrize("value,outcome", [(False, "FAIL_CONFIRMED"), (None, "REVIEW"), ("MISSING", "REVIEW")])
def test_no_positive_default_for_missing_or_contrary_company_declarations(condition, key, value, outcome):
    profile = load_public_company_profile()
    if value == "MISSING":
        del profile["facts"][key]
    else:
        profile["facts"][key]["value"] = value
    assert item(condition, profile)["outcome"] == outcome


@pytest.mark.parametrize("condition", [
    "입찰공고일 현재 법정관리·화의개시 중이 아니며 정부기관에 의한 부정당업체 제재 중이 아닌 자",
    "입찰공고일 현재 청산·합병·매각 등 정리절차 중이거나 계획 중인 업체, 법원에 화의 또는 법정관리(신청 중)인 업체는 입찰 참가 불가",
    "법정관리 중이 아니고 부정당업자로 지정되지 않은 업체",
    "국세 체납이 없고 부정당 제재를 받지 않는 업체",
    "계약 불이행 이력이 없고 부정당 제재를 받지 않는 업체",
])
def test_compound_conditions_do_not_inherit_one_clearance(condition):
    assert item(condition)["outcome"] == "REVIEW"


@pytest.mark.parametrize("condition", [
    "낙찰 후 계약 불이행 시 입찰참가자격 제한 처분을 받음",
    "계약을 정당한 이유 없이 이행하지 않는 경우 손해배상을 해야 함",
    "금융기관 채무불이행 이력이 없는 업체",
    "국세 및 지방세 납세증명서를 제출해야 함",
    "국세 체납 사실이 있는 업체만 참가 가능",
    "법정관리 중인 업체만 참가 가능",
    "계약 불이행 이력이 있는 업체만 참가 가능",
])
def test_future_duties_and_other_predicates_do_not_use_clearance(condition):
    assert item(condition).get("company_fact_key") not in {key for _, key in CASES}


@pytest.mark.parametrize("condition,key", CASES[::3])
def test_strict_mode_requires_deadline_reconfirmation(condition, key):
    profile = load_public_company_profile()
    profile["eligibility_assessment_mode"] = "DEADLINE_EVIDENCE"
    assert item(condition, profile, deadline="2026-01-14")["outcome"] == "REVIEW"
    assert item(condition, profile, deadline="2027-10-01", evaluation_date="2027-10-01")["outcome"] == "REVIEW"


@pytest.mark.parametrize("value,expected", [(True, "PASS"), (False, "FAIL"), (None, "REVIEW"), ("MISSING", "REVIEW")])
def test_stored_declaration_drives_evaluation_without_becoming_verified(value, expected):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    deadline = datetime(2026, 1, 14, tzinfo=timezone.utc)
    profile = load_public_company_profile()
    condition, key = CASES[0]
    mapped = item(condition, profile)
    with build_session_factory(engine)() as session:
        sync_public_company_profile(session)
        sync_public_company_profile(session)
        facts = session.scalars(select(CompanyFact).where(CompanyFact.fact_key == key)).all()
        assert len(facts) == 1
        assert facts[0].value is True
        assert facts[0].verified is False
        assert facts[0].evidence_id is None
        assert facts[0].value_label == "COMPANY_CONFIRMED"
        facts[0].value = value
        inputs = _prototype_eligibility_facts(profile, deadline=deadline, company_facts=[] if value == "MISSING" else facts)
        atom = AtomicRequirement(
            requirement_key="SYN-CLEARANCE", group_key="SYN-CLEARANCE", path_key="SYN-PATH",
            sequence=0, label=condition, fact_key=mapped["evaluation_fact_key"],
            operator="eq", required_value=True, evidence_required=False, mandatory=True,
            pass_rule_id="P-DOCUMENT", linked_review_code="R04", review_trigger_value="__MISSING__",
            parse_confidence=1.0, active=True,
        )
        result = evaluate_notice(
            notice=Notice(notice_key="SYN-CLEARANCE", deadline=deadline, risk_dimensions={}),
            version=NoticeVersion(document_complete=True, extraction_confidence=1.0, extraction_status="COMPLETE"),
            requirements=[atom], company_facts=inputs,
        )
        assert result.eligibility.value == expected
        assert not session.scalar(select(CompanyFact).where(CompanyFact.fact_key.like("prototype.%")))
    engine.dispose()
