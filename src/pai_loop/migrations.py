from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
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
from .models import (
    AnalysisRun,
    BidOutcome,
    CompanyPerformanceRecord,
    NoticeAnalysisPolicy,
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
        PRESPEC_MIGRATION_ID,
        PRESPEC_MIGRATION_CHECKSUM,
        (
            PreSpecification.__table__,
            PreSpecificationVersion.__table__,
            PreSpecificationDocument.__table__,
            PreSpecificationAnalysisRun.__table__,
        ),
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


def _applied_checksum(connection: Connection, migration_id: str) -> str | None:
    return connection.execute(
        select(schema_migrations.c.checksum).where(
            schema_migrations.c.migration_id == migration_id
        )
    ).scalar_one_or_none()


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
        return pending


def apply_additive_migrations(engine: Engine) -> list[str]:
    """Apply additive tables/nullable columns and record idempotent ledger rows.

    Existing rows are never rewritten and tables/columns are never dropped.
    Existing application tables must already be present. A new installation
    should run ``Base.metadata.create_all`` first (the CLI exposes this as
    ``--create-base``).
    """

    with engine.begin() as connection:
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
                continue
            for table in tables:
                table.create(connection, checkfirst=True)
            if migration_id == COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID:
                _add_company_performance_recognized_amount_columns(connection)
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
