from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from pai_loop import decision_persistence
from pai_loop.models import Evaluation, Notice, NoticeVersion, UserDecision
from pai_loop.schemas import DecisionCreate
from pai_loop.pps_enrichment import PPS_METADATA_SCHEMA


def _evaluation(notice_id, version_id, deadline):
    return Evaluation(
        notice_id=notice_id, notice_version_id=version_id, deadline_snapshot_at=deadline,
        eligibility="PASS", reason_code="PASS", readiness_score=100, readiness_status="GREEN",
        evidence_coverage=100, risk_score=10, risk_band="GO", ruleset_version="SYN-regression",
        atomic_results=[], explanation={},
    )


def _seed(client, state):
    with client.app.state.session_factory() as session:
        notice = Notice(
            notice_key="PPS-SYN_INDEPENDENT", bid_notice_no="SYN-INDEPENDENT", revision_no="00",
            title="SYN independent decision", agency="SYN agency", status="OPEN",
            deadline=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        version = NoticeVersion(version_no=1, file_sha256="a" * 64, source_payload={
            "kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA, "attachment_manifest": [],
        })
        notice.versions.append(version)
        session.add(notice)
        session.flush()
        evaluation = None
        if state != "missing":
            evaluation = _evaluation(notice.id, version.id, notice.deadline)
            session.add(evaluation)
        if state == "stale":
            notice.versions.append(NoticeVersion(version_no=2, file_sha256="b" * 64, source_payload={
                "kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA, "attachment_manifest": [],
            }))
        session.commit()
        return notice.notice_key, notice.id, version.id, evaluation.id if evaluation else None


@pytest.mark.parametrize("state", ["missing", "stale", "incomplete"])
def test_persistence_requires_real_reason_for_every_unfinished_state(client, state):
    key, _, _, evaluation_id = _seed(client, state)
    # Verify the service boundary even if a trusted internal caller bypasses
    # Pydantic's independent HTTP-level nonblank validation.
    payload = DecisionCreate.model_construct(
        choice="HOLD", rationale=" \n\t ",
        evaluation_id=evaluation_id if state == "incomplete" else None,
    )
    with client.app.state.session_factory() as session:
        with pytest.raises(HTTPException) as error:
            decision_persistence.persist_current_evaluation_decision(
                session, notice_key=key, payload=payload, require_explicit_evaluation_id=True,
            )
        assert error.value.status_code == 422
        assert "사유" in error.value.detail
    with client.app.state.session_factory() as session:
        assert session.scalar(select(UserDecision)) is None


@pytest.mark.parametrize("state", ["stale", "incomplete"])
def test_decision_snapshot_distinguishes_unfinished_evaluation_and_expired_notice(client, state):
    key, _, _, evaluation_id = _seed(client, state)
    payload = {"choice": "GO", "rationale": "SYN operator participation reason"}
    if state == "incomplete":
        payload["evaluation_id"] = evaluation_id
    response = client.post(f"/api/v1/notices/{key}/decisions", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    snapshot = body["analysis_snapshot"]
    assert body["analysis_state_snapshot"] == ("INCOMPLETE" if state == "incomplete" else "NOT_EVALUATED")
    assert snapshot["analysis_complete"] is False
    assert snapshot["analysis_reason_state"] != "ANALYZED"
    assert snapshot["notice_status"] == "EXPIRED"
    assert snapshot["stored_notice_status"] == "OPEN"
    assert snapshot["deadline_passed"] is True
    assert body["evaluation_id"] == (evaluation_id if state == "incomplete" else None)
    if state == "stale":
        assert snapshot["evaluation"] is None
        refused = client.post(f"/api/v1/notices/{key}/decisions", json={**payload, "evaluation_id": evaluation_id})
        assert refused.status_code in {409, 422}
    with client.app.state.session_factory() as session:
        assert session.get(Evaluation, evaluation_id) is not None
        assert len(list(session.scalars(select(UserDecision)))) == 1


def test_null_evaluation_decision_serializes_concurrent_first_evaluation(client, monkeypatch):
    key, notice_id, version_id, _ = _seed(client, "missing")
    snapshot_started, writer_attempting, writer_committed = (threading.Event() for _ in range(3))
    original_snapshot = decision_persistence._analysis_snapshot

    def paused_snapshot(notice, evaluation, *, captured_at):
        assert evaluation is None
        snapshot_started.set()
        assert writer_attempting.wait(timeout=3)
        assert not writer_committed.wait(timeout=0.2)
        return original_snapshot(notice, evaluation, captured_at=captured_at)

    def write_first_evaluation():
        assert snapshot_started.wait(timeout=3)
        with client.app.state.session_factory() as session:
            evaluation = _evaluation(notice_id, version_id, datetime(2020, 1, 1, tzinfo=timezone.utc))
            evaluation.evaluated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            session.add(evaluation)
            writer_attempting.set()
            session.commit()
            writer_committed.set()
            return evaluation.id

    monkeypatch.setattr(decision_persistence, "_analysis_snapshot", paused_snapshot)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(write_first_evaluation)
        response = client.post(f"/api/v1/notices/{key}/decisions", json={
            "choice": "NO_GO", "rationale": "SYN recorded before first evaluation",
        })
        assert response.status_code == 201, response.text
        evaluation_id = writer.result(timeout=5)
    body = response.json()
    assert body["evaluation_id"] is None
    assert body["analysis_state_snapshot"] == "NOT_EVALUATED"
    with client.app.state.session_factory() as session:
        decision = session.get(UserDecision, body["id"])
        assert decision.evaluation_id is None
        assert decision.analysis_snapshot["evaluation"] is None
        assert session.get(Evaluation, evaluation_id) is not None
