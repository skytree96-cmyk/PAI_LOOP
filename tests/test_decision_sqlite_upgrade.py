from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from pai_loop.database import Base, build_engine
from pai_loop import migrations
from pai_loop.models import BidOutcome, Evaluation, Notice, NoticeVersion


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
            assert row[len(original):] == (None, None)
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
