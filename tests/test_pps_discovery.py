from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pai_loop.accounts import departments, router as accounts_router
from pai_loop.config import Settings
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.analysis_api import (
    AnalysisBackfillPlanRequest,
    AnalysisBatchRequest,
    _create_batch_job,
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
}


def _login_department(client: TestClient, *, paid: bool = True) -> dict[str, str]:
    # Synthetic account only, bootstrapped into this test's disposable database.
    password = "SYN-account-fixture-password-0908"
    accounts = [{"username": "SYN_DISCOVERY_DEPT", "password": password, "role": "DEPARTMENT",
                 "department_id": next(iter(departments())), "active": True,
                 "paid_analysis_allowed": paid}]
    server_headers = {"X-PAI-LOOP-API-KEY": "SYN-discovery-bootstrap-only"}
    preview = client.post("/api/v1/accounts/bootstrap", headers=server_headers, json={"accounts": accounts})
    assert preview.status_code == 200, preview.text
    applied = client.post("/api/v1/accounts/bootstrap", headers=server_headers,
                          json={"accounts": accounts, "dry_run": False, "preview_id": preview.json()["preview_id"]})
    assert applied.status_code == 200, applied.text
    origin = _HEADERS
    login = client.post("/api/v1/accounts/login", headers=origin,
                        json={"username": "SYN_DISCOVERY_DEPT", "password": password})
    assert login.status_code == 200, login.text
    assert login.json()["authenticated"] is True
    assert login.json()["account"]["paid_analysis_allowed"] is paid
    return {**origin, "X-CSRF-Token": login.json()["csrf_token"]}


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
        department_accounts_enabled=True,
        database_url=f"sqlite:///{database_path.as_posix()}",
        api_key="SYN-discovery-bootstrap-only",
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
    app.include_router(accounts_router)
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
        client.headers["X-CSRF-Token"] = _login_department(client)["X-CSRF-Token"]
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


def test_search_requires_same_origin_account_and_writes_nothing(
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

    with TestClient(discovery_client.app, base_url="https://testserver") as anonymous:
        for retired_pin in (None, "1357", _OPERATOR_TOKEN):
            headers = {"Origin": "https://testserver"}
            if retired_pin is not None:
                headers["X-PAI-Manual-Token"] = retired_pin
            denied = anonymous.post(
                "/api/v1/pps-discovery/search", headers=headers,
                json={"query": "교육 용역", "from_date": "2026-08-01", "to_date": "2026-08-23"},
            )
            assert denied.status_code == 401
    assert _FakeDiscoveryClient.requests == []

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
        assert session.scalar(select(func.count(NoticeAnalysisPolicy.notice_key))) == 1


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


def test_manually_saved_notice_is_visible_but_excluded_from_automatic_queues(
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
        assert session.get(NoticeAnalysisPolicy, notice_key) is not None

        briefing = daily_briefing(session, days=7, limit=20, as_of=now)
        assert notice_key in {
            item["notice_key"] for item in briefing["notices"]
        }
        assert notice_key not in briefing["analysis_queue"]["notice_keys"]
        assert _select_backfill_notice_keys(
            session,
            AnalysisBackfillPlanRequest(include_retryable=True),
            now=now,
        ) == []

        # Even an automatic parent planned before the marker was observed is
        # stopped again at the child execution boundary.
        operation_id = str(uuid.uuid4())
        session.add(
            IngestionJob(
                id=operation_id,
                source="ANALYSIS_BACKFILL",
                mode="LIVE",
                status="RUNNING",
                window_json={"scope": "OPEN_NOT_SELECTED"},
                request_json={},
                notice_keys=[notice_key],
                warnings=[],
            )
        )
        session.commit()

    with pytest.raises(HTTPException) as blocked:
        _create_batch_job(
            SimpleNamespace(app=discovery_client.app),
            AnalysisBatchRequest(
                notice_keys=[notice_key],
                enrich_missing=True,
                max_notices=1,
                operation_id=operation_id,
                segment_id=str(uuid.uuid4()),
                chunk_index=0,
            ),
        )
    assert blocked.value.status_code == 409

    # Keep the explicit-action assertion independent of the wall clock. The
    # discovery fixture is intentionally dated, so its original deadline will
    # eventually pass even though this test is not exercising expiry behavior.
    with factory() as session:
        notice = session.scalar(
            select(Notice).where(Notice.notice_key == notice_key)
        )
        assert notice is not None
        notice.deadline = datetime.now(timezone.utc) + timedelta(days=1)
        session.commit()

    # The durable marker filters only schedulers. The explicit user action is
    # still accepted through the existing same-origin manual-analysis route.
    manual = discovery_client.post(
        f"/api/v1/notices/{notice_key}/analysis/request",
        headers=_HEADERS,
        json={"run_extraction": True},
    )
    assert manual.status_code == 200
    assert manual.json()["outcome"] == "QUEUED"
