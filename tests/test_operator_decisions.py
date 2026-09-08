from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pai_loop.decision_persistence import _begin_current_evaluation_snapshot
from pai_loop.models import Evaluation, Notice, PpsNoticeAuthority, UserDecision


def test_server_operator_route_can_persist_and_reload_final_decision(
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
    ).status_code == 401

    headers = {"X-PAI-LOOP-API-KEY": "server-only-api-key"}
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


def test_final_decision_rejects_a_stale_evaluation_for_both_server_routes(
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
    operator_headers = server_headers
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


def test_retired_operator_pin_never_authorizes_even_after_repeated_requests(
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
    assert locked.status_code == 401
    valid_after_attack = client.get(
        endpoint,
        headers={**headers, "X-PAI-Manual-Token": "2468"},
    )
    assert valid_after_attack.status_code == 401


def test_runtime_profile_does_not_reenable_retired_pin_decisions(client: TestClient) -> None:
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
    assert profile.json()["operator_decisions_enabled"] is False


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


# --- The human decision is independent of the AI analysis -------------------
#
# A person is accountable for participating or not whether or not the
# deterministic engine produced a current evaluation. These cases pin that the
# record can be written before analysis, after a failed extraction and after
# the deadline, while every existing defense stays exactly where it was.

SERVER_KEY = "server-only-api-key"
OPERATOR_PIN = "2468"
# Human cookie/CAS coverage is in test_department_accounts; these cases keep
# checking evaluation invariants through the retained server operator route.
OPERATOR_HEADERS = {"X-PAI-LOOP-API-KEY": SERVER_KEY}


def _production_settings(client: TestClient) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        environment="production",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token=OPERATOR_PIN,
        api_key=SERVER_KEY,
    )


def _unanalysed_notice(client: TestClient, notice_key: str, *, deadline: str) -> None:
    """Create a stored notice that has never been evaluated.

    Nothing here fabricates an evaluation or an AI result; the notice simply
    has none, which is the state this feature has to remain usable in.
    """

    created = client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": f"{notice_key}-NO",
            "title": "SYN 분석 전 담당자 판단 공고",
            "agency": "SYN 기관",
            "deadline": deadline,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["latest_evaluation"] is None


def test_decision_is_recordable_before_any_analysis(client: TestClient) -> None:
    notice_key = "SYN-DECISION-UNANALYSED"
    _unanalysed_notice(client, notice_key, deadline="2099-09-09T09:00:00Z")
    _production_settings(client)
    endpoint = (
        f"https://testserver/api/v1/operator-decisions/notices/{notice_key}"
    )

    saved = client.post(
        endpoint,
        headers=OPERATOR_HEADERS,
        json={
            "choice": "NO_GO",
            "actor_label": "KMA 입찰팀",
            "rationale": "분석 전이지만 인력 일정이 없어 불참으로 기록합니다.",
        },
    )

    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["choice"] == "NO_GO"
    assert body["evaluation_id"] is None
    assert body["analysis_state_snapshot"] == "NOT_EVALUATED"
    snapshot = body["analysis_snapshot"]
    assert snapshot["analysis_state"] == "NOT_EVALUATED"
    assert snapshot["evaluation"] is None
    assert snapshot["captured_at"]
    assert snapshot["deadline_passed"] is False
    assert snapshot["analysis_reason_state"] in {"ANALYZED", "REVIEW", "PENDING"}

    history = client.get(endpoint, headers=OPERATOR_HEADERS)
    assert history.status_code == 200
    assert [item["choice"] for item in history.json()] == ["NO_GO"]


@pytest.mark.parametrize("choice", ["GO", "HOLD", "NO_GO", "CONDITIONAL_GO"])
def test_every_participation_choice_is_recordable_before_analysis(
    client: TestClient, choice: str,
) -> None:
    notice_key = f"SYN-DECISION-CHOICE-{choice}"
    _unanalysed_notice(client, notice_key, deadline="2099-09-09T09:00:00Z")

    saved = client.post(
        f"/api/v1/notices/{notice_key}/decisions",
        json={"choice": choice, "rationale": "분석 전 담당자 판단 기록"},
    )

    assert saved.status_code == 201, saved.text
    assert saved.json()["choice"] == choice
    assert saved.json()["evaluation_id"] is None


def test_decision_after_the_deadline_records_the_expired_state(
    client: TestClient,
) -> None:
    notice_key = "SYN-DECISION-EXPIRED"
    _unanalysed_notice(client, notice_key, deadline="2020-01-01T09:00:00Z")

    saved = client.post(
        f"/api/v1/notices/{notice_key}/decisions",
        json={"choice": "NO_GO", "rationale": "마감 경과 후 불참 사실을 기록합니다."},
    )

    assert saved.status_code == 201, saved.text
    assert saved.json()["analysis_snapshot"]["deadline_passed"] is True
    assert saved.json()["analysis_snapshot"]["deadline"].startswith("2020-01-01")


@pytest.mark.parametrize("rationale", ["", "   ", "\n\t "])
def test_unanalysed_decision_requires_a_written_rationale(
    client: TestClient, rationale: str,
) -> None:
    notice_key = "SYN-DECISION-NO-REASON"
    _unanalysed_notice(client, notice_key, deadline="2099-09-09T09:00:00Z")

    refused = client.post(
        f"/api/v1/notices/{notice_key}/decisions",
        json={"choice": "GO", "rationale": rationale},
    )

    assert refused.status_code == 422
    with client.app.state.session_factory() as session:
        assert session.scalar(select(UserDecision)) is None


def test_missing_evaluation_id_is_still_refused_when_one_is_current(
    client: TestClient,
) -> None:
    """Omitting the identifier must not become a way around the stale check."""

    assert client.post("/api/v1/ingestion/replay").status_code == 200
    _production_settings(client)

    refused = client.post(
        "https://testserver/api/v1/operator-decisions/notices/SYN-REVIEW-001",
        headers=OPERATOR_HEADERS,
        json={"choice": "HOLD", "rationale": "화면에서 본 평가를 지정하지 않은 저장"},
    )

    assert refused.status_code == 422
    assert "evaluation_id" in refused.json()["detail"]


def test_stale_and_foreign_evaluation_ids_stay_refused_on_the_operator_route(
    client: TestClient,
) -> None:
    assert client.post("/api/v1/ingestion/replay").status_code == 200
    _production_settings(client)
    foreign_evaluation_id = client.get(
        "/api/v1/notices/SYN-PASS-001"
    ).json()["latest_evaluation"]["id"]
    stale_evaluation_id = client.get(
        "/api/v1/notices/SYN-REVIEW-001"
    ).json()["latest_evaluation"]["id"]
    endpoint = (
        "https://testserver/api/v1/operator-decisions/notices/SYN-REVIEW-001"
    )

    foreign = client.post(
        endpoint,
        headers=OPERATOR_HEADERS,
        json={
            "evaluation_id": foreign_evaluation_id,
            "choice": "GO",
            "rationale": "다른 공고의 평가 식별자",
        },
    )
    assert foreign.status_code == 422
    assert "이 공고의 evaluation_id가 아닙니다." == foreign.json()["detail"]

    refreshed = client.post(
        "/api/v1/notices/SYN-REVIEW-001/evaluate",
        headers={"X-PAI-LOOP-API-KEY": SERVER_KEY},
        json={"ruleset_version": "independent-decision-regression"},
    )
    assert refreshed.status_code == 201, refreshed.text
    stale = client.post(
        endpoint,
        headers=OPERATOR_HEADERS,
        json={
            "evaluation_id": stale_evaluation_id,
            "choice": "GO",
            "rationale": "이전 평가 화면에서 작성한 판단",
        },
    )
    assert stale.status_code == 409
    assert "평가가 갱신" in stale.json()["detail"]


def test_a_cancelled_notice_still_refuses_an_unanalysed_decision(
    client: TestClient,
) -> None:
    """The cancellation constraint applies to the new path as well."""

    notice_key = "PPS-DECISION-CANCELLED"
    _unanalysed_notice(client, notice_key, deadline="2099-09-09T09:00:00Z")
    with client.app.state.session_factory() as session:
        session.add(
            PpsNoticeAuthority(
                bid_notice_no=f"{notice_key}-NO",
                disposition="CANCELLED",
                authority_sha256="a" * 64,
            )
        )
        session.commit()

    refused = client.post(
        f"/api/v1/notices/{notice_key}/decisions",
        json={"choice": "GO", "rationale": "취소 공고에 대한 판단 시도"},
    )

    assert refused.status_code == 409
    assert "취소된 공고" in refused.json()["detail"]


def test_reanalysis_never_rewrites_or_rebinds_a_recorded_decision(
    client: TestClient,
) -> None:
    """A later evaluation is added; it does not touch what a person recorded."""

    assert client.post("/api/v1/ingestion/replay").status_code == 200
    server_headers = {"X-PAI-LOOP-API-KEY": SERVER_KEY}
    _production_settings(client)
    notice_url = "/api/v1/notices/SYN-REVIEW-001"
    original_evaluation_id = client.get(notice_url).json()["latest_evaluation"]["id"]

    recorded = client.post(
        f"{notice_url}/decisions",
        headers=server_headers,
        json={
            "evaluation_id": original_evaluation_id,
            "choice": "HOLD",
            "actor_label": "KMA 입찰팀",
            "rationale": "재분석 전에 확정한 담당자 판단",
        },
    )
    assert recorded.status_code == 201, recorded.text
    before = recorded.json()

    refreshed = client.post(
        f"{notice_url}/evaluate",
        headers=server_headers,
        json={"ruleset_version": "independent-decision-reanalysis"},
    )
    assert refreshed.status_code == 201, refreshed.text
    assert refreshed.json()["id"] != original_evaluation_id

    history = client.get(f"{notice_url}/decisions", headers=server_headers)
    assert history.status_code == 200
    assert len(history.json()) == 1
    after = history.json()[0]
    assert after == before
    assert after["evaluation_id"] == original_evaluation_id
    assert after["analysis_snapshot"]["evaluation"]["id"] == original_evaluation_id


def test_reanalysis_never_adopts_an_unbound_decision_as_its_own(
    client: TestClient,
) -> None:
    """A decision saved with no evaluation stays unbound once one appears."""

    notice_key = "SYN-DECISION-LATER-ANALYSIS"
    _unanalysed_notice(client, notice_key, deadline="2099-09-09T09:00:00Z")
    recorded = client.post(
        f"/api/v1/notices/{notice_key}/decisions",
        json={"choice": "GO", "rationale": "분석 전에 참여를 확정했습니다."},
    )
    assert recorded.status_code == 201, recorded.text
    before = recorded.json()
    assert before["evaluation_id"] is None

    versioned = client.post(
        f"/api/v1/notices/{notice_key}/versions",
        json={"version_no": 1, "file_sha256": "b" * 64},
    )
    assert versioned.status_code == 201, versioned.text
    evaluated = client.post(f"/api/v1/notices/{notice_key}/evaluate", json={})
    assert evaluated.status_code == 201, evaluated.text

    history = client.get(f"/api/v1/notices/{notice_key}/decisions")
    assert history.status_code == 200
    assert history.json() == [before]
    assert history.json()[0]["evaluation_id"] is None
    assert history.json()[0]["analysis_state_snapshot"] == "NOT_EVALUATED"
