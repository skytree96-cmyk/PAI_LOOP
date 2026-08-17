from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from pai_loop.models import IngestionJob, MockNotification, Notice


def _create_notice(
    client: TestClient,
    *,
    notice_key: str,
    published_at: str,
    title: str = "공공기관 AI 교육 및 컨설팅 용역",
) -> None:
    response = client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": notice_key,
            "title": title,
            "agency": "가상 공공기관",
            "published_at": published_at,
            "deadline": "2026-08-31T09:00:00+09:00",
            "estimated_amount": 120_000_000,
        },
    )
    assert response.status_code == 201


def test_daily_briefing_is_seven_day_stored_data_view_with_zero_source_calls(
    client: TestClient,
) -> None:
    _create_notice(
        client,
        notice_key="DAILY-RECENT",
        published_at="2026-08-16T08:30:00+09:00",
    )
    _create_notice(
        client,
        notice_key="DAILY-OLD",
        published_at="2026-08-01T08:30:00+09:00",
    )

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Asia/Seoul"
    assert body["window"]["days"] == 7
    assert body["totals"]["observed"] == 1
    assert [item["notice_key"] for item in body["notices"]] == ["DAILY-RECENT"]
    assert body["notices"][0]["fit"]["eligibility"] == "PENDING"
    assert body["notices"][0]["top_departments"]
    assert body["notices"][0]["quantitative_estimate"]["overall_status"] == "REVIEW"
    assert body["notices"][0]["pricing_intelligence"]["record_count"] == 0
    assert (
        body["notices"][0]["pricing_intelligence"]["prediction"]["award_rate"]["status"]
        == "INSUFFICIENT_DATA"
    )
    assert body["source_calls"] == {"pps": 0, "openai": 0, "teams": 0}
    assert body["delivery"] == {
        "channel": "teams",
        "mode": "mock",
        "actual_push_sent": False,
    }


def test_retention_defaults_to_preview_and_preserves_canonical_records(
    client: TestClient,
) -> None:
    _create_notice(
        client,
        notice_key="RETENTION-NOTICE",
        published_at="2026-08-16T08:30:00+09:00",
    )
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key="RETENTION-NOTICE").one()
        old = datetime.now(timezone.utc) - timedelta(days=8)
        session.add(
            IngestionJob(
                source="FIXTURE",
                mode="DRY_RUN",
                status="COMPLETED",
                window_json={"from": "2026-08-01", "to": "2026-08-01"},
                request_json={},
                notice_keys=[],
                warnings=[],
                completed_at=old,
                created_at=old,
            )
        )
        session.add(
            MockNotification(
                notice_id=notice.id,
                status="MOCK_RECORDED",
                correlation_id="old-daily-mock",
                card={"type": "AdaptiveCard", "body": []},
                created_at=old,
            )
        )
        session.commit()

    preview = client.post(
        "/api/v1/operations/retention",
        json={"retention_days": 7},
    )
    assert preview.status_code == 200
    assert preview.json()["dry_run"] is True
    assert preview.json()["eligible"] == {
        "ingestion_jobs": 1,
        "mock_notifications": 1,
    }
    assert preview.json()["deleted"] == {
        "ingestion_jobs": 0,
        "mock_notifications": 0,
    }

    applied = client.post(
        "/api/v1/operations/retention",
        json={"retention_days": 7, "dry_run": False},
    )
    assert applied.status_code == 200
    assert applied.json()["deleted"] == {
        "ingestion_jobs": 1,
        "mock_notifications": 1,
    }
    assert "notices" in applied.json()["preserved"]
    assert client.get("/api/v1/notices/RETENTION-NOTICE").status_code == 200


def test_retention_never_deletes_running_job(client: TestClient) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=30)
    with client.app.state.session_factory() as session:
        session.add(
            IngestionJob(
                source="FIXTURE",
                mode="DRY_RUN",
                status="RUNNING",
                window_json={"from": "2026-07-01", "to": "2026-07-01"},
                request_json={},
                notice_keys=[],
                warnings=[],
                completed_at=old,
                created_at=old,
            )
        )
        session.commit()

    response = client.post(
        "/api/v1/operations/retention",
        json={"retention_days": 7, "dry_run": False},
    )
    assert response.status_code == 200
    assert response.json()["eligible"]["ingestion_jobs"] == 0
    assert response.json()["deleted"]["ingestion_jobs"] == 0
