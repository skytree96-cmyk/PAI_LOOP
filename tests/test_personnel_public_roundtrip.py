"""Synthetic roster -> validated source -> persisted estimate -> public API."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

import pytest
from sqlalchemy import func, select

from conftest import login_department_reader
from pai_loop.integrations.openai_extraction import ExtractionPayload, PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.models import AnalysisRun, CompanyFact, Notice, NoticeVersion, ScoreSnapshot
from pai_loop.pps_enrichment import PPS_ATTACHMENT_SOURCE, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest
from pai_loop.quantitative_personnel import PERSONNEL_ROSTER_FACT_KEY
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from pai_loop.quantitative_scoring import QUANTITATIVE_ENGINE_VERSION
from test_quantitative_personnel_binding import _envelope, _member


def _stored_personnel_source(session, *, projected, incomplete):
    label = f"SYN-PERSONNEL-{projected}-{incomplete}"
    attachment_id = "PPS-ATT-" + hashlib.sha256(label.encode()).hexdigest()[:24]
    criterion_literal = "SYN 학사학위이상 보유인력 20점"
    row_literal = "2명 이상 20점, 미충족 0점"
    total_literal = "SYN 정량평가 합계 20점"
    source = "\n".join((criterion_literal, row_literal, total_literal))

    def evidence(quote):
        return dict(attachment_id=attachment_id, page=1, section="SYN 정량", quote=quote, confidence=1)

    payload = ExtractionPayload.model_validate({
        "document_type": "RFP", "requirements": [], "missing_or_unreadable": [],
        "summary": "SYN validated personnel score table",
        "quantitative_tables": [{
            "table_id": "SYN-TABLE", "label": "SYN 정량평가", "total_points": 20,
            "total_evidence": evidence(total_literal), "minimum_score": None,
            "minimum_evidence": None, "ambiguity_reason": None,
            "criteria": [{
                "criterion_id": "SYN-PERSONNEL", "label": "SYN 학사학위이상 보유인력",
                "criterion_literal": criterion_literal, "max_points": 20,
                "scoring_method": "THRESHOLD", "metric": "PERSONNEL_COUNT", "unit": "명",
                "brackets": [], "cases": [], "formula_literal": None,
                "recognition_conditions": [], "required_evidence": ["company.personnel.count"],
                "evidence": evidence(criterion_literal), "ambiguity_reason": None,
                "threshold": {"literal": row_literal, "operator": "GTE", "threshold_value": 2,
                              "points_if_met": 20, "points_if_not_met": 0,
                              "evidence": evidence(row_literal)},
            }],
        }],
    })
    attachment = dict(
        attachment_id=attachment_id, file_name="SYN 제안요청서.pdf", media_type="application/pdf",
        slot=1, url="https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-PERSONNEL&fileSeq=1",
    )
    manifest = [attachment]
    if incomplete:
        manifest.append({**attachment, "attachment_id": "PPS-ATT-" + "f" * 24,
                         "file_name": "SYN unread attachment.pdf", "slot": 2,
                         "url": attachment["url"].replace("fileSeq=1", "fileSeq=2")})
    document_sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(
        payload, source_text=source, attachment_id=attachment_id,
        document_sha256=document_sha, manifest_sha256=_digest(manifest),
    )
    assert record.status == "AVAILABLE", [issue.code for issue in record.issues]
    notice = Notice(
        notice_key=label, bid_notice_no=label, revision_no="00", title="SYN personnel estimate",
        agency="SYN agency", status="OPEN", published_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        deadline=datetime(2030, 1, 31, 9, tzinfo=timezone.utc),
    )
    notice.versions = [
        NoticeVersion(version_no=1, file_sha256="c" * 64, extraction_status="METADATA", document_complete=False,
                      source_payload={"kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA,
                                      "attachment_manifest": manifest}),
        NoticeVersion(version_no=2, file_sha256=document_sha, extraction_status="ACCEPTED", document_complete=True,
                      extraction_confidence=1, source_payload={
                          "kind": "OPENAI_REQUIREMENT_EXTRACTION", "source_kind": PPS_ATTACHMENT_SOURCE,
                          "attachment_id": attachment_id, "source_label": attachment["file_name"],
                          "document_sha256": document_sha, "manifest_sha256": _digest(attachment),
                          "current_manifest_sha256": _digest(manifest), "prompt_version": PROMPT_VERSION,
                          "schema_version": SCHEMA_VERSION, "processing_version": PPS_PROCESSING_VERSION,
                          "status": "ACCEPTED", "result": payload.model_dump(mode="json"),
                          "document_processing": {"source_read_complete": True, "analysis_input_complete": True},
                          "quantitative_validation_record": record.model_dump(mode="json"),
                      }),
    ]
    roster = _envelope(
        snapshot_date="2030-01-01", verified_through="2030-01-15" if projected else "2030-01-31",
        members=[_member(key=f"SYN-PRIVATE-MEMBER-{index}",
                         degrees=[{"level": "BACHELOR", "major": "SYN-PRIVATE-MAJOR"}]) for index in range(2)],
    )
    if projected:
        roster.update(projection_through="2030-01-31", projection_assumption="CURRENT_ROSTER_UNCHANGED")
    fact = CompanyFact(
        fact_key=PERSONNEL_ROSTER_FACT_KEY, value=roster, source="SYN-PRIVATE-ROSTER",
        effective_from=datetime(2029, 12, 31, 15, tzinfo=timezone.utc), verified=True,
    )
    session.add_all([notice, fact])
    session.flush()
    return notice.notice_key, fact.id, document_sha


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("incomplete", [False, True])
def test_personnel_estimate_persists_and_restores_public_items_without_private_inputs(
    client, monkeypatch, projected, incomplete,
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("SYN provider calls and public recalculation are forbidden")

    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract_quantitative_probe", forbidden)
    with client.app.state.session_factory() as session:
        key, roster_id, document_sha = _stored_personnel_source(
            session, projected=projected, incomplete=incomplete,
        )
        session.commit()

    path = f"/api/v1/notices/{key}/quantitative-estimate"
    internal = client.get(path)
    assert internal.status_code == 200, internal.text
    expected = internal.json()
    assert expected["activation_status"] == ("PARTIAL_SOURCE" if incomplete else "AUTO_ACTIVE")
    assert expected["overall_status"] == ("REVIEW" if incomplete else "ESTIMATED")
    assert expected["confirmed_points"] == 0
    assert expected["lower_points"] == expected["upper_points"] == 20
    assert expected["estimated_points"] == (None if incomplete else 20)
    assert expected["criteria"][0]["status"] == "ESTIMATED"
    assert expected["criteria"][0]["estimated_points"] == 20

    batch = client.post("/api/v1/notices/analysis/batch", json={
        "notice_keys": [key], "enrich_missing": False, "dry_run": False,
    })
    assert batch.status_code == 200, batch.text
    assert batch.json()["failed"] == batch.json()["openai_calls"] == 0, batch.json()
    assert batch.json()["results"][0]["snapshot_status"] == "CREATED"
    with client.app.state.session_factory() as session:
        run = session.scalar(select(AnalysisRun))
        score = session.scalar(select(ScoreSnapshot).where(ScoreSnapshot.score_key == "quantitative.total"))
        assert roster_id in run.input_manifest["company_fact_ids"]
        assert score.method_version == QUANTITATIVE_ENGINE_VERSION
        assert score.status == expected["overall_status"]
        assert score.value == expected["estimated_points"]
        assert score.basis_json["confirmed_points"] == 0
        assert score.basis_json["public_criteria"]["items"][0]["status"] == "ESTIMATED"
        stored = json.dumps({"manifest": run.input_manifest, "score": score.basis_json})
        assert "SYN-PRIVATE" not in stored
        before_runs = session.scalar(select(func.count(AnalysisRun.id)))
        before_versions = session.scalar(select(func.count(NoticeVersion.id)))

    monkeypatch.setattr("pai_loop.quantitative_scoring.estimate_for_notice", forbidden)
    client.app.state.settings = replace(client.app.state.settings, public_read_only=True)
    login_department_reader(client)
    public = client.get(path)
    assert public.status_code == 200, public.text
    actual = public.json()
    assert public.headers["cache-control"] == "no-store"
    for field in ("activation_status", "overall_status", "estimated_points", "confirmed_points",
                  "lower_points", "upper_points", "total_max_points"):
        assert actual[field] == expected[field]
    assert len(actual["criteria"]) == 1
    item = actual["criteria"][0]
    assert item["label"] == "전문인력 보유"
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 20
    assert item["fact_binding_sha256"] is item["source_anchor"] is None
    assert "현재 명부 기반 추정" in item["rationale"]
    assert "유지 가정" in item["rationale"] and "재확인" in item["rationale"]
    for private in ("SYN-PRIVATE", roster_id, document_sha, "a" * 64, "member_key", "joined_on",
                    "research_grade", "source_sha256", "verification_attestation"):
        assert private not in public.text
    if incomplete:
        assert actual["estimated_points"] is actual["minimum_score"] is actual["meets_minimum"] is None
        assert any("공고 총점이 아닙니다" in assumption for assumption in actual["assumptions"])
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(AnalysisRun.id))) == before_runs
        assert session.scalar(select(func.count(NoticeVersion.id))) == before_versions
