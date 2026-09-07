from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import pai_loop.manual_analysis as manual
from pai_loop.models import IngestionJob
from test_manual_analysis import _app, _create_open_pps_notice, SAME_ORIGIN_HEADERS
from test_department_accounts import account_client, _login, NOTICE as ACCOUNT_NOTICE


NOTICE = "PPS-SYN-MANUAL-RESUME"
URL = f"/api/v1/notices/{NOTICE}/analysis/request"


def _reservation(app, *, age_minutes=0, notice=NOTICE, source="MANUAL_ANALYSIS"):
    with app.state.session_factory() as session:
        job = IngestionJob(
            id="SYN-reserved-request", source=source, mode="LIVE", status="RUNNING",
            notice_keys=[notice], window_json={},
            request_json={"account_id": "SYN-original-actor", "evaluation_only": False},
            created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        )
        session.add(job)
        session.commit()
        return job.id


@pytest.mark.parametrize("worker_busy,age_minutes", [(True, 0), (True, 20), (False, 6)])
def test_same_notice_reuses_running_reservation_without_new_callback(monkeypatch, worker_busy, age_minutes):
    app = _app(monkeypatch)
    callbacks = []
    monkeypatch.setattr(manual, "_execute_reserved_manual_job", lambda *args: callbacks.append(args))
    with TestClient(app) as client:
        _create_open_pps_notice(client, NOTICE)
        request_id = _reservation(app, age_minutes=age_minutes)
        if worker_busy:
            assert manual._PUBLIC_MANUAL_PROCESS_LOCK.acquire(blocking=False)
        try:
            response = client.post(URL, headers=SAME_ORIGIN_HEADERS, json={"run_extraction": True})
        finally:
            if worker_busy:
                manual._PUBLIC_MANUAL_PROCESS_LOCK.release()
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "QUEUED"
        assert response.json()["request_id"] == request_id
        assert callbacks == []
        with app.state.session_factory() as session:
            rows = list(session.scalars(select(IngestionJob)))
            assert len(rows) == 1
            assert rows[0].request_json["account_id"] == "SYN-original-actor"
            assert rows[0].status == "RUNNING"


def test_lost_request_id_recovers_only_idle_old_reservation_without_restarting_it(monkeypatch):
    app = _app(monkeypatch)
    calls = []
    monkeypatch.setattr(manual, "run_notice_analysis_batch", lambda *args: calls.append(args))
    with TestClient(app) as client:
        _create_open_pps_notice(client, NOTICE)
        request_id = _reservation(app, age_minutes=20)
        response = client.post(URL, headers=SAME_ORIGIN_HEADERS, json={"run_extraction": True})
        assert response.status_code == 200
        assert response.json()["outcome"] == "REVIEW"
        assert response.json()["request_id"] == request_id
        manual._execute_reserved_manual_job(SimpleNamespace(app=app), request_id, NOTICE)
        assert calls == []
        with app.state.session_factory() as session:
            rows = list(session.scalars(select(IngestionJob)))
            assert len(rows) == 1
            assert rows[0].status == "FAILED"
            assert "MANUAL_WORKER_INTERRUPTED" in rows[0].warnings


@pytest.mark.parametrize("notice,source", [("PPS-SYN-OTHER", "MANUAL_ANALYSIS"), (NOTICE, "MANUAL_PRESPEC_ANALYSIS")])
def test_busy_worker_never_reuses_a_different_request_scope(monkeypatch, notice, source):
    app = _app(monkeypatch)
    with TestClient(app) as client:
        _create_open_pps_notice(client, NOTICE)
        _reservation(app, notice=notice, source=source)
        assert manual._PUBLIC_MANUAL_PROCESS_LOCK.acquire(blocking=False)
        try:
            response = client.post(URL, headers=SAME_ORIGIN_HEADERS, json={"run_extraction": True})
        finally:
            manual._PUBLIC_MANUAL_PROCESS_LOCK.release()
        assert response.status_code == 409


def test_account_resume_preserves_original_actor_and_cannot_create_paid_work(account_client, monkeypatch):
    headers, me = _login(account_client)
    request_id = _reservation(account_client.app, notice=ACCOUNT_NOTICE)
    callbacks = []
    monkeypatch.setattr(manual, "_execute_reserved_manual_job", lambda *args: callbacks.append(args))
    url = f"/api/v1/notices/{ACCOUNT_NOTICE}/analysis/request"
    # A resume is an authenticated status read, not a new paid reservation.
    response = account_client.post(url, headers=headers, json={"run_extraction": True})
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == request_id
    assert callbacks == []
    with account_client.app.state.session_factory() as session:
        job = session.get(IngestionJob, request_id)
        assert job.request_json["account_id"] == "SYN-original-actor"
        assert job.request_json["account_id"] != me["account"]["id"]
        assert len(list(session.scalars(select(IngestionJob)))) == 1
        job.status = "COMPLETED"
        session.commit()
    monkeypatch.setattr(manual, "_source_kind", lambda notice: "PPS")
    denied = account_client.post(url, headers=headers, json={"run_extraction": True})
    assert denied.status_code == 403
    assert callbacks == []


def test_resume_requires_same_origin_and_valid_account_csrf(account_client):
    headers, _ = _login(account_client)
    request_id = _reservation(account_client.app, notice=ACCOUNT_NOTICE)
    url = f"/api/v1/notices/{ACCOUNT_NOTICE}/analysis/request"
    assert account_client.post(url, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert account_client.post(url, headers={"Origin": "http://testserver"}).status_code == 403
    with account_client.app.state.session_factory() as session:
        assert session.get(IngestionJob, request_id).status == "RUNNING"
