from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pai_loop.decision_persistence import _begin_current_evaluation_snapshot
from pai_loop.models import Evaluation, Notice, UserDecision


def test_public_operator_pin_can_persist_and_reload_final_decision(
    client: TestClient,
) -> None:
    assert client.post("/api/v1/ingestion/replay").status_code == 200
    token = "2468"
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token=token,
        api_key="server-only-api-key",
    )
    origin = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
    }
    endpoint = "https://testserver/api/v1/operator-decisions/notices/SYN-REVIEW-001"
    evaluation_id = client.get(
        "https://testserver/api/v1/notices/SYN-REVIEW-001"
    ).json()["latest_evaluation"]["id"]

    assert client.post(
        endpoint,
        headers=origin,
        json={"choice": "HOLD", "rationale": "추가 증빙 확인"},
    ).status_code == 401
    assert client.post(
        endpoint,
        headers={
            **origin,
            "Origin": "https://attacker.test",
            "X-PAI-Manual-Token": token,
        },
        json={"choice": "HOLD", "rationale": "추가 증빙 확인"},
    ).status_code == 403

    headers = {**origin, "X-PAI-Manual-Token": token}
    missing_evaluation = client.post(
        endpoint,
        headers=headers,
        json={"choice": "HOLD", "rationale": "평가 식별자 누락"},
    )
    assert missing_evaluation.status_code == 422
    saved = client.post(
        endpoint,
        headers=headers,
        json={
            "evaluation_id": evaluation_id,
            "choice": "HOLD",
            "actor_label": "KMA 입찰팀",
            "rationale": "추가 증빙 확인",
            "conditions": ["실적증명서 확인"],
        },
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["choice"] == "HOLD"
    assert token not in saved.text

    history = client.get(endpoint, headers=headers)
    assert history.status_code == 200
    assert [item["choice"] for item in history.json()] == ["HOLD"]


def test_final_decision_rejects_a_stale_evaluation_for_pin_and_server_routes(
    client: TestClient,
) -> None:
    assert client.post("/api/v1/ingestion/replay").status_code == 200
    token = "2468"
    server_key = "server-only-api-key"
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token=token,
        api_key=server_key,
    )
    notice_key = "SYN-REVIEW-001"
    notice_url = f"https://testserver/api/v1/notices/{notice_key}"
    server_headers = {"X-PAI-LOOP-API-KEY": server_key}
    stale_evaluation_id = client.get(notice_url).json()["latest_evaluation"]["id"]
    refreshed = client.post(
        f"{notice_url}/evaluate",
        headers=server_headers,
        json={"ruleset_version": "stale-decision-regression"},
    )
    assert refreshed.status_code == 201, refreshed.text
    current_evaluation_id = refreshed.json()["id"]
    assert current_evaluation_id != stale_evaluation_id

    stale_payload = {
        "evaluation_id": stale_evaluation_id,
        "choice": "HOLD",
        "rationale": "이전 평가 화면에서 작성한 판단",
    }
    operator_endpoint = (
        f"https://testserver/api/v1/operator-decisions/notices/{notice_key}"
    )
    operator_headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-PAI-Manual-Token": token,
    }
    stale_operator = client.post(
        operator_endpoint,
        headers=operator_headers,
        json=stale_payload,
    )
    assert stale_operator.status_code == 409
    assert "평가가 갱신" in stale_operator.json()["detail"]

    stale_server = client.post(
        f"{notice_url}/decisions",
        headers=server_headers,
        json=stale_payload,
    )
    assert stale_server.status_code == 409
    assert "평가가 갱신" in stale_server.json()["detail"]

    current = client.post(
        operator_endpoint,
        headers=operator_headers,
        json={**stale_payload, "evaluation_id": current_evaluation_id},
    )
    assert current.status_code == 201, current.text
    assert current.json()["evaluation_id"] == current_evaluation_id


def test_public_operator_pin_failures_are_throttled_without_limiting_valid_runs(
    client: TestClient,
) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token="2468",
        api_key="server-only-api-key",
    )
    endpoint = "https://testserver/api/v1/operator-decisions/notices/missing"
    headers = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
        "X-PAI-Manual-Token": "0000",
    }

    assert [client.get(endpoint, headers=headers).status_code for _ in range(5)] == [401] * 5
    locked = client.get(endpoint, headers=headers)
    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == "600"
    valid_after_attack = client.get(
        endpoint,
        headers={**headers, "X-PAI-Manual-Token": "2468"},
    )
    assert valid_after_attack.status_code == 404


def test_runtime_profile_exposes_pin_gated_decisions(client: TestClient) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token="2468",
        api_key="server-only-api-key",
    )

    profile = client.get("/api/v1/runtime-profile")

    assert profile.status_code == 200
    assert profile.json()["write_controls_enabled"] is False
    assert profile.json()["operator_decisions_enabled"] is True


def test_sqlite_decision_snapshot_serializes_a_concurrent_evaluation_writer(
    client: TestClient,
) -> None:
    assert client.post("/api/v1/ingestion/replay").status_code == 200
    notice_key = "SYN-REVIEW-001"
    current_evaluation_id = client.get(
        f"/api/v1/notices/{notice_key}"
    ).json()["latest_evaluation"]["id"]
    writer_attempting = threading.Event()
    writer_committed = threading.Event()

    decision_session = client.app.state.session_factory()
    try:
        _begin_current_evaluation_snapshot(decision_session)
        notice = decision_session.scalar(
            select(Notice)
            .where(Notice.notice_key == notice_key)
            .options(
                selectinload(Notice.versions),
                selectinload(Notice.evaluations),
            )
        )
        assert notice is not None
        assert any(
            evaluation.id == current_evaluation_id
            for evaluation in notice.evaluations
        )

        def write_new_evaluation() -> str:
            with client.app.state.session_factory() as writer_session:
                original = writer_session.get(Evaluation, current_evaluation_id)
                assert original is not None
                evaluation = Evaluation(
                    notice_id=original.notice_id,
                    notice_version_id=original.notice_version_id,
                    evaluated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                    deadline_snapshot_at=original.deadline_snapshot_at,
                    eligibility=original.eligibility,
                    reason_code=original.reason_code,
                    readiness_score=original.readiness_score,
                    readiness_status=original.readiness_status,
                    evidence_coverage=original.evidence_coverage,
                    risk_score=original.risk_score,
                    risk_band=original.risk_band,
                    ruleset_version="concurrent-evaluation-writer",
                    atomic_results=original.atomic_results,
                    explanation=original.explanation,
                )
                writer_session.add(evaluation)
                writer_attempting.set()
                writer_session.commit()
                writer_committed.set()
                return evaluation.id

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(write_new_evaluation)
            assert writer_attempting.wait(timeout=3)
            assert writer_committed.wait(timeout=0.2) is False

            decision_session.add(
                UserDecision(
                    notice_id=notice.id,
                    evaluation_id=current_evaluation_id,
                    choice="HOLD",
                    actor_label="KMA 입찰팀",
                    rationale="동시 평가보다 먼저 확정된 현재 평가 판단",
                )
            )
            decision_session.commit()
            new_evaluation_id = future.result(timeout=5)
    finally:
        decision_session.close()

    assert writer_committed.is_set()
    with client.app.state.session_factory() as verification_session:
        decision = verification_session.scalar(
            select(UserDecision).where(
                UserDecision.evaluation_id == current_evaluation_id
            )
        )
        assert decision is not None
        assert verification_session.get(Evaluation, new_evaluation_id) is not None


def test_postgres_decision_snapshot_uses_evaluation_insert_barrier() -> None:
    session = Mock()
    session.in_transaction.return_value = False
    session.get_bind.return_value.dialect.name = "postgresql"

    _begin_current_evaluation_snapshot(session)

    statement = session.execute.call_args.args[0]
    assert str(statement) == "LOCK TABLE evaluations IN SHARE MODE"
