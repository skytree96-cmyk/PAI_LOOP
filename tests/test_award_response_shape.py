from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from conftest import login_department_reader
from pai_loop.integrations.awards import PpsAwardClient
from pai_loop.models import IngestionJob
from pai_loop.schemas import AwardShapeDiagnostics


CANARY = "SYN-shape-private-provider-content"


def envelope(body):
    return {"response": {"header": {"resultCode": "00", "resultMsg": CANARY}, "body": body}}


@pytest.mark.parametrize("body,kind,field,value", [
    ({"totalCount": 1}, "AWARD_PAGE_INVALID", "items_type", "MISSING"),
    ({"totalCount": 0.0, "items": []}, "AWARD_PAGE_INVALID", "total_count_type", "NUMBER"),
    ({"totalCount": " 0", "items": []}, "AWARD_PAGE_INVALID", "total_count_explicit_zero", False),
    ({"totalCount": None, "items": []}, "AWARD_PAGE_INVALID", "total_count_type", "NULL"),
    ({"totalCount": "", "items": []}, "AWARD_PAGE_INVALID", "total_count_type", "STRING"),
    ({"totalCount": CANARY, "items": []}, "INVALID_TOTAL_COUNT", "total_count_type", "STRING"),
    ({"items": []}, "MISSING_TOTAL_COUNT", "total_count_type", "MISSING"),
    ({"totalCount": 0, "items": {"item": None}}, "INVALID_ITEMS", "item_type", "NULL"),
    ({"totalCount": 0, "items": {"item": ""}}, "INVALID_ITEMS", "item_type", "STRING"),
    ({"totalCount": 2, "items": [{"private": CANARY}, CANARY]}, "AWARD_PAGE_INVALID", "object_rows", 1),
])
def test_failed_page_shape_preserves_existing_rejection_without_provider_values(body, kind, field, value):
    body[CANARY] = CANARY  # Unexpected field names and values never enter diagnostics.
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", max_retries=0,
                       transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope(body)))) as client:
        assert list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, 1), keyword="SYN",
                                       continue_on_window_error=True)) == []
        assert client.request_count == 1 and client.fallback_window_count == 0
        assert client.window_error_counts[0]["error_type"] == kind
        diagnostic = AwardShapeDiagnostics.model_validate(client.page_shape_diagnostics).model_dump()
        row = diagnostic["counts"][0]
        assert row["phase"] == "PRIMARY" and row["count"] == 1 and row["error_type"] == kind
        assert row["shape"][field] == value
        assert CANARY not in json.dumps(diagnostic)
        assert "example.test" not in json.dumps(diagnostic)


@pytest.mark.parametrize("items", [[], None, "", {}, {"item": []}])
@pytest.mark.parametrize("total", [0, "0"])
def test_successful_empty_shapes_remain_successful_without_diagnostics(items, total):
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", max_retries=0,
                       transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope({"items": items, "totalCount": total})))) as client:
        assert list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, 1), keyword="SYN")) == []
        assert client.request_count == 1 and not client.window_errors
        assert client.page_shape_diagnostics == {"counts": [], "suppressed_count": 0}


def test_shape_storage_is_bounded_and_resets_without_changing_fallback_calls():
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=envelope({"totalCount": calls, "items": [CANARY] * calls}))
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", max_retries=0,
                       transport=httpx.MockTransport(handler)) as client:
        assert list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 8, 1), keyword="SYN",
                                       continue_on_window_error=True)) == []
        diagnostic = AwardShapeDiagnostics.model_validate(client.page_shape_diagnostics).model_dump()
        assert len(diagnostic["counts"]) == 32
        assert diagnostic["suppressed_count"] > 0
        assert sum(row["count"] for row in diagnostic["counts"]) + diagnostic["suppressed_count"] == calls
        assert sum(row["count"] for row in client.window_error_counts) == calls == client.request_count
        assert {row["phase"] for row in diagnostic["counts"]} == {"PRIMARY", "FALLBACK"}
        before = calls
        list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, 1), keyword="SYN", continue_on_window_error=True))
        assert calls == before + 1
        assert len(client.page_shape_diagnostics["counts"]) == 1
        assert client.page_shape_diagnostics["suppressed_count"] == 0


def test_missing_envelope_and_large_rows_capture_types_and_capped_counts_only():
    payloads = iter(({CANARY: CANARY}, envelope({"totalCount": 1001, "items": [CANARY] * 1001})))
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", max_retries=0,
                       transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(payloads)))) as client:
        for expected in ("MISSING_RESPONSE", "AWARD_PAGE_INVALID"):
            list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, 1), keyword="SYN", continue_on_window_error=True))
            row = client.page_shape_diagnostics["counts"][0]
            assert row["error_type"] == expected
            if expected == "MISSING_RESPONSE":
                assert row["shape"]["response_type"] == "MISSING"
            else:
                assert row["shape"]["array_length"] == 1000
                assert row["shape"]["object_rows"] == 0
                assert row["shape"]["array_length_capped"] is True
            assert CANARY not in json.dumps(row)


def test_refresh_keeps_contract_and_ops_projection_is_read_only_server_only(client, monkeypatch):
    key = "PPS-SYN-AWARD-SHAPE"
    assert client.post("/api/v1/notices", json={"notice_key": key, "bid_notice_no": key,
        "title": "SYN 교육", "agency": "SYN 기관", "deadline": "2099-01-01T00:00:00Z"}).status_code == 201
    client.app.state.settings = replace(client.app.state.settings, pps_api_key="SYN-provider")
    calls = []
    class Provider(PpsAwardClient):
        def __init__(self, **kwargs):
            def handler(_request):
                calls.append(1)
                return httpx.Response(200, json=envelope({"totalCount": 1, CANARY: CANARY}))
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))
        def iter_awards(self, **kwargs):
            kwargs.update(start=date(2025, 1, 1), end=date(2025, 1, 1))
            return super().iter_awards(**kwargs)
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", Provider)
    response = client.post(f"/api/v1/notices/{key}/award-history/refresh", json={"keyword": "SYN", "years": 1})
    assert response.status_code == 200 and response.json()["status"] == "PARTIAL"
    assert "diagnostics" not in response.json() and "page_shape_diagnostics" not in response.json()
    job_id = response.json()["job_id"]
    path = f"/api/v1/ingestion/jobs/{job_id}/award-diagnostics"
    result = client.get(path)
    assert result.status_code == 200 and set(result.json()) == {"job_id", "diagnostics"}
    assert result.json()["diagnostics"]["counts"][0]["shape"]["items_type"] == "MISSING"
    assert len(calls) == 1 and CANARY not in result.text + response.text
    with client.app.state.session_factory() as session:
        job = session.get(IngestionJob, job_id)
        stored = job.request_json
        assert stored["award_page_shape_diagnostics"] == result.json()["diagnostics"]
        assert CANARY not in json.dumps(stored)
        count = len(list(session.scalars(select(IngestionJob))))
        # A legacy job has no shape evidence, not a measured zero-error result.
        job.request_json = {"unrelated_private_field": CANARY}
        session.commit()
    assert client.get(path).json()["diagnostics"] is None
    with client.app.state.session_factory() as session:
        job = session.get(IngestionJob, job_id)
        job.request_json = {**stored, "award_page_shape_diagnostics": {"counts": [], "suppressed_count": 0, "raw": CANARY}}
        session.commit()
    rejected = client.get(path)
    assert rejected.status_code == 409 and CANARY not in rejected.text
    with client.app.state.session_factory() as session:
        job = session.get(IngestionJob, job_id)
        job.request_json = [CANARY]
        session.commit()
    rejected = client.get(path)
    assert rejected.status_code == 409 and CANARY not in rejected.text
    with client.app.state.session_factory() as session:
        job = session.get(IngestionJob, job_id)
        job.source = "SYN-OTHER"
        session.commit()
    assert client.get(path).status_code == 404  # Other job request data is never projected.
    assert client.get("/api/v1/ingestion/jobs/SYN-absent/award-diagnostics").status_code == 404
    login_department_reader(client)
    assert client.get(path).status_code == 401  # Real active cookie is insufficient.
    client.cookies.clear()
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-PAI-Manual-Token": "SYN-retired"}).status_code == 401
    with client.app.state.session_factory() as session:
        assert len(list(session.scalars(select(IngestionJob)))) == count
    assert len(calls) == 1
