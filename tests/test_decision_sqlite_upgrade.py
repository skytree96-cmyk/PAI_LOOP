from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from pai_loop.database import Base, build_engine
from pai_loop import migrations
from pai_loop.models import BidOutcome, Evaluation, Notice, NoticeVersion


ACCOUNT_COLUMNS = ("account_id", "department_id", "department_name", "department_revision")
ACCOUNT_TABLES = ("account_sessions", "account_audit", "account_login_buckets", "account_bootstrap_previews", "department_accounts")
# Drop descendants first when constructing a database predating personal Teams.
TEAMS_TABLES = ("teams_follow_deliveries", "teams_follows", "teams_link_codes", "teams_session_links", "teams_recipients")
COMBINED_MIGRATIONS = (
    (migrations.INDEPENDENT_DECISION_MIGRATION_ID, migrations.INDEPENDENT_DECISION_MIGRATION_CHECKSUM),
    (migrations.AWARD_OPENING_RESULT_MIGRATION_ID, migrations.AWARD_OPENING_RESULT_MIGRATION_CHECKSUM),
    (migrations.ACCOUNT_MIGRATION_ID, migrations.ACCOUNT_MIGRATION_CHECKSUM),
    (migrations.AWARD_AGENCY_METADATA_MIGRATION_ID, migrations.AWARD_AGENCY_METADATA_MIGRATION_CHECKSUM),
    (migrations.TEAMS_FOLLOWUPS_MIGRATION_ID, migrations.TEAMS_FOLLOWUPS_MIGRATION_CHECKSUM),
)


def _legacy_database(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'SYN-legacy.db').as_posix()}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    with engine.begin() as connection:
        # Fresh empty test schema is replaced with the supported pre-upgrade
        # table, including an extension column, index, CHECK and insert trigger.
        connection.exec_driver_sql("DROP TABLE user_decisions")
        connection.exec_driver_sql("""CREATE TABLE user_decisions (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            notice_id VARCHAR(36) NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
            evaluation_id VARCHAR(36) NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
            choice VARCHAR(32) NOT NULL CHECK(choice IN ('GO','HOLD','NO_GO','CONDITIONAL_GO')),
            actor_label VARCHAR(120) NOT NULL, rationale TEXT NOT NULL, conditions JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            legacy_note TEXT DEFAULT 'SYN retained'
        )""")
        connection.exec_driver_sql("CREATE INDEX ix_user_decisions_evaluation_id ON user_decisions(evaluation_id)")
        connection.exec_driver_sql("CREATE INDEX SYN_held_decisions ON user_decisions(choice) WHERE choice='HOLD'")
        connection.exec_driver_sql("CREATE TABLE SYN_decision_audit (decision_id TEXT)")
        connection.exec_driver_sql("""CREATE TRIGGER SYN_decision_insert AFTER INSERT ON user_decisions
            BEGIN INSERT INTO SYN_decision_audit VALUES (NEW.id); END""")
        connection.execute(Notice.__table__.insert().values(
            id="SYN-notice", notice_key="SYN-LEGACY", bid_notice_no="SYN-LEGACY", revision_no="00",
            title="SYN legacy notice", agency="SYN agency", deadline=now, status="CLOSED",
        ))
        connection.execute(NoticeVersion.__table__.insert().values(
            id="SYN-version", notice_id="SYN-notice", version_no=1, file_sha256="a" * 64,
        ))
        connection.execute(Evaluation.__table__.insert().values(
            id="SYN-evaluation", notice_id="SYN-notice", notice_version_id="SYN-version",
            deadline_snapshot_at=now, eligibility="REVIEW", reason_code="R07", readiness_score=50,
            readiness_status="YELLOW", evidence_coverage=50, risk_band="HOLD", atomic_results=[], explanation={},
        ))
        connection.exec_driver_sql("""INSERT INTO user_decisions
            (id,notice_id,evaluation_id,choice,actor_label,rationale,conditions,created_at,legacy_note)
            VALUES ('SYN-decision','SYN-notice','SYN-evaluation','HOLD','SYN operator',
                    'SYN retained rationale','["SYN condition"]','2026-09-08 00:00:00','SYN extension')""")
        connection.execute(BidOutcome.__table__.insert().values(
            id="SYN-outcome", notice_id="SYN-notice", decision_id="SYN-decision",
            evaluation_id="SYN-evaluation", outcome_key="SYN-outcome", status="WON",
        ))
        original = tuple(connection.exec_driver_sql("SELECT * FROM user_decisions").one())
    return engine, original


def test_sqlite_upgrade_preserves_old_records_references_schema_objects_and_is_idempotent(tmp_path):
    engine, original = _legacy_database(tmp_path)
    try:
        assert migrations.INDEPENDENT_DECISION_MIGRATION_ID in migrations.apply_additive_migrations(engine)
        assert migrations.apply_additive_migrations(engine) == []
        assert migrations.pending_migrations(engine) == []
        columns = {item["name"]: item for item in inspect(engine).get_columns("user_decisions")}
        assert columns["evaluation_id"]["nullable"]
        assert columns["legacy_note"]["default"] == "'SYN retained'"
        indexes = {item["name"] for item in inspect(engine).get_indexes("user_decisions")}
        assert {"ix_user_decisions_evaluation_id", "SYN_held_decisions", "ix_user_decisions_analysis_state_snapshot"} <= indexes
        with engine.begin() as connection:
            row = tuple(connection.exec_driver_sql("SELECT * FROM user_decisions").one())
            assert row[:len(original)] == original
            assert list(columns)[len(original):] == ["analysis_state_snapshot", "analysis_snapshot", *ACCOUNT_COLUMNS]
            assert row[len(original):] == (None,) * 6
            assert connection.exec_driver_sql("SELECT decision_id FROM bid_outcomes").scalar_one() == "SYN-decision"
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM SYN_decision_audit").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            connection.exec_driver_sql("""INSERT INTO user_decisions
                (id,notice_id,evaluation_id,choice,actor_label,rationale)
                VALUES ('SYN-new','SYN-notice',NULL,'NO_GO','SYN operator','SYN independent reason')""")
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM SYN_decision_audit").scalar_one() == 2
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("""INSERT INTO user_decisions
                (id,notice_id,evaluation_id,choice,actor_label,rationale)
                VALUES ('SYN-invalid','SYN-missing',NULL,'GO','SYN operator','SYN invalid FK')""")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.exec_driver_sql("""INSERT INTO user_decisions
                (id,notice_id,evaluation_id,choice,actor_label,rationale)
                VALUES ('SYN-invalid-choice','SYN-notice',NULL,'INVALID','SYN operator','SYN invalid choice')""")
    finally:
        engine.dispose()


def test_sqlite_upgrade_rolls_back_copy_and_restores_foreign_keys_on_failure(tmp_path, monkeypatch):
    engine, original = _legacy_database(tmp_path)
    validate = migrations._validate_independent_decision_columns

    def fail_after_copy(columns, *, require_all):
        validate(columns, require_all=require_all)
        if require_all:
            raise migrations.MigrationError("SYN simulated post-copy failure")

    monkeypatch.setattr(migrations, "_validate_independent_decision_columns", fail_after_copy)
    try:
        with pytest.raises(migrations.MigrationError, match="SYN simulated"):
            migrations.apply_additive_migrations(engine)
        with engine.connect() as connection:
            assert tuple(connection.exec_driver_sql("SELECT * FROM user_decisions").one()) == original
            assert connection.exec_driver_sql("SELECT decision_id FROM bid_outcomes").scalar_one() == "SYN-decision"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        columns = {item["name"]: item for item in inspect(engine).get_columns("user_decisions")}
        assert not columns["evaluation_id"]["nullable"]
        assert "analysis_snapshot" not in columns
        assert "__pai_user_decisions_nullable" not in inspect(engine).get_table_names()
        assert migrations.INDEPENDENT_DECISION_MIGRATION_ID in migrations.pending_migrations(engine)
    finally:
        engine.dispose()


def _legacy_combined_database(tmp_path):
    engine, original_decision = _legacy_database(tmp_path)
    with engine.begin() as connection:
        for column in ("opening_results", "opening_results_status", "opening_results_read_at"):
            connection.exec_driver_sql(f'ALTER TABLE award_history_items DROP COLUMN "{column}"')
        connection.exec_driver_sql("DROP INDEX uq_outcome_notice_department_revision")
        for column in ACCOUNT_COLUMNS:
            connection.exec_driver_sql(f'ALTER TABLE bid_outcomes DROP COLUMN "{column}"')
        for table in (*TEAMS_TABLES, *ACCOUNT_TABLES):
            connection.exec_driver_sql(f'DROP TABLE "{table}"')
        connection.exec_driver_sql("""INSERT INTO award_history_items
            (id,target_notice_id,external_identity,bid_notice_no,revision_no,title,agency,
             winner_name,award_amount,similarity_score,source,created_at)
            VALUES ('SYN-award','SYN-notice','SYN-award|000|0|000','SYN-award','000',
                    'SYN old award','SYN agency','SYN winner',123456,90,'PPS',
                    '2026-09-08 00:00:00')""")
        original_award = dict(connection.exec_driver_sql("SELECT * FROM award_history_items").mappings().one())
        original_outcome = dict(connection.exec_driver_sql("SELECT * FROM bid_outcomes").mappings().one())
    return engine, original_decision, original_award, original_outcome


def _assert_combined_upgrade(engine, original_decision, original_award, original_outcome):
    assert migrations.apply_additive_migrations(engine) == []
    assert migrations.pending_migrations(engine) == []
    assert next(column for column in inspect(engine).get_columns("user_decisions")
                if column["name"] == "evaluation_id")["nullable"] is True
    for table in ("user_decisions", "bid_outcomes"):
        columns = {column["name"]: column for column in inspect(engine).get_columns(table)}
        assert all(columns[column]["nullable"] for column in ACCOUNT_COLUMNS)
        assert any(index["unique"] and index["column_names"] == ["notice_id", "department_id", "department_revision"]
                   for index in inspect(engine).get_indexes(table))
    with engine.connect() as connection:
        ledger = dict(connection.execute(select(migrations.schema_migrations.c.migration_id, migrations.schema_migrations.c.checksum)).all())
        assert {key: ledger[key] for key, _ in COMBINED_MIGRATIONS} == dict(COMBINED_MIGRATIONS)
        decision = tuple(connection.exec_driver_sql("SELECT * FROM user_decisions").one())
        assert decision[:len(original_decision)] == original_decision
        assert decision[len(original_decision):] == (None,) * 6
        outcome = dict(connection.exec_driver_sql("SELECT * FROM bid_outcomes").mappings().one())
        assert {key: outcome[key] for key in original_outcome} == original_outcome
        assert {key: value for key, value in outcome.items() if key not in original_outcome} == dict.fromkeys(ACCOUNT_COLUMNS)
        award = dict(connection.exec_driver_sql("SELECT * FROM award_history_items").mappings().one())
        assert {key: award[key] for key in original_award} == original_award
        assert {key: value for key, value in award.items() if key not in original_award} == {
            "opening_results": None, "opening_results_status": None, "opening_results_read_at": None,
        }
        assert all(connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar_one() == 0
                   for table in (*ACCOUNT_TABLES, *TEAMS_TABLES))
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM SYN_decision_audit").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_legacy_decisions_awards_and_accounts_upgrade_together_without_losing_rows(tmp_path):
    engine, original_decision, original_award, original_outcome = _legacy_combined_database(tmp_path)
    try:
        applied = migrations.apply_additive_migrations(engine)
        assert applied[-len(COMBINED_MIGRATIONS):] == [key for key, _ in COMBINED_MIGRATIONS]
        _assert_combined_upgrade(engine, original_decision, original_award, original_outcome)
    finally:
        engine.dispose()


def test_combined_legacy_upgrade_rolls_back_every_migration_after_account_failure(tmp_path, monkeypatch):
    engine, original_decision, original_award, original_outcome = _legacy_combined_database(tmp_path)
    account_upgrade = migrations._account_identity_columns

    def fail_after_accounts(connection, *, validate_only=False):
        account_upgrade(connection, validate_only=validate_only)
        if not validate_only:
            raise migrations.MigrationError("SYN final account migration failure")

    monkeypatch.setattr(migrations, "_account_identity_columns", fail_after_accounts)
    try:
        with pytest.raises(migrations.MigrationError, match="SYN final account"):
            migrations.apply_additive_migrations(engine)
        with engine.connect() as connection:
            assert tuple(connection.exec_driver_sql("SELECT * FROM user_decisions").one()) == original_decision
            assert dict(connection.exec_driver_sql("SELECT * FROM award_history_items").mappings().one()) == original_award
            assert dict(connection.exec_driver_sql("SELECT * FROM bid_outcomes").mappings().one()) == original_outcome
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM SYN_decision_audit").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        tables = inspect(engine).get_table_names()
        assert not set((*ACCOUNT_TABLES, *TEAMS_TABLES)) & set(tables)
        assert "schema_migrations" not in tables and "__pai_user_decisions_nullable" not in tables
        columns = {column["name"]: column for column in inspect(engine).get_columns("user_decisions")}
        assert not columns["evaluation_id"]["nullable"] and "analysis_snapshot" not in columns
        monkeypatch.setattr(migrations, "_account_identity_columns", account_upgrade)
        migrations.apply_additive_migrations(engine)
        _assert_combined_upgrade(engine, original_decision, original_award, original_outcome)
    finally:
        engine.dispose()


def test_concurrent_combined_legacy_upgrades_apply_each_migration_once(tmp_path):
    engine, original_decision, original_award, original_outcome = _legacy_combined_database(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            applied = list(pool.map(lambda _: migrations.apply_additive_migrations(engine), range(2)))
        assert sum(bool(result) for result in applied) == 1
        assert [key for result in applied for key in result][-len(COMBINED_MIGRATIONS):] == [key for key, _ in COMBINED_MIGRATIONS]
        _assert_combined_upgrade(engine, original_decision, original_award, original_outcome)
    finally:
        engine.dispose()
