"""SYN standard-clause scenarios; no real notices or certificate bodies."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pai_loop import private_company_evidence as credit, quantitative_scoring as scoring
from pai_loop.analysis_pipeline import _selected_fact_manifest
from pai_loop.models import CompanyFact, Notice
from pai_loop.quantitative_credit_scenario import (
    CREDIT_SCENARIO_FACT_KEY, recognized_credit_scenario_conditions,
)
from pai_loop.quantitative_rule_extraction import merge_validated_quantitative_records
from test_dense_case_source_binding import credit_fixture, record, anchor, ATT, DOC, MANIFEST
from test_private_credit_registry import certificate, setup, configured_company
from test_private_company_evidence import _seed_notice, NOTICE_KEY

ROOT = "https://testserver/api/v1/operator-evidence"
SCENARIO = ROOT + "/credit-rating-scenarios"
CONTEXT = ROOT + f"/notices/{NOTICE_KEY}/credit-rating/scenario-binding"


def conditions():
    # Deliberately assembled SYN grammar examples, not a private source fixture.
    issuer = ("신용정보의 이용 및 보호에 관한 법률 제2조 제8의3에 해당하는 신용조회사 또는 "
              "자본시장과 금융투자업에 관한 법률 제335조의3에 따라 업무를 영위하는 신용평가사가 ")
    validity = "공고일 이전에 평가하고 유효기간 내에 있는 "
    grades = "회사채 기업어음 및 기업신용평가등급"
    first = issuer + validity + grades + "을 기준으로 평가한다"
    selection = "평가대상자의 회사채(또는 기업어음) 및 기업신용평가등급에 따른 평점이 다른 경우 "
    second = selection + "높은 평점으로 평가하며 신용평가등급 확인서가 확인되지 않은 경우에는 최저등급으로 평가한다"
    return [first, second]


def install(monkeypatch, *, partial=True, literals=None):
    raw, source = credit_fixture()
    literals = conditions() if literals is None else literals
    raw["quantitative_tables"][0]["criteria"][0]["recognition_conditions"] = [
        {"literal": literal, "evidence": anchor(literal)} for literal in literals]
    source += "\n" + "\n".join(literals)
    stored = record(raw, source)
    assert stored.status == "AVAILABLE", stored.issues
    documents = {ATT: DOC}
    if partial:
        documents["SYN-MISSING-SIBLING"] = "d" * 64
    profile = merge_validated_quantitative_records([stored], expected_documents=documents,
                                                  manifest_sha256=MANIFEST)
    monkeypatch.setattr(credit, "_current_dynamic_quantitative_profile", lambda _notice: profile)
    monkeypatch.setattr(scoring, "_current_dynamic_quantitative_profile", lambda _notice: profile)
    return profile


def prepare(client, monkeypatch, **overrides):
    setup(client)
    _seed_notice(client)
    install(monkeypatch)
    cert = certificate(issued_on="2026-07-31", effective_on="2026-07-01")
    cid = client.post(ROOT + "/credit-ratings", json=cert).json()["certificate_id"]
    context = client.get(CONTEXT)
    assert context.status_code == 200, context.text
    application = {key: value for key, value in context.json().items()
                   if key in credit.CreditScenarioApplication.model_fields}
    policy = dict(certificate_id=cid,
                  certificate_registration_sha256=credit._certificate_metadata(
                      credit.PrivateCreditCertificateRegistration.model_validate(cert))["registration_sha256"],
                  attestation="HUMAN_APPROVED_CERTIFICATE_SCENARIO", accepted_on="2026-08-01",
                  effective_through="2026-08-31", qualified_issuer=True,
                  procurement_system_visible=True, single_unchanged_rating=True,
                  same_legal_entity=True, no_succession_or_cooperative_exception=True,
                  assumption="SOLE_BID_UNCHANGED_CERTIFICATE", applications=[application])
    policy.update(overrides)
    return policy


def load(session):
    notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
    facts = list(session.scalars(select(CompanyFact).where(CompanyFact.fact_key == CREDIT_SCENARIO_FACT_KEY)
                                .options(selectinload(CompanyFact.evidence))))
    return notice, facts


def test_partial_source_scenario_never_creates_confirmed_fact_and_public_warning_survives(client, monkeypatch):
    payload = prepare(client, monkeypatch)
    response = client.post(SCENARIO, json=payload)
    assert response.status_code == 200, response.text
    assert response.json() == {"registration_status": "CREATED", "application_count": 1, "assurance": "ESTIMATED_ONLY"}
    assert response.headers["cache-control"] == "no-store"
    assert client.post(SCENARIO, json=payload).json()["registration_status"] == "UNCHANGED"
    with client.app.state.session_factory() as session:
        notice, facts = load(session)
        assert not list(session.scalars(select(CompanyFact).where(CompanyFact.fact_key == "company.credit_rating")))
        result = scoring.estimate_for_notice(notice, facts)
        assert result.confirmed_points == 0
        assert result.estimated_points is None
        assert result.criteria[0].estimated_points == 9
        assert result.overall_status == "REVIEW"
        assert result.criteria[0].status == "ESTIMATED"
        public = scoring._public_quantitative_projection(result)
        assert "단독입찰" in public.criteria[0].rationale
        encoded = public.model_dump_json()
        assert "private-evidence" not in encoded and payload["certificate_id"] not in encoded
        req = scoring.quantitative_request_from_candidate_profile(credit._current_dynamic_quantitative_profile(notice),
                                                                  allow_partial_source=True)
        without_notice = scoring.bind_quantitative_company_inputs(req, facts, as_of=notice.deadline)
        assert not any(f.status == "ESTIMATED" for f in without_notice.facts)


@pytest.mark.parametrize("mutation", ["verified", "source", "certificate-status", "certificate-metadata",
    "policy-hash", "manifest", "binding", "conditions", "publication", "horizon", "duplicates"])
def test_changed_policy_certificate_or_source_fails_closed(client, monkeypatch, mutation):
    payload = prepare(client, monkeypatch)
    assert client.post(SCENARIO, json=payload).status_code == 200
    with client.app.state.session_factory() as session:
        notice, facts = load(session)
        fact = facts[0]
        if mutation == "verified": fact.verified = False
        elif mutation == "source": fact.source = "SYN-UNTRUSTED"
        elif mutation == "certificate-status": fact.evidence.status = "REVOKED"
        elif mutation == "certificate-metadata": fact.evidence.metadata_json = {}
        elif mutation == "publication": notice.published_at = None
        elif mutation == "duplicates": facts.append(fact)
        else:
            value = deepcopy(fact.value)
            if mutation == "policy-hash": value["certificate_registration_sha256"] = "f" * 64
            elif mutation == "horizon": value["effective_through"] = "2026-08-29"
            else:
                field = {"manifest": "manifest_sha256", "binding": "fact_binding_sha256",
                         "conditions": "conditions_sha256"}[mutation]
                value["applications"][0][field] = "f" * 64
            fact.value = value
        result = scoring.estimate_for_notice(notice, facts)
        assert result.confirmed_points == 0 and result.estimated_points is None
        assert result.criteria[0].status == "REVIEW"


@pytest.mark.parametrize("change", ["unknown-clause", "negated", "missing", "duplicate", "contradictory"])
def test_full_clause_recognition_rejects_unknown_or_incomplete_program(change):
    rows = conditions()
    if change == "unknown-clause": rows.append("SYN 추가 자격을 요구한다")
    elif change == "negated": rows[0] = rows[0].replace("이전에", "이후에")
    elif change == "missing": rows.pop()
    elif change == "duplicate": rows.append(rows[0])
    else: rows[1] += " 단 최저 평점을 적용한다"
    assert recognized_credit_scenario_conditions(rows) is None


@pytest.mark.parametrize("field,value", [("qualified_issuer", False), ("qualified_issuer", "true"),
    ("single_unchanged_rating", 1), ("attestation", "HUMAN_REVIEWED_NOTICE_CONDITIONS")])
def test_policy_is_explicit_and_separate_from_notice_review(client, monkeypatch, field, value):
    payload = prepare(client, monkeypatch)
    payload[field] = value
    assert client.post(SCENARIO, json=payload).status_code == 422


def test_unknown_notice_condition_blocks_registration_even_with_matching_hashes(client, monkeypatch):
    payload = prepare(client, monkeypatch)
    install(monkeypatch, literals=conditions() + ["SYN 추가 증빙 제출 필수"])
    assert client.post(SCENARIO, json=payload).status_code == 422
    assert client.get(CONTEXT).status_code == 422


def test_raw_policy_and_certificate_metadata_are_private_cache_inputs(client, monkeypatch):
    payload = prepare(client, monkeypatch)
    assert client.post(SCENARIO, json=payload).status_code == 200
    with client.app.state.session_factory() as session:
        notice, facts = load(session)
        def manifest():
            return _selected_fact_manifest(facts, fact_keys={CREDIT_SCENARIO_FACT_KEY}, deadline=notice.deadline)
        before = manifest()
        facts[0].evidence.metadata_json = {"SYN": "changed"}
        assert manifest() != before
        before = manifest()
        facts[0].effective_to = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert manifest() != before and len(manifest()) == 1
        assert set(manifest()[0]) == {"company_fact_id", "basis_sha256"}


def test_source_change_before_registration_is_conflict_and_postpublication_certificate_rejected(client, monkeypatch):
    payload = prepare(client, monkeypatch)
    bad = deepcopy(payload)
    bad["applications"][0]["conditions_sha256"] = "f" * 64
    assert client.post(SCENARIO, json=bad).status_code == 409
    with client.app.state.session_factory() as session:
        notice, _ = load(session)
        notice.published_at = datetime(2026, 7, 31, tzinfo=timezone.utc)
        session.commit()
    assert client.post(SCENARIO, json=payload).status_code == 422


def test_malformed_later_policy_returns_explicit_conflict(client, monkeypatch):
    payload = prepare(client, monkeypatch)
    assert client.post(SCENARIO, json=payload).status_code == 200
    with client.app.state.session_factory() as session:
        session.add(CompanyFact(fact_key=CREDIT_SCENARIO_FACT_KEY, value={"SYN": "malformed"},
                               value_label="SYN", verified=True, source="SYN",
                               effective_from=datetime(2026, 8, 1, tzinfo=timezone.utc)))
        session.commit()
    assert client.post(SCENARIO, json=payload).status_code == 409
