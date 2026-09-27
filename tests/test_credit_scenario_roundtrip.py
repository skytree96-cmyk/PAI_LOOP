"""SYN native source and approved scenario -> persisted public estimate."""
from dataclasses import replace
from datetime import datetime, timezone
import json

from sqlalchemy import func, select

from conftest import login_department_reader
from pai_loop import private_company_evidence as credit
from pai_loop.extraction_contracts import CURRENT_EXTRACTION_CONTRACT as CURRENT
from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.models import AnalysisRun, CompanyFact, Notice, NoticeVersion, ScoreSnapshot
from pai_loop.pps_enrichment import PPS_METADATA_SCHEMA, extract_pps_document_content
from pai_loop.quantitative_credit_scenario import CREDIT_SCENARIO_FACT_KEY
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from pai_loop.quantitative_scoring import QUANTITATIVE_ENGINE_VERSION
from test_credit_scenario import conditions, CONTEXT, ROOT, SCENARIO
from test_private_company_evidence import NOTICE_KEY, _seed_notice, PRIVATE_REFERENCE
from test_private_credit_registry import certificate, configured_company, setup
from test_quantitative_source_revalidation import fixture, native_docx, sha


def test_credit_scenario_persists_partial_estimate_and_public_read_does_not_recalculate(client, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("SYN external provider or public recalculation forbidden")

    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract_quantitative_probe", forbidden)
    original_settings = client.app.state.settings
    original_headers = dict(client.headers)
    setup(client)
    _seed_notice(client)
    inputs = fixture(credit=True, sibling=True, contract=CURRENT)
    attempt = inputs["source_attempt"]
    aid = attempt["attachment_id"]
    raw = attempt["result"]
    raw["quantitative_tables"][0]["criteria"][0]["recognition_conditions"] = [
        {"literal": literal, "evidence": {"attachment_id": aid, "page": 1,
         "section": "SYN", "quote": literal, "confidence": 1}}
        for literal in conditions()
    ]
    native = native_docx(inputs["canonical_text"] + "\n" + "\n".join(conditions()))
    parsed = extract_pps_document_content(attempt["source_label"], native)
    native_sha = sha(native)
    proof = validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(raw), source_text=parsed.text,
        attachment_id=aid, document_sha256=native_sha,
        manifest_sha256=inputs["expected_manifest_sha256"],
    )
    assert proof.status == "AVAILABLE", proof.issues
    attempt.update(document_sha256=native_sha, quantitative_validation_record=proof.model_dump(mode="json"))
    attempt["document_processing"] = {"source_text_sha256": sha(parsed.text),
        "source_read_complete": True, "analysis_input_complete": True}
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        notice.deadline = datetime(2030, 1, 31, 9, tzinfo=timezone.utc)
        notice.versions = [
            NoticeVersion(version_no=1, file_sha256="0" * 64, extraction_status="METADATA",
                document_complete=False, source_payload={"kind": "PPS_NOTICE_METADATA",
                    "schema_version": PPS_METADATA_SCHEMA, "attachment_manifest": inputs["full_manifest"]}),
            NoticeVersion(version_no=2, file_sha256=native_sha, extraction_status="ACCEPTED",
                document_complete=True, extraction_confidence=1, source_payload=attempt),
        ]
        session.commit()
    registration = certificate(issued_on="2026-07-31", effective_on="2026-07-01", valid_until="2030-02-01")
    response = client.post(ROOT + "/credit-ratings", json=registration)
    assert response.status_code == 200, response.text
    certificate_id = response.json()["certificate_id"]
    response = client.get(CONTEXT)
    assert response.status_code == 200, response.text
    application = {key: value for key, value in response.json().items()
                   if key in credit.CreditScenarioApplication.model_fields}
    policy = dict(certificate_id=certificate_id,
        certificate_registration_sha256=credit._certificate_metadata(
            credit.PrivateCreditCertificateRegistration.model_validate(registration))["registration_sha256"],
        attestation="HUMAN_APPROVED_CERTIFICATE_SCENARIO", accepted_on="2026-08-01",
        effective_through="2030-01-31", qualified_issuer=True, procurement_system_visible=True,
        single_unchanged_rating=True, same_legal_entity=True,
        no_succession_or_cooperative_exception=True, assumption="SOLE_BID_UNCHANGED_CERTIFICATE",
        applications=[application])
    response = client.post(SCENARIO, json=policy)
    assert response.status_code == 200, response.text
    client.app.state.settings = original_settings
    client.headers.update(original_headers)
    path = f"/api/v1/notices/{NOTICE_KEY}/quantitative-estimate"
    response = client.get(path)
    assert response.status_code == 200, response.text
    expected = response.json()
    assert expected["overall_status"] == "REVIEW"
    assert expected["activation_status"] == "PARTIAL_SOURCE"
    assert expected["confirmed_points"] == 0 and expected["estimated_points"] is None
    assert expected["criteria"][0]["status"] == "ESTIMATED"
    assert expected["criteria"][0]["estimated_points"] == 9
    batch = client.post("/api/v1/notices/analysis/batch", json={
        "notice_keys": [NOTICE_KEY], "enrich_missing": False, "dry_run": False})
    assert batch.status_code == 200, batch.text
    assert batch.json()["failed"] == batch.json()["openai_calls"] == 0, batch.json()
    assert batch.json()["results"][0]["snapshot_status"] == "CREATED"
    with client.app.state.session_factory() as session:
        fact = session.scalar(select(CompanyFact).where(CompanyFact.fact_key == CREDIT_SCENARIO_FACT_KEY))
        run = session.scalar(select(AnalysisRun))
        score = session.scalar(select(ScoreSnapshot).where(ScoreSnapshot.score_key == "quantitative.total"))
        assert fact.id in run.input_manifest["company_fact_ids"]
        assert score.method_version == QUANTITATIVE_ENGINE_VERSION
        assert score.status == "REVIEW" and score.value is None
        assert score.basis_json["public_criteria"]["items"][0]["status"] == "ESTIMATED"
        assert score.basis_json["confirmed_points"] == 0
        private_id = fact.id
        stored = json.dumps({"manifest": run.input_manifest, "score": score.basis_json})
        assert certificate_id not in stored and PRIVATE_REFERENCE not in stored
        before_runs = session.scalar(select(func.count(AnalysisRun.id)))
        before_versions = session.scalar(select(func.count(NoticeVersion.id)))
    monkeypatch.setattr("pai_loop.quantitative_scoring.estimate_for_notice", forbidden)
    client.app.state.settings = replace(original_settings, public_read_only=True)
    login_department_reader(client)
    response = client.get(path)
    assert response.status_code == 200, response.text
    actual = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert actual["overall_status"] == "REVIEW" and actual["activation_status"] == "PARTIAL_SOURCE"
    assert actual["confirmed_points"] == 0
    assert actual["estimated_points"] is actual["minimum_score"] is actual["meets_minimum"] is None
    assert actual["lower_points"] == actual["upper_points"] == 9
    item = actual["criteria"][0]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 9
    assert item["fact_binding_sha256"] is item["source_anchor"] is None
    assert "단독입찰" in item["rationale"] and "재확인" in item["rationale"]
    for private in (certificate_id, private_id, PRIVATE_REFERENCE, native_sha,
                    application["conditions_sha256"], "HUMAN_APPROVED_CERTIFICATE_SCENARIO"):
        assert private not in response.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(AnalysisRun.id))) == before_runs
        assert session.scalar(select(func.count(NoticeVersion.id))) == before_versions
