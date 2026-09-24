from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    JSON,
    MetaData,
    String,
    Table,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine

from .database import Base, build_engine
from .account_models import AccountAudit, AccountBootstrapPreview, AccountLoginBucket, AccountSession, DepartmentAccount
from .teams_identity_models import TeamsLinkCode, TeamsRecipient, TeamsSessionLink
from .followup_models import TeamsFollow, TeamsFollowDelivery
from .briefing_models import TeamsBriefingDelivery
from .models import (
    AnalysisRun,
    AwardHistoryItem,
    BidOutcome,
    CompanyPerformanceRecord,
    UserDecision,
    PerformanceNormalizationBatch,
    PerformanceNormalizationRevision,
    NoticeAnalysisPolicy,
    NoticeAwardAgencyMetadata,
    RecommendationSnapshot,
    ReferenceDataVersion,
    RequirementResultSnapshot,
    ScoreSnapshot,
)
from .prespec_models import (
    PreSpecification,
    PreSpecificationAnalysisRun,
    PreSpecificationDocument,
    PreSpecificationVersion,
)


MIGRATION_ID = "20260817_01_analysis_persistence"
MIGRATION_CONTRACT = (
    "analysis_runs:v1;requirement_result_snapshots:v1;score_snapshots:v1;"
    "recommendation_snapshots:v1;reference_data_versions:v1;bid_outcomes:v1"
)
MIGRATION_CHECKSUM = hashlib.sha256(MIGRATION_CONTRACT.encode("utf-8")).hexdigest()
NOTICE_ANALYSIS_POLICY_MIGRATION_ID = "20260823_02_notice_analysis_policy"
NOTICE_ANALYSIS_POLICY_MIGRATION_CONTRACT = "notice_analysis_policies:v1"
NOTICE_ANALYSIS_POLICY_MIGRATION_CHECKSUM = hashlib.sha256(
    NOTICE_ANALYSIS_POLICY_MIGRATION_CONTRACT.encode("utf-8")
).hexdigest()
COMPANY_PERFORMANCE_MIGRATION_ID = "20260823_03_company_performance_records"
COMPANY_PERFORMANCE_MIGRATION_CONTRACT = "company_performance_records:v1"
COMPANY_PERFORMANCE_MIGRATION_CHECKSUM = hashlib.sha256(
    COMPANY_PERFORMANCE_MIGRATION_CONTRACT.encode("utf-8")
).hexdigest()
COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID = (
    "20260902_01_company_performance_recognized_amount"
)
COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_CONTRACT = (
    "company_performance_records:gross_contract_amount_krw:bigint|null;"
    "recognized_performance_amount_krw:bigint|null;"
    "recognized_amount_is_net_of_share:boolean|null"
)
COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_CHECKSUM = hashlib.sha256(
    COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_CONTRACT.encode("utf-8")
).hexdigest()
PERFORMANCE_NORMALIZATION_MIGRATION_ID = "20260906_01_performance_normalization_revisions"
PERFORMANCE_NORMALIZATION_MIGRATION_CONTRACT = (
    "performance_normalization_batches:v1;performance_normalization_revisions:v1"
)
PERFORMANCE_NORMALIZATION_MIGRATION_CHECKSUM = hashlib.sha256(
    PERFORMANCE_NORMALIZATION_MIGRATION_CONTRACT.encode("utf-8")
).hexdigest()
INDEPENDENT_DECISION_MIGRATION_ID = "20260908_01_independent_operator_decisions"
INDEPENDENT_DECISION_MIGRATION_CONTRACT = (
    "user_decisions:evaluation_id:nullable;"
    "analysis_state_snapshot:varchar32|null;analysis_snapshot:json|null"
)
INDEPENDENT_DECISION_MIGRATION_CHECKSUM = hashlib.sha256(
    INDEPENDENT_DECISION_MIGRATION_CONTRACT.encode("utf-8")
).hexdigest()
AWARD_OPENING_RESULT_MIGRATION_ID = "20260908_01_award_opening_results"
AWARD_OPENING_RESULT_MIGRATION_CONTRACT = (
    "award_history_items:opening_results:json|null;"
    "opening_results_status:varchar32|null;opening_results_read_at:timestamptz|null"
)
AWARD_OPENING_RESULT_MIGRATION_CHECKSUM = hashlib.sha256(
    AWARD_OPENING_RESULT_MIGRATION_CONTRACT.encode("utf-8")
).hexdigest()
AWARD_AGENCY_METADATA_MIGRATION_ID = "20260913_01_notice_award_agency_metadata"
AWARD_AGENCY_METADATA_MIGRATION_CHECKSUM = hashlib.sha256(
    b"notice_award_agency_metadata:v1;append-only:exact-notice-revision-current-pps-version;"
    b"award_history_items:demand_agency_code:varchar500|null"
).hexdigest()
PRESPEC_MIGRATION_ID = "20260823_04_pre_specifications"
PRESPEC_MIGRATION_CONTRACT = (
    "pre_specifications:v1;pre_specification_versions:v1;"
    "pre_specification_documents:v1;pre_specification_analysis_runs:v1"
)
PRESPEC_MIGRATION_CHECKSUM = hashlib.sha256(
    PRESPEC_MIGRATION_CONTRACT.encode("utf-8")
).hexdigest()

_ledger_metadata = MetaData()
schema_migrations = Table(
    "schema_migrations",
    _ledger_metadata,
    Column("migration_id", String(120), primary_key=True),
    Column("checksum", String(64), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

_migration_tables = (
    AnalysisRun.__table__,
    RequirementResultSnapshot.__table__,
    ScoreSnapshot.__table__,
    RecommendationSnapshot.__table__,
    ReferenceDataVersion.__table__,
    BidOutcome.__table__,
)
ACCOUNT_MIGRATION_ID = "20260908_02_department_accounts"
ACCOUNT_MIGRATION_CHECKSUM = hashlib.sha256(b"department_accounts:v1;account_sessions:v1;account_login_buckets:v1;account_audit:v1;account_bootstrap_previews:v1;user_decisions+bid_outcomes:nullable-account_id-department_id-department_name-department_revision:unique-per-department-revision:v1").hexdigest()
_account_tables = (DepartmentAccount.__table__, AccountSession.__table__, AccountLoginBucket.__table__, AccountAudit.__table__, AccountBootstrapPreview.__table__)
TEAMS_FOLLOWUPS_MIGRATION_ID = "20260913_01_teams_personal_followups"
TEAMS_FOLLOWUPS_MIGRATION_CHECKSUM = hashlib.sha256(
    b"teams_recipients:v1;teams_session_links:v1;teams_link_codes:v1;teams_follows:v1;teams_follow_deliveries:v1"
).hexdigest()
TEAMS_BRIEFING_MIGRATION_ID = "20260924_01_teams_daily_briefing"
TEAMS_BRIEFING_MIGRATION_CHECKSUM = hashlib.sha256(
    b"teams_briefing_deliveries:v1;"
    b"teams_recipients:briefing_enabled:boolean|null-means-subscribed"
).hexdigest()
_migrations = (
    (MIGRATION_ID, MIGRATION_CHECKSUM, _migration_tables),
    (
        NOTICE_ANALYSIS_POLICY_MIGRATION_ID,
        NOTICE_ANALYSIS_POLICY_MIGRATION_CHECKSUM,
        (NoticeAnalysisPolicy.__table__,),
    ),
    (
        COMPANY_PERFORMANCE_MIGRATION_ID,
        COMPANY_PERFORMANCE_MIGRATION_CHECKSUM,
        (CompanyPerformanceRecord.__table__,),
    ),
    (
        COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID,
        COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_CHECKSUM,
        (),
    ),
    (
        PERFORMANCE_NORMALIZATION_MIGRATION_ID,
        PERFORMANCE_NORMALIZATION_MIGRATION_CHECKSUM,
        (PerformanceNormalizationBatch.__table__, PerformanceNormalizationRevision.__table__),
    ),
    (
        PRESPEC_MIGRATION_ID,
        PRESPEC_MIGRATION_CHECKSUM,
        (
            PreSpecification.__table__,
            PreSpecificationVersion.__table__,
            PreSpecificationDocument.__table__,
            PreSpecificationAnalysisRun.__table__,
        ),
    ),
    (
        INDEPENDENT_DECISION_MIGRATION_ID,
        INDEPENDENT_DECISION_MIGRATION_CHECKSUM,
        (),
    ),
    (
        AWARD_OPENING_RESULT_MIGRATION_ID,
        AWARD_OPENING_RESULT_MIGRATION_CHECKSUM,
        (),
    ),
    (ACCOUNT_MIGRATION_ID, ACCOUNT_MIGRATION_CHECKSUM, _account_tables),
    (
        AWARD_AGENCY_METADATA_MIGRATION_ID,
        AWARD_AGENCY_METADATA_MIGRATION_CHECKSUM,
        (NoticeAwardAgencyMetadata.__table__,),
    ),
    (TEAMS_FOLLOWUPS_MIGRATION_ID, TEAMS_FOLLOWUPS_MIGRATION_CHECKSUM, (
        TeamsRecipient.__table__, TeamsSessionLink.__table__, TeamsLinkCode.__table__,
        TeamsFollow.__table__, TeamsFollowDelivery.__table__,
    )),
    (
        TEAMS_BRIEFING_MIGRATION_ID,
        TEAMS_BRIEFING_MIGRATION_CHECKSUM,
        (TeamsBriefingDelivery.__table__,),
    ),
)
_required_base_tables = {
    "notices",
    "notice_versions",
    "evaluations",
    "user_decisions",
}
_MIGRATION_ADVISORY_LOCK_KEY = 0x5041494C  # "PAIL"


class MigrationError(RuntimeError):
    """Raised when an additive schema migration cannot be applied safely."""


_COMPANY_PERFORMANCE_RECOGNIZED_COLUMNS = {
    "gross_contract_amount_krw": ("BIGINT", BigInteger),
    "recognized_performance_amount_krw": ("BIGINT", BigInteger),
    "recognized_amount_is_net_of_share": ("BOOLEAN", Boolean),
}


def _validate_company_performance_recognized_amount_columns(
    columns: Sequence[dict[str, object]],
    *,
    require_all: bool,
) -> None:
    by_name = {str(column["name"]): column for column in columns}
    for column_name, (_sql_type, expected_type) in (
        _COMPANY_PERFORMANCE_RECOGNIZED_COLUMNS.items()
    ):
        column = by_name.get(column_name)
        if column is None:
            if require_all:
                raise MigrationError(
                    "company_performance_records migration did not create required "
                    f"column {column_name}"
                )
            continue
        actual_type = column.get("type")
        if not isinstance(actual_type, expected_type):
            raise MigrationError(
                "company_performance_records has incompatible existing column "
                f"{column_name}; expected {expected_type.__name__}"
            )
        if column.get("nullable") is not True:
            raise MigrationError(
                "company_performance_records has incompatible existing column "
                f"{column_name}; the additive column must be nullable"
            )


def _validate_applied_company_performance_recognized_amount_migration(
    connection: Connection,
) -> None:
    table_name = CompanyPerformanceRecord.__tablename__
    if table_name not in inspect(connection).get_table_names():
        raise MigrationError(
            "company_performance_records is missing although its recognized-amount "
            "migration is recorded as applied"
        )
    _validate_company_performance_recognized_amount_columns(
        inspect(connection).get_columns(table_name),
        require_all=True,
    )


def _add_company_performance_recognized_amount_columns(
    connection: Connection,
) -> None:
    """Add v2 amount columns without rewriting existing business rows."""

    table_name = CompanyPerformanceRecord.__tablename__
    if table_name not in inspect(connection).get_table_names():
        CompanyPerformanceRecord.__table__.create(connection, checkfirst=True)
    existing_columns = inspect(connection).get_columns(table_name)
    _validate_company_performance_recognized_amount_columns(
        existing_columns,
        require_all=False,
    )
    existing = {str(column["name"]) for column in existing_columns}
    for column_name, (sql_type, _expected_type) in (
        _COMPANY_PERFORMANCE_RECOGNIZED_COLUMNS.items()
    ):
        if column_name in existing:
            continue
        connection.exec_driver_sql(
            f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {sql_type}'
        )
        existing.add(column_name)
    _validate_company_performance_recognized_amount_columns(
        inspect(connection).get_columns(table_name),
        require_all=True,
    )


_INDEPENDENT_DECISION_COLUMNS = {
    "analysis_state_snapshot": ("VARCHAR(32)", String),
    "analysis_snapshot": ("JSON", JSON),
}


def _validate_independent_decision_columns(
    columns: Sequence[dict[str, object]],
    *,
    require_all: bool,
) -> None:
    by_name = {str(column["name"]): column for column in columns}
    for column_name, (_sql_type, expected_type) in _INDEPENDENT_DECISION_COLUMNS.items():
        column = by_name.get(column_name)
        if column is None:
            if require_all:
                raise MigrationError(
                    "user_decisions migration did not create required column "
                    f"{column_name}"
                )
            continue
        if not isinstance(column.get("type"), expected_type):
            raise MigrationError(
                "user_decisions has an incompatible existing column "
                f"{column_name}; expected {expected_type.__name__}"
            )
        if column.get("nullable") is not True:
            raise MigrationError(
                "user_decisions has an incompatible existing column "
                f"{column_name}; the additive column must be nullable"
            )
    evaluation_id = by_name.get("evaluation_id")
    if evaluation_id is None:
        raise MigrationError("user_decisions is missing its evaluation_id column")
    if require_all and evaluation_id.get("nullable") is not True:
        raise MigrationError(
            "user_decisions.evaluation_id is still NOT NULL; a decision recorded "
            "before analysis cannot be stored"
        )


def _validate_applied_independent_decision_migration(connection: Connection) -> None:
    table_name = UserDecision.__tablename__
    if table_name not in inspect(connection).get_table_names():
        raise MigrationError(
            "user_decisions is missing although its independent-decision "
            "migration is recorded as applied"
        )
    _validate_independent_decision_columns(
        inspect(connection).get_columns(table_name),
        require_all=True,
    )


def _relax_independent_decision_columns(connection: Connection) -> None:
    """Add the nullable snapshot columns and release the evaluation_id gate.

    PostgreSQL relaxes NOT NULL in place. SQLite copies the complete existing
    table inside the migration transaction, preserving data and schema objects.
    """

    table_name = UserDecision.__tablename__
    if table_name not in inspect(connection).get_table_names():
        UserDecision.__table__.create(connection, checkfirst=True)
    existing_columns = inspect(connection).get_columns(table_name)
    _validate_independent_decision_columns(existing_columns, require_all=False)
    existing = {str(column["name"]) for column in existing_columns}
    for column_name, (sql_type, _expected_type) in _INDEPENDENT_DECISION_COLUMNS.items():
        if column_name in existing:
            continue
        connection.exec_driver_sql(
            f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {sql_type}'
        )

    evaluation_id = next(
        column
        for column in inspect(connection).get_columns(table_name)
        if str(column["name"]) == "evaluation_id"
    )
    if evaluation_id.get("nullable") is not True:
        dialect = connection.dialect.name
        if dialect == "postgresql":
            connection.exec_driver_sql(
                f'ALTER TABLE "{table_name}" ALTER COLUMN "evaluation_id" DROP NOT NULL'
            )
        elif dialect == "sqlite":
            _rebuild_sqlite_decisions_with_nullable_evaluation(connection)
        else:
            raise MigrationError(
                f"{dialect} cannot relax user_decisions.evaluation_id in place; "
                "recreate this development database with --create-base"
            )
    _validate_independent_decision_columns(
        inspect(connection).get_columns(table_name),
        require_all=True,
    )
    for index in UserDecision.__table__.indexes:
        if index.name == "ix_user_decisions_analysis_state_snapshot":
            index.create(connection, checkfirst=True)


def _rebuild_sqlite_decisions_with_nullable_evaluation(connection: Connection) -> None:
    """Preserve the original DDL, all values, indexes, triggers and child rows."""
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one():
        raise MigrationError("SQLite decision rebuild requires migration-scoped foreign key suspension")
    original_sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_decisions'"
    ).scalar_one()
    # Historical supported schemas use VARCHAR(36); accept equivalent SQLite
    # text types/identifier quoting, and fail before copying on unknown DDL.
    relaxed_sql, changes = re.subn(
        r'((?:\(|,)\s*(?:"evaluation_id"|`evaluation_id`|\[evaluation_id\]|evaluation_id)'
        r'\s+(?:VARCHAR|CHAR|TEXT)(?:\s*\(\s*\d+\s*\))?\s+)NOT\s+NULL\b',
        r'\1', original_sql, flags=re.IGNORECASE,
    )
    if changes != 1:
        raise MigrationError("Cannot identify exactly one SQLite evaluation_id NOT NULL constraint")
    temporary_name = "__pai_user_decisions_nullable"
    if temporary_name in inspect(connection).get_table_names():
        raise MigrationError("SQLite decision migration temporary table already exists; preserve it for review")
    replacement_sql, changes = re.subn(
        r'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'(?:"user_decisions"|`user_decisions`|\[user_decisions\]|user_decisions)(?=\s*\()',
        f'CREATE TABLE "{temporary_name}"', relaxed_sql, count=1, flags=re.IGNORECASE,
    )
    if changes != 1:
        raise MigrationError("Cannot identify the SQLite user_decisions table declaration")
    schema_objects = list(connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE tbl_name='user_decisions' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type, name"
    ).scalars())
    quote = connection.dialect.identifier_preparer.quote
    columns = ", ".join(
        quote(row[1]) for row in connection.exec_driver_sql('PRAGMA table_xinfo("user_decisions")')
        if row[6] == 0  # Generated columns are recomputed by their unchanged DDL.
    )
    connection.exec_driver_sql(replacement_sql)
    connection.exec_driver_sql(
        f'INSERT INTO "{temporary_name}" ({columns}) SELECT {columns} FROM user_decisions'
    )
    count_before = connection.exec_driver_sql("SELECT COUNT(*) FROM user_decisions").scalar_one()
    count_after = connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{temporary_name}"').scalar_one()
    changed = connection.exec_driver_sql(
        f'SELECT {columns} FROM user_decisions EXCEPT SELECT {columns} FROM "{temporary_name}" LIMIT 1'
    ).first()
    if count_before != count_after or changed is not None:
        raise MigrationError("SQLite decision migration copy verification failed")
    connection.exec_driver_sql('DROP TABLE "user_decisions"')
    connection.exec_driver_sql(f'ALTER TABLE "{temporary_name}" RENAME TO "user_decisions"')
    for statement in schema_objects:
        connection.exec_driver_sql(statement)


@contextmanager
def _migration_transaction(engine: Engine) -> Iterator[Connection]:
    """Make SQLite DDL transactional and restore FK enforcement on every exit."""
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        foreign_keys = 0
        if sqlite:
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        try:
            with connection.begin():
                if sqlite:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield connection
                if sqlite and connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise MigrationError("SQLite migration foreign key verification failed")
        finally:
            if sqlite:
                try:
                    connection.exec_driver_sql(f"PRAGMA foreign_keys={int(foreign_keys)}")
                    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != foreign_keys:
                        raise MigrationError("SQLite migration could not restore foreign key enforcement")
                    connection.commit()
                except Exception:
                    connection.invalidate()
                    raise


_AWARD_OPENING_RESULT_COLUMNS = {
    "opening_results": ("JSON", JSON),
    "opening_results_status": ("VARCHAR(32)", String),
    "opening_results_read_at": ("TIMESTAMP WITH TIME ZONE", DateTime),
}


def _validate_award_opening_result_columns(
    columns: Sequence[dict[str, object]],
    *,
    require_all: bool,
) -> None:
    by_name = {str(column["name"]): column for column in columns}
    for column_name, (_sql_type, expected_type) in _AWARD_OPENING_RESULT_COLUMNS.items():
        column = by_name.get(column_name)
        if column is None:
            if require_all:
                raise MigrationError(
                    "award_history_items migration did not create required column "
                    f"{column_name}"
                )
            continue
        if not isinstance(column.get("type"), expected_type):
            raise MigrationError(
                "award_history_items has an incompatible existing column "
                f"{column_name}; expected {expected_type.__name__}"
            )
        if column.get("nullable") is not True:
            raise MigrationError(
                "award_history_items has an incompatible existing column "
                f"{column_name}; the additive column must be nullable"
            )


def _validate_applied_award_opening_result_migration(connection: Connection) -> None:
    table_name = AwardHistoryItem.__tablename__
    if table_name not in inspect(connection).get_table_names():
        raise MigrationError(
            "award_history_items is missing although its opening-result "
            "migration is recorded as applied"
        )
    _validate_award_opening_result_columns(
        inspect(connection).get_columns(table_name),
        require_all=True,
    )


def _add_award_opening_result_columns(connection: Connection) -> None:
    """Add nullable opening-result columns without touching existing awards.

    Every existing row keeps NULL, which the API reports as "not collected"
    rather than as an empty competitor set or a zero score.
    """

    table_name = AwardHistoryItem.__tablename__
    if table_name not in inspect(connection).get_table_names():
        AwardHistoryItem.__table__.create(connection, checkfirst=True)
    existing_columns = inspect(connection).get_columns(table_name)
    _validate_award_opening_result_columns(existing_columns, require_all=False)
    existing = {str(column["name"]) for column in existing_columns}
    for column_name, (sql_type, _expected_type) in _AWARD_OPENING_RESULT_COLUMNS.items():
        if column_name in existing:
            continue
        if column_name == "opening_results_read_at" and connection.dialect.name != "postgresql":
            sql_type = "DATETIME"
        connection.exec_driver_sql(
            f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {sql_type}'
        )
    _validate_award_opening_result_columns(
        inspect(connection).get_columns(table_name),
        require_all=True,
    )


def _applied_checksum(connection: Connection, migration_id: str) -> str | None:
    return connection.execute(
        select(schema_migrations.c.checksum).where(
            schema_migrations.c.migration_id == migration_id
        )
    ).scalar_one_or_none()


def _account_identity_columns(connection: Connection, *, validate_only: bool = False) -> None:
    for table in _account_tables:
        if table.name not in inspect(connection).get_table_names():
            raise MigrationError("department account table is missing")
        physical = {row["name"]: row for row in inspect(connection).get_columns(table.name)}
        for column in table.columns:
            actual = physical.get(column.name)
            if actual is None or actual["nullable"] != column.nullable or actual["type"]._type_affinity != column.type._type_affinity:
                raise MigrationError("department account table has an incompatible column")
    for table_name in ("user_decisions", "bid_outcomes"):
        if table_name not in inspect(connection).get_table_names():
            raise MigrationError("department record table is missing")
        columns = {row["name"]: row for row in inspect(connection).get_columns(table_name)}
        for name, length in (("account_id", 36), ("department_id", 120), ("department_name", 120), ("department_revision", None)):
            if name not in columns:
                if validate_only:
                    raise MigrationError("department record identity column is missing")
                column_type = f"VARCHAR({length})" if length else "INTEGER"
                connection.exec_driver_sql(f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {column_type} NULL')
            elif not columns[name]["nullable"]:
                raise MigrationError("legacy department ownership must remain nullable")
    if not validate_only:
        connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_notice_department_revision ON user_decisions (notice_id, department_id, department_revision)")
        connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_outcome_notice_department_revision ON bid_outcomes (notice_id, department_id, department_revision)")
    for table_name, index_name in (("user_decisions", "uq_decision_notice_department_revision"), ("bid_outcomes", "uq_outcome_notice_department_revision")):
        index = next((row for row in inspect(connection).get_indexes(table_name) if row["name"] == index_name), None)
        if not index or not index["unique"] or index["column_names"] != ["notice_id", "department_id", "department_revision"]:
            raise MigrationError("department revision uniqueness is missing")


def _award_agency_metadata_columns(connection: Connection, *, validate_only: bool = False) -> None:
    table_name = AwardHistoryItem.__tablename__
    if table_name not in inspect(connection).get_table_names():
        raise MigrationError("award_history_items is missing for demand-agency migration")
    columns = {row["name"]: row for row in inspect(connection).get_columns(table_name)}
    column = columns.get("demand_agency_code")
    if column is None:
        if validate_only:
            raise MigrationError("award demand_agency_code is missing")
        connection.exec_driver_sql(
            'ALTER TABLE "award_history_items" ADD COLUMN "demand_agency_code" VARCHAR(500) NULL'
        )
    elif not isinstance(column["type"], String) or column["nullable"] is not True:
        raise MigrationError("award demand_agency_code must be a nullable string")
    if NoticeAwardAgencyMetadata.__tablename__ not in inspect(connection).get_table_names():
        raise MigrationError("notice_award_agency_metadata is missing")


def _teams_briefing_columns(connection: Connection, *, validate_only: bool = False) -> None:
    """Add the per-person briefing opt-out.

    The column must stay nullable: an existing recipient has no stored choice,
    and NULL is read as subscribed so nobody silently stops receiving the
    briefing when this migration lands.
    """

    table_name = TeamsRecipient.__tablename__
    if table_name not in inspect(connection).get_table_names():
        raise MigrationError("teams_recipients is missing for the briefing migration")
    columns = {row["name"]: row for row in inspect(connection).get_columns(table_name)}
    column = columns.get("briefing_enabled")
    if column is None:
        if validate_only:
            raise MigrationError("teams_recipients briefing_enabled is missing")
        connection.exec_driver_sql(
            'ALTER TABLE "teams_recipients" ADD COLUMN "briefing_enabled" BOOLEAN NULL'
        )
    elif column["nullable"] is not True:
        raise MigrationError("teams_recipients briefing_enabled must be nullable")


def pending_migrations(engine: Engine) -> list[str]:
    """Return pending migration IDs without creating or changing any table."""

    with engine.connect() as connection:
        if "schema_migrations" not in inspect(connection).get_table_names():
            return [migration_id for migration_id, _checksum, _tables in _migrations]
        pending: list[str] = []
        for migration_id, expected_checksum, _tables in _migrations:
            applied = _applied_checksum(connection, migration_id)
            if applied is None:
                pending.append(migration_id)
                continue
            if applied != expected_checksum:
                raise MigrationError(
                    f"migration checksum mismatch for {migration_id}; manual review is required"
                )
            if migration_id == COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID:
                _validate_applied_company_performance_recognized_amount_migration(
                    connection
                )
            if migration_id == INDEPENDENT_DECISION_MIGRATION_ID:
                _validate_applied_independent_decision_migration(connection)
            if migration_id == AWARD_OPENING_RESULT_MIGRATION_ID:
                _validate_applied_award_opening_result_migration(connection)
            if migration_id == ACCOUNT_MIGRATION_ID:
                _account_identity_columns(connection, validate_only=True)
            if migration_id == AWARD_AGENCY_METADATA_MIGRATION_ID:
                _award_agency_metadata_columns(connection, validate_only=True)
        return pending


def apply_additive_migrations(engine: Engine) -> list[str]:
    """Apply additive tables/nullable columns and record idempotent ledger rows.

    Existing records and relationships are preserved. SQLite's nullable
    evaluation upgrade rebuilds only user_decisions in an atomic transaction.
    Existing application tables must already be present. A new installation
    should run ``Base.metadata.create_all`` first (the CLI exposes this as
    ``--create-base``).
    """

    with _migration_transaction(engine) as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _MIGRATION_ADVISORY_LOCK_KEY},
            )
        existing = set(inspect(connection).get_table_names())
        missing_base = sorted(_required_base_tables - existing)
        if missing_base:
            raise MigrationError(
                "base schema is missing required table(s): "
                + ", ".join(missing_base)
                + "; initialise a fresh database with --create-base"
            )

        schema_migrations.create(connection, checkfirst=True)
        newly_applied: list[str] = []
        for migration_id, expected_checksum, tables in _migrations:
            applied = _applied_checksum(connection, migration_id)
            if applied is not None:
                if applied != expected_checksum:
                    raise MigrationError(
                        f"migration checksum mismatch for {migration_id}; manual review is required"
                    )
                if migration_id == COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID:
                    _validate_applied_company_performance_recognized_amount_migration(
                        connection
                    )
                if migration_id == INDEPENDENT_DECISION_MIGRATION_ID:
                    _validate_applied_independent_decision_migration(connection)
                if migration_id == AWARD_OPENING_RESULT_MIGRATION_ID:
                    _validate_applied_award_opening_result_migration(connection)
                if migration_id == ACCOUNT_MIGRATION_ID:
                    _account_identity_columns(connection, validate_only=True)
                if migration_id == AWARD_AGENCY_METADATA_MIGRATION_ID:
                    _award_agency_metadata_columns(connection, validate_only=True)
                if migration_id == TEAMS_BRIEFING_MIGRATION_ID:
                    _teams_briefing_columns(connection, validate_only=True)
                continue
            for table in tables:
                table.create(connection, checkfirst=True)
            if migration_id == COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID:
                _add_company_performance_recognized_amount_columns(connection)
            if migration_id == INDEPENDENT_DECISION_MIGRATION_ID:
                _relax_independent_decision_columns(connection)
            if migration_id == AWARD_OPENING_RESULT_MIGRATION_ID:
                _add_award_opening_result_columns(connection)
            if migration_id == ACCOUNT_MIGRATION_ID:
                _account_identity_columns(connection)
            if migration_id == AWARD_AGENCY_METADATA_MIGRATION_ID:
                _award_agency_metadata_columns(connection)
            if migration_id == TEAMS_BRIEFING_MIGRATION_ID:
                _teams_briefing_columns(connection)
            connection.execute(
                schema_migrations.insert().values(
                    migration_id=migration_id,
                    checksum=expected_checksum,
                )
            )
            newly_applied.append(migration_id)
    return newly_applied


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply PAI LOOP additive database migrations using PAI_LOOP_DATABASE_URL."
    )
    parser.add_argument(
        "--create-base",
        action="store_true",
        help="Create the full SQLAlchemy schema first; use only for a fresh database.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report pending migrations without changing the database.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get("PAI_LOOP_DATABASE_URL", "").strip()
    if not database_url:
        raise MigrationError("PAI_LOOP_DATABASE_URL is required")

    engine = build_engine(database_url)
    try:
        if args.check:
            pending = pending_migrations(engine)
            print(json.dumps({"pending": pending}, sort_keys=True))
            return int(bool(pending))
        if args.create_base:
            Base.metadata.create_all(engine)
        applied = apply_additive_migrations(engine)
        print(
            json.dumps(
                {"applied": applied, "migration": MIGRATION_ID, "status": "ok"},
                sort_keys=True,
            )
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
