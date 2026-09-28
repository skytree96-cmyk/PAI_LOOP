"""SYN hyphen-separated CASE row -> reviewed row approval -> estimated subtotal."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import login_department_reader
from pai_loop.integrations.openai_extraction import ExtractionPayload, PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.models import AnalysisRun, CompanyFact, Notice, NoticeVersion, ScoreSnapshot
from pai_loop.pps_enrichment import PPS_ATTACHMENT_SOURCE, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest
from pai_loop.quantitative_personnel import PERSONNEL_ROSTER_FACT_KEY
from pai_loop.quantitative_row_approval import (
    ROW_APPROVAL_FACT_KEY, ROW_APPROVAL_REASON, ROW_APPROVAL_SOURCE, QuantitativeRowApproval,
    hyphen_case_rows_match,
)
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from pai_loop import quantitative_scoring as qs
from test_quantitative_personnel_binding import _envelope, _member

ROOT = "/api/v1/operator-evidence"
APPROVALS = ROOT + "/quantitative-row-approvals"
KEY = "SYN-ROW-APPROVAL"
ATT = "PPS-ATT-" + hashlib.sha256(b"SYN-ROW-APPROVAL").hexdigest()[:24]
UNREAD = "PPS-ATT-" + "e" * 24
DEADLINE = datetime(2030, 1, 31, 9, tzinfo=timezone.utc)
ROWS = (("5명이상-10점", "GTE", 5, 10, 1), ("3명이상-8점", "GTE", 3, 8, 2), ("3명미만-6점", "LT", 3, 6, 3))
CONTEXT = f"{ROOT}/notices/{KEY}/quantitative-rows/approval-context"
ESTIMATE = f"/api/v1/notices/{KEY}/quantitative-estimate"


def _evidence(quote):
    return dict(attachment_id=ATT, page=None, section="SYN 제안서 평가지표", quote=quote, confidence=1)


def _payload(*, extra=""):
    criterion = ("o SYN 제안업체 인력 보유상태\n- SYN 연수지원 인력 조직" + extra
                 + "\n※ SYN 인력보유상태는 재직증명서로 확인 가능한 경우에 한하여 인정\n"
                 + "\n".join(row[0] for row in ROWS))
    total = "o SYN 제안 업체 인력 상태\n10점"
    condition = "SYN 인력보유상태는 재직증명서로 확인 가능한 경우에 한하여 인정"
    payload = ExtractionPayload.model_validate({
        "document_type": "RFP", "requirements": [], "missing_or_unreadable": [], "summary": "SYN",
        "quantitative_tables": [{
            "table_id": "SYN-TBL", "label": "SYN 객관적 평가(10점)", "total_points": 10,
            "total_evidence": _evidence(total), "minimum_score": None, "minimum_evidence": None,
            "ambiguity_reason": None,
            "criteria": [{
                "criterion_id": "personnel_count", "label": "SYN 제안업체 인력 보유상태",
                "criterion_literal": criterion, "max_points": 10, "scoring_method": "CASE_TABLE",
                "metric": "PERSONNEL_COUNT", "unit": "명", "brackets": [], "threshold": None,
                "formula_literal": None,
                "cases": [{"literal": literal, "operator": operator, "comparison_value": value,
                           "comparison_upper_value": None, "category_values": [], "award_kind": "POINTS",
                           "award_value": award, "row_order": order, "evidence": _evidence(literal)}
                          for literal, operator, value, award, order in ROWS],
                "recognition_conditions": [{"literal": condition, "evidence": _evidence("※ " + condition)}],
                "required_evidence": ["company.personnel.count"], "evidence": _evidence(criterion),
                "ambiguity_reason": None,
            }],
        }],
    })
    return payload, total + "\n" + criterion


def _store(client, payload, source, *, members=6):
    attachment = dict(attachment_id=ATT, file_name="SYN 제안요청서.hwp", media_type="application/x-hwp",
                      slot=1, url="https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-ROW&fileSeq=1")
    manifest = [attachment, {**attachment, "attachment_id": UNREAD, "file_name": "SYN 공고문.pdf",
                             "media_type": "application/pdf", "slot": 2,
                             "url": attachment["url"].replace("fileSeq=1", "fileSeq=2")}]
    document_sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(
        payload, source_text=source, attachment_id=ATT,
        document_sha256=document_sha, manifest_sha256=_digest(manifest),
    )
    notice = Notice(notice_key=KEY, bid_notice_no=KEY, revision_no="00", title="SYN row approval",
                    agency="SYN agency", status="OPEN", published_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
                    deadline=DEADLINE)
    notice.versions = [
        NoticeVersion(version_no=1, file_sha256="c" * 64, extraction_status="METADATA", document_complete=False,
                      source_payload={"kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA,
                                      "attachment_manifest": manifest}),
        NoticeVersion(version_no=2, file_sha256=document_sha, extraction_status="ACCEPTED", document_complete=True,
                      extraction_confidence=1, source_payload={
                          "kind": "OPENAI_REQUIREMENT_EXTRACTION", "source_kind": PPS_ATTACHMENT_SOURCE,
                          "attachment_id": ATT, "source_label": attachment["file_name"],
                          "document_sha256": document_sha, "manifest_sha256": _digest(attachment),
                          "current_manifest_sha256": _digest(manifest), "prompt_version": PROMPT_VERSION,
                          "schema_version": SCHEMA_VERSION, "processing_version": PPS_PROCESSING_VERSION,
                          "status": "ACCEPTED", "result": payload.model_dump(mode="json"),
                          "document_processing": {"source_read_complete": True, "analysis_input_complete": True},
                          "quantitative_validation_record": record.model_dump(mode="json"),
                      }),
    ]
    roster = _envelope(snapshot_date="2030-01-01", verified_through="2030-01-31",
                       members=[_member(f"SYN-PRIVATE-MEMBER-{index}") for index in range(members)])
    with client.app.state.session_factory() as session:
        session.add_all([notice, CompanyFact(
            fact_key=PERSONNEL_ROSTER_FACT_KEY, value=roster, source="SYN-PRIVATE-ROSTER",
            effective_from=datetime(2029, 12, 31, 15, tzinfo=timezone.utc), verified=True,
        )])
        session.commit()
    return record, document_sha


def _approval(row, **updates):
    value = dict(
        attestation="HUMAN_REVIEWED_ROW_PROGRAM", interpretation="HYPHEN_SEPARATED_POINTS",
        accepted_on="2026-01-01", effective_through="2030-01-31",
        notice_key=row["notice_key"], manifest_sha256=row["manifest_sha256"],
        attachment_id=row["attachment_id"], document_sha256=row["document_sha256"],
        table_id=row["table_id"], criterion_id=row["criterion_id"],
        raw_candidate_sha256=row["raw_candidate_sha256"], waived_issue_codes=row["issue_codes"],
        program=row["program"],
    )
    value.update(updates)
    return value


def _context_row(client):
    response = client.get(CONTEXT)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    [row] = response.json()
    return row


def _forbid_provider(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("SYN provider call forbidden")
    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", forbidden)


def test_reviewed_hyphen_row_persists_estimated_partial_subtotal_and_public_disclosure(client, monkeypatch):
    _forbid_provider(monkeypatch)
    payload, source = _payload()
    record, document_sha = _store(client, payload, source)
    assert {item.criterion_id: set(item.issue_codes) for item in record.review_candidates} == {
        "personnel_count": {"CASE_NUMBER_MISMATCH", "MAX_POINTS_LITERAL_MISMATCH"}}
    before = client.get(ESTIMATE).json()
    assert before["criteria"] == [] and before["activation_status"] == "REVIEW_REQUIRED"

    row = _context_row(client)
    assert row["approvable"] is True
    assert row["issue_codes"] == ["CASE_NUMBER_MISMATCH", "MAX_POINTS_LITERAL_MISMATCH"]
    assert [case["award_value"] for case in row["program"]["cases"]] == [10, 8, 6]
    approval = _approval(row)
    created = client.post(APPROVALS, json=approval)
    assert created.status_code == 200, created.text
    assert created.json() == {"registration_status": "CREATED", "assurance": "ESTIMATED_ONLY"}
    assert client.post(APPROVALS, json=approval).json()["registration_status"] == "UNCHANGED"
    conflicting = _approval(row, accepted_on="2026-01-02")
    assert client.post(APPROVALS, json=conflicting).status_code == 409

    estimate = client.get(ESTIMATE).json()
    assert estimate["activation_status"] == "PARTIAL_SOURCE"
    assert ROW_APPROVAL_REASON in estimate["activation_reasons"]
    assert estimate["overall_status"] == "REVIEW" and estimate["confirmed_points"] == 0
    assert estimate["estimated_points"] is estimate["minimum_score"] is None
    [item] = estimate["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 10 and item["max_points"] == 10
    assert "사람이 원문 행" in item["rationale"]

    batch = client.post("/api/v1/notices/analysis/batch", json={
        "notice_keys": [KEY], "enrich_missing": False, "dry_run": False})
    assert batch.status_code == 200, batch.text
    assert batch.json()["failed"] == batch.json()["openai_calls"] == 0, batch.json()
    with client.app.state.session_factory() as session:
        approval_fact = session.scalar(select(CompanyFact).where(CompanyFact.fact_key == ROW_APPROVAL_FACT_KEY))
        run = session.scalar(select(AnalysisRun))
        score = session.scalar(select(ScoreSnapshot).where(ScoreSnapshot.score_key == "quantitative.total"))
        assert approval_fact.id in run.input_manifest["company_fact_ids"]
        assert score.basis_json["public_criteria"]["items"][0]["status"] == "ESTIMATED"
        assert ROW_APPROVAL_REASON in score.basis_json["activation_reasons"]
        stored = json.dumps({"manifest": run.input_manifest, "score": score.basis_json}, ensure_ascii=False)
        assert row["raw_candidate_sha256"] not in stored

    original = client.app.state.settings
    monkeypatch.setattr("pai_loop.quantitative_scoring.estimate_for_notice", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("SYN public recalculation forbidden")))
    client.app.state.settings = replace(original, public_read_only=True)
    login_department_reader(client)
    public = client.get(ESTIMATE)
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["lower_points"] == body["upper_points"] == 10 and body["estimated_points"] is None
    assert body["criteria"][0]["status"] == "ESTIMATED"
    assert "사람이 행 해석을 승인" in body["opinion"]
    text = json.dumps(body, ensure_ascii=False)
    for private in (row["raw_candidate_sha256"], document_sha, ATT, "HUMAN_REVIEWED_ROW_PROGRAM"):
        assert private not in text


@pytest.mark.parametrize("change", ["program", "codes", "raw", "manifest", "document", "horizon", "table"])
def test_registration_rejects_any_changed_binding(client, change):
    payload, source = _payload()
    _store(client, payload, source)
    row = _context_row(client)
    updates = {
        "program": {"program": {**row["program"], "cases": [
            {**row["program"]["cases"][0], "award_value": 9}, *row["program"]["cases"][1:]]}},
        "codes": {"waived_issue_codes": ["CASE_NUMBER_MISMATCH"]},
        "raw": {"raw_candidate_sha256": "0" * 64},
        "manifest": {"manifest_sha256": "1" * 64},
        "document": {"document_sha256": "2" * 64},
        "horizon": {"effective_through": "2030-01-30"},
        "table": {"table_id": "SYN-OTHER"},
    }[change]
    response = client.post(APPROVALS, json=_approval(row, **updates))
    assert response.status_code == 409, response.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(CompanyFact).where(CompanyFact.fact_key == ROW_APPROVAL_FACT_KEY)) is None


@pytest.mark.parametrize("state", ["unverified", "foreign_source", "duplicate", "expired", "malformed"])
def test_resolver_ignores_untrusted_or_duplicate_stored_approvals(client, state):
    payload, source = _payload()
    _store(client, payload, source)
    row = _context_row(client)
    value = QuantitativeRowApproval.model_validate(_approval(row)).model_dump(mode="json")
    start = datetime(2025, 12, 31, 15, tzinfo=timezone.utc)
    facts = [CompanyFact(fact_key=ROW_APPROVAL_FACT_KEY, value=value, source=ROW_APPROVAL_SOURCE,
                         verified=True, effective_from=start, effective_to=None)]
    if state == "unverified":
        facts[0].verified = False
    elif state == "foreign_source":
        facts[0].source = "SYN-OTHER"
    elif state == "duplicate":
        facts.append(CompanyFact(fact_key=ROW_APPROVAL_FACT_KEY, value={**value, "accepted_on": "2026-01-02"},
                                 source=ROW_APPROVAL_SOURCE, verified=True, effective_from=start))
    elif state == "expired":
        facts[0].effective_to = datetime(2030, 1, 30, tzinfo=timezone.utc)
    elif state == "malformed":
        facts[0].value = {**value, "waived_issue_codes": ["UNKNOWN_METRIC"]}
    with client.app.state.session_factory() as session:
        session.add_all(facts)
        session.commit()
    estimate = client.get(ESTIMATE).json()
    assert estimate["criteria"] == [] and ROW_APPROVAL_REASON not in estimate["activation_reasons"]


def test_deduction_wording_is_never_read_as_a_hyphen_separator(client):
    payload, source = _payload(extra="\n- SYN 미충족 인원은 감점")
    record, _ = _store(client, payload, source)
    raw = payload.quantitative_tables[0].criteria[0]
    assert hyphen_case_rows_match(raw) is False
    assert hyphen_case_rows_match(_payload()[0].quantitative_tables[0].criteria[0]) is True
    if record.review_candidates:
        row = _context_row(client)
        assert row["approvable"] is False
        assert client.post(APPROVALS, json=_approval(row)).status_code in {409, 422}


def test_confirmed_evidence_on_an_approved_row_stays_estimated(client, monkeypatch):
    payload, source = _payload()
    _store(client, payload, source)
    row = _context_row(client)
    assert client.post(APPROVALS, json=_approval(row)).status_code == 200

    def confirmed(criteria, _facts, **_kwargs):
        return [qs.QuantitativeFact(metric_key=item.metric_key, status="CONFIRMED", value=4,
                                    evidence_key=item.metric_key, fact_binding_sha256=item.fact_binding_sha256,
                                    confidence=1, rationale="SYN confirmed")
                for item in criteria if item.personnel_scope is not None]

    monkeypatch.setattr(qs, "resolve_personnel_register_facts", confirmed)
    estimate = client.get(ESTIMATE).json()
    [item] = estimate["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 8
    assert estimate["confirmed_points"] == 0


def test_approval_context_requires_private_operator_auth(client):
    payload, source = _payload()
    _store(client, payload, source)
    with TestClient(client.app) as anonymous:
        assert anonymous.get(CONTEXT).status_code == 401
        assert anonymous.post(APPROVALS, json={}).status_code == 401


def test_approval_model_rejects_unreviewable_codes_and_reversed_horizon():
    base = dict(attestation="HUMAN_REVIEWED_ROW_PROGRAM", interpretation="HYPHEN_SEPARATED_POINTS",
                accepted_on="2026-01-02", effective_through="2026-01-31", notice_key=KEY,
                manifest_sha256="a" * 64, attachment_id=ATT, document_sha256="b" * 64, table_id="T",
                criterion_id="C", raw_candidate_sha256="c" * 64, waived_issue_codes=["CASE_NUMBER_MISMATCH"],
                program={"metric": "PERSONNEL_COUNT", "scoring_method": "CASE_TABLE", "max_points": 10,
                         "unit": "명", "cases": [{"literal": "5명이상-10점", "operator": "GTE",
                                                  "comparison_value": 5, "award_value": 10}]})
    QuantitativeRowApproval.model_validate(base)
    for bad in ({"waived_issue_codes": ["AMBIGUOUS_RULE"]},
                {"waived_issue_codes": ["MAX_POINTS_LITERAL_MISMATCH", "CASE_NUMBER_MISMATCH"]},
                {"effective_through": "2026-01-01"}):
        with pytest.raises(ValueError):
            QuantitativeRowApproval.model_validate({**base, **bad})
