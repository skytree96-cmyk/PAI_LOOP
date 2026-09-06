from __future__ import annotations

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from pai_loop.models import (
    AnalysisRun, CompanyPerformanceRecord, Evidence, Notice,
    PerformanceNormalizationBatch, PerformanceNormalizationRevision, ScoreSnapshot,
)
from pai_loop.performance_records import PRIVATE_IMPORT_ATTESTATION
from pai_loop.private_performance_normalization import (
    ALGORITHM_VERSION, parse_explicit_performance_period,
    performance_normalization_state, performance_normalization_state_sha256,
)
from pai_loop.quantitative_performance import PerformanceRecognitionScope, derive_performance_value

PATH = "/api/v1/operator-evidence/performance-period-normalizations"
HEADERS = {"X-PAI-LOOP-API-KEY": "SYN-server-key"}
PERIOD = "24.04.11~09.20"


def _seed(client, count=1, **field_changes):
    client.app.state.settings = replace(
        client.app.state.settings, environment="production", api_key="SYN-server-key",
        public_read_only=True, public_manual_analysis_enabled=True,
        public_manual_analysis_token="2468",
        private_evidence_token="SYN-private-evidence-token-00000001",
    )
    payload = {
        "source_sha256": "a" * 64, "sheet_name": "프로젝트DB",
        "schema_version": "kma-private-performance-v1", "source_row_count": count,
        "batch_index": 0, "batch_count": 1,
        "verification_attestation": PRIVATE_IMPORT_ATTESTATION,
        "records": [{
            "source_row": index + 2, "row_key": f"{index + 1:064x}",
            "fields": {
                "record_status": "DRAFT", "project_name": f"SYN 교육 운영 {index}",
                "agency": "SYN 공공기관", "division": "SYN 수행부서",
                "contract_date": "2024-01-01", "start_date": None, "end_date": None,
                "contract_amount": 100_000_000, "gross_contract_amount_krw": 100_000_000,
                "recognized_performance_amount_krw": 50_000_000,
                "recognized_amount_is_net_of_share": True,
                "vat_basis": "INCLUDED", "completed": True, "share_pct": 50,
                "certificate_status": "ISSUED", "keywords": ["교육"], **field_changes,
            },
        } for index in range(count)],
    }
    # The real importer uses the first 40 characters as a record key. Keep the
    # synthetic full row hashes distinct in that stored prefix too.
    for index, item in enumerate(payload["records"]):
        item["row_key"] = hashlib.sha256(f"SYN-row-{index}".encode()).hexdigest()
    response = client.post("/api/v1/performance-records/private-import", headers=HEADERS, json=payload)
    assert response.status_code == 200, response.text
    return payload


def _request(client, imported):
    items = []
    with client.app.state.session_factory() as session:
        for item in imported["records"]:
            record = session.scalar(select(CompanyPerformanceRecord).where(
                CompanyPerformanceRecord.record_key == "private-" + item["row_key"][:40]
            ))
            items.append({
                "record_id": record.id, "row_key": item["row_key"],
                "source_row": item["source_row"], "expected_revision": record.revision,
                "expected_state_sha256": performance_normalization_state_sha256(record),
                "source_period": PERIOD,
            })
    return {
        "idempotency_key": str(uuid4()), "source_sha256": imported["source_sha256"],
        "sheet_name": "프로젝트DB", "algorithm_version": ALGORITHM_VERSION,
        "source_binding_basis": "AUTHENTICATED_LOCAL_SOURCE_MATCH", "records": items,
    }


def _counts(client):
    with client.app.state.session_factory() as session:
        return tuple(int(session.scalar(select(func.count()).select_from(model))) for model in (
            CompanyPerformanceRecord, PerformanceNormalizationBatch, PerformanceNormalizationRevision,
        ))


@pytest.mark.parametrize("period,expected", [
    ("24.04.11~09.20", (date(2024, 4, 11), date(2024, 9, 20))),
    ("2024-05-11\n2024-12-20", (date(2024, 5, 11), date(2024, 12, 20))),
    ("20240411 ~ 2025/\n06/20", (date(2024, 4, 11), date(2025, 6, 20))),
    ("2024.11.12~25.07.21", (date(2024, 11, 12), date(2025, 7, 21))),
])
def test_explicit_server_parser_has_no_fallback(period, expected):
    assert parse_explicit_performance_period(period) == expected


@pytest.mark.parametrize("period", [
    "24.10.12~05.21", "2024.12.11~2024.02.12", "24.02.30~03.20",
    "24.12~25.01", "20241220", "45400", "계약체결후 260 일",
    "20244.05.13~2024.11.20", "2024.03.22~202412.25",
    "SYN 주석 2024.01.01~2024.12.31", "2024.01.01~2024.12.31 2025.01.01",
])
def test_server_parser_rejects_ambiguous_or_extra_source_content(period):
    assert parse_explicit_performance_period(period) is None


def test_normalization_preserves_identity_source_and_old_score_and_records_revision(client):
    imported = _seed(client)
    request = _request(client, imported)
    record_id = request["records"][0]["record_id"]
    with client.app.state.session_factory() as session:
        record = session.get(CompanyPerformanceRecord, record_id)
        before = performance_normalization_state(record)
        evidence = session.scalar(select(Evidence).where(Evidence.sha256 == "a" * 64))
        evidence_before = copy.deepcopy(evidence.metadata_json)
        notice = Notice(notice_key="SYN-normalization-notice", bid_notice_no="SYN-001",
                        title="SYN notice", deadline=datetime(2025, 1, 1, tzinfo=timezone.utc))
        session.add(notice); session.flush()
        run = AnalysisRun(notice_id=notice.id, idempotency_key="SYN-old-run", input_sha256="f" * 64)
        session.add(run); session.flush()
        old_score = ScoreSnapshot(analysis_run_id=run.id, score_key="SYN-score", score_type="QUANTITATIVE",
                                 value=None, status="REVIEW", basis_json={"record_revision": record.revision})
        session.add(old_score); session.commit()
        score_id = old_score.id
    response = client.post(PATH, headers=HEADERS, json=request)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "APPLIED", "normalized": 1, "activated": 1, "unchanged": 0}
    assert response.headers["cache-control"] == "no-store"
    assert record_id not in response.text and imported["source_sha256"] not in response.text
    with client.app.state.session_factory() as session:
        record = session.get(CompanyPerformanceRecord, record_id)
        after = performance_normalization_state(record)
        changed = {key for key in before if before[key] != after[key]}
        assert changed == {"start_date", "end_date", "record_status", "revision", "updated_by"}
        assert record.start_date == date(2024, 4, 11)
        assert record.end_date == date(2024, 9, 20)
        assert record.revision == before["revision"] + 1
        revision = session.scalar(select(PerformanceNormalizationRevision))
        assert revision.before_state == before and revision.after_state == after
        assert revision.from_revision == before["revision"] and revision.to_revision == record.revision
        assert revision.source_cell == "프로젝트DB!G2"
        assert revision.source_period_sha256 == hashlib.sha256(PERIOD.encode()).hexdigest()
        receipt = session.scalar(select(PerformanceNormalizationBatch))
        assert receipt.source_binding_basis == "AUTHENTICATED_LOCAL_SOURCE_MATCH"
        assert receipt.algorithm_version == ALGORITHM_VERSION
        evidence = session.scalar(select(Evidence).where(Evidence.sha256 == "a" * 64))
        assert evidence.metadata_json == evidence_before and evidence.status == "VERIFIED"
        old = session.get(ScoreSnapshot, score_id)
        assert old.status == "REVIEW" and old.value is None
        assert old.basis_json == {"record_revision": before["revision"]}
        assert performance_normalization_state_sha256(record) != request["records"][0]["expected_state_sha256"]
    assert _counts(client) == (1, 1, 1)


def test_exact_replay_is_idempotent_and_conflicting_replay_rejected(client):
    request = _request(client, _seed(client))
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 200
    replay = client.post(PATH, headers=HEADERS, json=request)
    assert replay.status_code == 200
    assert replay.json() == {"status": "UNCHANGED", "normalized": 0, "activated": 0, "unchanged": 1}
    request["records"][0]["source_period"] = "2024.01.01~2024.12.31"
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 409
    assert _counts(client) == (1, 1, 1)


@pytest.mark.parametrize("attribute,value", [
    ("expected_revision", 999), ("expected_state_sha256", "0" * 64),
    ("source_row", 99), ("row_key", "0" * 64), ("record_id", "00000000-0000-0000-0000-000000000000"),
])
def test_wrong_row_or_before_binding_never_changes_any_record(client, attribute, value):
    request = _request(client, _seed(client, 2))
    request["records"][1][attribute] = value
    response = client.post(PATH, headers=HEADERS, json=request)
    assert response.status_code == 409
    assert _counts(client) == (2, 0, 0)
    with client.app.state.session_factory() as session:
        assert all(r.start_date is None for r in session.scalars(select(CompanyPerformanceRecord)))


@pytest.mark.parametrize("source_status", ["PENDING", "SUPERSEDED"])
def test_normalization_cannot_revive_pending_or_replaced_source(client, source_status):
    request = _request(client, _seed(client))
    with client.app.state.session_factory() as session:
        evidence = session.scalar(select(Evidence).where(Evidence.sha256 == "a" * 64))
        evidence.status = source_status; session.commit()
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 409
    assert _counts(client) == (1, 0, 0)


@pytest.mark.parametrize("change", [
    {"source": "MANUAL"}, {"record_status": "VALIDATED"}, {"record_status": "ARCHIVED"},
    {"start_date": date(2024, 1, 1)}, {"end_date": date(2024, 12, 31)},
    {"evidence_reference": "private-evidence://SYN/wrong-anchor"},
])
def test_only_empty_dates_on_active_private_drafts_are_eligible(client, change):
    imported = _seed(client)
    with client.app.state.session_factory() as session:
        record = session.scalar(select(CompanyPerformanceRecord))
        for key, value in change.items(): setattr(record, key, value)
        session.commit()
    request = _request(client, imported)
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 409
    assert _counts(client) == (1, 0, 0)


@pytest.mark.parametrize("changes", [
    {"agency": ""}, {"contract_date": None}, {"division": ""}, {"share_pct": 0},
    {"gross_contract_amount_krw": None},
    {"recognized_performance_amount_krw": None, "recognized_amount_is_net_of_share": False},
])
def test_date_correction_keeps_other_missing_evidence_as_draft(client, changes):
    request = _request(client, _seed(client, **changes))
    response = client.post(PATH, headers=HEADERS, json=request)
    assert response.status_code == 200, response.text
    assert response.json()["activated"] == 0
    with client.app.state.session_factory() as session:
        record = session.scalar(select(CompanyPerformanceRecord))
        assert record.record_status == "DRAFT" and record.end_date == date(2024, 9, 20)


def test_strong_auth_validation_redaction_and_existing_immutable_routes(client):
    imported = _seed(client)
    request = _request(client, imported)
    for headers in ({}, {"Origin": "https://testserver", "X-PAI-Manual-Token": "2468"}):
        response = client.post(PATH, headers=headers, json=request)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    malicious = copy.deepcopy(request)
    malicious["records"][0]["project_name"] = "SYN-PRIVATE-INPUT-MARKER"
    malicious["records"][0]["start_date"] = "2024-01-01"
    response = client.post(PATH, headers=HEADERS, json=malicious)
    assert response.status_code == 422
    assert response.json()["code"] == "PRIVATE_EVIDENCE_VALIDATION_ERROR"
    assert "SYN-PRIVATE-INPUT-MARKER" not in response.text
    assert request["source_sha256"] not in response.text
    private_headers = {"X-PAI-Private-Evidence-Token": "SYN-private-evidence-token-00000001"}
    assert client.post(PATH, headers=private_headers, json=request).status_code == 200
    # The prior APIs retain their stronger immutability boundary after correction.
    imported["records"][0]["fields"]["start_date"] = "2024-01-01"
    assert client.post("/api/v1/performance-records/private-import", headers=HEADERS, json=imported).status_code == 409
    patch = client.patch("/api/v1/performance-records/" + request["records"][0]["record_id"],
                         headers=HEADERS, json={"expected_updated_at": "2024-01-01T00:00:00Z", "end_date": "2024-12-31"})
    assert patch.status_code == 409


def test_invalid_period_late_in_batch_rolls_back_all_rows(client):
    request = _request(client, _seed(client, 2))
    request["records"][1]["source_period"] = "SYN-PRIVATE-SOURCE-MARKER"
    response = client.post(PATH, headers=HEADERS, json=request)
    assert response.status_code == 422
    assert "SYN-PRIVATE-SOURCE-MARKER" not in response.text
    assert _counts(client) == (2, 0, 0)


def test_parallel_normalization_uses_import_lock_and_creates_one_revision(client):
    request = _request(client, _seed(client))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(PATH, headers=HEADERS, json=request), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert {r.json()["status"] for r in responses} == {"APPLIED", "UNCHANGED"}
    assert _counts(client) == (1, 1, 1)


def test_source_replacement_after_normalization_does_not_reactivate_old_replay(client):
    imported = _seed(client)
    request = _request(client, imported)
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 200
    replacement = copy.deepcopy(imported)
    replacement["source_sha256"] = "d" * 64
    assert client.post("/api/v1/performance-records/private-import", headers=HEADERS, json=replacement).status_code == 200
    assert client.post(PATH, headers=HEADERS, json=request).json()["status"] == "UNCHANGED"
    with client.app.state.session_factory() as session:
        record = session.scalar(select(CompanyPerformanceRecord))
        assert record.start_date is None and record.record_status == "DRAFT"
        assert "/" + "d" * 64 + "/" in record.evidence_reference
        revision = session.scalar(select(PerformanceNormalizationRevision))
        assert revision.after_state["record_status"] == "VALIDATED"


def test_new_quantitative_binding_uses_revised_record_but_remaining_drafts_still_block(client):
    imported = _seed(client, 2)
    request = _request(client, imported)
    request["records"] = request["records"][:1]
    scope = PerformanceRecognitionScope(
        metric_key="company.performance.amount", lookback_years=3,
        similarity_keywords=("교육",), vat_basis="INCLUDED", completion_required=True,
        aggregation="SUM_AMOUNT", consortium_share_rule="APPLY_SHARE", source_literal="SYN scope",
    )
    def estimate():
        with client.app.state.session_factory() as session:
            return derive_performance_value(scope, list(session.scalars(select(CompanyPerformanceRecord))),
                                            as_of=datetime(2025, 1, 1, tzinfo=timezone.utc))
    before = estimate()
    assert before.status == "REVIEW"
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 200
    after = estimate()
    assert after.status == "REVIEW" and after.lower_value == 50_000_000
    assert after.evidence_sha256 != before.evidence_sha256


def test_duplicate_rows_and_changed_attestation_are_rejected(client):
    request = _request(client, _seed(client))
    request["records"].append(copy.deepcopy(request["records"][0]))
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 422
    request["records"].pop()
    request["verification_attestation"] = "SYN-new-human-confirmation"
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 422
    assert _counts(client) == (1, 0, 0)


def test_failure_after_flush_rolls_back_audit_and_current_projection(client, monkeypatch):
    from pai_loop import private_performance_normalization as module

    request = _request(client, _seed(client, 2))
    original = module._apply_normalization

    def fail_after_flush(payload, session):
        original(payload, session)
        raise RuntimeError("SYN forced transaction failure")

    monkeypatch.setattr(module, "_apply_normalization", fail_after_flush)
    with pytest.raises(RuntimeError, match="SYN forced transaction failure"):
        client.post(PATH, headers=HEADERS, json=request)
    assert _counts(client) == (2, 0, 0)
    with client.app.state.session_factory() as session:
        assert all(r.start_date is None and r.record_status == "DRAFT"
                   for r in session.scalars(select(CompanyPerformanceRecord)))


def test_competing_request_ids_cannot_apply_the_same_old_revision_twice(client):
    first = _request(client, _seed(client))
    second = copy.deepcopy(first)
    second["idempotency_key"] = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda payload: client.post(PATH, headers=HEADERS, json=payload), [first, second]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert _counts(client) == (1, 1, 1)


def test_wrong_source_metadata_and_unauthorized_non_date_state_cannot_promote(client):
    imported = _seed(client)
    request = _request(client, imported)
    request["source_sha256"] = "0" * 64
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 409
    request["source_sha256"] = imported["source_sha256"]
    with client.app.state.session_factory() as session:
        evidence = session.scalar(select(Evidence).where(Evidence.sha256 == imported["source_sha256"]))
        evidence.metadata_json = {**evidence.metadata_json, "verification_attestation": "SYN incorrect attestation"}
        session.commit()
    assert client.post(PATH, headers=HEADERS, json=request).status_code == 409
    assert _counts(client) == (1, 0, 0)
