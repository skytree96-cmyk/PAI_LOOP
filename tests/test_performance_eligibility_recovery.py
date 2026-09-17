from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from pai_loop.analysis_pipeline import run_analysis_pipeline
from pai_loop.eligibility_policy import POLICY_VERSION, classify_requirements
from pai_loop.models import AnalysisRun, CompanyFact, Evaluation
from test_analysis_pipeline import _notice, _requirement, _source_version, db_session
from test_quantitative_performance_generalized import _record


PROFILE = {
    "classification": "SYNTHETIC",
    "profile_version": "SYN-PERFORMANCE-GATE",
    "facts": {},
    "evidence": [],
}
CONDITION = (
    "최근 3년간 완료된 교육 용역 실적 2건 이상을 보유한 업체만 "
    "입찰에 참가할 수 있다."
)


def policy(condition=CONDITION, *, category="PERFORMANCE", mandatory=True,
           requirement_id="SYN-REQUIRED-PERFORMANCE", profile=None):
    return classify_requirements(
        [{"requirement_id": requirement_id, "category": category,
          "normalized_condition": condition, "mandatory": mandatory}],
        profile=profile or PROFILE,
        deadline="2026-12-30", evaluation_date="2026-09-07",
    )["items"][0]


@pytest.mark.parametrize("condition", [
    CONDITION,
    "교육 용역 수행 실적을 갖춘 사업자만 입찰 참가 가능하다.",
    "완료 실적을 보유하고 있는 법인만 참가할 수 있다.",
    "입찰참가자격: 최근 4년간 완료된 용역 실적을 보유한 업체",
    "입찰 참가 자격은 유사 용역 실적을 보유해야 한다.",
    "입찰에 참가하려는 업체는 유사 용역 실적을 보유해야 한다.",
    "실적이 없는 업체는 입찰에 참가할 수 없다.",
    "실적 미보유 사업자는 참가 불가하다.",
    "단일 용역 실적 3.5억원 이상을 보유한 업체만 입찰에 참가할 수 있다.",
])
def test_explicit_performance_bidder_restriction_remains_pending(condition):
    item = policy(condition)
    assert item["policy_class"] == "ELIGIBILITY"
    assert item["outcome"] == "REVIEW"
    assert item["blocking"] is True
    assert item["evidence_state"] == "MISSING_OR_UNMAPPED"
    assert item["evidence"] is None
    assert item["requires_performance_scope_binding"] is True
    assert item["evaluation_fact_key"].startswith("eligibility.performance.unbound.")
    assert "연결이 아직 없어" in item["message"]


@pytest.mark.parametrize("category", ["PERFORMANCE", "OTHER", "ENTITY", "INDUSTRY_CODE"])
def test_explicit_restriction_does_not_depend_on_provider_category(category):
    assert policy(category=category)["requires_performance_scope_binding"] is True


@pytest.mark.parametrize("condition", [
    "최근 3년간 교육 용역 실적을 평가한다.",
    "교육 실적 2건 이상이면 5점을 부여한다.",
    "실적증명서를 제안서에 첨부한다.",
    "낙찰자는 계약 완료 후 수행실적 보고서를 제출해야 한다.",
    "실적을 보유한 업체는 평가에서 가점을 받는다.",
    "실적과 관계없이 모든 업체가 입찰에 참가할 수 있다.",
    "실적이 없어도 업체는 입찰에 참가할 수 있다.",
    "실적은 참가자격이 아니며 평가 항목이다.",
    "실적 자료를 평가한다. 등록한 업체만 입찰에 참가할 수 있다.",
    "수행 실적이 있는 업체의 제안서를 참고자료로 사용한다.",
    "실적을 보유한 업체만 입찰에 참가할 수 있는 것은 아니다.",
    "입찰에 참가하려는 업체는 실적을 보유해야 한다는 의무가 없다.",
    "과거에는 실적을 보유한 업체만 입찰에 참가할 수 있었으나 이번 공고에는 해당 제한을 적용하지 않는다.",
    "실적은 평가항목이며 등록증을 보유한 업체만 입찰에 참가할 수 있다.",
])
def test_performance_category_or_score_prose_does_not_create_bidder_gate(condition):
    item = policy(condition)
    assert item["policy_class"] == "INFORMATION"
    assert "requires_performance_scope_binding" not in item


def test_optional_performance_requirement_does_not_create_mandatory_gate():
    assert policy(mandatory=False)["policy_class"] == "INFORMATION"


def test_provider_id_cannot_select_evaluation_fact_key():
    original = policy()
    arbitrary_id = policy(requirement_id="company.performance.count")
    assert original["evaluation_fact_key"] == arbitrary_id["evaluation_fact_key"]
    changed = policy(CONDITION.replace("2건", "9건"))
    assert original["evaluation_fact_key"] != changed["evaluation_fact_key"]
    assert original["evaluation_fact_key"] == policy("  " + CONDITION.replace(" ", "  "))["evaluation_fact_key"]
    # Pinned on purpose: moving the policy version is the signal to re-check
    # that the key above still derives from the condition alone. v13 carries
    # the structural reading of a nonprofit alternative, which changes which
    # requirements pass but not how this key is built.
    assert POLICY_VERSION == "pai-loop-requirement-policy-2026.09.17-v13"


def test_public_generic_or_exact_boolean_never_claims_scope_is_bound():
    key = policy()["evaluation_fact_key"]
    supplied = {**PROFILE, "facts": {
        "company.performance.count": {"value": 777},
        "notice_performance_eligibility": {"value": True},
        key: {"value": True, "evidence_state": "VERIFIED"},
    }}
    item = policy(profile=supplied)
    assert item["outcome"] == "REVIEW"
    assert item["evidence"] is None


def create_notice(session, *, title="SYN service notice", source_status="ACCEPTED",
                  complete=True, confidence=0.98, with_registration=False,
                  condition=CONDITION, category="PERFORMANCE", logic="SINGLE"):
    notice = _notice(session, notice_key="SYN-PERFORMANCE-GATE", title=title)
    notice.agency = "SYN-ORG"
    notice.risk_dimensions = None
    req = _requirement("SYN-PERFORMANCE-GATE", condition,
                       attachment_id="SYN-ATT", category=category,
                       confidence=confidence)
    req["evidence"][0]["quote"] = condition
    req["logic"] = logic
    requirements = [req]
    if with_registration:
        registration = "경쟁입찰참가자격 등록을 완료한 업체여야 함"
        other = _requirement("SYN-REGISTRATION", registration, attachment_id="SYN-ATT")
        other["evidence"][0]["quote"] = registration
        requirements.append(other)
    _source_version(notice, version_no=1, attachment_id="SYN-ATT", digest_char="a",
                    requirements=requirements, status=source_status,
                    document_complete=complete, extraction_confidence=confidence,
                    source_label="SYN-notice.pdf")
    return notice


def add_fact(session, key, value):
    session.add(CompanyFact(fact_key=key, value=value,
                           effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc)))


@pytest.mark.parametrize("record_count", [0, 1, 3])
@pytest.mark.parametrize("title", ["SYN unrelated service", "SYN other location service"])
def test_pipeline_keeps_mandatory_performance_gate_without_using_register_total(
    db_session, record_count, title,
):
    notice = create_notice(db_session, title=title)
    for index in range(record_count):
        db_session.add(_record(
            f"SYN-ROW-{index}", project_name="SYN 교육", agency="SYN-ORG",
            division="SYN", overview="SYN 교육", source="PRIVATE_IMPORT",
            recognized_performance_amount_krw=100, contract_amount=100,
        ))
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert result.status == "COMPLETED"
    assert result.eligibility == "REVIEW"
    assert result.materialized_requirement_count == 1
    assert "NO_BLOCKING_REQUIREMENTS_VERIFIED" not in result.warnings
    assert run.requirement_results[0].policy_class == "ELIGIBILITY"
    assert run.requirement_results[0].outcome == "REVIEW"
    assert run.requirement_results[0].result_json["performance_scope_binding_pending"] is True
    assert run.basis_versions["requirement_policy"] == POLICY_VERSION


@pytest.mark.parametrize("fake_key", ["company.performance.count", "notice_performance_eligibility", "exact-pending-key"])
@pytest.mark.parametrize("fake_value", [True, False, 777])
def test_generic_or_fabricated_exact_fact_cannot_satisfy_pending_scope(
    db_session, fake_key, fake_value,
):
    notice = create_notice(db_session)
    key = policy()["evaluation_fact_key"] if fake_key == "exact-pending-key" else fake_key
    add_fact(db_session, key, fake_value)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert result.eligibility == "REVIEW"
    assert evaluation.atomic_results[0]["reason_code"] == "R04"
    assert evaluation.atomic_results[0]["actual_value"] is None
    assert db_session.scalar(select(CompanyFact).where(CompanyFact.fact_key == key)).value == fake_value


@pytest.mark.parametrize("registration_value,expected", [(True, "REVIEW"), (False, "FAIL")])
def test_pending_review_preserves_other_mandatory_pass_or_explicit_fail(
    db_session, registration_value, expected,
):
    notice = create_notice(db_session, with_registration=True)
    add_fact(db_session, "bidder_registration", registration_value)
    add_fact(db_session, policy()["evaluation_fact_key"], True)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert result.eligibility == expected
    atomics = {item["label"]: item for item in evaluation.atomic_results}
    assert atomics[CONDITION]["result"] == "REVIEW"
    other = atomics["경쟁입찰참가자격 등록을 완료한 업체여야 함"]
    assert other["result"] == ("PASS" if registration_value else "FAIL")
    if not registration_value:
        assert other["reason_code"] == "DF-000"


@pytest.mark.parametrize("source_status,complete,confidence", [
    ("REVIEW", True, 0.98), ("ACCEPTED", False, 0.98), ("ACCEPTED", True, 0.5),
])
def test_unreadable_or_unverified_source_never_becomes_qualification_pass(
    db_session, source_status, complete, confidence,
):
    notice = create_notice(db_session, source_status=source_status,
                           complete=complete, confidence=confidence)
    add_fact(db_session, policy()["evaluation_fact_key"], True)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    assert result.eligibility == "REVIEW"
    assert "NO_BLOCKING_REQUIREMENTS_VERIFIED" not in result.warnings


@pytest.mark.parametrize("prefix,category,fact_key", [
    ("경쟁입찰참가자격 등록을 완료", "ENTITY", "bidder_registration"),
    ("직접생산확인증명서를 보유", "CERTIFICATION", "direct_production_certificate"),
    ("직접생산확인증명서를 보유하여야 ", "CERTIFICATION", "direct_production_certificate"),
    ("소기업 확인서를 보유", "CERTIFICATION", "small_business_certificate"),
    ("소기업 또는 소상공인 확인서를 보유", "CERTIFICATION", "small_business_certificate"),
])
@pytest.mark.parametrize("fact_value,expected", [(False, "FAIL"), (True, "REVIEW")])
def test_same_source_and_preserves_known_fact_and_pending_scope(
    db_session, prefix, category, fact_key, fact_value, expected,
):
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    connector = "하며 " if prefix.endswith(" ") else "하고 "
    clause = prefix + connector + CONDITION
    prefix = prefix.strip()
    source = {"requirement_id": "SYN-COMPOUND", "category": category,
              "normalized_condition": clause, "mandatory": True, "logic": "SINGLE",
              "evidence": [{"attachment_id": "SYN-ATT", "quote": clause}]}
    parts = expand_statutory_qualification_requirements([source])
    assert [part["normalized_condition"] for part in parts] == [prefix, CONDITION]
    assert expand_statutory_qualification_requirements(parts) == parts
    assert len({part["requirement_id"] for part in parts}) == 2
    assert all(part["evidence"] == source["evidence"] for part in parts)
    classified = classify_requirements(parts, profile=PROFILE, deadline="2026-12-30")
    assert [item["requirement_id"] for item in classified["items"]] == [part["requirement_id"] for part in parts]
    assert classified["items"][0]["company_fact_key"] == fact_key
    assert classified["items"][1]["requires_performance_scope_binding"] is True
    notice = create_notice(db_session, condition=clause, category=category)
    add_fact(db_session, fact_key, fact_value)
    add_fact(db_session, policy()["evaluation_fact_key"], True)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    assert result.eligibility == expected
    assert result.materialized_requirement_count == 2
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    snapshots = sorted(run.requirement_results, key=lambda item: item.sequence)
    assert snapshots[0].result_json["performance_scope_binding_pending"] is False
    assert snapshots[1].result_json["performance_scope_binding_pending"] is True
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    atomics = {item["label"]: item for item in evaluation.atomic_results}
    assert atomics[prefix]["result"] == ("PASS" if fact_value else "FAIL")
    assert atomics[CONDITION]["result"] == "REVIEW"
    if not fact_value:
        assert atomics[prefix]["reason_code"] == "DF-000"
    assert db_session.scalar(select(CompanyFact).where(CompanyFact.fact_key == fact_key)).value is fact_value


@pytest.mark.parametrize("connector", ["하고 ", "하며 ", " 및 "])
def test_known_and_connectors_preserve_exact_original_parts(connector):
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    prefix = "직접생산확인증명서를 보유"
    original = {"requirement_id": "SYN-AND", "category": "CERTIFICATION",
                "normalized_condition": prefix + connector + CONDITION, "mandatory": True}
    parts = expand_statutory_qualification_requirements([original])
    assert [item["normalized_condition"] for item in parts] == [prefix, CONDITION]
    assert all(item["normalized_condition"] in original["normalized_condition"] for item in parts)


@pytest.mark.parametrize("condition,logic,ambiguity", [
    ("직접생산확인증명서를 보유 또는 " + CONDITION, "SINGLE", None),
    ("직접생산확인증명서를 보유하고 " + CONDITION, "OR", None),
    ("직접생산확인증명서를 보유하고 " + CONDITION, "SINGLE", "SYN unclear relationship"),
    ("과거에는 직접생산확인증명서를 보유하고 " + CONDITION.replace("있다", "있었다"), "SINGLE", None),
    ("직접생산확인증명서를 보유하지 않고 " + CONDITION, "SINGLE", None),
])
def test_ambiguous_or_negative_relations_never_expand_to_and(condition, logic, ambiguity):
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    original = {"requirement_id": "SYN-NO-AND", "category": "CERTIFICATION",
                "normalized_condition": condition, "mandatory": True,
                "logic": logic, "ambiguity_reason": ambiguity}
    assert expand_statutory_qualification_requirements([original]) == [original]


@pytest.mark.parametrize("damage", ["reorder", "omit", "duplicate", "extra"])
def test_policy_materialization_pairing_mismatch_fails_closed(db_session, monkeypatch, damage):
    import pai_loop.analysis_pipeline as pipeline
    notice = create_notice(db_session, with_registration=True)
    notice_id = notice.id
    db_session.commit()
    classify = pipeline.classify_requirements
    def corrupt(*args, **kwargs):
        result = classify(*args, **kwargs)
        items = result["items"]
        if damage == "reorder":
            result["items"] = list(reversed(items))
        elif damage == "omit":
            result["items"] = items[:-1]
        elif damage == "duplicate":
            result["items"] = [items[0], items[0]]
        else:
            result["items"] = items + [items[0]]
        return result
    monkeypatch.setattr(pipeline, "classify_requirements", corrupt)
    with pytest.raises(pipeline.AnalysisPipelineSourceError, match="source pairing"):
        pipeline.run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)



def test_policy_revision_recalculates_prior_false_pass_once_without_new_source(
    db_session, monkeypatch,
):
    import pai_loop.analysis_pipeline as pipeline
    import pai_loop.eligibility_policy as policy_module
    from pai_loop.models import NoticeVersion

    notice = create_notice(db_session)
    notice_id = notice.id
    db_session.commit()
    with monkeypatch.context() as old:
        old.setattr(policy_module, "_is_explicit_performance_eligibility", lambda _text: False)
        old.setattr(pipeline, "POLICY_VERSION", "pai-loop-requirement-policy-2026.09.06-v10")
        previous = pipeline.run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    assert previous.eligibility == "PASS"
    current = pipeline.run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    assert current.eligibility == "REVIEW"
    assert current.analysis_run_id != previous.analysis_run_id
    assert current.reused is False
    assert pipeline.run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE).reused
    versions = list(db_session.scalars(select(NoticeVersion)))
    assert sum((item.source_payload or {}).get("kind") == "OPENAI_REQUIREMENT_EXTRACTION" for item in versions) == 1
    assert db_session.get(AnalysisRun, previous.analysis_run_id).output_summary["eligibility"] == "PASS"


def test_score_prose_cannot_borrow_registration_possession():
    clause = "실적은 평가항목이며 입찰참가자격 등록증을 보유한 업체만 입찰에 참가할 수 있다."
    item = policy(clause, profile={**PROFILE, "facts": {"bidder_registration": {"value": True}}})
    assert item["company_fact_key"] == "bidder_registration"
    assert item["outcome"] == "PASS_CURRENT"
    assert "requires_performance_scope_binding" not in item


def test_one_negated_clause_does_not_suppress_another_complete_positive_gate():
    clause = "실적을 보유한 업체만 입찰에 참가할 수 있는 것은 아니다. " + CONDITION
    assert policy(clause)["requires_performance_scope_binding"] is True


@pytest.mark.parametrize("clause", [
    "직접생산확인증명서를 보유 또는 " + CONDITION,
])
@pytest.mark.parametrize("value", [False, True])
def test_unexpanded_combination_preserves_prior_mapping_and_marks_remaining_scope(
    db_session, monkeypatch, clause, value,
):
    import pai_loop.eligibility_policy as policy_module
    given = {**PROFILE, "facts": {"direct_production_certificate": {
        "value": value, "evidence_state": "VERIFIED", "deadline_policy": "STATIC",
    }}}
    with monkeypatch.context() as prior:
        prior.setattr(policy_module, "_is_explicit_performance_eligibility", lambda _text: False)
        old = policy(clause, category="CERTIFICATION", profile=given)
    current = policy(clause, category="CERTIFICATION", profile=given)
    for key in ("company_fact_key", "evaluation_fact_key", "operator", "required_value",
                "outcome", "evidence_state", "evidence", "blocking"):
        assert current[key] == old[key]
    assert current["performance_relation_unresolved"] is True
    assert "requires_performance_scope_binding" not in current
    assert "실적 조건의 충족을 증명하지 않습니다" in current["message"]
    notice = create_notice(db_session, condition=clause, category="CERTIFICATION")
    add_fact(db_session, "direct_production_certificate", value)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    # Unsupported relationships retain the prior known-condition result. This
    # patch does not claim to have resolved their missing performance binding.
    assert result.eligibility == ("PASS" if value else "FAIL")
    assert result.materialized_requirement_count == 1
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert run.requirement_results[0].result_json["performance_relation_unresolved"] is True
    assert run.requirement_results[0].result_json["performance_scope_binding_pending"] is False
    if not value:
        evaluation = db_session.get(Evaluation, result.evaluation_id)
        assert evaluation.atomic_results[0]["reason_code"] == "DF-000"


@pytest.mark.parametrize("condition", [
    "입찰참가자격요건: 최근 3년간 완료된 교육 용역 실적 2건 이상을 보유한 업체",
    "입찰 참가 자격 요건은 유사 용역 실적을 보유해야 한다.",
    "경쟁입찰참가자격: " + CONDITION,
    "경쟁입찰참가자격: 최근 3년간 완료된 교육 용역 실적 2건 이상을 보유한 업체",
])
@pytest.mark.parametrize("registration_value", [False, True])
def test_performance_only_heading_cannot_borrow_registration_fact(
    db_session, condition, registration_value,
):
    item = policy(condition, profile={**PROFILE, "facts": {
        "bidder_registration": {"value": registration_value, "evidence_state": "VERIFIED"},
    }})
    assert item["outcome"] == "REVIEW"
    assert item["requires_performance_scope_binding"] is True
    assert "performance_relation_unresolved" not in item
    notice = create_notice(db_session, condition=condition)
    add_fact(db_session, "bidder_registration", registration_value)
    add_fact(db_session, item["evaluation_fact_key"], True)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    assert result.eligibility == "REVIEW"
    assert result.materialized_requirement_count == 1
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation.atomic_results[0]["reason_code"] == "R04"
    assert evaluation.atomic_results[0]["actual_value"] is None


def test_heading_with_real_registration_still_preserves_its_mapping():
    clause = "입찰참가자격요건: 경쟁입찰참가자격 등록을 완료하여야 한다. " + CONDITION
    item = policy(clause, profile={**PROFILE, "facts": {"bidder_registration": {"value": True}}})
    assert item["company_fact_key"] == "bidder_registration"
    assert item["performance_relation_unresolved"] is True
    assert "requires_performance_scope_binding" not in item


EXPLICIT_AND_CASES = [
    ("direct_production_certificate", "직접생산확인증명서를 반드시 보유하고 " + CONDITION),
    ("bidder_registration", "경쟁입찰참가자격 등록을 완료하여야 하며 " + CONDITION),
    ("direct_production_certificate", "직접생산확인증명서를 보유하여야 한다. 또한 " + CONDITION),
    ("direct_production_certificate", "직접생산확인증명서를 보유한 업체로서 " + CONDITION),
    ("small_business_certificate", "소기업 또는 소상공인 확인서를 반드시 보유하고 " + CONDITION),
    ("direct_production_certificate", "직접생산확인증명서(품목 A 또는 품목 B)를 반드시 보유하고 " + CONDITION),
    ("direct_production_certificate", "입찰참가자격요건: 직접생산확인증명서를 반드시 보유하고 " + CONDITION),
]


@pytest.mark.parametrize("fact_key,clause", EXPLICIT_AND_CASES)
@pytest.mark.parametrize("value,expected", [(False, "FAIL"), (True, "REVIEW")])
@pytest.mark.parametrize("logic", ["SINGLE", "AND"])
def test_proven_and_boundaries_never_omit_performance_gate(
    db_session, fact_key, clause, value, expected, logic,
):
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    original = {"requirement_id": "SYN-AND-REMAINING", "category": "CERTIFICATION",
                "normalized_condition": clause, "mandatory": True, "logic": logic,
                "evidence": [{"attachment_id": "SYN-ATT", "quote": clause}]}
    parts = expand_statutory_qualification_requirements([original])
    assert len(parts) == 2
    assert all(part["normalized_condition"] in clause.casefold() for part in parts)
    assert parts[1]["normalized_condition"] == CONDITION
    assert all(part["evidence"] == original["evidence"] for part in parts)
    assert expand_statutory_qualification_requirements(parts) == parts
    notice = create_notice(db_session, condition=clause, category="CERTIFICATION", logic=logic)
    add_fact(db_session, fact_key, value)
    add_fact(db_session, policy()["evaluation_fact_key"], True)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    assert result.eligibility == expected
    assert result.materialized_requirement_count == 2
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert sum(item.result_json["performance_scope_binding_pending"] for item in run.requirement_results) == 1
    assert not any(item.result_json["performance_relation_unresolved"] for item in run.requirement_results)
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    atomics = {item["label"]: item for item in evaluation.atomic_results}
    assert atomics[CONDITION]["result"] == "REVIEW"
    other = atomics[parts[0]["normalized_condition"]]
    assert other["result"] == ("PASS" if value else "FAIL")
    if not value:
        assert other["reason_code"] == "DF-000"


@pytest.mark.parametrize("clause", [
    "직접생산확인증명서를 보유한 업체 또는 최근 3년간 완료된 교육 용역 실적 2건 이상을 보유한 업체는 입찰에 참가할 수 있다.",
    "직접생산확인증명서를 보유하거나 " + CONDITION,
])
@pytest.mark.parametrize("logic", ["SINGLE", "OR"])
def test_complete_or_path_is_not_rewritten_as_and(db_session, clause, logic):
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    source = {"requirement_id": "SYN-OR", "normalized_condition": clause,
              "category": "CERTIFICATION", "mandatory": True, "logic": logic}
    assert expand_statutory_qualification_requirements([source]) == [source]
    notice = create_notice(db_session, condition=clause, category="CERTIFICATION", logic=logic)
    add_fact(db_session, "direct_production_certificate", True)
    notice_id = notice.id
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice_id, company_profile=PROFILE)
    assert result.eligibility == "PASS"
    assert result.materialized_requirement_count == 1
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert not any(item.result_json["performance_scope_binding_pending"] for item in run.requirement_results)


@pytest.mark.parametrize("prefix", [
    "직접생산확인증명서는 평가항목이며 등록증을 반드시 보유하고 ",
    "소기업 확인서는 참고자료이며 등록증을 반드시 보유하고 ",
    "직접생산확인증명서를 보유하지 않아도 되며 ",
    "과거에는 직접생산확인증명서를 보유하고 ",
    "직접생산확인증명서 사본을 제출하고 ",
    "직접생산확인증명서를 보유(조건 A 또는 조건 B하고 ",
])
def test_known_clause_cannot_borrow_another_object_or_invent_positive_and(prefix):
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    source = {"requirement_id": "SYN-NOT-AND", "normalized_condition": prefix + CONDITION,
              "category": "CERTIFICATION", "mandatory": True, "logic": "SINGLE"}
    assert expand_statutory_qualification_requirements([source]) == [source]
