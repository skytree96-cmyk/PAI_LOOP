from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from pai_loop.integrations.openai_extraction import PROMPT_VERSION
from pai_loop.models import AwardHistoryItem, IngestionJob, MockNotification, Notice, NoticeVersion
from pai_loop.pps_enrichment import PPS_ATTACHMENT_SOURCE, PPS_METADATA_KIND


def _create_notice(
    client: TestClient,
    *,
    notice_key: str,
    published_at: str,
    title: str = "공공기관 AI 교육 및 컨설팅 용역",
    deadline: str = "2026-08-31T09:00:00+09:00",
    status: str = "OPEN",
) -> None:
    response = client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": notice_key,
            "title": title,
            "agency": "가상 공공기관",
            "published_at": published_at,
            "deadline": deadline,
            "status": status,
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
    assert body["notices"][0]["fit"]["risk_score"] is None
    assert body["notices"][0]["fit"]["risk_band"] == "UNKNOWN"
    assert body["notices"][0]["top_departments"]
    assert body["notices"][0]["quantitative_estimate"]["overall_status"] == "REVIEW"
    assert body["notices"][0]["pricing_intelligence"]["record_count"] == 0
    assert (
        body["notices"][0]["pricing_intelligence"]["prediction"]["award_rate"]["status"]
        == "INSUFFICIENT_DATA"
    )
    assert body["notices"][0]["competition_risk"]["status"] == "UNKNOWN"
    assert body["notices"][0]["competition_risk"]["score"] is None
    assert body["notices"][0]["analysis_snapshot"] is None
    assert body["analysis_queue"] == {
        "policy": "NEVER_ATTEMPTED_THEN_OLDEST_RETRY",
        "pending_total": 1,
        "never_attempted_total": 1,
        "retryable_total": 0,
        "deferred_terminal_total": 0,
        "notice_keys": ["DAILY-RECENT"],
        "never_attempted_notice_keys": ["DAILY-RECENT"],
        "retryable_notice_keys": [],
        "limit": 50,
        "note": "미시도 공고를 먼저 처리하고 실패 건은 가장 오래된 시도부터 재검토합니다. 첨부 없음·구형 HWP 전용은 manifest가 바뀔 때까지 자동 재시도하지 않습니다.",
    }
    assert body["source_calls"] == {"pps": 0, "openai": 0, "teams": 0}
    assert body["delivery"] == {
        "channel": "teams",
        "mode": "mock",
        "actual_push_sent": False,
    }


def test_daily_briefing_includes_only_currently_open_notices(client: TestClient) -> None:
    published_at = "2026-08-16T08:30:00+09:00"
    _create_notice(
        client,
        notice_key="DAILY-ACTIVE-OPEN",
        published_at=published_at,
        deadline="2026-08-17T00:00:00+00:00",
    )
    _create_notice(
        client,
        notice_key="DAILY-CLOSED",
        published_at=published_at,
        deadline="2026-08-31T09:00:00+09:00",
        status="CLOSED",
    )
    _create_notice(
        client,
        notice_key="DAILY-EXPIRED-OPEN",
        published_at=published_at,
        deadline="2026-08-16T23:59:59+00:00",
    )

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["observed"] == 1
    assert body["totals"]["included"] == 1
    assert [item["notice_key"] for item in body["notices"]] == ["DAILY-ACTIVE-OPEN"]


def test_daily_analysis_queue_does_not_let_failed_or_terminal_items_starve_new_work(
    client: TestClient,
) -> None:
    published_at = "2026-08-16T08:30:00+09:00"
    for notice_key in ("PPS-NEVER", "PPS-RETRY", "PPS-HWP-ONLY"):
        _create_notice(client, notice_key=notice_key, published_at=published_at)

    def attachment(suffix: str, media_type: str, token: str) -> dict:
        return {
            "attachment_id": f"PPS-ATT-{token * 24}",
            "file_name": f"공고문{suffix}",
            "media_type": media_type,
            "url": (
                "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
                f"?bidPbancNo={token * 8}&fileSeq=1"
            ),
            "slot": 1,
        }

    pdf_never = attachment(".pdf", "application/pdf", "a")
    pdf_retry = attachment(".pdf", "application/pdf", "b")
    hwp_terminal = attachment(".hwp", "application/x-hwp", "c")
    manifest_sha = hashlib.sha256(
        json.dumps(
            pdf_retry,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with client.app.state.session_factory() as session:
        notices = {
            item.notice_key: item
            for item in session.query(Notice).filter(Notice.notice_key.like("PPS-%")).all()
        }
        for key, manifest in (
            ("PPS-NEVER", [pdf_never]),
            ("PPS-RETRY", [pdf_retry]),
            ("PPS-HWP-ONLY", [hwp_terminal]),
        ):
            session.add(
                NoticeVersion(
                    notice_id=notices[key].id,
                    version_no=1,
                    file_sha256=hashlib.sha256(key.encode()).hexdigest(),
                    document_complete=False,
                    extraction_status="METADATA",
                    extraction_confidence=1.0,
                    source_payload={
                        "kind": PPS_METADATA_KIND,
                        "attachment_manifest": manifest,
                    },
                )
            )
        session.add(
            NoticeVersion(
                notice_id=notices["PPS-RETRY"].id,
                version_no=2,
                file_sha256="d" * 64,
                document_complete=False,
                extraction_status="REVIEW",
                extraction_confidence=0.0,
                source_payload={
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "source_kind": PPS_ATTACHMENT_SOURCE,
                    "attachment_id": pdf_retry["attachment_id"],
                    "manifest_sha256": manifest_sha,
                    "prompt_version": PROMPT_VERSION,
                    "status": "REVIEW",
                    "error_code": "UNVERIFIED_QUOTE",
                },
            )
        )
        session.commit()

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )
    assert response.status_code == 200
    queue = response.json()["analysis_queue"]
    assert queue["notice_keys"] == ["PPS-NEVER", "PPS-RETRY"]
    assert queue["never_attempted_notice_keys"] == ["PPS-NEVER"]
    assert queue["retryable_notice_keys"] == ["PPS-RETRY"]
    assert (
        queue["never_attempted_notice_keys"] + queue["retryable_notice_keys"]
        == queue["notice_keys"]
    )
    assert queue["never_attempted_total"] == 1
    assert queue["retryable_total"] == 1
    assert queue["deferred_terminal_total"] == 1


def test_daily_briefing_exposes_competition_risk_without_mixing_eligibility(
    client: TestClient,
) -> None:
    _create_notice(
        client,
        notice_key="DAILY-RISK",
        published_at="2026-08-16T08:30:00+09:00",
    )
    winners = ["합성 수행사 A", "합성 수행사 B", "합성 수행사 A", "합성 수행사 C", "합성 수행사 B", "합성 수행사 A"]
    participants = [2, 3, 2, 4, 3, 2]
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter_by(notice_key="DAILY-RISK").one()
        for index, (winner, participant_count) in enumerate(zip(winners, participants), start=1):
            session.add(
                AwardHistoryItem(
                    target_notice_id=notice.id,
                    external_identity=f"DAILY-RISK-{index}",
                    bid_notice_no=f"SYN-DAILY-{index}",
                    title=f"합성 AI 교육 {index}",
                    agency="합성 발주기관",
                    winner_name=winner,
                    participant_count=participant_count,
                    award_amount=90_000_000 + index,
                    award_rate=90 + index / 10,
                    awarded_at=datetime(2025, index, 1, tzinfo=timezone.utc),
                    similarity_score=80,
                    source="SYNTHETIC_TEST_ONLY",
                )
            )
        session.commit()

    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-08-17T09:00:00+09:00"},
    )
    assert response.status_code == 200
    item = response.json()["notices"][0]
    assert item["fit"]["risk_band"] == "UNKNOWN"
    assert item["competition_risk"]["score"] == 62.83
    assert item["competition_risk"]["band"] == "HIGH"
    assert item["pricing_intelligence"]["competition_risk"] == item["competition_risk"]


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
