from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient


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
