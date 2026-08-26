from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import pai_loop.analysis_api as analysis_api
import pai_loop.pps_metadata_refresh as metadata_refresh
from pai_loop.api import _pps_notice_key
from pai_loop.integrations.pps import KST, PpsApiError
from pai_loop.models import IngestionJob, Notice, NoticeVersion
from pai_loop.pps_enrichment import PPS_METADATA_KIND, PPS_METADATA_SCHEMA


def _provider_item(bid_notice_no: str) -> dict[str, Any]:
    deadline = datetime.now(timezone.utc) + timedelta(days=10)
    return {
        "identity": f"{bid_notice_no}|00|{deadline.isoformat()}",
        "bid_notice_no": bid_notice_no,
        "revision_no": "00",
        "title": "진행 중 메타데이터 갱신 검증 용역",
        "agency": "공공기관",
        "published_at": datetime.now(timezone.utc) - timedelta(days=2),
        "provider_changed_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "deadline": deadline,
        "deadline_basis": "BID_CLOSE",
        "estimated_amount": 100_000_000,
        "notice_kind": "일반공고",
        "bid_method": "전자입찰",
        "contract_method": "일반경쟁",
        "award_method": "협상에 의한 계약",
        "direct_contract_signal": False,
        "source_url": f"https://www.g2b.go.kr/notice/{bid_notice_no}",
        "raw": {
            "bidNtceNo": bid_notice_no,
            "bidNtceOrd": "00",
            "ntceSpecFileNm1": "제안요청서.pdf",
            "ntceSpecDocUrl1": (
                "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
                f"?bidPbancNo={bid_notice_no}&bidPbancOrd=00&fileSeq=1&fileType="
            ),
        },
    }


def _store_notice(
    client: TestClient,
    *,
    item: dict[str, Any],
    schema_version: str | None,
    notice_key: str | None = None,
    status: str = "OPEN",
    deadline: datetime | None = None,
) -> tuple[str, str]:
    key = notice_key or _pps_notice_key(item)
    with client.app.state.session_factory() as session:
        notice = Notice(
            notice_key=key,
            bid_notice_no=str(item["bid_notice_no"]),
            revision_no=str(item["revision_no"]),
            title=str(item["title"]),
            agency=str(item["agency"]),
            published_at=item["published_at"],
            deadline=deadline or item["deadline"],
            status=status,
            category="용역",
            estimated_amount=item["estimated_amount"],
            source_url=item["source_url"],
        )
        session.add(notice)
        session.flush()
        if schema_version is not None:
            session.add(
                NoticeVersion(
                    notice_id=notice.id,
                    version_no=1,
                    file_sha256="a" * 64,
                    document_complete=False,
                    extraction_status="METADATA",
                    extraction_confidence=1.0,
                    source_payload={
                        "kind": PPS_METADATA_KIND,
                        "schema_version": schema_version,
                        "attachment_manifest": [],
                    },
                )
            )
        session.commit()
        return notice.id, key


class _ExactPpsClient:
    item: dict[str, Any]
    calls: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.request_count = 1
        self.hit_page_limit = False
        self.hit_time_limit = False

    def __enter__(self) -> "_ExactPpsClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_notices(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        type(self).calls.append(kwargs)
        yield dict(type(self).item)


class _FailingPpsClient(_ExactPpsClient):
    def iter_notices(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        type(self).calls.append(kwargs)
        raise PpsApiError("synthetic provider failure")
        yield  # pragma: no cover


def _enable_pps(client: TestClient) -> None:
    client.app.state.settings = replace(
        client.app.state.settings,
        pps_api_key="server-only-test-key",
    )


def _request(client: TestClient) -> SimpleNamespace:
    return SimpleNamespace(app=client.app)


def test_current_metadata_skips_provider_and_writes_no_job(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _provider_item("R26BK-METADATA-CURRENT")
    notice_id, _key = _store_notice(
        client,
        item=item,
        schema_version=PPS_METADATA_SCHEMA,
    )
    monkeypatch.setattr(
        metadata_refresh,
        "PpsClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider called")),
    )

    result = metadata_refresh.refresh_pps_metadata_for_analysis(
        _request(client),
        notice_id=notice_id,
        dry_run=False,
        deadline_monotonic=time.monotonic() + 10,
    )

    assert result.status == "CURRENT"
    assert result.provider_calls == 0
    with client.app.state.session_factory() as session:
        assert session.scalar(
            select(func.count(IngestionJob.id)).where(
                IngestionJob.source == "PPS_METADATA_REFRESH"
            )
        ) == 0


def test_stale_metadata_exactly_refreshes_and_persists_current_schema(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_pps(client)
    item = _provider_item("R26BK-METADATA-STALE")
    notice_id, _key = _store_notice(
        client,
        item=item,
        schema_version="pai-loop-pps-notice-metadata-0.1.0",
    )
    _ExactPpsClient.item = item
    _ExactPpsClient.calls = []
    monkeypatch.setattr(metadata_refresh, "PpsClient", _ExactPpsClient)

    result = metadata_refresh.refresh_pps_metadata_for_analysis(
        _request(client),
        notice_id=notice_id,
        dry_run=False,
        deadline_monotonic=time.monotonic() + 10,
    )

    assert result.status == "REFRESHED"
    assert result.provider_calls == 1
    assert len(_ExactPpsClient.calls) == 1
    call = _ExactPpsClient.calls[0]
    assert call["inquiry_division"] == "1"
    assert call["extra_params"] == {"bidNtceNo": item["bid_notice_no"]}
    assert call["start"] == item["published_at"].astimezone(KST).date()
    assert call["end"] == item["published_at"].astimezone(KST).date()
    assert call["rows"] == 999
    assert call["max_pages"] == 5
    with client.app.state.session_factory() as session:
        versions = list(
            session.scalars(
                select(NoticeVersion)
                .where(NoticeVersion.notice_id == notice_id)
                .order_by(NoticeVersion.version_no.desc())
            ).all()
        )
        metadata = next(
            version
            for version in versions
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == PPS_METADATA_KIND
        )
        assert metadata.source_payload["schema_version"] == PPS_METADATA_SCHEMA
        job = session.scalar(
            select(IngestionJob).where(
                IngestionJob.source == "PPS_METADATA_REFRESH"
            )
        )
        assert job is not None
        assert job.status == "COMPLETED"
        assert job.api_calls == 1


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("inactive", "INACTIVE"), ("non_pps", "NOT_APPLICABLE")],
)
def test_inactive_and_non_pps_notices_never_call_provider(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected: str,
) -> None:
    item = _provider_item(f"R26BK-METADATA-{kind.upper()}")
    notice_id, _key = _store_notice(
        client,
        item=item,
        schema_version="pai-loop-pps-notice-metadata-0.1.0" if kind == "inactive" else None,
        notice_key="MANUAL-NON-PPS" if kind == "non_pps" else None,
        deadline=(
            datetime.now(timezone.utc) - timedelta(minutes=1)
            if kind == "inactive"
            else None
        ),
    )
    monkeypatch.setattr(
        metadata_refresh,
        "PpsClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider called")),
    )

    result = metadata_refresh.refresh_pps_metadata_for_analysis(
        _request(client),
        notice_id=notice_id,
        dry_run=False,
        deadline_monotonic=time.monotonic() + 10,
    )

    assert result.status == expected
    assert result.provider_calls == 0


def test_stale_dry_run_calls_no_provider_and_writes_nothing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _provider_item("R26BK-METADATA-DRY-RUN")
    notice_id, _key = _store_notice(
        client,
        item=item,
        schema_version="pai-loop-pps-notice-metadata-0.1.0",
    )
    monkeypatch.setattr(
        metadata_refresh,
        "PpsClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider called")),
    )

    with client.app.state.session_factory() as session:
        before_versions = session.scalar(select(func.count(NoticeVersion.id)))
        before_jobs = session.scalar(select(func.count(IngestionJob.id)))
    result = metadata_refresh.refresh_pps_metadata_for_analysis(
        _request(client),
        notice_id=notice_id,
        dry_run=True,
        deadline_monotonic=time.monotonic() + 10,
    )

    assert result.status == "DRY_RUN"
    assert result.provider_calls == 0
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(NoticeVersion.id))) == before_versions
        assert session.scalar(select(func.count(IngestionJob.id))) == before_jobs


def test_provider_failure_blocks_enrichment_pipeline_and_claude(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_pps(client)
    item = _provider_item("R26BK-METADATA-FAIL")
    _notice_id, notice_key = _store_notice(
        client,
        item=item,
        schema_version="pai-loop-pps-notice-metadata-0.1.0",
    )
    _FailingPpsClient.calls = []
    monkeypatch.setattr(metadata_refresh, "PpsClient", _FailingPpsClient)
    monkeypatch.setattr(
        analysis_api,
        "_enrich_one_notice",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Claude enrichment called")
        ),
    )
    monkeypatch.setattr(
        analysis_api,
        "run_analysis_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("analysis pipeline called")
        ),
    )

    response = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [notice_key],
            "enrich_missing": True,
            "max_notices": 1,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["openai_calls"] == 0
    assert body["results"][0]["status"] == "FAILED"
    assert body["results"][0]["document_status"] == "PPS_METADATA_REFRESH_REQUIRED"
    with client.app.state.session_factory() as session:
        refresh_job = session.scalar(
            select(IngestionJob).where(
                IngestionJob.source == "PPS_METADATA_REFRESH"
            )
        )
        assert refresh_job is not None
        assert refresh_job.status == "FAILED"
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        assert notice is not None
        metadata = next(
            version
            for version in session.scalars(
                select(NoticeVersion)
                .where(NoticeVersion.notice_id == notice.id)
                .order_by(NoticeVersion.version_no.desc())
            ).all()
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == PPS_METADATA_KIND
        )
        assert metadata.source_payload["schema_version"] == (
            "pai-loop-pps-notice-metadata-0.1.0"
        )
