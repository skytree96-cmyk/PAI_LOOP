from __future__ import annotations

import copy
from dataclasses import replace
from conftest import login_department_reader
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import InvalidRequestError

from pai_loop import api as api_module
from pai_loop.integrations.openai_extraction import ExtractionPayload, PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.models import AnalysisRun, Evaluation, Notice, NoticeVersion, PpsNoticeAuthority, RecommendationSnapshot, ScoreSnapshot
from pai_loop.notice_freshness import latest_current_analysis_run
from pai_loop.pps_enrichment import PPS_ATTACHMENT_SOURCE, PPS_METADATA_KIND, PPS_METADATA_SCHEMA, PPS_PROCESSING_VERSION, _digest
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
from pai_loop.quantitative_scoring import QUANTITATIVE_ENGINE_VERSION

NOW = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


def _score(input_sha, *, malformed=False):
    return ScoreSnapshot(
        score_key="quantitative.total", score_type="QUANTITATIVE_ESTIMATE",
        value=20, lower_value=20, upper_value=20, unit="POINTS",
        status="CONFIRMED", band="GREEN", confidence=0.75,
        method_version=QUANTITATIVE_ENGINE_VERSION,
        basis_json={
            "input_sha256": "0" * 64 if malformed else input_sha,
            "profile_output_sha256": "b" * 64,
            "rule_source_status": "AVAILABLE", "source_validation_status": "SOURCE_VALIDATED",
            "activation_status": "AUTO_ACTIVE", "total_max_points": 20,
            "confirmed_points": 20, "evidence_coverage_pct": 100,
        },
    )


def _seed_history(client, count=55, *, depth=3, blob_size=0):
    """All identifiers and all large payloads are synthetic."""
    ids = []
    with client.app.state.session_factory() as session:
        for index in range(count):
            is_pps = index >= 25
            notice = Notice(
                notice_key=f"PPS-SYN_DASH-{index:03d}" if is_pps else f"SYN-DASH-{index:03d}",
                bid_notice_no=f"SYN-DASH-BID-{index:03d}", revision_no="00",
                title=f"SYN dashboard {index}", agency="SYN agency", status="OPEN",
                deadline=NOW + timedelta(days=2, minutes=index), created_at=NOW,
            )
            session.add(notice); session.flush()
            payload = {"kind": "SYN_MANUAL_SOURCE", "blob": "SYN-" + "x" * blob_size}
            if is_pps:
                attachment = {
                    "attachment_id": "PPS-ATT-" + f"{index:024x}",
                    "file_name": "SYN 제안요청서.pdf", "media_type": "application/pdf",
                    "url": "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-DASH&fileSeq=1",
                    "slot": 1,
                }
                payload = {"kind": PPS_METADATA_KIND, "schema_version": PPS_METADATA_SCHEMA,
                           "attachment_manifest": [attachment]}
            basis = NoticeVersion(notice_id=notice.id, version_no=1, file_sha256="a" * 64,
                                  source_payload=payload, extraction_status="COMPLETE", document_complete=True)
            session.add(basis); session.flush()
            if is_pps:
                manifest_sha = _digest([attachment])
                record = validate_quantitative_attachment_extraction(
                    ExtractionPayload(document_type="RFP", requirements=[], quantitative_tables=[],
                                      quantitative_table_not_applicable=None, missing_or_unreadable=[], summary="SYN source"),
                    source_text="SYN source", attachment_id=attachment["attachment_id"],
                    document_sha256="b" * 64, manifest_sha256=manifest_sha,
                )
                session.add(NoticeVersion(
                    notice_id=notice.id, version_no=2, file_sha256="b" * 64,
                    source_payload={
                        "kind": "OPENAI_REQUIREMENT_EXTRACTION", "source_kind": PPS_ATTACHMENT_SOURCE,
                        "attachment_id": attachment["attachment_id"], "manifest_sha256": _digest(attachment),
                        "current_manifest_sha256": manifest_sha,
                        "prompt_version": "SYN-stale-prompt" if index % 7 == 0 else PROMPT_VERSION,
                        "schema_version": SCHEMA_VERSION, "processing_version": PPS_PROCESSING_VERSION, "status": "ACCEPTED",
                        "quantitative_validation_record": record.model_dump(mode="json"),
                        "document_processing": {"source_read_complete": True, "analysis_input_complete": True},
                        "result": {"summary": "SYN complete raw payload retained", "blob": "y" * blob_size},
                    }, extraction_status="ACCEPTED", document_complete=True,
                ))
            for history_index in range(depth):
                evaluation = Evaluation(
                    notice_id=notice.id, notice_version_id=basis.id,
                    evaluated_at=NOW - timedelta(days=depth-history_index),
                    deadline_snapshot_at=notice.deadline, eligibility=("PASS", "REVIEW", "FAIL")[index % 3],
                    reason_code="SYN-REASON", readiness_score=100, readiness_status="GREEN",
                    evidence_coverage=100, risk_score=10, risk_band="GO", ruleset_version="SYN-rule",
                    atomic_results=[{"result": "REVIEW", "label": "SYN condition", "evidence_valid": True,
                                     "evidence_key": "SYN-EVIDENCE", "large": "x" * blob_size}],
                    explanation={"SYN": "y" * blob_size},
                )
                session.add(evaluation); session.flush()
                input_sha = f"{index * 100 + history_index + 1:064x}"
                run = AnalysisRun(
                    notice_id=notice.id, notice_version_id=basis.id, evaluation_id=evaluation.id,
                    idempotency_key=f"SYN-DASH-{index}-{history_index}", input_sha256=input_sha,
                    generated_at=evaluation.evaluated_at,
                    basis_versions={"quantitative_engine": QUANTITATIVE_ENGINE_VERSION},
                    input_manifest={"SYN": "x" * blob_size}, output_summary={"SYN": "y" * blob_size},
                )
                # Older success must not fill a missing latest score/recommendation.
                if history_index != depth-1 or index % 5 != 0:
                    run.scores.append(_score(input_sha, malformed=history_index == depth-1 and index % 11 == 0))
                    run.recommendations.append(RecommendationSnapshot(
                        recommendation_key="bid:system", rank=0,
                        recommendation="HOLD" if index % 3 == 1 else "GO",
                        detail_json={"eligibility": evaluation.eligibility, "readiness_status": "GREEN",
                                     "SYN-private-unused": "z" * blob_size},
                    ))
                run.recommendations.append(RecommendationSnapshot(
                    recommendation_key="department:SYN", rank=1, recommendation="GO",
                    detail_json={"SYN-unused": "z" * blob_size},
                ))
                session.add(run)
            ids.append(notice.id)
        session.commit()
    return ids


@pytest.mark.parametrize("public_view", [False, True])
def test_lean_dashboard_matches_full_graph_projection_and_current_board(client, monkeypatch, public_view):
    monkeypatch.setattr(api_module, "datetime", _FixedDateTime)
    client.app.state.settings = replace(client.app.state.settings, public_read_only=public_view)
    _seed_history(client)
    if public_view:
        login_department_reader(client)
    original = api_module._load_dashboard_notice_batch
    with monkeypatch.context() as patch:
        patch.setattr(api_module, "_load_dashboard_notice_batch",
                      lambda session, ids: api_module._load_notice_summary_batch(session, ids, include_quantitative_scores=True))
        full = client.get("/api/v1/dashboard")
        assert full.status_code == 200, full.text
    assert api_module._load_dashboard_notice_batch is original
    lean = client.get("/api/v1/dashboard")
    assert lean.status_code == 200, lean.text
    assert lean.json() == full.json()
    dashboard = lean.json()
    board = client.get("/api/v1/notices", params={"limit": 200}).json()
    pps = [row for row in board if row["source_kind"] == "PPS"]
    stats = dashboard["analysis_statistics"]
    assert stats["notice_count"] == len(pps) == 30
    assert stats["accepted_attachment_count"] == sum(row["analysis_attachments_accepted"] for row in pps)
    assert stats["recorded_attempt_attachment_count"] == 30
    assert stats["analysis_state_counts"]["PENDING"] == 4  # stale prompts: 28/35/42/49
    assert stats["score_counts"] == {"CONFIRMED": 19, "ESTIMATED": 0, "UNSCORABLE": 0, "REVIEW": 0, "NOT_EVALUATED": 11}
    assert dashboard["totals"]["evaluations"] == 165
    assert len(dashboard["recent_notices"]) == 10
    if public_view:
        assert dashboard["recent_notices"][1]["latest_evaluation"]["atomic_results"] == []
    else:
        assert dashboard["recent_notices"][1]["latest_evaluation"]["atomic_results"]


def test_lean_graph_keeps_full_sources_and_raises_on_unused_historical_payloads(client):
    ids = _seed_history(client, 28, depth=4, blob_size=4096)
    with client.app.state.session_factory() as session:
        notices = api_module._load_dashboard_notice_batch(session, ids[-3:])
        source_before = {version.id: copy.deepcopy(version.source_payload) for notice in notices for version in notice.versions}
        for notice in notices:
            assert {"agency", "estimated_amount"} <= inspect(notice).unloaded
            for evaluation in notice.evaluations:
                assert {"atomic_results", "explanation", "readiness_score"} <= inspect(evaluation).unloaded
                with pytest.raises(InvalidRequestError):
                    _ = evaluation.atomic_results
            for run in notice.analysis_runs:
                assert {"input_manifest", "output_summary", "scores", "recommendations"} <= inspect(run).unloaded
                with pytest.raises(InvalidRequestError):
                    _ = run.input_manifest
                with pytest.raises(InvalidRequestError):
                    _ = run.scores
            # Source fields and the complete result payload are deliberately retained.
            assert len(notice.versions[-1].source_payload["result"]["blob"]) == 4096
            assert latest_current_analysis_run(notice) is not None
        assert not session.new and not session.dirty and not session.deleted
        assert source_before == {version.id: version.source_payload for notice in notices for version in notice.versions}


def test_only_selected_runs_are_hydrated_in_two_queries_independent_of_history(client):
    ids = _seed_history(client, 3, depth=20, blob_size=4096)
    with client.app.state.session_factory() as session:
        notices = api_module._load_dashboard_notice_batch(session, ids)
        runs = [latest_current_analysis_run(notice) for notice in notices]
        selected_ids = {run.id for run in runs}
        queries = []
        def record_sql(_connection, _cursor, statement, parameters, _context, _many):
            queries.append((statement, parameters))
        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", record_sql)
        try:
            scores, recommendations = api_module._load_dashboard_run_snapshots(session, selected_ids)
        finally:
            event.remove(bind, "before_cursor_execute", record_sql)
        assert len(queries) == 2
        assert all(set(parameters) - {"quantitative.total", "bid:system"} == selected_ids
                   for _statement, parameters in queries)
        assert all(row.analysis_run_id in selected_ids for group in scores.values() for row in group)
        assert all(row.analysis_run_id in selected_ids and row.recommendation_key == "bid:system"
                   for group in recommendations.values() for row in group)
        assert runs[0].id not in scores and runs[0].id not in recommendations
        assert len(scores) == len(recommendations) == 2
        assert sum(isinstance(obj, ScoreSnapshot) for obj in session.identity_map.values()) == 2
        assert sum(isinstance(obj, RecommendationSnapshot) for obj in session.identity_map.values()) == 2
        for rows in recommendations.values():
            with pytest.raises(InvalidRequestError):
                _ = rows[0].detail_json
        assert not session.new and not session.dirty and not session.deleted


def test_dashboard_graph_and_selected_snapshot_sets_remain_bounded(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", _FixedDateTime)
    _seed_history(client, 55, depth=2, blob_size=16384)
    batches, snapshot_sizes = [], []
    full = api_module._load_notice_summary_batch
    lean = api_module._load_dashboard_notice_batch
    snapshots = api_module._load_dashboard_run_snapshots
    def full_recording(session, ids, **kwargs):
        assert not session.identity_map
        batches.append(("full", len(ids)))
        return full(session, ids, **kwargs)
    def lean_recording(session, ids):
        assert not session.identity_map
        batches.append(("lean", len(ids)))
        return lean(session, ids)
    def snapshots_recording(session, ids):
        snapshot_sizes.append(len(ids))
        result = snapshots(session, ids)
        assert not session.new and not session.dirty and not session.deleted
        return result
    monkeypatch.setattr(api_module, "_load_notice_summary_batch", full_recording)
    monkeypatch.setattr(api_module, "_load_dashboard_notice_batch", lean_recording)
    monkeypatch.setattr(api_module, "_load_dashboard_run_snapshots", snapshots_recording)
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200, response.text
    assert batches == [("full", 25), ("lean", 25), ("lean", 5)]
    assert snapshot_sizes and max(snapshot_sizes) <= 25
    assert response.json()["totals"]["notices"] == 55


def test_empty_lean_inputs_do_not_query(client):
    with client.app.state.session_factory() as session:
        queries = []
        def record_sql(*args): queries.append(args)
        event.listen(session.get_bind(), "before_cursor_execute", record_sql)
        try:
            assert api_module._load_dashboard_notice_batch(session, []) == []
            assert api_module._load_dashboard_run_snapshots(session, set()) == ({}, {})
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record_sql)
        assert not queries


def test_lean_authority_projection_preserves_cross_batch_representative_and_ended_counts(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", _FixedDateTime)
    ids = _seed_history(client)
    with client.app.state.session_factory() as session:
        closed = session.get(Notice, ids[52])
        closed.status = "CLOSED"
        expired = session.get(Notice, ids[53])
        expired.deadline = NOW - timedelta(days=1)
        for evaluation in expired.evaluations:
            evaluation.deadline_snapshot_at = expired.deadline
        current = session.get(Notice, ids[54])
        historical = Notice(
            notice_key="PPS-SYN_HISTORY-054", bid_notice_no=current.bid_notice_no,
            revision_no="00", title="SYN historical representative", agency="SYN agency",
            deadline=NOW + timedelta(days=1), status="OPEN", created_at=NOW - timedelta(days=1),
        )
        historical.versions.append(NoticeVersion(
            version_no=1, file_sha256="f" * 64,
            source_payload=copy.deepcopy(current.versions[0].source_payload),
        ))
        session.add(historical)
        session.add(PpsNoticeAuthority(
            bid_notice_no=current.bid_notice_no, revision_no="00", disposition="CANCELLED",
            event_kind="SYN_CANCELLED", required_fields_complete=True,
            deadline=current.deadline, authority_sha256="c" * 64,
        ))
        session.commit()
    with monkeypatch.context() as patch:
        patch.setattr(api_module, "_load_dashboard_notice_batch",
                      lambda session, ids: api_module._load_notice_summary_batch(session, ids, include_quantitative_scores=True))
        full = client.get("/api/v1/dashboard")
        assert full.status_code == 200, full.text
    lean = client.get("/api/v1/dashboard")
    assert lean.status_code == 200, lean.text
    assert lean.json() == full.json()
    result = lean.json()
    assert result["totals"]["notices"] == 56
    assert result["totals"]["active"] == 54
    assert result["cancelled_count"] == 1
    assert result["closed_count"] == result["expired_count"] == 1
    assert result["analyzed_ended_count"] == 2
    assert result["visible_ended_count"] == 3
    assert result["analysis_statistics"]["notice_count"] == 28
