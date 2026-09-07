"""Real PostgreSQL account migrations and concurrent HTTP writes.

The existing CI PostgreSQL 16 service supplies PAI_LOOP_TEST_POSTGRES_URL.
No services are started here; unset local configuration skips integration cases.
Every database mutation is confined to a new, generated SYN schema.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Event

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from pai_loop.account_models import AccountAudit, AccountBootstrapPreview, AccountLoginBucket, AccountSession, DepartmentAccount
from pai_loop.accounts import CSRF_COOKIE, SESSION_COOKIE, departments, now_utc, password_hash
from pai_loop.config import Settings
from pai_loop.database import Base, build_session_factory
from pai_loop import migrations
from pai_loop.migrations import ACCOUNT_MIGRATION_ID, apply_additive_migrations, pending_migrations, schema_migrations
from pai_loop.models import AwardHistoryItem, BidOutcome, Evaluation, Notice, NoticeVersion, UserDecision
from pai_loop.operator_decisions import router as decisions_router
from pai_loop.result_learning import router as results_router
from pai_loop.outcome_write_lock import outcome_notice_lock_key


def _disposable_postgres_url() -> URL:
    raw = os.environ.get("PAI_LOOP_TEST_POSTGRES_URL")
    if not raw:
        if os.environ.get("CI", "").casefold() == "true":
            pytest.fail("CI must configure the disposable PostgreSQL account test database", pytrace=False)
        pytest.skip("disposable PostgreSQL URL is not configured")
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("invalid disposable PostgreSQL test configuration", pytrace=False)
    if (
        url.get_backend_name() != "postgresql"
        or url.host not in {"localhost", "127.0.0.1", "::1", "postgres"}
        or not (url.database or "").startswith("pai_loop_test")
        or url.query
        or any(os.environ.get(name) for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"))
    ):
        # Query/service/hostaddr overrides must not redirect a safe-looking URL.
        # Never include the supplied URL or environment values in this error.
        pytest.fail("account tests require an explicitly local disposable PostgreSQL database without connection overrides", pytrace=False)
    return url.set(drivername="postgresql+psycopg")


@pytest.fixture
def postgres_account_engine():
    url = _disposable_postgres_url()
    schema = "syn_accounts_" + uuid.uuid4().hex
    assert re.fullmatch(r"syn_accounts_[a-f0-9]{32}", schema)
    control = create_engine(url, hide_parameters=True, pool_size=2, max_overflow=0, pool_timeout=5)
    engine = None
    created = False
    try:
        with control.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        created = True
        engine = create_engine(
            url,
            hide_parameters=True,
            pool_size=8,
            max_overflow=0,
            pool_timeout=5,
            connect_args={"options": f"-csearch_path={schema} -cstatement_timeout=15000 -clock_timeout=10000 -cidle_in_transaction_session_timeout=20000"},
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            # Only this generated, validated schema is eligible for cleanup.
            assert re.fullmatch(r"syn_accounts_[a-f0-9]{32}", schema)
            with control.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        control.dispose()


@pytest.fixture
def postgres_account_app(postgres_account_engine):
    engine = postgres_account_engine
    Base.metadata.create_all(engine)
    apply_additive_migrations(engine)
    factory = build_session_factory(engine)
    app = FastAPI()
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.settings = Settings(department_accounts_enabled=True, public_read_only=True)
    app.include_router(decisions_router)
    app.include_router(results_router)
    notice_key = "SYN-PG-ACCOUNTS-" + uuid.uuid4().hex
    catalog = list(departments().items())[:2]
    authenticated = []
    now = now_utc()
    synthetic_hash = password_hash("SYN-PG-fixture-password-only")
    with factory() as session:
        session.add(Notice(notice_key=notice_key, bid_notice_no=notice_key, revision_no="00", title="SYN PostgreSQL department record", agency="SYN agency", deadline=now + timedelta(days=10), status="OPEN"))
        for index, (department_id, department_name) in enumerate(catalog):
            account = DepartmentAccount(username=f"SYN_KMA{index + 1}", role="DEPARTMENT", department_id=department_id, password_hash=synthetic_hash, active=True, paid_analysis_allowed=False, created_at=now)
            session.add(account)
            session.flush()
            token, csrf = "SYN-session-" + uuid.uuid4().hex, "SYN-csrf-" + uuid.uuid4().hex
            session.add(AccountSession(account_id=account.id, token_hash=hashlib.sha256(token.encode()).hexdigest(), csrf_hash=hashlib.sha256(csrf.encode()).hexdigest(), created_at=now, expires_at=now + timedelta(hours=1)))
            authenticated.append({"account_id": account.id, "department_id": department_id, "department_name": department_name, "token": token, "csrf": csrf})
        session.commit()
    return app, notice_key, authenticated


def _request(app, actor, method, path, payload=None):
    # Each concurrent request owns its client and request-scoped DB Session.
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, actor["token"])
        client.cookies.set(CSRF_COOKIE, actor["csrf"])
        return client.request(method, path, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin", "X-CSRF-Token": actor["csrf"]}, json=payload)


def _create_record(app, key, actor, kind, *, nonce="one", expected=None):
    if kind == "decision":
        return _request(app, actor, "POST", f"/api/v1/operator-decisions/notices/{key}", {"choice": "HOLD", "rationale": "SYN before analysis", "actor_label": "SYN forged actor", "expected_decision_id": expected})
    return _request(app, actor, "POST", "/api/v1/result-learning", {"notice_key": key, "status": "NO_BID", "idempotency_key": "SYN-request-" + nonce, "expected_outcome_id": expected})


def _department_lock_key(key, actor, kind):
    scope = f"{key}:{actor['department_id']}"
    if kind == "result":
        scope = "result:" + scope
    return int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)


@contextmanager
def _hold_department_lock(engine, key):
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            yield


def _assert_waiters(engine, key, count):
    """Wait for real PG lock waiters, avoiding a thread-scheduling-only race."""
    unsigned = key & ((1 << 64) - 1)
    until = time.monotonic() + 5
    while time.monotonic() < until:
        with engine.connect() as connection:
            waiting = connection.scalar(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND classid = CAST(:high AS oid) AND objid = CAST(:low AS oid) "
                "AND objsubid = 1 AND NOT granted"
            ), {"high": unsigned >> 32, "low": unsigned & 0xFFFFFFFF})
        if waiting == count:
            return
        Event().wait(0.02)
    pytest.fail("synthetic PostgreSQL department requests did not reach the expected lock")


def test_postgres_account_migration_preserves_unassigned_history_and_reapplies(postgres_account_engine):
    engine = postgres_account_engine
    Base.metadata.create_all(engine)
    apply_additive_migrations(engine)
    factory = build_session_factory(engine)
    public_department_name = next(iter(departments().values()))
    with factory() as session:
        notice = Notice(notice_key="SYN-PG-LEGACY", bid_notice_no="SYN-PG-LEGACY", revision_no="00", title="SYN preserved legacy", agency="SYN agency", status="OPEN", deadline=now_utc() + timedelta(days=1))
        session.add(notice)
        session.flush()
        decision = UserDecision(id="SYN-PG-LEGACY-DECISION", notice_id=notice.id, evaluation_id=None, choice="HOLD", actor_label=public_department_name, rationale="SYN original reason", analysis_state_snapshot="NOT_EVALUATED", analysis_snapshot={"synthetic": "preserve-original"})
        session.add(decision)
        session.flush()
        session.add(BidOutcome(id="SYN-PG-LEGACY-OUTCOME", notice_id=notice.id, decision_id=decision.id, outcome_key="SYN-legacy-outcome", status="NO_BID", source="MANUAL_UI", evidence_json={"operator_note": "SYN original outcome"}))
        session.commit()

    # Reconstruct the actual pre-account schema within this disposable schema.
    # No migration history or business row is deleted except this test's new
    # account migration entry; identity values are all null before removal.
    with engine.begin() as connection:
        connection.execute(schema_migrations.delete().where(schema_migrations.c.migration_id == ACCOUNT_MIGRATION_ID))
        for table in (AccountSession.__table__, AccountAudit.__table__, AccountLoginBucket.__table__, AccountBootstrapPreview.__table__, DepartmentAccount.__table__):
            table.drop(connection)
        for table_name in ("user_decisions", "bid_outcomes"):
            for column in ("account_id", "department_id", "department_name", "department_revision"):
                connection.exec_driver_sql(f'ALTER TABLE "{table_name}" DROP COLUMN "{column}"')
    assert pending_migrations(engine) == [ACCOUNT_MIGRATION_ID]
    assert apply_additive_migrations(engine) == [ACCOUNT_MIGRATION_ID]
    assert apply_additive_migrations(engine) == []
    assert pending_migrations(engine) == []
    with factory() as session:
        decision = session.get(UserDecision, "SYN-PG-LEGACY-DECISION")
        outcome = session.get(BidOutcome, "SYN-PG-LEGACY-OUTCOME")
        assert decision.actor_label == public_department_name
        assert decision.rationale == "SYN original reason"
        assert decision.analysis_snapshot == {"synthetic": "preserve-original"}
        assert decision.evaluation_id is None
        assert outcome.decision_id == decision.id
        assert outcome.evidence_json == {"operator_note": "SYN original outcome"}
        for row in (decision, outcome):
            assert (row.account_id, row.department_id, row.department_name, row.department_revision) == (None, None, None, None)
        assert session.scalar(select(func.count()).select_from(DepartmentAccount)) == 0
        assert session.scalar(select(func.count()).select_from(UserDecision)) == 1
        assert session.scalar(select(func.count()).select_from(BidOutcome)) == 1
    for table_name in ("user_decisions", "bid_outcomes"):
        columns = {column["name"]: column for column in inspect(engine).get_columns(table_name)}
        assert all(columns[name]["nullable"] for name in ("account_id", "department_id", "department_name", "department_revision"))
        assert any(index["unique"] and index["column_names"] == ["notice_id", "department_id", "department_revision"] for index in inspect(engine).get_indexes(table_name))


def test_postgres_concurrent_combined_legacy_migrations_preserve_rows_and_nullable_decisions(postgres_account_engine):
    engine = postgres_account_engine
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    now = now_utc()
    with factory() as session:
        notice = Notice(id="SYN-PG-COMBINED-N", notice_key="SYN-PG-COMBINED", bid_notice_no="SYN-PG-COMBINED", revision_no="00", title="SYN combined legacy", agency="SYN agency", status="OPEN", deadline=now + timedelta(days=1))
        session.add(notice)
        session.flush()
        version = NoticeVersion(id="SYN-PG-COMBINED-V", notice_id=notice.id, version_no=1, file_sha256="a" * 64)
        session.add(version)
        session.flush()
        evaluation = Evaluation(id="SYN-PG-COMBINED-E", notice_id=notice.id, notice_version_id=version.id, deadline_snapshot_at=now, eligibility="REVIEW", reason_code="R07", readiness_score=50, readiness_status="YELLOW", evidence_coverage=50, risk_band="HOLD", atomic_results=[], explanation={})
        session.add(evaluation)
        session.flush()
        decision = UserDecision(id="SYN-PG-COMBINED-D", notice_id=notice.id, evaluation_id=evaluation.id, choice="HOLD", actor_label="SYN legacy actor", rationale="SYN preserved reason")
        session.add(decision)
        session.flush()
        session.add(BidOutcome(id="SYN-PG-COMBINED-O", notice_id=notice.id, decision_id=decision.id, evaluation_id=evaluation.id, outcome_key="SYN-combined-outcome", status="NO_BID", source="MANUAL_UI"))
        session.add(AwardHistoryItem(id="SYN-PG-COMBINED-A", target_notice_id=notice.id, external_identity="SYN-combined-award", bid_notice_no="SYN-OLD-AWARD", revision_no="000", title="SYN old award", agency="SYN agency", winner_name="SYN winner", award_amount=123456, similarity_score=90, source="PPS"))
        session.commit()

    identity_columns = ("account_id", "department_id", "department_name", "department_revision")
    with engine.begin() as connection:
        for table in (AccountSession.__table__, AccountAudit.__table__, AccountLoginBucket.__table__, AccountBootstrapPreview.__table__, DepartmentAccount.__table__):
            table.drop(connection)
        for table_name in ("user_decisions", "bid_outcomes"):
            for column in identity_columns:
                connection.exec_driver_sql(f'ALTER TABLE "{table_name}" DROP COLUMN "{column}"')
        for column in ("analysis_state_snapshot", "analysis_snapshot"):
            connection.exec_driver_sql(f'ALTER TABLE user_decisions DROP COLUMN "{column}"')
        connection.exec_driver_sql('ALTER TABLE user_decisions ALTER COLUMN evaluation_id SET NOT NULL')
        for column in ("opening_results", "opening_results_status", "opening_results_read_at"):
            connection.exec_driver_sql(f'ALTER TABLE award_history_items DROP COLUMN "{column}"')
        original = {table: dict(connection.exec_driver_sql(f'SELECT * FROM "{table}"').mappings().one())
                    for table in ("user_decisions", "bid_outcomes", "award_history_items")}

    expected = {
        migrations.INDEPENDENT_DECISION_MIGRATION_ID: migrations.INDEPENDENT_DECISION_MIGRATION_CHECKSUM,
        migrations.AWARD_OPENING_RESULT_MIGRATION_ID: migrations.AWARD_OPENING_RESULT_MIGRATION_CHECKSUM,
        migrations.ACCOUNT_MIGRATION_ID: migrations.ACCOUNT_MIGRATION_CHECKSUM,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        with _hold_department_lock(engine, migrations._MIGRATION_ADVISORY_LOCK_KEY):
            futures = [pool.submit(apply_additive_migrations, engine) for _ in range(2)]
            _assert_waiters(engine, migrations._MIGRATION_ADVISORY_LOCK_KEY, 2)
        applied = [future.result(timeout=20) for future in futures]
    assert sum(bool(result) for result in applied) == 1
    assert [key for result in applied for key in result][-3:] == list(expected)
    assert apply_additive_migrations(engine) == [] and pending_migrations(engine) == []
    with engine.connect() as connection:
        ledger = dict(connection.execute(select(schema_migrations.c.migration_id, schema_migrations.c.checksum)).all())
        assert {key: ledger[key] for key in expected} == expected
        for table, previous in original.items():
            current = dict(connection.exec_driver_sql(f'SELECT * FROM "{table}"').mappings().one())
            assert {key: current[key] for key in previous} == previous
            assert all(value is None for key, value in current.items() if key not in previous)
    columns = {column["name"]: column for column in inspect(engine).get_columns("user_decisions")}
    assert columns["evaluation_id"]["nullable"]
    with factory() as session:
        session.add(UserDecision(id="SYN-PG-COMBINED-NEW", notice_id="SYN-PG-COMBINED-N", evaluation_id=None, choice="NO_GO", actor_label="SYN independent actor", rationale="SYN no evaluation needed"))
        session.commit()
        assert session.scalar(select(func.count()).select_from(UserDecision)) == 2


@pytest.mark.parametrize("kind", ["decision", "result"])
def test_postgres_same_department_expected_null_has_one_winner(postgres_account_app, kind):
    app, key, actors = postgres_account_app
    actor = actors[0]
    lock_key = _department_lock_key(key, actor, kind)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with _hold_department_lock(app.state.engine, lock_key):
            futures = [pool.submit(_create_record, app, key, actor, kind, nonce=str(index)) for index in range(2)]
            _assert_waiters(app.state.engine, lock_key, 2)
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [201, 409]
    saved = next(response.json() for response in responses if response.status_code == 201)
    saved = saved if kind == "decision" else saved["outcome"]
    assert saved["department_id"] == actor["department_id"]
    assert saved["account_id"] == actor["account_id"]
    assert saved["department_revision"] == 1
    if kind == "decision":
        assert saved["actor_label"] == actor["department_name"]
        assert saved["analysis_state_snapshot"] == "NOT_EVALUATED"
    with app.state.session_factory() as session:
        model = UserDecision if kind == "decision" else BidOutcome
        assert session.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.parametrize("kind", ["decision", "result"])
def test_postgres_other_department_writes_while_first_waits_and_cannot_overwrite(postgres_account_app, kind):
    app, key, actors = postgres_account_app
    first, second = actors
    lock_key = _department_lock_key(key, first, kind)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with _hold_department_lock(app.state.engine, lock_key):
            future = pool.submit(_create_record, app, key, first, kind)
            _assert_waiters(app.state.engine, lock_key, 1)
            other = _create_record(app, key, second, kind)
            assert other.status_code == 201, other.text
            assert not future.done()
        original = future.result(timeout=20)
    assert original.status_code == 201, original.text
    one, two = original.json(), other.json()
    if kind == "result":
        one, two = one["outcome"], two["outcome"]
        denied = _request(app, second, "PATCH", f"/api/v1/result-learning/{one['id']}", {"expected_updated_at": one["updated_at"], "operator_note": "SYN forbidden overwrite"})
        assert denied.status_code == 403
    else:
        denied = _create_record(app, key, second, kind, expected=one["id"])
        assert denied.status_code == 409
    assert one["id"] != two["id"]
    assert one["department_revision"] == two["department_revision"] == 1
    with app.state.session_factory() as session:
        model = UserDecision if kind == "decision" else BidOutcome
        rows = session.scalars(select(model)).all()
        assert {(row.id, row.department_id, row.account_id) for row in rows} == {(one["id"], first["department_id"], first["account_id"]), (two["id"], second["department_id"], second["account_id"])}


def test_postgres_same_result_update_compares_old_version_atomically(postgres_account_app):
    app, key, actors = postgres_account_app
    actor = actors[0]
    initial = _create_record(app, key, actor, "result")
    assert initial.status_code == 201, initial.text
    row = initial.json()["outcome"]
    lock_key = outcome_notice_lock_key(key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with _hold_department_lock(app.state.engine, lock_key):
            futures = [pool.submit(_request, app, actor, "PATCH", f"/api/v1/result-learning/{row['id']}", {"expected_updated_at": row["updated_at"], "operator_note": f"SYN concurrent update {index}"}) for index in range(2)]
            _assert_waiters(app.state.engine, lock_key, 2)
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()["outcome"] for response in responses if response.status_code == 200)
    assert winner["revision"] == 2
    with app.state.session_factory() as session:
        saved = session.get(BidOutcome, row["id"])
        assert saved.evidence_json["operator_note"] == winner["operator_note"]
        assert saved.department_id == actor["department_id"]
        assert session.scalar(select(func.count()).select_from(BidOutcome)) == 1


@pytest.fixture
def postgres_participation(postgres_account_app, monkeypatch):
    from pai_loop import outcome_feedback
    from pai_loop.integrations.outcome_feedback import PpsOutcomeFeedbackClient
    from pai_loop.outcomes_api import router as generic_outcomes_router

    app, original_key, actors = postgres_account_app
    key = "PPS-" + original_key
    app.state.settings = replace(app.state.settings, api_key="SYN-server-key", pps_api_key="SYN-provider-key")
    app.include_router(outcome_feedback.router)
    app.include_router(generic_outcomes_router)
    with app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == original_key))
        notice.notice_key = key
        notice.status = "CLOSED"
        notice.deadline = now_utc() - timedelta(days=1)
        notice.published_at = now_utc() - timedelta(days=2)
        session.commit()
    identity = {"bid_notice_no": original_key, "revision_no": "0", "classification_no": "1", "rebid_no": "2"}
    raw = {"bidNtceNo": original_key, "bidNtceOrd": "00", "bidClsfcNo": "1", "rbidNo": "2"}

    def provider(request):
        if request.url.path.endswith("getOpengResultListInfoOpengCompt"):
            rows = [{**raw, "prcbdrBizno": number, "prcbdrNm": "SYN participant", "bidprcAmt": "86130000"}
                    for number in ("0000000000", "0000000001")]
        else:
            assert request.url.path.endswith("getScsbidListSttusServcPPSSrch")
            rows = [{**raw, "bidwinnrBizno": "0000000001", "bidwinnrNm": "SYN winner", "sucsfbidAmt": "90000000", "prtcptCnum": "2"}]
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}, "body": {
            "items": rows, "totalCount": len(rows), "pageNo": 1, "numOfRows": 100}}})

    monkeypatch.setattr(outcome_feedback, "DEFAULT_COMPANY_BUSINESS_NUMBER", "0000000000")
    monkeypatch.setattr(outcome_feedback, "PpsOutcomeFeedbackClient", lambda **kwargs: PpsOutcomeFeedbackClient(**kwargs, transport=httpx.MockTransport(provider)))

    def refresh():
        with TestClient(app) as client:
            return client.post("/api/v1/outcome-feedback/pps/refresh", headers={"X-PAI-LOOP-API-KEY": "SYN-server-key"},
                               json={"notice_keys": [key], "include_participation": True})

    return app, key, actors[0], identity, refresh


def _participation_human_write(app, key, actor, identity, mutation, status, record_status):
    fields = {"status": status, "record_status": record_status, "opening_identity": identity, "source_reference": "SYN human correction", "submitted_bid_amount": None}
    if mutation == "generic":
        def generic_write():
            with TestClient(app) as client:
                return client.post(f"/api/v1/notices/{key}/outcomes", headers={"X-PAI-LOOP-API-KEY": "SYN-server-key"}, json={
                    "status": status, "source": "MANUAL", "outcome_key": "SYN-server-human-correction",
                    "evidence_json": {"opening_identity": identity, "_workflow": {"human_reviewed": True, "record_status": record_status}}})
        return generic_write
    if mutation == "create":
        return lambda: _request(app, actor, "POST", "/api/v1/result-learning", {
            **fields, "notice_key": key, "expected_outcome_id": None, "idempotency_key": "SYN-concurrent-correction"})
    initial = _request(app, actor, "POST", "/api/v1/result-learning", {
        **fields, "status": "SUBMITTED", "submitted_bid_amount": 86130000, "record_status": "VALIDATED", "notice_key": key,
        "expected_outcome_id": None, "idempotency_key": "SYN-before-correction"})
    assert initial.status_code == 201, initial.text
    row = initial.json()["outcome"]
    return lambda: _request(app, actor, "PATCH", f"/api/v1/result-learning/{row['id']}", {**fields, "expected_updated_at": row["updated_at"]})


@pytest.mark.parametrize("mutation", ["create", "patch", "generic"])
@pytest.mark.parametrize("status,record_status", [("NO_BID", "VALIDATED"), ("CANCELLED", "VALIDATED"), ("SUBMITTED", "DRAFT")])
def test_postgres_human_correction_blocks_provider_write_and_is_rechecked(postgres_participation, monkeypatch, mutation, status, record_status):
    from pai_loop import outcomes_api, result_learning

    app, key, actor, identity, refresh = postgres_participation
    human_write = _participation_human_write(app, key, actor, identity, mutation, status, record_status)
    human_read, release_human = Event(), Event()
    target = outcomes_api if mutation == "generic" else result_learning
    function = "_reject_reserved_generic_mutation" if mutation == "generic" else "_evidence"
    original = getattr(target, function)

    def pause_human(*args, **kwargs):
        human_read.set()
        assert release_human.wait(10), "synthetic human writer was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, function, pause_human)
    with ThreadPoolExecutor(max_workers=2) as pool:
        human = pool.submit(human_write)
        try:
            assert human_read.wait(5)
            provider = pool.submit(refresh)
            _assert_waiters(app.state.engine, outcome_notice_lock_key(key), 1)
            assert not provider.done()
        finally:
            release_human.set()
        assert human.result(timeout=20).status_code == (200 if mutation == "patch" else 201)
        automatic = provider.result(timeout=20)
    assert automatic.status_code == 200, automatic.text
    assert automatic.json()["items"][0]["reason_code"] == "HUMAN_PARTICIPATION_CONFLICT"
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BidOutcome).where(BidOutcome.source == "PPS_AUTO_FEEDBACK")) == 0
        saved = session.scalar(select(BidOutcome).where(BidOutcome.source != "PPS_AUTO_FEEDBACK"))
        assert saved.status == status and saved.evidence_json["_workflow"]["record_status"] == record_status
        assert saved.department_id == (None if mutation == "generic" else actor["department_id"])


@pytest.mark.parametrize("mutation", ["create", "patch", "generic"])
def test_postgres_human_write_cannot_commit_between_provider_check_and_insert(postgres_participation, monkeypatch, mutation):
    from pai_loop import outcome_feedback

    app, key, actor, identity, refresh = postgres_participation
    human_write = _participation_human_write(app, key, actor, identity, mutation, "NO_BID", "VALIDATED")
    provider_read, release_provider = Event(), Event()
    original = outcome_feedback._human_participation_conflict

    def pause_after_check(*args, **kwargs):
        conflict = original(*args, **kwargs)
        provider_read.set()
        assert release_provider.wait(10), "synthetic provider writer was not released"
        return conflict

    monkeypatch.setattr(outcome_feedback, "_human_participation_conflict", pause_after_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        provider = pool.submit(refresh)
        try:
            assert provider_read.wait(5)
            human = pool.submit(human_write)
            _assert_waiters(app.state.engine, outcome_notice_lock_key(key), 1)
            assert not human.done(), "a correction must not commit after the check but before the provider write"
        finally:
            release_provider.set()
        automatic, manual = provider.result(timeout=20), human.result(timeout=20)
    assert automatic.status_code == 200, automatic.text
    assert automatic.json()["items"][0]["result"] == "CREATED"
    assert manual.status_code == (200 if mutation == "patch" else 201), manual.text
    saved_response = manual.json() if mutation == "generic" else manual.json()["outcome"]
    assert saved_response["status"] == "NO_BID"
    assert refresh().json()["items"][0]["reason_code"] == "HUMAN_PARTICIPATION_CONFLICT"
    with app.state.session_factory() as session:
        saved = session.scalar(select(BidOutcome).where(BidOutcome.source != "PPS_AUTO_FEEDBACK"))
        automatic_row = session.scalar(select(BidOutcome).where(BidOutcome.source == "PPS_AUTO_FEEDBACK"))
        assert saved.department_id == (None if mutation == "generic" else actor["department_id"]) and saved.status == "NO_BID"
        assert automatic_row.department_id is None
        if mutation != "generic":
            human_written_at = saved.observed_at if mutation == "create" else saved.updated_at
            assert automatic_row.updated_at <= human_written_at


@pytest.mark.parametrize("host,database,query", [("production.invalid", "pai_loop_test", {}), ("localhost", "production", {}), ("localhost", "pai_loop_test", {"host": "production.invalid"})])
def test_postgres_account_guard_rejects_non_disposable_connections(monkeypatch, host, database, query):
    candidate = URL.create("postgresql+psycopg", host=host, database=database, query=query)
    monkeypatch.setenv("PAI_LOOP_TEST_POSTGRES_URL", candidate.render_as_string())
    with pytest.raises(pytest.fail.Exception, match="explicitly local disposable"):
        _disposable_postgres_url()


def test_postgres_account_gate_cannot_silently_skip_in_ci(monkeypatch):
    monkeypatch.delenv("PAI_LOOP_TEST_POSTGRES_URL", raising=False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="CI must configure"):
        _disposable_postgres_url()


def test_postgres_account_gate_skips_unconfigured_local_run(monkeypatch):
    monkeypatch.delenv("PAI_LOOP_TEST_POSTGRES_URL", raising=False)
    monkeypatch.setenv("CI", "false")
    with pytest.raises(pytest.skip.Exception, match="not configured"):
        _disposable_postgres_url()
