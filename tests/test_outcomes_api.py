from __future__ import annotations

from fastapi.testclient import TestClient


def _notice_key(client: TestClient) -> str:
    response = client.post("/api/v1/ingestion/replay")
    assert response.status_code == 200
    return response.json()["notice_keys"][0]


def test_bid_outcome_upsert_and_history_are_idempotent(client: TestClient) -> None:
    notice_key = _notice_key(client)
    payload = {
        "outcome_key": "submission-round-1",
        "status": "LOST",
        "submitted_bid_amount": 123_000_000,
        "submitted_bid_rate": 88.1,
        "technical_score": 82.4,
        "price_score": 9.2,
        "total_score": 91.6,
        "rank": 2,
        "winner_name": "공개 낙찰기관",
        "reason_code": "PRICE_GAP",
        "loss_reason": "가격점수 차이 검토",
        "risk_summary": {"competition": "MODERATE"},
        "source": "MANUAL",
        "source_reference": "internal-review-1",
        "evidence_json": {"reviewed": True},
    }
    first = client.post(f"/api/v1/notices/{notice_key}/outcomes", json=payload)
    assert first.status_code == 201
    assert first.json()["status"] == "LOST"

    updated = client.post(
        f"/api/v1/notices/{notice_key}/outcomes",
        json={**payload, "rank": 3, "total_score": 90.5},
    )
    assert updated.status_code == 201
    assert updated.json()["id"] == first.json()["id"]
    assert updated.json()["rank"] == 3

    history = client.get(f"/api/v1/notices/{notice_key}/outcomes")
    assert history.status_code == 200
    assert len(history.json()) == 1


def test_no_bid_rejects_submitted_amount(client: TestClient) -> None:
    notice_key = _notice_key(client)
    response = client.post(
        f"/api/v1/notices/{notice_key}/outcomes",
        json={"status": "NO_BID", "submitted_bid_amount": 1},
    )
    assert response.status_code == 422


def test_outcome_rejects_foreign_evaluation(client: TestClient) -> None:
    keys = client.post("/api/v1/ingestion/replay").json()["notice_keys"]
    evaluated = client.post(f"/api/v1/notices/{keys[0]}/evaluate", json={})
    assert evaluated.status_code == 201
    response = client.post(
        f"/api/v1/notices/{keys[1]}/outcomes",
        json={
            "status": "SUBMITTED",
            "evaluation_id": evaluated.json()["id"],
        },
    )
    assert response.status_code == 422
