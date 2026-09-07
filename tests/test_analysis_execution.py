from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError

from pai_loop import analysis_execution
from pai_loop.analysis_execution import (
    ANALYSIS_EXECUTION_GATE_KEY,
    AnalysisExecutionBusy,
    _LANE_KEYS,
    _notice_execution_key,
    postgres_analysis_execution_slot,
)


@pytest.fixture
def postgres_engine():
    raw = os.environ.get("PAI_LOOP_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("disposable PostgreSQL URL is not configured")
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("invalid disposable PostgreSQL test configuration", pytrace=False)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1", "postgres"}
        or not (url.database or "").startswith("pai_loop_test")
    ):
        pytest.fail("PostgreSQL execution tests require an explicitly local disposable test database")
    engine = create_engine(url, pool_size=8, max_overflow=0, pool_timeout=5)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        yield engine
    finally:
        engine.dispose()


def _start(pool, engine, *, key=None, lane=None):
    entered, release = Event(), Event()

    def work():
        with postgres_analysis_execution_slot(engine, notice_key=key, chunk_index=lane):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("synthetic execution was not released")

    return entered, release, pool.submit(work)


def _assert_waiting(engine, key: int) -> None:
    """Observe a real ungranted PostgreSQL advisory lock, not thread timing."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = CAST(:high AS oid) AND objid = CAST(:low AS oid) "
                "AND objsubid = 1 AND NOT granted"
            ), {"high": key >> 32, "low": key & 0xFFFFFFFF})
        if waiting:
            return
        Event().wait(0.02)
    pytest.fail("synthetic PostgreSQL waiter did not reach the expected lock")


def _finish(*calls):
    for _entered, release, _future in calls:
        release.set()
    for _entered, _release, future in calls:
        future.result(timeout=10)


def test_postgres_distinct_notice_lanes_execute_concurrently(postgres_engine):
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            _start(pool, postgres_engine, key="SYN-A-" + uuid.uuid4().hex, lane=0),
            _start(pool, postgres_engine, key="SYN-B-" + uuid.uuid4().hex, lane=1),
        ]
        try:
            assert all(entered.wait(5) for entered, _, _ in calls)
        finally:
            _finish(*calls)


def test_postgres_third_notice_waits_for_one_of_two_lanes(postgres_engine):
    with ThreadPoolExecutor(max_workers=3) as pool:
        calls = [
            _start(pool, postgres_engine, key="SYN-A-" + uuid.uuid4().hex, lane=0),
            _start(pool, postgres_engine, key="SYN-B-" + uuid.uuid4().hex, lane=1),
        ]
        try:
            assert all(entered.wait(5) for entered, _, _ in calls)
            third = _start(pool, postgres_engine, key="SYN-C-" + uuid.uuid4().hex, lane=2)
            calls.append(third)
            _assert_waiting(postgres_engine, _LANE_KEYS[0])
            assert not third[0].is_set()
            calls[0][1].set()
            assert third[0].wait(5)
        finally:
            _finish(*calls)


def test_postgres_same_notice_serializes_even_on_different_lanes(postgres_engine):
    key = "SYN-SAME-" + uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = _start(pool, postgres_engine, key=key, lane=0)
        calls = [first]
        try:
            assert first[0].wait(5)
            second = _start(pool, postgres_engine, key=key, lane=1)
            calls.append(second)
            _assert_waiting(postgres_engine, _notice_execution_key(key))
            assert not second[0].is_set()
            first[1].set()
            assert second[0].wait(5)
        finally:
            _finish(*calls)


@pytest.mark.parametrize("generic_first", [False, True])
def test_postgres_generic_exclusive_gate_never_overlaps_queue(postgres_engine, generic_first):
    queue_args = {"key": "SYN-QUEUE-" + uuid.uuid4().hex, "lane": 0}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = _start(pool, postgres_engine, **({} if generic_first else queue_args))
        calls = [first]
        try:
            assert first[0].wait(5)
            second = _start(pool, postgres_engine, **(queue_args if generic_first else {}))
            calls.append(second)
            _assert_waiting(postgres_engine, ANALYSIS_EXECUTION_GATE_KEY)
            assert not second[0].is_set()
            first[1].set()
            assert second[0].wait(5)
        finally:
            _finish(*calls)


def test_postgres_exception_releases_notice_lane_and_shared_gate(postgres_engine):
    key = "SYN-FAILURE-" + uuid.uuid4().hex
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with postgres_analysis_execution_slot(postgres_engine, notice_key=key, chunk_index=0):
            raise RuntimeError("synthetic failure")
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = _start(pool, postgres_engine, key=key, lane=0)
        try:
            assert call[0].wait(5)
        finally:
            _finish(call)
    with postgres_analysis_execution_slot(postgres_engine):
        pass


def test_postgres_acquisition_timeout_discards_partial_locks_and_allows_retry(postgres_engine, monkeypatch):
    monkeypatch.setattr(analysis_execution, "ANALYSIS_EXECUTION_ACQUIRE_SECONDS", 0.5)
    key = "SYN-TIMEOUT-" + uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = _start(pool, postgres_engine, key="SYN-HOLDER-" + uuid.uuid4().hex, lane=0)
        calls = [first]
        try:
            assert first[0].wait(5)

            def blocked_work():
                with postgres_analysis_execution_slot(postgres_engine, notice_key=key, chunk_index=0):
                    pytest.fail("timed-out acquisition must not execute work")

            blocked = pool.submit(blocked_work)
            _assert_waiting(postgres_engine, _LANE_KEYS[0])
            with pytest.raises(AnalysisExecutionBusy):
                blocked.result(timeout=5)
            # The timed-out session acquired this notice lock before waiting on
            # lane 0. A separate lane must now acquire the same notice safely.
            retry = _start(pool, postgres_engine, key=key, lane=1)
            calls.append(retry)
            assert retry[0].wait(5)
        finally:
            _finish(*calls)
    with postgres_analysis_execution_slot(postgres_engine):
        pass


class _Connection:
    def __init__(self, *, acquire_failure=None, unlock_failure=None, database_error=None):
        self.acquire_failure = acquire_failure
        self.unlock_failure = unlock_failure
        self.database_error = database_error
        self.acquired = []
        self.released = []
        self.timeouts = []
        self.invalidated = False
        self.closed = False
        self.isolation_level = None

    def execution_options(self, *, isolation_level):
        self.isolation_level = isolation_level
        return self

    def execute(self, statement, parameters):
        if "set_config" in str(statement):
            self.timeouts.append(parameters["timeout"])
            return
        self.acquired.append((str(statement), parameters["lock_key"]))
        if len(self.acquired) == self.acquire_failure:
            if self.database_error is not None:
                raise self.database_error
            raise RuntimeError("synthetic acquisition failure")

    def scalar(self, statement, parameters=None):
        if str(statement) == "SHOW lock_timeout":
            return "0"
        self.released.append((str(statement), parameters["lock_key"]))
        if self.unlock_failure == "raise":
            raise RuntimeError("synthetic unlock failure")
        return self.unlock_failure != "false"

    def invalidate(self):
        self.invalidated = True

    def close(self):
        self.closed = True


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


@pytest.mark.parametrize("step", [1, 2, 3])
def test_uncertain_acquisition_invalidates_connection_even_before_tracking_lock(step):
    connection = _Connection(acquire_failure=step)
    with pytest.raises(RuntimeError, match="synthetic acquisition failure"):
        with postgres_analysis_execution_slot(_Engine(connection), notice_key="SYN-FAIL", chunk_index=0):
            pytest.fail("failed acquisition must not execute work")
    assert connection.invalidated and connection.closed


@pytest.mark.parametrize("failure", ["raise", "false"])
def test_unconfirmed_unlock_discards_pooled_session(failure):
    connection = _Connection(unlock_failure=failure)
    with pytest.raises(RuntimeError):
        with postgres_analysis_execution_slot(_Engine(connection), notice_key="SYN-FAIL", chunk_index=0):
            pass
    assert connection.invalidated and connection.closed


def test_body_exception_unlocks_in_reverse_order_and_closes_connection():
    connection = _Connection()
    with pytest.raises(RuntimeError, match="synthetic body failure"):
        with postgres_analysis_execution_slot(_Engine(connection), notice_key="SYN-FAIL", chunk_index=1):
            raise RuntimeError("synthetic body failure")
    assert [key for _, key in connection.released] == [key for _, key in reversed(connection.acquired)]
    assert "unlock_shared" in connection.released[-1][0]
    assert connection.isolation_level == "AUTOCOMMIT"
    assert connection.timeouts[-1] == "0"
    assert connection.closed and not connection.invalidated


@pytest.mark.parametrize("state_attribute", ["sqlstate", "pgcode"])
def test_database_lock_timeout_is_busy_and_invalidates_connection(state_attribute):
    original = RuntimeError("synthetic lock timeout")
    setattr(original, state_attribute, "55P03")
    connection = _Connection(
        acquire_failure=2,
        database_error=DBAPIError("synthetic lock", {}, original, False),
    )
    with pytest.raises(AnalysisExecutionBusy):
        with postgres_analysis_execution_slot(_Engine(connection), notice_key="SYN-TIMEOUT", chunk_index=0):
            pytest.fail("timed-out acquisition must not execute work")
    assert connection.invalidated and connection.closed


def test_non_timeout_database_error_is_not_misreported_as_busy():
    error = DBAPIError("synthetic lock", {}, RuntimeError("synthetic connection error"), False)
    connection = _Connection(acquire_failure=1, database_error=error)
    with pytest.raises(DBAPIError) as captured:
        with postgres_analysis_execution_slot(_Engine(connection)):
            pytest.fail("failed acquisition must not execute work")
    assert captured.value is error
    assert connection.invalidated and connection.closed


def test_all_locks_share_one_acquisition_deadline_and_restore_previous_timeout(monkeypatch):
    clock = iter([0.0, 0.5, 1.5, 3.0])
    monkeypatch.setattr(analysis_execution.time, "monotonic", lambda: next(clock))
    connection = _Connection()
    with postgres_analysis_execution_slot(_Engine(connection), notice_key="SYN-BUDGET", chunk_index=0):
        assert connection.timeouts == ["9500ms", "8500ms", "7000ms", "0"]
    assert connection.closed and not connection.invalidated


def test_expired_overall_deadline_does_not_start_another_lock_request(monkeypatch):
    clock = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr(analysis_execution.time, "monotonic", lambda: next(clock))
    connection = _Connection()
    with pytest.raises(AnalysisExecutionBusy):
        with postgres_analysis_execution_slot(_Engine(connection), notice_key="SYN-BUDGET", chunk_index=0):
            pytest.fail("expired acquisition must not execute work")
    assert len(connection.acquired) == 1
    assert connection.invalidated and connection.closed


@pytest.mark.parametrize("leased", [False, True])
def test_api_routes_only_leased_single_notice_requests_to_queue_slots(monkeypatch, leased):
    from pai_loop import analysis_api

    payload = analysis_api.AnalysisBatchRequest(
        notice_keys=["SYN-ROUTING"],
        max_notices=1,
        **({"operation_id": "SYN-" + "1" * 32, "segment_id": "SYN-" + "2" * 32, "chunk_index": 3} if leased else {}),
    )
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))
    acquired = []

    @contextmanager
    def slot(actual_engine, **kwargs):
        assert actual_engine is engine
        acquired.append(kwargs)
        yield

    monkeypatch.setattr(analysis_api, "_ANALYSIS_RUNTIME_SAFETY_ENABLED", True)
    monkeypatch.setattr(analysis_api, "postgres_analysis_execution_slot", slot)
    result = object()
    wrapped = analysis_api._serialize_analysis_execution(lambda *_args, **_kwargs: result)
    assert wrapped(payload, request) is result
    assert acquired == [{"notice_key": "SYN-ROUTING" if leased else None, "chunk_index": 3 if leased else None}]


def test_api_busy_returns_retryable_503_without_starting_claim_or_provider_work(monkeypatch):
    from pai_loop import analysis_api

    payload = analysis_api.AnalysisBatchRequest(notice_keys=["SYN-BUSY"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        engine=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )))

    def busy(*_args, **_kwargs):
        raise AnalysisExecutionBusy("synthetic busy")

    def work(*_args, **_kwargs):
        pytest.fail("busy acquisition must not enter claim or provider work")

    monkeypatch.setattr(analysis_api, "_ANALYSIS_RUNTIME_SAFETY_ENABLED", True)
    monkeypatch.setattr(analysis_api, "postgres_analysis_execution_slot", busy)
    with pytest.raises(HTTPException) as captured:
        analysis_api._serialize_analysis_execution(work)(payload, request)
    assert captured.value.status_code == 503
    assert captured.value.headers == {"Retry-After": "15"}
