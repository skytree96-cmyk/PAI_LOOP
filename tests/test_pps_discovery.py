from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pai_loop.config import Settings
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.analysis_api import (
    AnalysisBackfillPlanRequest,
    _select_backfill_notice_keys,
)
from pai_loop.daily_operations import (
    RetentionRequest,
    apply_operational_retention,
    daily_briefing,
)
from pai_loop.integrations.openai_extraction import OpenAITelemetry
from pai_loop.manual_analysis import router as manual_analysis_router
from pai_loop.models import IngestionJob, Notice, NoticeAnalysisPolicy, NoticeVersion
from pai_loop.pps_discovery import router as pps_discovery_router


_OPERATOR_TOKEN = "2468"
_HEADERS = {
    "Origin": "https://testserver",
    "Sec-Fetch-Site": "same-origin",
    "X-PAI-Manual-Token": _OPERATOR_TOKEN,
}


def _raw_attachment(notice_no: str) -> dict[str, str]:
    return {
        "bidNtceNo": notice_no,
        "bidNtceOrd": "00",
        "ntceSpecFileNm1": "제안요청서.pdf",
        "ntceSpecDocUrl1": (
            "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
            f"?bidPbancNo={notice_no}&bidPbancOrd=00&fileSeq=1&fileType="
        ),
    }


def _candidate(notice_no: str, *, day: int, title: str) -> dict[str, Any]:
    deadline = datetime.fromisoformat(f"2026-08-{day:02d}T17:00:00+09:00")
    return {
        "identity": f"{notice_no}|00|{deadline.isoformat()}",
        "bid_notice_no": notice_no,
        "revision_no": "00",
        "title": title,
        "agency": "공공기관",
        "published_at": datetime.fromisoformat("2026-08-20T09:00:00+09:00"),
        "provider_changed_at": datetime.fromisoformat("2026-08-20T10:00:00+09:00"),
        "deadline": deadline,
        "deadline_basis": "BID_CLOSE",
        "estimated_amount": 100_000_000,
        "notice_kind": "일반공고",
        "bid_method": "전자입찰",
        "contract_method": "일반경쟁",
        "award_method": "협상에 의한 계약",
        "direct_contract_signal": False,
        "source_url": (
            f"https://www.g2b.go.kr/notice/{notice_no}?serviceKey=DO-NOT-EXPOSE&view=1"
        ),
        "raw": _raw_attachment(notice_no),
    }


class _FakeDiscoveryClient:
    yielded = [
        _candidate("20260820001", day=28, title="공공기관 AI 교육 용역"),
        _candidate("20260820002", day=29, title="공공기관 리더십 교육 용역"),
    ]
    requests: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: object) -> None:
        self.request_count = 1
        self.hit_page_limit = False
        self.hit_time_limit = False

    def __enter__(self) -> "_FakeDiscoveryClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_notices(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        type(self).requests.append(kwargs)
        yield from type(self).yielded


@pytest.fixture()
def discovery_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    database_path = tmp_path / "pps-discovery.db"
    engine = build_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    settings = Settings(
        environment="production",
        database_url=f"sqlite:///{database_path.as_posix()}",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token=_OPERATOR_TOKEN,
        openai_api_key="server-only-openai-key",
        pps_api_key="server-only-pps-key",
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.include_router(pps_discovery_router)
    app.include_router(manual_analysis_router)
    monkeypatch.setattr("pai_loop.pps_discovery.PpsClient", _FakeDiscoveryClient)
    monkeypatch.setattr(
        "pai_loop.manual_analysis.run_notice_analysis_batch",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="COMPLETED",
            openai_calls=0,
            openai_telemetry=OpenAITelemetry(),
            completed=1,
            failed=0,
            job_id="fake-manual-batch",
            enrichment=SimpleNamespace(warnings=[]),
        ),
    )
    _FakeDiscoveryClient.requests = []
    _FakeDiscoveryClient.yielded = [
        _candidate("20260820001", day=28, title="공공기관 AI 교육 용역"),
        _candidate("20260820002", day=29, title="공공기관 리더십 교육 용역"),
    ]
    with TestClient(app, base_url="https://testserver") as client:
        yield client
    engine.dispose()


def _search(client: TestClient):
    return client.post(
        "/api/v1/pps-discovery/search",
        headers=_HEADERS,
        json={
            "query": "교육 용역",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
            "limit": 25,
        },
    )


def test_search_requires_same_origin_operator_token_and_writes_nothing(
    discovery_client: TestClient,
) -> None:
    denied_origin = discovery_client.post(
        "/api/v1/pps-discovery/search",
        json={
            "query": "교육 용역",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
        },
    )
    assert denied_origin.status_code == 403

    denied_token = discovery_client.post(
        "/api/v1/pps-discovery/search",
        headers={"Origin": "https://testserver"},
        json={
            "query": "교육 용역",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
        },
    )
    assert denied_token.status_code == 401

    response = _search(discovery_client)
    assert response.status_code == 200
    body = response.json()
    assert body["result_count"] == 2
    assert body["candidates"][0]["already_stored"] is False
    assert body["candidates"][0]["saveable"] is True
    assert "serviceKey" not in (body["candidates"][0]["source_url"] or "")
    assert _FakeDiscoveryClient.requests[0]["extra_params"] == {
        "bidNtceNm": "교육 용역"
    }
    assert _FakeDiscoveryClient.requests[0]["max_pages"] == 3

    factory = discovery_client.app.state.session_factory
    with factory() as session:
        assert session.scalar(select(func.count(Notice.id))) == 0
        assert session.scalar(select(func.count(NoticeVersion.id))) == 0
        assert session.scalar(select(func.count(IngestionJob.id))) == 0


@pytest.mark.parametrize(
    "deadline_timezone",
    [timezone.utc, timezone(timedelta(hours=9))],
    ids=["utc", "kst"],
)
def test_ended_notice_is_not_saveable_and_save_recheck_writes_nothing(
    discovery_client: TestClient,
    deadline_timezone: timezone,
) -> None:
    ended = _candidate(
        "20260820003",
        day=28,
        title="마감된 공공기관 교육 용역",
    )
    deadline = (datetime.now(timezone.utc) - timedelta(minutes=1)).astimezone(
        deadline_timezone
    )
    ended["deadline"] = deadline
    ended["identity"] = f"{ended['bid_notice_no']}|00|{deadline.isoformat()}"
    _FakeDiscoveryClient.yielded = [ended]

    search = _search(discovery_client)
    assert search.status_code == 200, search.text
    candidate = search.json()["candidates"][0]
    assert candidate["saveable"] is False
    assert "마감" in candidate["save_block_reason"]

    saved = discovery_client.post(
        "/api/v1/pps-discovery/save",
        headers=_HEADERS,
        json={
            "query": "교육 용역",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
            "bid_notice_no": candidate["bid_notice_no"],
            "selection_key": candidate["selection_key"],
        },
    )
    assert saved.status_code == 409, saved.text
    assert "마감" in saved.json()["detail"]

    factory = discovery_client.app.state.session_factory
    with factory() as session:
        assert session.scalar(select(func.count(Notice.id))) == 0
        assert session.scalar(select(func.count(NoticeVersion.id))) == 0
        assert session.scalar(select(func.count(IngestionJob.id))) == 0


def test_save_persists_only_selected_notice_manifest_and_is_idempotent(
    discovery_client: TestClient,
) -> None:
    search = _search(discovery_client)
    assert search.status_code == 200
    selected = next(
        item
        for item in search.json()["candidates"]
        if item["bid_notice_no"] == "20260820001"
    )
    payload = {
        "query": "교육 용역",
        "from_date": "2026-08-01",
        "to_date": "2026-08-23",
        "bid_notice_no": selected["bid_notice_no"],
        "selection_key": selected["selection_key"],
    }

    saved = discovery_client.post(
        "/api/v1/pps-discovery/save",
        headers=_HEADERS,
        json=payload,
    )
    assert saved.status_code == 200
    assert saved.json()["outcome"] == "CREATED"
    assert saved.json()["attachments_discovered"] == 1
    assert saved.json()["analysis_started"] is False
    assert saved.json()["openai_calls"] == 0

    factory = discovery_client.app.state.session_factory
    with factory() as session:
        notices = list(session.scalars(select(Notice)).all())
        assert len(notices) == 1
        assert notices[0].bid_notice_no == "20260820001"
        versions = list(session.scalars(select(NoticeVersion)).all())
        assert len(versions) == 1
        manifest = versions[0].source_payload["attachment_manifest"]
        assert len(manifest) == 1
        assert manifest[0]["file_name"] == "제안요청서.pdf"
        assert session.scalar(
            select(func.count(IngestionJob.id)).where(
                IngestionJob.source == "MANUAL_ANALYSIS"
            )
        ) == 0

        # Simulate a row left behind by the legacy discovery-save behavior.
        # Re-saving the same notice must remove it immediately.
        session.add(
            NoticeAnalysisPolicy(
                notice_key=payload["selection_key"],
                bid_notice_no=payload["bid_notice_no"],
                analysis_policy="MANUAL_ONLY",
                policy_source="USER_PPS_DISCOVERY",
            )
        )
        session.commit()

    repeated = discovery_client.post(
        "/api/v1/pps-discovery/save",
        headers=_HEADERS,
        json=payload,
    )
    assert repeated.status_code == 200
    assert repeated.json()["outcome"] == "ALREADY_STORED"
    assert repeated.json()["manifest_reused"] is True
    with factory() as session:
        assert session.scalar(select(func.count(Notice.id))) == 1
        assert session.scalar(select(func.count(NoticeVersion.id))) == 1
        assert session.scalar(select(func.count(NoticeAnalysisPolicy.notice_key))) == 0


def test_save_rejects_changed_provider_identity_before_persisting(
    discovery_client: TestClient,
) -> None:
    search = _search(discovery_client)
    selected = next(
        item
        for item in search.json()["candidates"]
        if item["bid_notice_no"] == "20260820001"
    )
    _FakeDiscoveryClient.yielded = [
        _candidate("20260820001", day=30, title="공공기관 AI 교육 용역")
    ]
    response = discovery_client.post(
        "/api/v1/pps-discovery/save",
        headers=_HEADERS,
        json={
            "query": "교육 용역",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
            "bid_notice_no": selected["bid_notice_no"],
            "selection_key": selected["selection_key"],
        },
    )
    assert response.status_code == 409
    assert "다시 검색" in response.json()["detail"]

    factory = discovery_client.app.state.session_factory
    with factory() as session:
        assert session.scalar(select(func.count(Notice.id))) == 0
        assert session.scalar(select(func.count(IngestionJob.id))) == 0


def test_manually_saved_notice_is_visible_and_included_in_automatic_queues(
    discovery_client: TestClient,
) -> None:
    search = _search(discovery_client)
    selected = next(
        item
        for item in search.json()["candidates"]
        if item["bid_notice_no"] == "20260820001"
    )
    saved = discovery_client.post(
        "/api/v1/pps-discovery/save",
        headers=_HEADERS,
        json={
            "query": "교육 용역",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
            "bid_notice_no": selected["bid_notice_no"],
            "selection_key": selected["selection_key"],
        },
    )
    assert saved.status_code == 200
    notice_key = saved.json()["notice_key"]

    factory = discovery_client.app.state.session_factory
    now = datetime.fromisoformat("2026-08-23T00:00:00+00:00")
    with factory() as session:
        save_job = session.scalar(
            select(IngestionJob).where(IngestionJob.source == "PPS_MANUAL_SAVE")
        )
        assert save_job is not None
        save_job.completed_at = datetime.now(timezone.utc) - timedelta(days=8)
        session.commit()
        retention = apply_operational_retention(
            RetentionRequest(retention_days=7, dry_run=False),
            session,
        )
        assert retention.deleted["ingestion_jobs"] == 1
        assert "notice_analysis_policies" in retention.preserved
        assert session.scalar(
            select(func.count(IngestionJob.id)).where(
                IngestionJob.source == "PPS_MANUAL_SAVE"
            )
        ) == 0
        assert session.get(NoticeAnalysisPolicy, notice_key) is None

        briefing = daily_briefing(session, days=7, limit=20, as_of=now)
        assert notice_key in {
            item["notice_key"] for item in briefing["notices"]
        }
        assert notice_key in briefing["analysis_queue"]["notice_keys"]
        assert notice_key in _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(include_retryable=True),
            now=now,
        )

    # The explicit user action remains available in addition to automation.
    manual = discovery_client.post(
        f"/api/v1/notices/{notice_key}/analysis/request",
        headers=_HEADERS,
        json={"run_extraction": True},
    )
    assert manual.status_code == 200
    assert manual.json()["outcome"] == "QUEUED"
