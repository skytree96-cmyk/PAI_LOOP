"""SYN confirmed sufficient rows: credit, roster and selected performance records."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib

import pytest
from sqlalchemy import select

from pai_loop import private_company_evidence as credit
from pai_loop import quantitative_scoring as qs
from pai_loop.integrations.openai_extraction import ExtractionPayload, PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.models import CompanyFact, CompanyPerformanceRecord, Notice, NoticeVersion
from pai_loop.pps_enrichment import PPS_ATTACHMENT_SOURCE, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest
from pai_loop.quantitative_personnel import PERSONNEL_ROSTER_FACT_KEY
from pai_loop.quantitative_row_approval import (
    ROW_APPROVAL_REASON, SUFFICIENT_ROW_FACT_KEY, QuantitativeSufficientRowApproval,
)
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from test_dense_case_source_binding import credit_fixture
from test_private_company_evidence import _enable_production_operator, _operator_headers, _payload
from test_quantitative_personnel_binding import _envelope, _member
from test_quantitative_row_approval import _payload as hyphen_payload

ROOT = "/api/v1/operator-evidence"
SUFFICIENT = ROOT + "/quantitative-sufficient-rows"
ROW_APPROVALS = ROOT + "/quantitative-row-approvals"
UNREAD = "PPS-ATT-" + "e" * 24
DEADLINE = datetime(2030, 1, 31, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def configured_company(monkeypatch):
    monkeypatch.setattr(credit, "load_public_company_profile",
                        lambda: {"organization": {"display_name": "SYN Company"}})


def _rebind(value, old, new):
    if isinstance(value, dict):
        return {key: _rebind(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebind(item, old, new) for item in value]
    return new if value == old else value


def _store(client, key, attachment_id, payload, source, *, roster_members=0, records=()):
    attachment = dict(attachment_id=attachment_id, file_name="SYN 제안요청서.hwp", media_type="application/x-hwp",
                      slot=1, url=f"https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo={key}&fileSeq=1")
    manifest = [attachment, {**attachment, "attachment_id": UNREAD, "file_name": "SYN 공고문.pdf",
                             "media_type": "application/pdf", "slot": 2,
                             "url": attachment["url"].replace("fileSeq=1", "fileSeq=2")}]
    document_sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(
        payload, source_text=source, attachment_id=attachment_id,
        document_sha256=document_sha, manifest_sha256=_digest(manifest),
    )
    notice = Notice(notice_key=key, bid_notice_no=key, revision_no="00", title="SYN sufficient row",
                    agency="SYN agency", status="OPEN", published_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
                    deadline=DEADLINE)
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
    rows = [notice]
    if roster_members:
        roster = _envelope(snapshot_date="2030-01-01", verified_through="2030-01-31",
                           members=[_member(f"SYN-PRIVATE-MEMBER-{index}") for index in range(roster_members)])
        rows.append(CompanyFact(fact_key=PERSONNEL_ROSTER_FACT_KEY, value=roster, source="SYN-PRIVATE-ROSTER",
                                effective_from=datetime(2029, 12, 31, 15, tzinfo=timezone.utc), verified=True))
    rows.extend(records)
    with client.app.state.session_factory() as session:
        session.add_all(rows)
        session.commit()
    return record


def _credit_payload(*, review=True):
    raw, source = credit_fixture()
    attachment_id = "PPS-ATT-" + "c" * 24
    raw = _rebind(raw, "SYN-COUNT-RANGE", attachment_id)
    if review:
        raw["quantitative_tables"][0]["criteria"][0]["ambiguity_reason"] = "SYN 회사채와 기업신용평가등급 중 적용 평점이 모호함"
    return attachment_id, ExtractionPayload.model_validate(raw), source


def _performance_payload():
    attachment_id = "PPS-ATT-" + "d" * 24
    rows = (("5건 이상\n6.0", "GTE", 5, None, 6.0, 1), ("4건\n5.4", "EQ", 4, None, 5.4, 2),
            ("2~3건\n4.8", "BETWEEN", 2, 3, 4.8, 3), ("0~1건\n4.2", "BETWEEN", 0, 1, 4.2, 4))
    criterion = "2) SYN 사업 수행실적 평가 기준 및 배점: 6점\n최근 5년 이내 단일 계약 1억원 이상(VAT 포함) 계약 건수\n" + "\n".join(r[0] for r in rows)
    total = "SYN 정량평가 합계 6점"

    def ev(quote):
        return dict(attachment_id=attachment_id, page=None, section="SYN", quote=quote, confidence=1)

    payload = ExtractionPayload.model_validate({
        "document_type": "RFP", "requirements": [], "missing_or_unreadable": [], "summary": "SYN",
        "quantitative_tables": [{
            "table_id": "SYN-PERF", "label": "SYN 정량평가", "total_points": 6, "total_evidence": ev(total),
            "minimum_score": None, "minimum_evidence": None, "ambiguity_reason": None,
            "criteria": [{
                "criterion_id": "performance_count", "label": "SYN 사업 수행실적", "criterion_literal": criterion,
                "max_points": 6, "scoring_method": "CASE_TABLE", "metric": "PERFORMANCE_COUNT", "unit": "건",
                "brackets": [], "threshold": None, "formula_literal": None,
                "cases": [{"literal": lit, "operator": op, "comparison_value": value, "comparison_upper_value": upper,
                           "category_values": [], "award_kind": "POINTS", "award_value": award, "row_order": order,
                           "evidence": ev(lit)} for lit, op, value, upper, award, order in rows],
                "recognition_conditions": [], "required_evidence": ["company.performance.count"],
                "evidence": ev(criterion), "ambiguity_reason": "SYN 인정 범위가 원문에 모호하게 적힘",
            }],
        }],
    })
    return attachment_id, payload, total + "\n" + criterion


def _records(count, **changes):
    return [CompanyPerformanceRecord(
        record_key=f"SYN-RECORD-{index}", record_status="VALIDATED", project_name=f"SYN 박람회 {index}",
        end_date=date(2027, 6, 1), completed=True, gross_contract_amount_krw=150_000_000,
        contract_amount=150_000_000, vat_basis="INCLUDED", share_pct=100.0, **changes,
    ) for index in range(count)]


def _context(client, key):
    response = client.get(f"{ROOT}/notices/{key}/quantitative-rows/sufficient-context")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _approval(row, choice_index=0, **updates):
    choice = row["choices"][choice_index]
    value = dict(
        attestation="HUMAN_CONFIRMED_SUFFICIENT_ROW", accepted_on="2026-01-01", effective_through="2030-01-31",
        notice_key=row["notice_key"], manifest_sha256=row["manifest_sha256"], attachment_id=row["attachment_id"],
        document_sha256=row["document_sha256"], table_id=row["table_id"], criterion_id=row["criterion_id"],
        raw_candidate_sha256=row["raw_candidate_sha256"], row_state=row["row_state"],
        waived_issue_codes=row["issue_codes"], metric=row["metric"], row_kind=choice["row_kind"],
        row_index=choice["row_index"], row_literal=choice["row_literal"], award_points=choice["award_points"],
    )
    value.update(updates)
    return value


def _estimate(client, key):
    response = client.get(f"/api/v1/notices/{key}/quantitative-estimate")
    assert response.status_code == 200, response.text
    return response.json()


def _register_certificate(client):
    original_settings, original_headers = client.app.state.settings, dict(client.headers)
    _enable_production_operator(client)
    client.headers.update(_operator_headers())
    registration = _payload(company_name="SYN Company", issuer_name="SYN Rating Agency",
                            rating_kind="ENTERPRISE_CREDIT", purpose="PUBLIC_PROCUREMENT",
                            issued_on="2029-12-01", effective_on="2029-12-01", valid_until="2030-12-31")
    response = client.post(ROOT + "/credit-ratings", json=registration)
    assert response.status_code == 200, response.text
    sha = credit._certificate_metadata(credit.PrivateCreditCertificateRegistration.model_validate(registration))["registration_sha256"]
    return response.json()["certificate_id"], sha, (original_settings, original_headers)


def _restore(client, saved):
    client.app.state.settings = saved[0]
    client.headers.clear()
    client.headers.update(saved[1])


def _credit_block(row, certificate_id, sha):
    return {"certificate_id": certificate_id, "certificate_registration_sha256": sha,
            "conditions_sha256": row["conditions_sha256"], "qualified_issuer": True,
            "issued_before_publication": True, "valid_through_deadline": True,
            "no_succession_joint_cooperative_or_startup_exception": True}


def test_confirmed_personnel_row_scores_only_while_the_roster_falls_into_it(client):
    payload, source = hyphen_payload()
    attachment_id = payload.quantitative_tables[0].criteria[0].evidence.attachment_id
    _store(client, "SYN-SUFFICIENT-PERSONNEL", attachment_id, payload, source, roster_members=6)
    [row] = _context(client, "SYN-SUFFICIENT-PERSONNEL")
    assert row["row_state"] == "REVIEW" and row["waivable"] is True
    assert [choice["award_points"] for choice in row["choices"]] == [10, 8, 6]
    created = client.post(SUFFICIENT, json=_approval(row))
    assert created.status_code == 200, created.text
    assert created.json()["registration_status"] == "CREATED"
    assert client.post(SUFFICIENT, json=_approval(row)).json()["registration_status"] == "UNCHANGED"
    estimate = _estimate(client, "SYN-SUFFICIENT-PERSONNEL")
    assert estimate["activation_status"] == "PARTIAL_SOURCE" and ROW_APPROVAL_REASON in estimate["activation_reasons"]
    [item] = estimate["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 10 and item["max_points"] == 10
    assert "한 배점 행" in item["rationale"]

    with client.app.state.session_factory() as session:
        roster = session.scalar(select(CompanyFact).where(CompanyFact.fact_key == PERSONNEL_ROSTER_FACT_KEY))
        value = deepcopy(roster.value)
        value["members"] = value["members"][:2]
        roster.value = value
        session.commit()
    [item] = _estimate(client, "SYN-SUFFICIENT-PERSONNEL")["criteria"]
    assert item["status"] != "ESTIMATED" and item["estimated_points"] is None
    assert item["lower_points"] == 0 and item["upper_points"] == 10


def test_registration_refuses_a_row_the_company_value_does_not_reach(client):
    payload, source = hyphen_payload()
    attachment_id = payload.quantitative_tables[0].criteria[0].evidence.attachment_id
    _store(client, "SYN-SUFFICIENT-SHORT", attachment_id, payload, source, roster_members=2)
    [row] = _context(client, "SYN-SUFFICIENT-SHORT")
    response = client.post(SUFFICIENT, json=_approval(row))
    assert response.status_code == 409, response.text
    lower = client.post(SUFFICIENT, json=_approval(row, 2))  # 3명미만 is a one-sided lower tail: unsupported alone
    assert lower.status_code == 409
    with client.app.state.session_factory() as session:
        assert session.scalar(select(CompanyFact).where(CompanyFact.fact_key == SUFFICIENT_ROW_FACT_KEY)) is None


def test_confirmed_credit_row_uses_the_registered_certificate_and_stays_estimated(client):
    attachment_id, payload, source = _credit_payload(review=True)
    record = _store(client, "SYN-SUFFICIENT-CREDIT", attachment_id, payload, source)
    assert record.review_candidates, [issue.code for issue in record.issues]
    certificate_id, sha, saved = _register_certificate(client)
    [row] = _context(client, "SYN-SUFFICIENT-CREDIT")
    assert row["metric"] == "CREDIT_RATING" and row["row_state"] == "REVIEW" and row["waivable"] is True
    assert row["choices"][0]["award_points"] == 9
    stale = _approval(row, credit={**_credit_block(row, certificate_id, sha), "conditions_sha256": "0" * 64})
    assert client.post(SUFFICIENT, json=stale).status_code == 409  # rejected before any approval exists
    approval = _approval(row, credit=_credit_block(row, certificate_id, sha))
    created = client.post(SUFFICIENT, json=approval)
    assert created.status_code == 200, created.text
    _restore(client, saved)
    [item] = _estimate(client, "SYN-SUFFICIENT-CREDIT")["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 9
    assert "단독입찰" in item["rationale"] and "한 배점 행" in item["rationale"]


def test_available_credit_row_can_bind_the_certificate_without_the_scenario_grammar(client):
    attachment_id, payload, source = _credit_payload(review=False)
    record = _store(client, "SYN-SUFFICIENT-AVAILABLE", attachment_id, payload, source)
    assert record.available_candidates
    certificate_id, sha, saved = _register_certificate(client)
    [row] = _context(client, "SYN-SUFFICIENT-AVAILABLE")
    assert row["row_state"] == "AVAILABLE"
    response = client.post(SUFFICIENT, json=_approval(row, credit=_credit_block(row, certificate_id, sha)))
    assert response.status_code == 200, response.text
    _restore(client, saved)
    [item] = _estimate(client, "SYN-SUFFICIENT-AVAILABLE")["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 9


def test_selected_performance_records_are_recounted_on_every_read(client):
    attachment_id, payload, source = _performance_payload()
    records = _records(6)
    _store(client, "SYN-SUFFICIENT-PERF", attachment_id, payload, source, records=records)
    [row] = _context(client, "SYN-SUFFICIENT-PERF")
    assert row["metric"] == "PERFORMANCE_COUNT" and row["waivable"] is True
    with client.app.state.session_factory() as session:
        ids = sorted(str(item.id) for item in session.scalars(select(CompanyPerformanceRecord)))
    selection = {"record_ids": ids, "window_start": "2025-01-31", "window_end": "2030-01-30",
                 "min_amount_krw": 100_000_000, "vat_included_required": True}
    too_few = {**selection, "record_ids": ids[:3]}
    assert client.post(SUFFICIENT, json=_approval(row, performance=too_few)).status_code == 409
    created = client.post(SUFFICIENT, json=_approval(row, performance=selection))
    assert created.status_code == 200, created.text
    [item] = _estimate(client, "SYN-SUFFICIENT-PERF")["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 6
    with client.app.state.session_factory() as session:
        first = session.get(CompanyPerformanceRecord, ids[0])
        first.record_status = "DRAFT"
        session.commit()
    [item] = _estimate(client, "SYN-SUFFICIENT-PERF")["criteria"]
    assert item["status"] != "ESTIMATED" and item["estimated_points"] is None


def test_a_row_cannot_carry_both_approval_kinds(client):
    payload, source = hyphen_payload()
    attachment_id = payload.quantitative_tables[0].criteria[0].evidence.attachment_id
    _store(client, "SYN-ROW-APPROVAL", attachment_id, payload, source, roster_members=6)
    hyphen_rows = client.get(f"{ROOT}/notices/SYN-ROW-APPROVAL/quantitative-rows/approval-context").json()
    from test_quantitative_row_approval import _approval as hyphen_approval
    assert client.post(ROW_APPROVALS, json=hyphen_approval(hyphen_rows[0])).status_code == 200
    [row] = _context(client, "SYN-ROW-APPROVAL")
    assert client.post(SUFFICIENT, json=_approval(row)).status_code == 409


@pytest.mark.parametrize("bad", [
    {"row_state": "AVAILABLE"},
    {"waived_issue_codes": []},
    {"waived_issue_codes": ["UNKNOWN_METRIC"]},
    {"metric": "CREDIT_RATING"},
    {"effective_through": "2025-12-31"},
])
def test_model_rejects_unsafe_shapes(bad):
    base = dict(attestation="HUMAN_CONFIRMED_SUFFICIENT_ROW", accepted_on="2026-01-01", effective_through="2030-01-31",
                notice_key="SYN", manifest_sha256="a" * 64, attachment_id="A", document_sha256="b" * 64,
                table_id="T", criterion_id="C", raw_candidate_sha256="c" * 64, row_state="REVIEW",
                waived_issue_codes=["AMBIGUOUS_RULE"], metric="PERSONNEL_COUNT", row_kind="CASE", row_index=0,
                row_literal="5명 이상", award_points=10)
    QuantitativeSufficientRowApproval.model_validate(base)
    with pytest.raises(ValueError):
        QuantitativeSufficientRowApproval.model_validate({**base, **bad})


def test_confirmed_roster_evidence_on_a_sufficient_row_stays_estimated(client, monkeypatch):
    payload, source = hyphen_payload()
    attachment_id = payload.quantitative_tables[0].criteria[0].evidence.attachment_id
    _store(client, "SYN-SUFFICIENT-CAP", attachment_id, payload, source, roster_members=6)
    [row] = _context(client, "SYN-SUFFICIENT-CAP")
    assert client.post(SUFFICIENT, json=_approval(row)).status_code == 200

    def confirmed(criteria, _facts, **_kwargs):
        return [qs.QuantitativeFact(metric_key=item.metric_key, status="CONFIRMED", value=9,
                                    evidence_key=item.metric_key, fact_binding_sha256=item.fact_binding_sha256,
                                    confidence=1, rationale="SYN confirmed")
                for item in criteria if item.personnel_scope is not None]

    monkeypatch.setattr(qs, "resolve_personnel_register_facts", confirmed)
    estimate = _estimate(client, "SYN-SUFFICIENT-CAP")
    [item] = estimate["criteria"]
    assert item["status"] == "ESTIMATED" and item["estimated_points"] == 10
    assert estimate["confirmed_points"] == 0


def test_a_lone_at_least_row_must_be_the_top_band(client):
    payload, source = hyphen_payload()
    attachment_id = payload.quantitative_tables[0].criteria[0].evidence.attachment_id
    _store(client, "SYN-SUFFICIENT-TOPBAND", attachment_id, payload, source, roster_members=6)
    [row] = _context(client, "SYN-SUFFICIENT-TOPBAND")
    # '3명이상-8점' also matches 6 people, but alone it would drop the 5명 cutoff above it.
    middle = client.post(SUFFICIENT, json=_approval(row, 1))
    assert middle.status_code == 409, middle.text
    assert client.post(SUFFICIENT, json=_approval(row, 0)).status_code == 200
