from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pai_loop.config import Settings
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.integrations.openai_extraction import (
    EvidenceAnchor,
    ExtractedRequirement,
    ExtractionOutcome,
    ExtractionPayload,
    OpenAIAttemptTelemetry,
    aggregate_openai_attempts,
)
from pai_loop.models import IngestionJob
from pai_loop.prespec_api import PRESPEC_ANALYSIS_STALE_AFTER, router as prespec_router
from pai_loop.prespec_models import (
    PreSpecification,
    PreSpecificationAnalysisRun,
    PreSpecificationDocument,
    PreSpecificationVersion,
)
from pai_loop.prespec_service import (
    FetchedPreSpecificationDocument,
    HttpPreSpecificationDocumentFetcher,
    PreSpecificationServiceError,
    analyse_pre_specification_documents,
    fetch_pre_specifications,
    persist_pre_specification,
    pre_specification_status,
)
from pai_loop.integrations.pps import PpsApiError


_TOKEN = "pre-specification-operator-token-32chars"
_HEADERS = {
    "Origin": "https://testserver",
    "Sec-Fetch-Site": "same-origin",
    "X-PAI-Manual-Token": _TOKEN,
}
_SAFE_URL = (
    "https://www.g2b.go.kr/pn/pnz/pnza/UntyAtchFile/downloadFile.do"
    "?bfSpecRegNo=R26BD00999999&fileSeq=1&fileType=BFDTL"
)


def _safe_url(registry_no: str, *, slot: int = 1) -> str:
    return (
        "https://www.g2b.go.kr/pn/pnz/pnza/UntyAtchFile/downloadFile.do"
        f"?bfSpecRegNo={registry_no}&fileSeq={slot}&fileType=BFDTL"
    )


def _record(
    *,
    title: str = "공공기관 AI 교육 사전규격",
    suffix: str = "00",
    registry_no: str = "R26BD00999999",
    document_urls: list[str] | None = None,
    linked_bid_notice_nos: list[str] | None = None,
    opinion_deadline: datetime | None = datetime.fromisoformat(
        "2027-08-30T18:00:00+09:00"
    ),
) -> dict[str, Any]:
    return {
        "source_kind": "PPS_PRESPEC",
        "pre_specification_key": f"PRESPEC-{registry_no}",
        "registry_no": registry_no,
        "title": title,
        "business_division": "일반용역",
        "reference_no": f"REF-{suffix}",
        "ordering_agency": "공공기관",
        "demand_agency": "교육부서",
        "budget_amount": 120_000_000,
        "received_at": datetime.fromisoformat("2026-08-20T09:00:00+09:00"),
        "opinion_deadline": opinion_deadline,
        "delivery_due": datetime.fromisoformat("2027-12-31T00:00:00+09:00"),
        "registered_at": datetime.fromisoformat("2026-08-20T09:00:00+09:00"),
        "changed_at": datetime.fromisoformat(f"2026-08-20T09:00:{suffix}+09:00"),
        "software_business": False,
        "document_urls": (
            [_safe_url(registry_no)] if document_urls is None else document_urls
        ),
        "linked_bid_notice_nos": list(linked_bid_notice_nos or []),
    }


class _FakePreSpecificationClient:
    records = [_record()]
    hit_page_limit_value = False
    hit_time_limit_value = False
    rows_quarantined_value = 0
    requests: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: object) -> None:
        self.request_count = 1
        self.hit_page_limit = self.hit_page_limit_value
        self.hit_time_limit = self.hit_time_limit_value
        self.rows_fetched = len(self.records)
        self.rows_quarantined = self.rows_quarantined_value

    def __enter__(self) -> "_FakePreSpecificationClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_pre_specifications(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        type(self).requests.append(kwargs)
        yield from type(self).records


class _FakeDocumentFetcher:
    calls = 0

    def fetch(self, **_kwargs: object) -> FetchedPreSpecificationDocument:
        type(self).calls += 1
        return FetchedPreSpecificationDocument(
            file_name="사전규격.html",
            content=(
                b"<html><body>" + "참가자격은 법인사업자입니다. 교육 수행실적을 제출합니다.".encode("utf-8") + b"</body></html>"
            ),
            content_type="text/html",
        )


class _FakeOpenAIClient:
    calls = 0

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeOpenAIClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract(self, *, document_text: str, allowed_attachment_ids: set[str]):
        type(self).calls += 1
        attachment_id = next(iter(allowed_attachment_ids))
        quote = "참가자격은 법인사업자입니다."
        assert quote in document_text
        telemetry = aggregate_openai_attempts(
            [
                OpenAIAttemptTelemetry(
                    attempt=1,
                    request_latency_ms=12,
                    response_received=True,
                    model="fixture-model",
                    service_tier="default",
                )
            ]
        )
        return ExtractionOutcome(
            status="ACCEPTED",
            message="fixture accepted",
            api_calls=1,
            openai_telemetry=telemetry,
            data=ExtractionPayload(
                document_type="OTHER",
                requirements=[
                    ExtractedRequirement(
                        requirement_id="REQ-1",
                        category="ENTITY",
                        logic="SINGLE",
                        normalized_condition="법인사업자",
                        mandatory=True,
                        deadline_basis=None,
                        evidence=[
                            EvidenceAnchor(
                                attachment_id=attachment_id,
                                page=None,
                                section=None,
                                quote=quote,
                                confidence=0.99,
                            )
                        ],
                        ambiguity_reason=None,
                    )
                ],
                quantitative_tables=[],
                quantitative_table_not_applicable=None,
                missing_or_unreadable=[],
                summary=(
                    "법인사업자 자격이 필요합니다. 문의 "
                    "test\u0040example.com 010\u002d1234\u002d5678"
                ),
            ),
        )


class _FailingPreSpecificationClient:
    def __init__(self, **_kwargs: object) -> None:
        raise PpsApiError("fixture provider failure")


class _ExplodingOpenAIClient:
    def __init__(self, **_kwargs: object) -> None:
        raise RuntimeError("fixture model boundary failure")


class _ReviewOpenAIClient(_FakeOpenAIClient):
    def extract(self, **_kwargs: object) -> ExtractionOutcome:
        telemetry = aggregate_openai_attempts(
            [
                OpenAIAttemptTelemetry(
                    attempt=1,
                    request_latency_ms=3,
                    response_received=True,
                    model="fixture-model",
                )
            ]
        )
        return ExtractionOutcome(
            status="REVIEW",
            review_code="R07",
            error_code="OPENAI_SCHEMA_INVALID",
            message="fixture review",
            api_calls=1,
            openai_telemetry=telemetry,
        )


@pytest.fixture()
def prespec_client(tmp_path: Path) -> Iterator[TestClient]:
    database_path = tmp_path / "prespec.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = build_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    app = FastAPI()
    app.state.settings = Settings(
        environment="production",
        database_url=database_url,
        api_key="server-api-key",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token=_TOKEN,
        public_manual_analysis_hourly_limit=12,
        public_manual_analysis_cooldown_hours=24,
        pps_api_key="server-only-pps-key",
        openai_api_key="server-only-openai-key",
    )
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.prespec_client_factory = _FakePreSpecificationClient
    app.state.prespec_document_fetcher = _FakeDocumentFetcher()
    app.state.prespec_openai_client_factory = _FakeOpenAIClient
    app.include_router(prespec_router)
    _FakePreSpecificationClient.records = [_record()]
    _FakePreSpecificationClient.hit_page_limit_value = False
    _FakePreSpecificationClient.hit_time_limit_value = False
    _FakePreSpecificationClient.rows_quarantined_value = 0
    _FakePreSpecificationClient.requests = []
    _FakeDocumentFetcher.calls = 0
    _FakeOpenAIClient.calls = 0
    with TestClient(app, base_url="https://testserver") as client:
        yield client
    engine.dispose()


def _search(client: TestClient):
    return client.post(
        "/api/v1/prespec-discovery/search",
        headers=_HEADERS,
        json={
            "query": "AI 교육",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
        },
    )


def _save(client: TestClient):
    search = _search(client)
    assert search.status_code == 200
    candidate = search.json()["candidates"][0]
    return client.post(
        "/api/v1/prespec-discovery/save",
        headers=_HEADERS,
        json={
            "query": "AI 교육",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
            "registry_no": candidate["registry_no"],
            "selection_token": candidate["selection_token"],
        },
    )


def _insert_analysis_run(
    client: TestClient,
    *,
    status: str,
    age: timedelta = timedelta(),
    openai_calls: int = 0,
    telemetry: dict[str, Any] | None = None,
    with_job: bool = True,
) -> tuple[str, str | None]:
    created_at = datetime.now(timezone.utc) - age
    with client.app.state.session_factory() as session:
        stored = session.scalar(select(PreSpecification))
        assert stored is not None
        run = PreSpecificationAnalysisRun(
            pre_specification_id=stored.id,
            source_digest=stored.source_digest,
            idempotency_key=(
                f"fixture-{status}-{created_at.timestamp()}-{openai_calls}"
            ),
            status=status,
            result_json=None,
            document_results=[],
            openai_calls=openai_calls,
            openai_telemetry=(
                OpenAIAttemptTelemetry(
                    attempt=1,
                    request_latency_ms=1,
                    response_received=True,
                ).model_dump(mode="json")
                if telemetry is None and openai_calls
                else (telemetry or {})
            ),
            warnings=[],
            created_at=created_at,
            completed_at=(None if status == "RUNNING" else created_at),
        )
        session.add(run)
        session.flush()
        job_id: str | None = None
        if with_job:
            job = IngestionJob(
                source="MANUAL_PRESPEC_ANALYSIS",
                mode="LIVE",
                status=status,
                window_json={"scope": "ONE_SAVED_PRE_SPECIFICATION"},
                request_json={
                    "registry_no": stored.registry_no,
                    "analysis_id": run.id,
                },
                notice_keys=[],
                warnings=[],
                api_calls=openai_calls,
                created_at=created_at,
                completed_at=(None if status == "RUNNING" else created_at),
            )
            session.add(job)
            session.flush()
            job_id = job.id
        session.commit()
        return run.id, job_id


def test_prespec_search_is_same_origin_zero_openai_and_write_free(
    prespec_client: TestClient,
) -> None:
    denied = prespec_client.post(
        "/api/v1/prespec-discovery/search",
        json={
            "query": "AI 교육",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
        },
    )
    assert denied.status_code == 403

    response = _search(prespec_client)
    assert response.status_code == 200
    body = response.json()
    assert body["provider_status"] == "COMPLETED"
    assert body["api_calls"] == 1
    assert body["openai_calls"] == 0
    assert body["result_count"] == 1
    assert body["candidates"][0]["selection_token"].startswith("PRESPECSEL-")
    request = _FakePreSpecificationClient.requests[0]
    assert request["rows"] == 999
    assert request["max_pages"] == 3

    with prespec_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(PreSpecification.id))) == 0
        assert session.scalar(select(func.count(IngestionJob.id))) == 0


def test_stored_prespec_search_scans_records_older_than_previous_500_cap(
    prespec_client: TestClient,
) -> None:
    base_time = datetime(2026, 8, 23, tzinfo=timezone.utc)
    with prespec_client.app.state.session_factory() as session:
        for index in range(501):
            registry_no = f"R26BD{index:08d}"
            session.add(
                PreSpecification(
                    registry_no=registry_no,
                    pre_specification_key=f"PRESPEC-{registry_no}",
                    title=(
                        "오래된 전체검색 표적 사전규격"
                        if index == 500
                        else f"일반 사전규격 {index:03d}"
                    ),
                    source_digest=f"{index:064x}",
                    changed_at=base_time - timedelta(minutes=index),
                    matched_keywords=[],
                    linked_bid_notice_nos=[],
                )
            )
        session.commit()

    response = prespec_client.get(
        "/api/v1/pre-specifications",
        params={"search_keywords": "전체검색 표적", "limit": 10},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result_count"] == 1
    assert body["items"][0]["registry_no"] == "R26BD00000500"
    assert body["truncated"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {
            "query": "  ",
            "from_date": "2026-08-01",
            "to_date": "2026-08-23",
        },
        {
            "query": "AI 교육",
            "from_date": "2026-08-23",
            "to_date": "2026-08-01",
        },
        {
            "query": "AI 교육",
            "from_date": "2026-07-01",
            "to_date": "2026-08-23",
        },
    ],
)
def test_prespec_search_rejects_unbounded_inputs(
    prespec_client: TestClient,
    payload: dict[str, str],
) -> None:
    response = prespec_client.post(
        "/api/v1/prespec-discovery/search",
        headers=_HEADERS,
        json=payload,
    )
    assert response.status_code == 422
    assert _FakePreSpecificationClient.requests == []


def test_prespec_search_fail_closed_and_partial_provider_reporting(
    prespec_client: TestClient,
) -> None:
    prespec_client.app.state.settings = replace(
        prespec_client.app.state.settings,
        public_manual_analysis_enabled=False,
    )
    assert _search(prespec_client).status_code == 404

    prespec_client.app.state.settings = replace(
        prespec_client.app.state.settings,
        public_manual_analysis_enabled=True,
        pps_api_key=None,
    )
    assert _search(prespec_client).status_code == 503

    prespec_client.app.state.settings = replace(
        prespec_client.app.state.settings,
        pps_api_key="server-only-pps-key",
    )
    prespec_client.app.state.prespec_client_factory = _FailingPreSpecificationClient
    provider_error = _search(prespec_client)
    assert provider_error.status_code == 502
    assert provider_error.json()["detail"]["code"] == "PPS_PRESPEC_PROVIDER_ERROR"

    prespec_client.app.state.prespec_client_factory = _FakePreSpecificationClient
    _FakePreSpecificationClient.hit_time_limit_value = True
    _FakePreSpecificationClient.rows_quarantined_value = 2
    partial = _search(prespec_client)
    assert partial.status_code == 200
    assert partial.json()["provider_status"] == "PARTIAL"
    assert partial.json()["quarantined"] == 2
    assert set(partial.json()["warnings"]) == {
        "PPS_TIME_LIMIT_REACHED",
        "PPS_ROWS_QUARANTINED",
    }


def test_prespec_save_is_idempotent_versioned_and_reports_partial(
    prespec_client: TestClient,
) -> None:
    first = _save(prespec_client)
    assert first.status_code == 200
    assert first.json()["outcome"] == "CREATED"
    assert first.json()["openai_calls"] == 0
    assert first.json()["document_count"] == 1

    repeated = _save(prespec_client)
    assert repeated.status_code == 200
    assert repeated.json()["outcome"] == "ALREADY_STORED"
    with prespec_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(PreSpecification.id))) == 1
        assert session.scalar(select(func.count(PreSpecificationVersion.id))) == 1

    _FakePreSpecificationClient.records = [_record(suffix="01")]
    changed = _save(prespec_client)
    assert changed.status_code == 200
    assert changed.json()["outcome"] == "UPDATED"
    with prespec_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count(PreSpecificationVersion.id))) == 2

    _FakePreSpecificationClient.hit_page_limit_value = True
    partial = _search(prespec_client)
    assert partial.status_code == 200
    assert partial.json()["provider_status"] == "PARTIAL"
    assert partial.json()["truncated"] is True
    assert "PPS_PAGE_LIMIT_REACHED" in partial.json()["warnings"]


def test_prespec_save_revalidates_presence_and_selection_digest(
    prespec_client: TestClient,
) -> None:
    searched = _search(prespec_client)
    candidate = searched.json()["candidates"][0]
    request = {
        "query": "AI 교육",
        "from_date": "2026-08-01",
        "to_date": "2026-08-23",
        "registry_no": candidate["registry_no"],
        "selection_token": candidate["selection_token"],
    }

    _FakePreSpecificationClient.records = []
    absent = prespec_client.post(
        "/api/v1/prespec-discovery/save",
        headers=_HEADERS,
        json=request,
    )
    assert absent.status_code == 404

    _FakePreSpecificationClient.records = [_record(suffix="01")]
    changed = prespec_client.post(
        "/api/v1/prespec-discovery/save",
        headers=_HEADERS,
        json=request,
    )
    assert changed.status_code == 409

    invalid = {
        **request,
        "registry_no": "bad registry!",
        "selection_token": "PRESPECSEL-" + "z" * 32,
    }
    assert prespec_client.post(
        "/api/v1/prespec-discovery/save",
        headers=_HEADERS,
        json=invalid,
    ).status_code == 422


def test_prespec_public_read_and_explicit_analysis_reuse(
    prespec_client: TestClient,
) -> None:
    saved = _save(prespec_client)
    assert saved.status_code == 200

    listing = prespec_client.get("/api/v1/pre-specifications")
    assert listing.status_code == 200
    assert listing.json()["source_calls"] == {"pps": 0, "openai": 0}
    assert listing.json()["items"][0]["status"] == "OPEN_FOR_OPINION"

    detail = prespec_client.get("/api/v1/pre-specifications/R26BD00999999")
    assert detail.status_code == 200
    assert detail.json()["documents"][0]["safe_url"] == _SAFE_URL
    assert detail.json()["analysis"] is None

    not_approved = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": False},
    )
    assert not_approved.status_code == 409

    analysed = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert analysed.status_code == 200
    assert analysed.json()["outcome"] == "QUEUED"
    analysis_id = analysed.json()["analysis_id"]

    completed = prespec_client.get(
        f"/api/v1/pre-specifications/R26BD00999999/analysis/{analysis_id}",
        headers=_HEADERS,
    )
    assert completed.status_code == 200
    assert completed.json()["outcome"] == "COMPLETED"
    assert completed.json()["openai_calls"] == 1
    assert completed.json()["openai_telemetry"]["api_calls"] == 1
    assert _FakeDocumentFetcher.calls == 1
    assert _FakeOpenAIClient.calls == 1

    analysed_detail = prespec_client.get(
        "/api/v1/pre-specifications/R26BD00999999"
    )
    assert analysed_detail.status_code == 200
    assert analysed_detail.json()["analysis"]["status"] == "COMPLETED"
    assert analysed_detail.json()["analysis"]["result"]["source_kind"] == "PPS_PRESPEC"
    analysed_listing = prespec_client.get("/api/v1/pre-specifications")
    assert analysed_listing.json()["items"][0]["analysis"]["status"] == "COMPLETED"
    assert "result" not in analysed_listing.json()["items"][0]["analysis"]

    reused = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": False},
    )
    assert reused.status_code == 200
    assert reused.json()["outcome"] == "ALREADY_ANALYZED"
    assert _FakeOpenAIClient.calls == 1

    with prespec_client.app.state.session_factory() as session:
        run = session.scalar(select(PreSpecificationAnalysisRun))
        assert run is not None
        assert run.status == "COMPLETED"
        assert run.result_json["source_kind"] == "PPS_PRESPEC"
        assert "test\u0040example.com" not in repr(run.result_json)
        assert "010\u002d1234\u002d5678" not in repr(run.document_results)
        assert "server-only-openai-key" not in repr(run.result_json)
        job = session.scalar(
            select(IngestionJob).where(
                IngestionJob.source == "MANUAL_PRESPEC_ANALYSIS"
            )
        )
        assert job is not None
        assert job.api_calls == 1
        assert "server-only-openai-key" not in repr(job.request_json)


def test_prespec_list_detail_status_keyword_and_limit_filters(
    prespec_client: TestClient,
) -> None:
    assert _save(prespec_client).status_code == 200
    with prespec_client.app.state.session_factory() as session:
        persist_pre_specification(
            session,
            record=_record(
                registry_no="R26BD00888888",
                title="교육 플랫폼 연계 사전규격",
                linked_bid_notice_nos=["20260824001-00"],
                suffix="02",
            ),
            matched_keywords=["교육", "플랫폼"],
        )
        session.commit()

    linked = prespec_client.get(
        "/api/v1/pre-specifications",
        params={"status": "LINKED_TO_BID", "search_keywords": "교육,플랫폼"},
    )
    assert linked.status_code == 200
    assert [item["registry_no"] for item in linked.json()["items"]] == [
        "R26BD00888888"
    ]

    unmatched = prespec_client.get(
        "/api/v1/pre-specifications",
        params={"search_keywords": "존재하지않음"},
    )
    assert unmatched.status_code == 200
    assert unmatched.json()["result_count"] == 0

    limited = prespec_client.get(
        "/api/v1/pre-specifications",
        params={"limit": 1},
    )
    assert limited.status_code == 200
    assert limited.json()["result_count"] == 1
    assert limited.json()["truncated"] is True

    assert prespec_client.get(
        "/api/v1/pre-specifications/DOES-NOT-EXIST"
    ).status_code == 404
    too_many = ",".join(f"검색어{index}" for index in range(11))
    assert prespec_client.get(
        "/api/v1/pre-specifications",
        params={"search_keywords": too_many},
    ).status_code == 422
    assert prespec_client.get(
        "/api/v1/pre-specifications",
        params={"search_keywords": "가" * 61},
    ).status_code == 422


def test_prespec_analysis_shares_the_existing_hourly_quota(
    prespec_client: TestClient,
) -> None:
    assert _save(prespec_client).status_code == 200
    prespec_client.app.state.settings = replace(
        prespec_client.app.state.settings,
        public_manual_analysis_hourly_limit=1,
    )
    with prespec_client.app.state.session_factory() as session:
        session.add(
            IngestionJob(
                source="MANUAL_ANALYSIS",
                mode="LIVE",
                status="COMPLETED",
                window_json={"scope": "ONE_OPEN_PPS_NOTICE"},
                request_json={},
                notice_keys=["PPS-QUOTA-001"],
                warnings=[],
                completed_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

    response = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert response.status_code == 429
    assert _FakeDocumentFetcher.calls == 0
    assert _FakeOpenAIClient.calls == 0


def test_prespec_analysis_cooldown_and_missing_model_configuration(
    prespec_client: TestClient,
) -> None:
    assert _save(prespec_client).status_code == 200
    _insert_analysis_run(
        prespec_client,
        status="REVIEW",
        age=timedelta(minutes=1),
        openai_calls=1,
    )
    cooldown = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert cooldown.status_code == 200
    assert cooldown.json()["outcome"] == "COOLDOWN"

    with prespec_client.app.state.session_factory() as session:
        session.query(PreSpecificationAnalysisRun).delete()
        session.query(IngestionJob).filter(
            IngestionJob.source == "MANUAL_PRESPEC_ANALYSIS"
        ).delete()
        session.commit()
    prespec_client.app.state.settings = replace(
        prespec_client.app.state.settings,
        openai_api_key=None,
    )
    missing_key = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert missing_key.status_code == 503


def test_prespec_analysis_without_documents_is_queued_for_review_without_cost(
    prespec_client: TestClient,
) -> None:
    _FakePreSpecificationClient.records = [_record(document_urls=[])]
    assert _save(prespec_client).status_code == 200
    queued = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": False},
    )
    assert queued.status_code == 200
    assert queued.json()["outcome"] == "QUEUED"
    status_response = prespec_client.get(
        "/api/v1/pre-specifications/R26BD00999999/analysis/"
        + queued.json()["analysis_id"],
        headers=_HEADERS,
    )
    assert status_response.status_code == 200
    assert status_response.json()["outcome"] == "REVIEW"
    assert status_response.json()["openai_calls"] == 0
    assert status_response.json()["warnings"] == ["PRESPEC_DOCUMENT_NONE"]
    assert _FakeDocumentFetcher.calls == 0
    assert _FakeOpenAIClient.calls == 0


def test_prespec_background_failure_is_audited_and_status_is_failed(
    prespec_client: TestClient,
) -> None:
    assert _save(prespec_client).status_code == 200
    prespec_client.app.state.prespec_openai_client_factory = _ExplodingOpenAIClient
    queued = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert queued.status_code == 200
    status_response = prespec_client.get(
        "/api/v1/pre-specifications/R26BD00999999/analysis/"
        + queued.json()["analysis_id"],
        headers=_HEADERS,
    )
    assert status_response.status_code == 200
    assert status_response.json()["outcome"] == "FAILED"
    assert status_response.json()["warnings"] == ["PRESPEC_ANALYSIS_FAILED"]
    assert status_response.json()["openai_telemetry"]["accounting_complete"] is False
    with prespec_client.app.state.session_factory() as session:
        job = session.scalar(
            select(IngestionJob).where(
                IngestionJob.source == "MANUAL_PRESPEC_ANALYSIS"
            )
        )
        assert job is not None
        assert job.status == "FAILED"
        assert job.error_code == "PRESPEC_ANALYSIS_FAILED"


def test_prespec_status_maps_running_partial_review_and_missing(
    prespec_client: TestClient,
) -> None:
    assert _save(prespec_client).status_code == 200
    running_id, _ = _insert_analysis_run(
        prespec_client,
        status="RUNNING",
        age=timedelta(minutes=1),
    )
    running = prespec_client.get(
        f"/api/v1/pre-specifications/R26BD00999999/analysis/{running_id}",
        headers=_HEADERS,
    )
    assert running.status_code == 200
    assert running.json()["outcome"] == "QUEUED"
    fresh_duplicate = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert fresh_duplicate.status_code == 409

    for run_status in ("PARTIAL", "REVIEW", "FAILED"):
        run_id, _ = _insert_analysis_run(
            prespec_client,
            status=run_status,
            telemetry={"not": "valid telemetry"},
            with_job=False,
        )
        response = prespec_client.get(
            f"/api/v1/pre-specifications/R26BD00999999/analysis/{run_id}",
            headers=_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == run_status
        assert response.json()["openai_telemetry"]["accounting_complete"] is False

    assert prespec_client.get(
        "/api/v1/pre-specifications/R26BD00999999/analysis/"
        "00000000-0000-0000-0000-000000000000",
        headers=_HEADERS,
    ).status_code == 404


def test_stale_running_analysis_is_failed_on_status_and_can_be_requeued(
    prespec_client: TestClient,
) -> None:
    assert _save(prespec_client).status_code == 200
    prespec_client.app.state.settings = replace(
        prespec_client.app.state.settings,
        public_manual_analysis_hourly_limit=1,
    )
    stale_id, stale_job_id = _insert_analysis_run(
        prespec_client,
        status="RUNNING",
        age=PRESPEC_ANALYSIS_STALE_AFTER + timedelta(seconds=1),
    )
    stale_status = prespec_client.get(
        f"/api/v1/pre-specifications/R26BD00999999/analysis/{stale_id}",
        headers=_HEADERS,
    )
    assert stale_status.status_code == 200
    assert stale_status.json()["outcome"] == "FAILED"
    assert "PRESPEC_ANALYSIS_STALE" in stale_status.json()["warnings"]
    assert "다시 요청" in stale_status.json()["message"]

    with prespec_client.app.state.session_factory() as session:
        stale_job = session.get(IngestionJob, stale_job_id)
        assert stale_job is not None
        assert stale_job.status == "FAILED"
        assert stale_job.error_code == "PRESPEC_ANALYSIS_STALE"

    requeued = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert requeued.status_code == 200
    assert requeued.json()["outcome"] == "QUEUED"
    assert requeued.json()["analysis_id"] != stale_id


def test_stale_running_analysis_is_recovered_during_direct_requeue(
    prespec_client: TestClient,
) -> None:
    assert _save(prespec_client).status_code == 200
    prespec_client.app.state.settings = replace(
        prespec_client.app.state.settings,
        public_manual_analysis_hourly_limit=1,
    )
    stale_id, stale_job_id = _insert_analysis_run(
        prespec_client,
        status="RUNNING",
        age=PRESPEC_ANALYSIS_STALE_AFTER + timedelta(minutes=1),
    )
    requeued = prespec_client.post(
        "/api/v1/pre-specifications/R26BD00999999/analysis",
        headers=_HEADERS,
        json={"allow_openai": True},
    )
    assert requeued.status_code == 200
    assert requeued.json()["outcome"] == "QUEUED"
    assert requeued.json()["analysis_id"] != stale_id
    with prespec_client.app.state.session_factory() as session:
        stale = session.get(PreSpecificationAnalysisRun, stale_id)
        stale_job = session.get(IngestionJob, stale_job_id)
        assert stale is not None and stale.status == "FAILED"
        assert stale.warnings == ["PRESPEC_ANALYSIS_STALE"]
        assert stale_job is not None and stale_job.status == "FAILED"
        assert stale_job.error_code == "PRESPEC_ANALYSIS_STALE"


def test_prespec_service_persistence_bounds_and_effective_status(
    prespec_client: TestClient,
) -> None:
    with prespec_client.app.state.session_factory() as session:
        with pytest.raises(
            PreSpecificationServiceError,
            match="PRESPEC_IDENTITY_INCOMPLETE",
        ):
            persist_pre_specification(
                session,
                record={**_record(), "registry_no": ""},
                matched_keywords=[],
            )
        with pytest.raises(
            PreSpecificationServiceError,
            match="PRESPEC_DOCUMENT_LIMIT",
        ):
            persist_pre_specification(
                session,
                record=_record(
                    document_urls=[
                        _safe_url("R26BD00999999", slot=slot)
                        for slot in range(1, 7)
                    ]
                ),
                matched_keywords=[],
            )

    now = datetime.now(timezone.utc)
    assert pre_specification_status(
        opinion_deadline=None,
        linked_bid_notice_nos=[],
        now=now,
    ) == "OPINION_CLOSED"
    assert pre_specification_status(
        opinion_deadline=now + timedelta(days=1),
        linked_bid_notice_nos=["20260824001-00"],
        now=now,
    ) == "LINKED_TO_BID"
    assert pre_specification_status(
        opinion_deadline=(now - timedelta(days=1)).replace(tzinfo=None),
        linked_bid_notice_nos=[],
        now=now,
    ) == "OPINION_CLOSED"


def _analysis_document(*, slot: int = 1) -> PreSpecificationDocument:
    return PreSpecificationDocument(
        slot=slot,
        safe_url=_safe_url("R26BD00999999", slot=slot),
        source_digest=f"{slot:064x}",
    )


def test_prespec_document_analysis_bounds_review_and_partial_paths() -> None:
    no_documents = analyse_pre_specification_documents(
        registry_no="R26BD00999999",
        source_digest="a" * 64,
        documents=[],
        openai_api_key="",
        openai_model="fixture-model",
    )
    assert no_documents.status == "REVIEW"
    assert no_documents.warnings == ["PRESPEC_DOCUMENT_NONE"]

    with pytest.raises(PreSpecificationServiceError, match="OPENAI_NOT_CONFIGURED"):
        analyse_pre_specification_documents(
            registry_no="R26BD00999999",
            source_digest="a" * 64,
            documents=[_analysis_document()],
            openai_api_key="",
            openai_model="fixture-model",
        )
    with pytest.raises(PreSpecificationServiceError, match="PRESPEC_DOCUMENT_LIMIT"):
        analyse_pre_specification_documents(
            registry_no="R26BD00999999",
            source_digest="a" * 64,
            documents=[_analysis_document(slot=slot) for slot in range(1, 7)],
            openai_api_key="fixture-key",
            openai_model="fixture-model",
        )

    reviewed = analyse_pre_specification_documents(
        registry_no="R26BD00999999",
        source_digest="a" * 64,
        documents=[_analysis_document()],
        openai_api_key="fixture-key",
        openai_model="fixture-model",
        document_fetcher=_FakeDocumentFetcher(),
        openai_client_factory=_ReviewOpenAIClient,
    )
    assert reviewed.status == "REVIEW"
    assert reviewed.openai_calls == 1
    assert reviewed.warnings == ["OPENAI_SCHEMA_INVALID"]

    class _LongDocumentFetcher:
        def fetch(self, **_kwargs: object) -> FetchedPreSpecificationDocument:
            return FetchedPreSpecificationDocument(
                file_name="long.html",
                content=(
                    b"<html><body>"
                    + "참가자격은 법인사업자입니다.\n".encode("utf-8")
                    + ("일반 설명 문장입니다.\n" * 10_000).encode("utf-8")
                    + b"</body></html>"
                ),
                content_type="text/html",
            )

    partial = analyse_pre_specification_documents(
        registry_no="R26BD00999999",
        source_digest="b" * 64,
        documents=[_analysis_document()],
        openai_api_key="fixture-key",
        openai_model="fixture-model",
        document_fetcher=_LongDocumentFetcher(),
        openai_client_factory=_FakeOpenAIClient,
    )
    assert partial.status == "PARTIAL"
    assert partial.result_json is not None
    assert "PRESPEC_ANALYSIS_INPUT_PARTIAL" in partial.warnings


def test_prespec_document_analysis_failures_are_public_safe() -> None:
    class _ShortDocumentFetcher:
        def fetch(self, **_kwargs: object) -> FetchedPreSpecificationDocument:
            return FetchedPreSpecificationDocument(
                file_name="short.html",
                content=b"<html>x</html>",
                content_type="text/html",
            )

    short = analyse_pre_specification_documents(
        registry_no="R26BD00999999",
        source_digest="a" * 64,
        documents=[_analysis_document()],
        openai_api_key="fixture-key",
        openai_model="fixture-model",
        document_fetcher=_ShortDocumentFetcher(),
        openai_client_factory=_FakeOpenAIClient,
    )
    assert short.status == "REVIEW"
    assert short.warnings == ["PRESPEC_DOCUMENT_TEXT_EMPTY_OR_SHORT"]
    assert short.openai_calls == 0

    class _UnexpectedDocumentFetcher:
        def fetch(self, **_kwargs: object) -> FetchedPreSpecificationDocument:
            raise ValueError("must not escape the boundary")

    unexpected = analyse_pre_specification_documents(
        registry_no="R26BD00999999",
        source_digest="a" * 64,
        documents=[_analysis_document()],
        openai_api_key="fixture-key",
        openai_model="fixture-model",
        document_fetcher=_UnexpectedDocumentFetcher(),
        openai_client_factory=_FakeOpenAIClient,
    )
    assert unexpected.status == "REVIEW"
    assert unexpected.warnings == ["PRESPEC_ANALYSIS_INTERNAL_ERROR"]
    assert unexpected.openai_telemetry.accounting_complete is False


def test_prespec_document_fetcher_allows_only_the_document_contract() -> None:
    def valid_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''%EC%82%AC%EC%A0%84%EA%B7%9C%EA%B2%A9.html",
                "Content-Type": "text/html; charset=utf-8",
            },
            content=b"<html><body>fixture document</body></html>",
        )

    fetched = HttpPreSpecificationDocumentFetcher(
        transport=httpx.MockTransport(valid_handler)
    ).fetch(
        safe_url=_SAFE_URL,
        registry_no="R26BD00999999",
        slot=1,
    )
    assert fetched.file_name == "사전규격.html"
    assert fetched.content.startswith(b"<html>")

    def redirect_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/file.pdf"})

    with pytest.raises(
        PreSpecificationServiceError,
        match="PRESPEC_DOCUMENT_REDIRECT_UNSAFE",
    ):
        HttpPreSpecificationDocumentFetcher(
            transport=httpx.MockTransport(redirect_handler)
        ).fetch(
            safe_url=_SAFE_URL,
            registry_no="R26BD00999999",
            slot=1,
        )


@pytest.mark.parametrize(
    ("response", "max_bytes", "error_code"),
    [
        (httpx.Response(503, content=b"unavailable"), 100, "PRESPEC_DOCUMENT_HTTP_503"),
        (
            httpx.Response(
                200,
                headers={"Content-Length": "not-a-number", "Content-Type": "text/html"},
                content=b"fixture",
            ),
            100,
            "PRESPEC_DOCUMENT_LENGTH_INVALID",
        ),
        (
            httpx.Response(
                200,
                headers={"Content-Length": "101", "Content-Type": "text/html"},
                content=b"fixture",
            ),
            100,
            "PRESPEC_DOCUMENT_TOO_LARGE",
        ),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "application/octet-stream"},
                content=b"fixture",
            ),
            100,
            "PRESPEC_DOCUMENT_FILENAME_UNSAFE",
        ),
        (
            httpx.Response(
                200,
                headers={
                    "Content-Disposition": "attachment; filename=../../escape.pdf",
                    "Content-Type": "application/pdf",
                },
                content=b"fixture",
            ),
            100,
            "PRESPEC_DOCUMENT_FILENAME_UNSAFE",
        ),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"",
            ),
            100,
            "PRESPEC_DOCUMENT_EMPTY",
        ),
    ],
)
def test_prespec_document_fetcher_reports_bounded_failure_codes(
    response: httpx.Response,
    max_bytes: int,
    error_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response.request = request
        return response

    with pytest.raises(PreSpecificationServiceError, match=error_code):
        HttpPreSpecificationDocumentFetcher(
            transport=httpx.MockTransport(handler),
            max_bytes=max_bytes,
        ).fetch(
            safe_url=_SAFE_URL,
            registry_no="R26BD00999999",
            slot=1,
        )


def test_prespec_document_fetcher_rejects_url_redirect_limit_and_network() -> None:
    with pytest.raises(
        PreSpecificationServiceError,
        match="PRESPEC_DOCUMENT_URL_UNSAFE",
    ):
        HttpPreSpecificationDocumentFetcher().fetch(
            safe_url="https://evil.example/file.pdf",
            registry_no="R26BD00999999",
            slot=1,
        )

    def redirect_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": _SAFE_URL})

    with pytest.raises(
        PreSpecificationServiceError,
        match="PRESPEC_DOCUMENT_REDIRECT_LIMIT",
    ):
        HttpPreSpecificationDocumentFetcher(
            transport=httpx.MockTransport(redirect_handler),
            max_redirects=0,
        ).fetch(
            safe_url=_SAFE_URL,
            registry_no="R26BD00999999",
            slot=1,
        )

    def network_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture network failure", request=request)

    with pytest.raises(
        PreSpecificationServiceError,
        match="PRESPEC_DOCUMENT_NETWORK_ERROR",
    ):
        HttpPreSpecificationDocumentFetcher(
            transport=httpx.MockTransport(network_handler)
        ).fetch(
            safe_url=_SAFE_URL,
            registry_no="R26BD00999999",
            slot=1,
        )
