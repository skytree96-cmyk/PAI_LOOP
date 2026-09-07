from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import pai_loop.manual_analysis as manual
from pai_loop.models import IngestionJob
from test_manual_analysis import _app, _create_open_pps_notice, _review_batch, SAME_ORIGIN_HEADERS
from test_department_accounts import account_client, _login, _peer, NOTICE as ACCOUNT_NOTICE


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


def test_other_department_reattaches_while_original_callback_keeps_its_lease(account_client, monkeypatch):
    _, original_actor = _login(account_client)
    request_id = _reservation(account_client.app, notice=ACCOUNT_NOTICE)
    with account_client.app.state.session_factory() as session:
        job = session.get(IngestionJob, request_id)
        job.request_json = {**job.request_json, "account_id": original_actor["account"]["id"],
                            "department_id": original_actor["account"]["department_id"],
                            "recompute_current": False, "retry_reviewed": False}
        session.commit()
        original_request = deepcopy(job.request_json)
    other = _peer(account_client)
    headers, other_actor = _login(other, "SYN_KMA2")
    assert original_actor["account"]["id"] != other_actor["account"]["id"]
    entered, finish = threading.Event(), threading.Event()
    batches = []

    def running_batch(*args, **kwargs):
        batches.append((args, kwargs))
        entered.set()
        assert finish.wait(timeout=15)
        return _review_batch(job_id="SYN-original-child")

    monkeypatch.setattr(manual, "run_notice_analysis_batch", running_batch)
    worker_request = SimpleNamespace(app=account_client.app)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(manual._execute_reserved_manual_job, worker_request, request_id, ACCOUNT_NOTICE)
        try:
            assert entered.wait(timeout=10)
            assert manual._PUBLIC_MANUAL_PROCESS_LOCK.locked()
            for intent in ({"run_extraction": False, "recompute_current": True},
                           {"run_extraction": True, "retry_reviewed": True}):
                response = other.post(f"/api/v1/notices/{ACCOUNT_NOTICE}/analysis/request", headers=headers, json=intent)
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == request_id
                assert response.json()["notice_key"] == ACCOUNT_NOTICE
                assert response.json()["outcome"] == "QUEUED"
            assert manual._PUBLIC_MANUAL_PROCESS_LOCK.locked(), "status read must not release the worker lease"
            with account_client.app.state.session_factory() as session:
                assert session.get(IngestionJob, request_id).request_json == original_request
                assert len(list(session.scalars(select(IngestionJob)))) == 1
            assert len(batches) == 1
        finally:
            finish.set()
        worker.result(timeout=10)
    manual._execute_reserved_manual_job(worker_request, request_id, ACCOUNT_NOTICE)
    assert len(batches) == 1, "a delayed callback cannot rerun a terminal request"
    with account_client.app.state.session_factory() as session:
        job = session.get(IngestionJob, request_id)
        assert job.status == "COMPLETED"
        assert job.request_json["account_id"] == original_actor["account"]["id"]


def test_failed_retry_uses_new_reservation_only_after_existing_cooldown_and_quota(monkeypatch):
    app = _app(monkeypatch)
    callbacks = []
    monkeypatch.setattr(manual, "_execute_reserved_manual_job", lambda *args: callbacks.append(args))
    with TestClient(app) as client:
        _create_open_pps_notice(client, NOTICE)
        failed_id = _reservation(app)
        with app.state.session_factory() as session:
            job = session.get(IngestionJob, failed_id)
            job.status = "FAILED"
            session.commit()
            previous_request = deepcopy(job.request_json)
        failed = client.get(f"/api/v1/notices/{NOTICE}/analysis/requests/{failed_id}", headers=SAME_ORIGIN_HEADERS)
        assert failed.json()["outcome"] == "REVIEW"
        immediate = client.post(URL, headers=SAME_ORIGIN_HEADERS, json={"run_extraction": True, "retry_reviewed": True})
        assert immediate.status_code == 200 and immediate.json()["outcome"] == "COOLDOWN"
        assert callbacks == []
        with app.state.session_factory() as session:
            session.get(IngestionJob, failed_id).created_at = datetime.now(timezone.utc) - timedelta(minutes=6)
            session.commit()
        app.state.settings = replace(app.state.settings, public_manual_analysis_hourly_limit=1)
        assert client.post(URL, headers=SAME_ORIGIN_HEADERS, json={"run_extraction": True}).status_code == 429
        assert callbacks == []
        app.state.settings = replace(app.state.settings, public_manual_analysis_hourly_limit=12)
        retry = client.post(URL, headers=SAME_ORIGIN_HEADERS, json={"run_extraction": True})
        assert retry.status_code == 200, retry.text
        assert retry.json()["outcome"] == "QUEUED" and retry.json()["request_id"] != failed_id
        assert len(callbacks) == 1 and callbacks[0][1] == retry.json()["request_id"]
        with app.state.session_factory() as session:
            old = session.get(IngestionJob, failed_id)
            assert old.status == "FAILED" and old.request_json == previous_request
            assert len(list(session.scalars(select(IngestionJob)))) == 2
