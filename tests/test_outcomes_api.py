from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.models import BidOutcome, Notice


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


@pytest.mark.parametrize(
    ("source", "outcome_key"),
    (
        ("PPS_AUTO_FEEDBACK", "provider-owned-result"),
        ("MANUAL_UI", "operator-review-result"),
        ("MANUAL", "pps-final-award:reserved-provider-key"),
        ("MANUAL", "manual-ui:reserved-review-key"),
    ),
)
def test_generic_outcome_upsert_rejects_reserved_source_and_key_namespaces(
    client: TestClient,
    source: str,
    outcome_key: str,
) -> None:
    notice_key = _notice_key(client)
    response = client.post(
        f"/api/v1/notices/{notice_key}/outcomes",
        json={
            "outcome_key": outcome_key,
            "status": "WON",
            "source": source,
            "winner_name": "원본 생성 시도",
        },
    )
    assert response.status_code == 409
    assert "결과 학습 검토본" in response.text
    assert client.get(f"/api/v1/notices/{notice_key}/outcomes").json() == []


@pytest.mark.parametrize(
    ("original_source", "outcome_key"),
    (
        ("PPS_AUTO_FEEDBACK", "pps-final-award:immutable-001"),
        ("EXTERNAL_PROVIDER", "external-provider-result:001"),
    ),
)
def test_generic_outcome_upsert_cannot_overwrite_an_external_original(
    client: TestClient,
    original_source: str,
    outcome_key: str,
) -> None:
    notice_key = _notice_key(client)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        assert notice is not None
        original = BidOutcome(
            notice_id=notice.id,
            outcome_key=outcome_key,
            status="WON",
            winning_bid_amount=99_000_000,
            winner_name="공개 원본 낙찰자",
            source=original_source,
            source_reference="provider:immutable:001",
            evidence_json={"provider_result_sha256": "a" * 64},
            observed_at=datetime.now(timezone.utc),
        )
        session.add(original)
        session.commit()
        original_id = original.id

    overwritten = client.post(
        f"/api/v1/notices/{notice_key}/outcomes",
        json={
            "outcome_key": outcome_key,
            "status": "LOST",
            "source": "MANUAL",
            "winner_name": "덮어쓰기 시도",
            "source_reference": "manual",
            "evidence_json": {},
        },
    )
    assert overwritten.status_code == 409
    assert "결과 학습 검토본" in overwritten.text

    history = client.get(f"/api/v1/notices/{notice_key}/outcomes").json()
    assert len(history) == 1
    assert history[0]["id"] == original_id
    assert history[0]["status"] == "WON"
    assert history[0]["source"] == original_source
    assert history[0]["winner_name"] == "공개 원본 낙찰자"
    assert history[0]["evidence_json"] == {"provider_result_sha256": "a" * 64}
