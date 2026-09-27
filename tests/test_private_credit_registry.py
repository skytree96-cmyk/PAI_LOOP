"""SYN certificate registry, independent of extraction and paid providers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from pai_loop.models import CompanyFact, Evidence, Notice, NoticeVersion
from pai_loop import private_company_evidence as credit
from pai_loop.extraction_contracts import CURRENT_EXTRACTION_CONTRACT
from pai_loop.pps_enrichment import PPS_METADATA_SCHEMA
from pai_loop.quantitative_rule_extraction import (
    QuantitativeReviewCandidate,
    merge_validated_quantitative_records,
)
from pai_loop.quantitative_scoring import (
    estimate_for_notice, estimate_quantitative_score, quantitative_request_from_candidate_profile,
    resolve_verified_quantitative_facts,
)
from test_dense_case_source_binding import credit_fixture, record, ATT, DOC, MANIFEST
from test_private_company_evidence import (
    _payload, _seed_notice, _enable_production_operator, _operator_headers,
    NOTICE_KEY, DOCUMENT_SHA256, PRIVATE_REFERENCE,
)
from test_quantitative_source_revalidation import fixture as source_fixture

ROOT = "https://testserver/api/v1/operator-evidence"
REGISTER = ROOT + "/credit-ratings"
CONTEXT = ROOT + f"/notices/{NOTICE_KEY}/credit-rating/binding"
BIND = ROOT + f"/notices/{NOTICE_KEY}/credit-rating/bind"


def certificate(**changes):
    return _payload(company_name="SYN Company", issuer_name="SYN Rating Agency",
                    rating_kind="ENTERPRISE_CREDIT", purpose="PUBLIC_PROCUREMENT", **changes)


@pytest.fixture(autouse=True)
def configured_company(monkeypatch):
    monkeypatch.setattr(credit, "load_public_company_profile",
                        lambda: {"organization": {"display_name": "SYN Company"}})


def setup(client):
    _enable_production_operator(client)
    client.headers.update(_operator_headers())


def counts(client):
    with client.app.state.session_factory() as session:
        return (session.scalar(select(func.count(Evidence.id)).where(Evidence.evidence_key.like("PRIVATE-CREDIT-%"))),
                session.scalar(select(func.count(CompanyFact.id)).where(CompanyFact.source == "PRIVATE_DOCUMENT")))


def install_profile(monkeypatch, *, partial=False):
    raw, source = credit_fixture()
    stored = record(raw, source)
    assert stored.status == "AVAILABLE", stored.issues
    documents = {ATT: DOC}
    if partial:
        documents["SYN-UNREAD-SIBLING"] = "c" * 64
    profile = merge_validated_quantitative_records([stored], expected_documents=documents,
                                                   manifest_sha256=MANIFEST)
    monkeypatch.setattr(credit, "_current_dynamic_quantitative_profile", lambda _notice: profile)
    return profile


def seed_partial_credit_notice(client):
    inputs = source_fixture(credit=True, sibling=True, contract=CURRENT_EXTRACTION_CONTRACT)
    _seed_notice(client)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        session.add_all([
            NoticeVersion(notice=notice, version_no=1, file_sha256="0" * 64,
                document_complete=False, extraction_status="METADATA",
                source_payload={"kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA,
                                "attachment_manifest": inputs["full_manifest"]}),
            NoticeVersion(notice=notice, version_no=2,
                file_sha256=inputs["expected_native_sha256"], document_complete=True,
                extraction_status="ACCEPTED", source_payload=inputs["source_attempt"]),
        ])
        session.commit()


def binding_payload(certificate_id, binding):
    return {"certificate_id": certificate_id, "expected_fact_binding_sha256": binding,
            "verification_attestation": "HUMAN_REVIEWED_NOTICE_CONDITIONS"}


def test_register_and_read_without_notice_then_preserve_certificate_when_rules_missing(client):
    setup(client)
    created = client.post(REGISTER, json=certificate())
    assert created.status_code == 200, created.text
    data = created.json()
    assert data["registration_status"] == "CREATED" and data["notice_binding_required"] is True
    assert created.headers["cache-control"] == "no-store"
    assert DOCUMENT_SHA256 not in created.text and PRIVATE_REFERENCE not in created.text
    again = client.post(REGISTER, json=certificate())
    assert again.json()["certificate_id"] == data["certificate_id"]
    assert again.json()["registration_status"] == "UNCHANGED"
    fetched = client.get(REGISTER + "/" + data["certificate_id"])
    assert fetched.status_code == 200 and fetched.json()["registration_status"] == "REGISTERED"
    assert counts(client) == (1, 0)
    _seed_notice(client)  # Deliberately no source records / no scoreable rules.
    assert client.get(CONTEXT).status_code == 422
    assert client.post(BIND, json=binding_payload(data["certificate_id"], "b" * 64)).status_code == 422
    assert counts(client) == (1, 0)
    assert client.get(REGISTER + "/" + data["certificate_id"]).status_code == 200


def test_registered_source_binds_into_real_compiled_credit_rule_and_legacy_reuses_it(client, monkeypatch):
    setup(client)
    _seed_notice(client)
    profile = install_profile(monkeypatch)
    created = client.post(REGISTER, json=certificate()).json()
    context = client.get(CONTEXT)
    assert context.status_code == 200, context.text
    binding = context.json()["fact_binding_sha256"]
    payload = binding_payload(created["certificate_id"], binding)
    result = client.post(BIND, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["binding_status"] == "CREATED"
    assert client.post(BIND, json=payload).json()["binding_status"] == "UNCHANGED"
    old = client.post(ROOT + f"/notices/{NOTICE_KEY}/credit-rating", json=_payload())
    assert old.status_code == 200 and old.json()["binding_status"] == "UNCHANGED"
    assert counts(client) == (2, 1)  # One source, one legacy-compatible projection.
    req = quantitative_request_from_candidate_profile(profile)
    with client.app.state.session_factory() as session:
        facts = list(session.scalars(select(CompanyFact).where(CompanyFact.source == "PRIVATE_DOCUMENT")
                                    .options(selectinload(CompanyFact.evidence))))
        resolved = resolve_verified_quantitative_facts(req.criteria, facts,
            as_of=datetime(2026, 8, 30, 9, tzinfo=timezone.utc))
        assert len(resolved) == 1 and resolved[0].status == "CONFIRMED"
        scored = estimate_quantitative_score(req.model_copy(update={"facts": resolved}))
        assert scored.confirmed_points == 9
        expired = resolve_verified_quantitative_facts(req.criteria, facts,
            as_of=datetime(2026, 9, 1, tzinfo=timezone.utc))
        assert not expired


def test_partial_manifest_binds_validated_credit_but_never_confirms_notice_total(client):
    setup(client)
    seed_partial_credit_notice(client)
    legacy = ROOT + f"/notices/{NOTICE_KEY}/credit-rating"
    assert client.post(legacy, json=_payload()).status_code == 422
    assert counts(client) == (0, 0)
    created = client.post(REGISTER, json=certificate()).json()
    context = client.get(CONTEXT)
    assert context.status_code == 200, context.text
    binding = context.json()["fact_binding_sha256"]
    payload = binding_payload(created["certificate_id"], binding)
    automatic = dict(payload, verification_attestation="AUTOMATIC")
    assert client.post(BIND, json=automatic).status_code == 422
    assert counts(client) == (1, 0)
    response = client.post(BIND, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["binding_status"] == "CREATED"
    assert client.post(BIND, json=payload).json()["binding_status"] == "UNCHANGED"
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY)
                                .options(selectinload(Notice.versions)))
        profile = credit._current_dynamic_quantitative_profile(notice)
        assert profile.status == "INCOMPLETE"
        req = quantitative_request_from_candidate_profile(profile, allow_partial_source=True)
        assert req.activation_status == "PARTIAL_SOURCE"
        assert req.criteria[0].fact_binding_sha256 == binding
        facts = list(session.scalars(select(CompanyFact).where(CompanyFact.source == "PRIVATE_DOCUMENT")
                                    .options(selectinload(CompanyFact.evidence))))
        score = estimate_for_notice(notice, company_facts=facts)
        assert score.confirmed_points == 9
        assert score.overall_status == "REVIEW"
        assert score.meets_minimum is None
        assert any("공고 총점이 아닙니다" in item for item in score.assumptions)
    assert counts(client) == (2, 1)


@pytest.mark.parametrize("mutation", ["stale-manifest", "stale-schema", "invalid-source"])
def test_partial_binding_rechecks_current_manifest_and_source_integrity(client, mutation):
    setup(client)
    seed_partial_credit_notice(client)
    cid = client.post(REGISTER, json=certificate()).json()["certificate_id"]
    context = client.get(CONTEXT)
    assert context.status_code == 200, context.text
    with client.app.state.session_factory() as session:
        version_no = 2 if mutation == "invalid-source" else 1
        version = session.scalar(select(NoticeVersion).where(NoticeVersion.version_no == version_no))
        payload = deepcopy(version.source_payload)
        if mutation == "invalid-source":
            payload["quantitative_validation_record"]["validation_fingerprint_sha256"] = "0" * 64
        elif mutation == "stale-schema":
            payload["schema_version"] = "SYN-STALE"
        else:
            payload["attachment_manifest"][0]["file_name"] = "SYN-changed-source.docx"
        version.source_payload = payload
        session.commit()
    assert client.get(CONTEXT).status_code == 422
    assert client.post(BIND, json=binding_payload(cid, context.json()["fact_binding_sha256"])).status_code == 422
    assert counts(client) == (1, 0)


@pytest.mark.parametrize("mutation", [
    "duplicate-credit", "review-credit", "wrong-unit", "unregistered-key",
    "empty-anchor", "unprocessed-credit", "missing-binding", "ambiguous-table",
])
def test_partial_binding_keeps_credit_source_and_compiler_gates(client, monkeypatch, mutation):
    setup(client)
    _seed_notice(client)
    profile = install_profile(monkeypatch, partial=True)
    assert profile.status == "INCOMPLETE"
    candidate = profile.available_candidates[0]
    if mutation == "duplicate-credit":
        profile = profile.model_copy(update={"available_candidates": (candidate, candidate)})
    elif mutation == "review-credit":
        review = QuantitativeReviewCandidate(status="REVIEW", source_attachment_id=ATT,
            table_id=candidate.table_id, criterion_id="SYN-REVIEW-CREDIT", label="SYN credit",
            max_points=9, scoring_method="UNKNOWN", metric="CREDIT_RATING",
            issue_codes=("SYN-REVIEW",))
        profile = profile.model_copy(update={"review_candidates": (review,)})
    elif mutation in {"wrong-unit", "unregistered-key", "empty-anchor"}:
        change = {"unit": "원"} if mutation == "wrong-unit" else (
            {"required_evidence": ("company.other",)} if mutation == "unregistered-key" else
            {"evidence": candidate.evidence.model_copy(update={"quote": ""})})
        profile = profile.model_copy(update={"available_candidates": (candidate.model_copy(update=change),)})
    elif mutation == "unprocessed-credit":
        profile = profile.model_copy(update={"processed_attachment_ids": ()})
    elif mutation == "missing-binding":
        profile = profile.model_copy(update={"document_bindings": ()})
    else:
        profile = profile.model_copy(update={"tables": (*profile.tables, profile.tables[0])})
    monkeypatch.setattr(credit, "_current_dynamic_quantitative_profile", lambda _notice: profile)
    cid = client.post(REGISTER, json=certificate()).json()["certificate_id"]
    assert client.get(CONTEXT).status_code == 422
    assert client.post(BIND, json=binding_payload(cid, "b" * 64)).status_code == 422
    assert counts(client) == (1, 0)


def test_partial_binding_changes_when_source_recognition_condition_changes(client, monkeypatch):
    setup(client)
    _seed_notice(client)
    install_profile(monkeypatch, partial=True)
    cid = client.post(REGISTER, json=certificate()).json()["certificate_id"]
    context = client.get(CONTEXT)
    assert context.status_code == 200, context.text
    old_binding = context.json()["fact_binding_sha256"]
    raw, source = credit_fixture()
    literal = "SYN 신용평가등급은 입찰공고일 이전에 평가한 등급만 인정한다."
    candidate = raw["quantitative_tables"][0]["criteria"][0]
    candidate["recognition_conditions"] = [{"literal": literal,
        "evidence": {"attachment_id": ATT, "page": 1, "section": "SYN", "quote": literal, "confidence": 1}}]
    stored = record(raw, source + "\n" + literal)
    assert stored.status == "AVAILABLE", stored.issues
    profile = merge_validated_quantitative_records([stored],
        expected_documents={ATT: DOC, "SYN-UNREAD-SIBLING": "c" * 64}, manifest_sha256=MANIFEST)
    monkeypatch.setattr(credit, "_current_dynamic_quantitative_profile", lambda _notice: profile)
    current = client.get(CONTEXT)
    assert current.status_code == 200, current.text
    assert current.json()["fact_binding_sha256"] != old_binding
    assert client.post(BIND, json=binding_payload(cid, old_binding)).status_code == 409
    assert counts(client) == (1, 0)


def test_same_certificate_reusable_for_distinct_conditions_with_no_reupload(client, monkeypatch):
    setup(client)
    _seed_notice(client)
    created = client.post(REGISTER, json=certificate()).json()
    active = ["b" * 64]
    monkeypatch.setattr(credit, "_credit_rating_binding_for_notice", lambda _notice, **_kwargs: active[0])
    assert client.post(BIND, json=binding_payload(created["certificate_id"], active[0])).status_code == 200
    active[0] = "c" * 64
    stale = client.post(BIND, json=binding_payload(created["certificate_id"], "b" * 64))
    assert stale.status_code == 409 and counts(client) == (2, 1)
    assert client.post(BIND, json=binding_payload(created["certificate_id"], active[0])).status_code == 200
    assert counts(client) == (3, 2)
    assert client.post(REGISTER, json=certificate()).json()["registration_status"] == "UNCHANGED"


@pytest.mark.parametrize("deadline,issued,allowed", [
    (datetime(2026, 7, 31, 15, tzinfo=timezone.utc), "2026-08-01", True),
    (datetime(2026, 7, 31, 14, 59, 59, tzinfo=timezone.utc), "2026-08-01", False),
    (datetime(2026, 8, 31, 14, 59, 59, 999999, tzinfo=timezone.utc), "2026-08-01", True),
    (datetime(2026, 8, 31, 15, tzinfo=timezone.utc), "2026-08-01", False),
    (datetime(2026, 8, 1, 9, tzinfo=timezone.utc), "2026-08-02", False),
])
def test_binding_enforces_issue_and_korean_deadline_bounds(client, monkeypatch, deadline, issued, allowed):
    setup(client)
    _seed_notice(client)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        notice.deadline = deadline
        session.commit()
    monkeypatch.setattr(credit, "_credit_rating_binding_for_notice", lambda _notice, **_kwargs: "b" * 64)
    created = client.post(REGISTER, json=certificate(issued_on=issued))
    assert created.status_code == 200
    response = client.post(BIND, json=binding_payload(created.json()["certificate_id"], "b" * 64))
    assert response.status_code == (200 if allowed else 422), response.text
    assert counts(client)[1] == int(allowed)


@pytest.mark.parametrize("change", [
    {"rating": "A+"}, {"issuer_name": "SYN Changed Agency"},
    {"valid_until": "2026-09-30"}, {"evidence_reference": "private-evidence://synthetic/changed"},
])
def test_same_document_conflicts_are_not_overwritten(client, change):
    setup(client)
    first = client.post(REGISTER, json=certificate())
    assert first.status_code == 200
    payload = certificate(); payload.update(change)
    assert client.post(REGISTER, json=payload).status_code == 409
    assert counts(client) == (1, 0)


@pytest.mark.parametrize("field", ["status", "sha256", "source_location", "issued_at", "valid_from", "valid_until", "metadata"])
def test_changed_or_revoked_certificate_cannot_be_read_or_bound(client, monkeypatch, field):
    setup(client)
    _seed_notice(client)
    monkeypatch.setattr(credit, "_credit_rating_binding_for_notice", lambda _notice, **_kwargs: "b" * 64)
    cid = client.post(REGISTER, json=certificate()).json()["certificate_id"]
    with client.app.state.session_factory() as session:
        evidence = session.get(Evidence, cid)
        if field == "metadata":
            metadata = deepcopy(evidence.metadata_json)
            metadata["registration"]["rating"] = "AAA"
            evidence.metadata_json = metadata
        elif field in ("issued_at", "valid_from", "valid_until"):
            setattr(evidence, field, datetime(2027, 1, 1, tzinfo=timezone.utc))
        else:
            setattr(evidence, field, {"status": "REVOKED", "sha256": "c" * 64,
                                     "source_location": "private-evidence://synthetic/changed"}[field])
        session.commit()
    assert client.get(REGISTER + "/" + cid).status_code == 409
    assert client.post(BIND, json=binding_payload(cid, "b" * 64)).status_code == 409
    assert counts(client)[1] == 0


def test_company_and_credit_kind_must_match_and_errors_do_not_echo_private_payload(client):
    setup(client)
    for change in ({"company_name": "SYN Different Company"}, {"rating_kind": "BOND"},
                   {"issuer_name": " "}, {"purpose": "INTERNAL"}, {"valid_until": "2026-07-31"}):
        payload = certificate(); payload.update(change)
        response = client.post(REGISTER, json=payload)
        assert response.status_code == 422
        assert DOCUMENT_SHA256 not in response.text and PRIVATE_REFERENCE not in response.text
        assert "SYN Different Company" not in response.text
    assert counts(client) == (0, 0)


def test_lookup_and_binding_require_private_auth_and_binding_attestation(client, monkeypatch):
    setup(client)
    _seed_notice(client)
    monkeypatch.setattr(credit, "_credit_rating_binding_for_notice", lambda _notice, **_kwargs: "b" * 64)
    cid = client.post(REGISTER, json=certificate()).json()["certificate_id"]
    assert client.get(REGISTER + "/" + str(uuid4())).status_code == 404
    for payload in ({"certificate_id": cid},
                    dict(binding_payload(cid, "b" * 64), rating="AAA"),
                    dict(binding_payload(cid, "b" * 64), verification_attestation="AUTOMATIC")):
        response = client.post(BIND, json=payload)
        assert response.status_code == 422
    client.headers.clear()
    assert client.post(REGISTER, json=certificate()).status_code == 401
    assert client.get(REGISTER + "/" + cid).status_code == 401
    assert client.get(CONTEXT).status_code == 401
    assert client.post(BIND, json=binding_payload(cid, "b" * 64)).status_code == 401
    assert counts(client) == (1, 0)


def test_concurrent_registration_creates_only_one_certificate(client):
    setup(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(REGISTER, json=certificate()), range(2)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()["certificate_id"] for r in results}) == 1
    assert {r.json()["registration_status"] for r in results} == {"CREATED", "UNCHANGED"}
    assert counts(client) == (1, 0)
