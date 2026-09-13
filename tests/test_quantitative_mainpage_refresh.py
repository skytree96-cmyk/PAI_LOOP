"""Five SYN stored-input paths; local test DB only, with provider calls forbidden."""
import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

import pytest
from sqlalchemy import func, select

from conftest import login_department_reader
from pai_loop.integrations.openai_extraction import ExtractionPayload, PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.models import AnalysisRun, Notice, NoticeVersion, ScoreSnapshot
from pai_loop.pps_enrichment import PPS_ATTACHMENT_SOURCE, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from pai_loop.quantitative_scoring import (
    QUANTITATIVE_ENGINE_VERSION, _current_dynamic_quantitative_profile,
    quantitative_company_fact_payload_sha256, quantitative_request_from_candidate_profile,
)
from test_quantitative_auto_activation import _company_fact
from test_quantitative_count_ranges import ATT, fixture


def stored_source(session, *, label, mixed):
    payload, source = fixture(inline=True)
    criterion = payload["quantitative_tables"][0]["criteria"][0]
    old = criterion["recognition_conditions"][0]["literal"]
    condition = old + ", 계약별 50명 이상"
    criterion["recognition_conditions"][0].update(
        literal=condition, evidence={**criterion["evidence"], "quote": condition},
    )
    source = source.replace(old, condition)
    evidence = lambda quote: dict(attachment_id=ATT, page=1, section="SYN 정량", quote=quote, confidence=1)
    credit = dict(
        criterion_id="SYN-CREDIT", label="기업신용평가등급", criterion_literal="SYN 기업신용평가등급 5점",
        max_points=5, scoring_method="CASE_TABLE", metric="CREDIT_RATING", unit="등급",
        brackets=[], threshold=None, formula_literal=None, required_evidence=["company.credit_rating"],
        recognition_conditions=[], ambiguity_reason=None, evidence=evidence("SYN 기업신용평가등급 5점"),
        cases=[dict(literal="BBB0 이상 5점", operator="IN", comparison_value=None, comparison_upper_value=None,
                    category_values=["BBB0 이상"], award_kind="POINTS", award_value=5, row_order=1,
                    evidence=evidence("BBB0 이상 5점")),
               dict(literal="BBB0 미만 4점", operator="IN", comparison_value=None, comparison_upper_value=None,
                    category_values=["BBB0 미만"], award_kind="POINTS", award_value=4, row_order=2,
                    evidence=evidence("BBB0 미만 4점"))],
    )
    table = payload["quantitative_tables"][0]
    table["criteria"] = [criterion, credit] if mixed else [credit]
    total = 10 if mixed else 5
    table["total_points"] = total
    table["total_evidence"] = evidence(f"SYN 정량평가 합계 {total}점")
    source += f"\nSYN 기업신용평가등급 5점\nBBB0 이상 5점\nBBB0 미만 4점\nSYN 정량평가 합계 {total}점"
    attachment_id = "PPS-ATT-" + hashlib.sha256(label.encode()).hexdigest()[:24]
    payload = ExtractionPayload.model_validate(json.loads(json.dumps(payload).replace(ATT, attachment_id)))
    attachment = dict(attachment_id=attachment_id, file_name="SYN 제안요청서.pdf", media_type="application/pdf",
        slot=1, url="https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-FREE&fileSeq=1")
    manifest = [attachment]
    document_sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(payload, source_text=source,
        attachment_id=attachment_id, document_sha256=document_sha, manifest_sha256=_digest(manifest))
    assert record.status == "AVAILABLE", [item.code for item in record.issues]
    notice = Notice(notice_key="SYN-FREE-" + label, bid_notice_no="SYN-FREE-" + label, revision_no="00",
        title="SYN 저장자료 무료 재계산", agency="SYN agency", status="OPEN",
        published_at=datetime(2026, 8, 1, tzinfo=timezone.utc), deadline=datetime(2030, 1, 31, tzinfo=timezone.utc))
    notice.versions = [
        NoticeVersion(version_no=1, file_sha256="a" * 64, extraction_status="METADATA", document_complete=False,
            source_payload={"kind":"PPS_NOTICE_METADATA", "schema_version":PPS_METADATA_SCHEMA, "attachment_manifest":manifest}),
        NoticeVersion(version_no=2, file_sha256=document_sha, extraction_status="ACCEPTED", document_complete=True,
            extraction_confidence=1, source_payload={
                "kind":"OPENAI_REQUIREMENT_EXTRACTION", "source_kind":PPS_ATTACHMENT_SOURCE,
                "attachment_id":attachment_id, "source_label":attachment["file_name"],
                "document_sha256":document_sha, "manifest_sha256":_digest(attachment),
                "current_manifest_sha256":_digest(manifest), "prompt_version":PROMPT_VERSION,
                "schema_version":SCHEMA_VERSION, "processing_version":PPS_PROCESSING_VERSION,
                "status":"ACCEPTED", "result":payload.model_dump(mode="json"),
                "document_processing":{"source_read_complete":True, "analysis_input_complete":True},
                "quantitative_validation_record":record.model_dump(mode="json"),
            }),
    ]
    session.add(notice)
    session.flush()
    req = quantitative_request_from_candidate_profile(_current_dynamic_quantitative_profile(notice))
    assert req.activation_status == "AUTO_ACTIVE", req.activation_reasons
    credit_binding = next(item.fact_binding_sha256 for item in req.criteria if item.metric_key == "company.credit_rating")
    fact = _company_fact(fact_key="company.credit_rating", value={"value":"A0", "unit":"등급", "fact_binding_sha256":credit_binding})
    fact.source = "SYN"
    fact.evidence.evidence_key = "SYN-CREDIT-EVIDENCE-" + label
    fact.evidence.metadata_json = {
        **fact.evidence.metadata_json,
        "company_fact_payload_sha256": quantitative_company_fact_payload_sha256(fact),
    }
    session.add(fact)
    session.flush()
    return notice, fact


@pytest.mark.parametrize("case", ["complete", "manual_performance_missing", "stale_fact", "source_unverified", "new_manifest"])
def test_stored_inputs_refresh_to_mainpage_without_paid_calls(client, monkeypatch, case):
    def forbidden(*args, **kwargs):
        raise AssertionError("SYN_PAID_OR_SOURCE_ENRICHMENT_FORBIDDEN")
    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract_quantitative_probe", forbidden)
    with client.app.state.session_factory() as session:
        notice, fact = stored_source(session, label=case, mixed=case != "complete")
        key = notice.notice_key
        if case == "stale_fact":
            fact.value = {**fact.value, "fact_binding_sha256":"f" * 64}
        elif case == "source_unverified":
            latest = notice.versions[-1]
            latest.source_payload = {**latest.source_payload, "quantitative_validation_record":{}}
        session.commit()
    path = f"/api/v1/notices/{key}/quantitative-estimate"
    internal = client.get(path)
    assert internal.status_code == 200
    if case == "source_unverified":
        assert internal.json()["lower_points"] is None
    elif case == "stale_fact":
        assert internal.json()["estimated_points"] is None
        assert internal.json()["confirmed_points"] == 0
    else:
        assert internal.json()["lower_points"] == 5, [(item["label"], item["rationale"]) for item in internal.json()["criteria"]]
    batch = client.post("/api/v1/notices/analysis/batch", json={"notice_keys":[key], "enrich_missing":False, "dry_run":False})
    assert batch.status_code == 200, batch.text
    body = batch.json()
    assert body["openai_calls"] == 0
    assert body["enrichment"]["requested"] == body["enrichment"]["attempted"] == 0
    assert body["failed"] == 0, body
    assert body["results"][0]["snapshot_status"] in {"CREATED", "REUSED"}
    with client.app.state.session_factory() as session:
        score = session.scalar(select(ScoreSnapshot).where(ScoreSnapshot.score_key=="quantitative.total"))
        assert score.method_version == QUANTITATIVE_ENGINE_VERSION
        assert score.lower_value == internal.json()["lower_points"]
        if case == "new_manifest":
            notice = session.scalar(select(Notice).where(Notice.notice_key == key))
            latest_metadata = next(v for v in notice.versions if v.source_payload.get("kind") == "PPS_NOTICE_METADATA")
            metadata = copy.deepcopy(latest_metadata.source_payload)
            metadata["attachment_manifest"][0]["file_name"] = "SYN 새 제안요청서.pdf"
            notice.versions.append(NoticeVersion(version_no=max(v.version_no for v in notice.versions)+1,
                file_sha256="d"*64, document_complete=False, extraction_status="METADATA", source_payload=metadata))
            session.commit()
        before_runs = session.scalar(select(func.count(AnalysisRun.id)))
        before_versions = session.scalar(select(func.count(NoticeVersion.id)))
    client.app.state.settings = replace(client.app.state.settings, public_read_only=True)
    login_department_reader(client)
    public = client.get(path)
    assert public.status_code == 200, public.text
    result = public.json()
    assert public.headers["cache-control"] == "no-store"
    if case in {"source_unverified", "new_manifest"}:
        assert result["lower_points"] is None
        assert result["estimated_points"] is None
    elif case == "complete":
        assert (result["confirmed_points"], result["lower_points"], result["estimated_points"]) == (5, 5, 5)
    elif case == "stale_fact":
        assert result["confirmed_points"] == 0
        assert result["estimated_points"] is None
    else:
        assert (result["confirmed_points"], result["lower_points"], result["upper_points"]) == (5, 5, 10)
        assert result["estimated_points"] is None
        assert any(item["estimated_points"] is None and item["rationale"] for item in result["criteria"])
    assert "SYN-CREDIT-EVIDENCE" not in public.text
    assert all(item["fact_binding_sha256"] is None and item["source_anchor"] is None for item in result["criteria"])
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(AnalysisRun.id))) == before_runs
        assert session.scalar(select(func.count(NoticeVersion.id))) == before_versions


def test_five_key_free_batch_is_not_limited_by_three_enrichment_targets(client, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("SYN_PAID_OR_SOURCE_ENRICHMENT_FORBIDDEN")
    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract_quantitative_probe", forbidden)
    keys = [f"SYN-FREE-BATCH-{index}" for index in range(5)]
    with client.app.state.session_factory() as session:
        session.add_all([Notice(notice_key=key, bid_notice_no=key, revision_no="00",
            title="SYN source pending", agency="SYN agency", status="OPEN",
            published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            deadline=datetime(2030, 1, 31, tzinfo=timezone.utc)) for key in keys])
        session.commit()
    response = client.post("/api/v1/notices/analysis/batch", json={
        "notice_keys": keys, "enrich_missing": False, "dry_run": False, "force": False,
        "max_notices": 3, "max_attachments_per_notice": 10,
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["processed"] == 5 and result["failed"] == 0
    assert [item["notice_key"] for item in result["results"]] == keys
    assert result["openai_calls"] == 0
    assert result["enrichment"]["requested"] == result["enrichment"]["attempted"] == 0
    assert all(item["snapshot_status"] == "CREATED" for item in result["results"])
    with client.app.state.session_factory() as session:
        scores = list(session.scalars(select(ScoreSnapshot).where(ScoreSnapshot.score_key == "quantitative.total")))
        assert len(scores) == 5
        assert all(score.value is None and score.lower_value is None for score in scores)
