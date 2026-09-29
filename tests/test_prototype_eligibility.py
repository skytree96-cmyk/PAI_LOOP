from datetime import datetime, timezone

import pytest

from pai_loop.eligibility_policy import classify_requirements, load_public_company_profile
from pai_loop.analysis_pipeline import _prototype_eligibility_facts
from pai_loop.evaluator import evaluate_notice
from pai_loop.models import AtomicRequirement, CompanyFact, Notice, NoticeVersion


CONDITIONS = [
    ("INDUSTRY_CODE", "나라장터(G2B)에 [기타자유업(행사대행업)(9901)] 업종을 입찰참가자격으로 등록해야 함"),
    ("CERTIFICATION", "직접생산확인증명서[기타행사기획및대행서비스(8014199001)]를 소지해야 함"),
    ("ENTITY", "중소기업기본법상 중소기업자 또는 소상공인 보호 및 지원에 관한 법률상 소상공인으로서 중소기업·소상공인 확인서를 소지해야 함"),
    ("SANCTION", "국가계약법 제8조의2에 해당하는 자, 조세포탈 등으로 유죄확정 후 2년 미경과자는 입찰참가자격 없음"),
    ("SUBMISSION", "나라장터에 기타자유업(행사대행업)(9901) 업종을 전자입찰서 제출 마감일 전일까지 등록해야 함"),
    ("CERTIFICATION", "중소기업자 또는 소상공인으로서 중소기업·소상공인 확인서(공공기관 입찰용)를 소지해야 함"),
    ("SANCTION", "조세포탈 등으로 유죄판결 확정 후 2년이 지나지 않은 자는 입찰참가자격 없음"),
    ("SUBMISSION", "입찰참가등록증상 상호 및 대표자가 법인등기부등본(개인은 사업자등록증)과 다를 경우 변경등록 후 입찰 참여해야 하며, 미변경 시 무효입찰임"),
    ("REGION", "국내입찰로 구분됨"),
]


def requirements(conditions=CONDITIONS):
    return [dict(requirement_id=f"SYN-PROTOTYPE-{index}", category=category,
                 normalized_condition=condition, mandatory=True)
            for index, (category, condition) in enumerate(conditions)]


@pytest.mark.parametrize("deadline", ["2026-01-14", "2026-10-01", "2027-06-01"])
def test_user_example_has_six_definite_groups_without_losing_nine_source_rows(deadline):
    result = classify_requirements(requirements(), profile=load_public_company_profile(), deadline=deadline)
    assert [i["outcome"] for i in result["items"]] == [
        "PASS_CURRENT", "FAIL_CONFIRMED", "FAIL_CONFIRMED", "PASS_CURRENT",
        "PASS_CURRENT", "FAIL_CONFIRMED", "PASS_CURRENT", "PASS_CURRENT", "PASS_CURRENT",
    ]
    assert len(result["items"]) == 9
    assert len(result["display_items"]) == 6
    assert result["duplicate_count"] == 3
    assert result["verdict_counts"] == {"P": 4, "F": 2}
    assert sum(len(i["source_requirement_ids"]) for i in result["display_items"]) == 9
    assert all(i["assessment_basis"] == "PROTOTYPE_CURRENT_FACTS" for i in result["items"])


@pytest.mark.parametrize("condition,outcome", [
    ("직접생산확인증명서는 비영리법인에 적용하지 않으며 참여 가능함.", "PASS_EXCEPTION"),
    ("중소기업제품 구매촉진 및 판로지원에 관한 법률에 따른 직접생산확인증명서를 소지해야 함.", "FAIL_CONFIRMED"),
    ("중소기업확인서는 비영리법인에 적용하지 않으며 참여 가능함.", "PASS_EXCEPTION"),
    ("중소기업확인서는 비영리법인도 보유해야 함.", "FAIL_CONFIRMED"),
    ("직접생산확인증명서는 비영리법인도 보유해야 함.", "FAIL_CONFIRMED"),
    ("중소기업 확인서 소지자 또는 중소기업자로 간주되는 비영리법인이어야 함", "REVIEW"),
    ("나라장터 업종코드 9901 또는 9998로 등록해야 함", "PASS_CURRENT"),
    ("나라장터 업종코드 9901 및 9998로 모두 등록해야 함", "FAIL_CONFIRMED"),
    ("나라장터 업종코드 9901/9998로 등록해야 함", "REVIEW"),
])
def test_prototype_preserves_nonprofit_scope_and_industry_logic(condition, outcome):
    result = classify_requirements(requirements([("CERTIFICATION", condition)]),
                                   profile=load_public_company_profile(), deadline="2026-10-01")
    assert result["items"][0]["outcome"] == outcome


def test_missing_company_value_still_requires_review_and_strict_mode_is_available():
    profile = load_public_company_profile()
    del profile["facts"]["industry_code_inventory"]
    assert classify_requirements(requirements(CONDITIONS[:1]), profile=profile, deadline="2026-10-01")["items"][0]["outcome"] == "REVIEW"
    profile = load_public_company_profile()
    profile["eligibility_assessment_mode"] = "DEADLINE_EVIDENCE"
    assert classify_requirements(requirements(CONDITIONS[:1]), profile=profile, deadline="2026-01-14")["items"][0]["outcome"] == "REVIEW"


def test_different_product_codes_and_verdicts_are_not_collapsed():
    rows = requirements([
        ("CERTIFICATION", "직접생산확인증명서(8014199001)를 보유해야 함"),
        ("CERTIFICATION", "직접생산확인증명서(8014199002)를 보유해야 함"),
        ("INDUSTRY_CODE", "업종코드 9901 등록"),
        ("INDUSTRY_CODE", "업종코드 9998 등록"),
    ])
    result = classify_requirements(rows, profile=load_public_company_profile(), deadline="2026-10-01")
    assert len(result["display_items"]) == 4


def test_prototype_inputs_reach_evaluator_without_changing_stored_company_facts():
    profile = load_public_company_profile()
    deadline = datetime(2026, 1, 14, tzinfo=timezone.utc)
    result = classify_requirements(requirements(), profile=profile, deadline=deadline)
    atoms = [AtomicRequirement(
        requirement_key=item["requirement_id"], group_key=item["requirement_id"],
        path_key="SYN-PATH", sequence=index, label=item["condition"],
        fact_key=item["evaluation_fact_key"], operator=item["operator"],
        required_value=item["required_value"], evidence_required=False, mandatory=True,
        pass_rule_id="P-DOCUMENT", linked_review_code="R04", review_trigger_value="__MISSING__",
        parse_confidence=1.0, active=True,
    ) for index, item in enumerate(result["items"])]
    notice = Notice(notice_key="SYN-PROTOTYPE", deadline=deadline, risk_dimensions={})
    version = NoticeVersion(document_complete=True, extraction_confidence=1.0, extraction_status="COMPLETE")
    stored = [CompanyFact(fact_key=key, value=fact["value"], effective_from=datetime(2026, 9, 29, tzinfo=timezone.utc)) for key, fact in profile["facts"].items()]
    facts = _prototype_eligibility_facts(profile, deadline=deadline, company_facts=stored)
    outcome = evaluate_notice(notice=notice, version=version, requirements=atoms, company_facts=facts)
    assert outcome.eligibility.value == "FAIL"
    assert [item["result"] for item in outcome.atomic_results] == ["PASS", "FAIL", "FAIL", "PASS", "PASS", "FAIL", "PASS", "PASS", "PASS"]
    assert all(fact.source == "PROTOTYPE_COMPANY_BASELINE" for fact in facts)
    assert profile["facts"]["industry_code_inventory"]["effective_from"] == "2026-08-05"


def test_pipeline_persists_example_and_recomputes_when_current_fact_changes():
    from sqlalchemy import select
    from pai_loop.database import Base, build_engine, build_session_factory
    from pai_loop.analysis_pipeline import run_analysis_pipeline
    from pai_loop.reference_registry import sync_public_company_profile
    from pai_loop.models import Evaluation
    from test_analysis_pipeline import _notice, _source_version, _requirement

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with build_session_factory(engine)() as session:
        sync_public_company_profile(session)
        notice = _notice(session, notice_key="SYN-PROTOTYPE-PIPELINE", title="합성 행사 운영")
        notice.deadline = datetime(2026, 1, 14, tzinfo=timezone.utc)
        rows = [_requirement(f"SYN-PROTOTYPE-{index}", text, attachment_id="SYN-ATTACHMENT", category=category)
                for index, (category, text) in enumerate(CONDITIONS)]
        _source_version(notice, version_no=1, attachment_id="SYN-ATTACHMENT", digest_char="a", requirements=rows)
        session.commit()
        first = run_analysis_pipeline(session, notice_id=notice.id)
        assert first.eligibility == "FAIL"
        evaluation = session.get(Evaluation, first.evaluation_id)
        assert sorted(row["result"] for row in evaluation.atomic_results) == ["FAIL"] * 3 + ["PASS"] * 6
        session.commit()
        assert run_analysis_pipeline(session, notice_id=notice.id).reused
        original = session.scalar(select(CompanyFact).where(CompanyFact.fact_key == "industry_code_inventory"))
        original.value = [code for code in original.value if code != "9901"]
        session.commit()
        second = run_analysis_pipeline(session, notice_id=notice.id)
        assert not second.reused
        assert first.analysis_run_id != second.analysis_run_id
        refreshed = session.get(Evaluation, second.evaluation_id)
        assert sum(row["result"] == "FAIL" for row in refreshed.atomic_results) == 5
        # The prototype projection never creates or rewrites stored company facts.
        assert not session.scalar(select(CompanyFact).where(CompanyFact.fact_key.like("prototype.%")))
    engine.dispose()
