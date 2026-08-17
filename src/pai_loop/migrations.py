from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence

from sqlalchemy import Column, DateTime, MetaData, String, Table, func, inspect, select
from sqlalchemy.engine import Connection, Engine

from .database import Base, build_engine
from .models import (
    AnalysisRun,
    BidOutcome,
    RecommendationSnapshot,
    ReferenceDataVersion,
    RequirementResultSnapshot,
    ScoreSnapshot,
)


MIGRATION_ID = "20260817_01_analysis_persistence"
MIGRATION_CONTRACT = (
    "analysis_runs:v1;requirement_result_snapshots:v1;score_snapshots:v1;"
    "recommendation_snapshots:v1;reference_data_versions:v1;bid_outcomes:v1"
)
MIGRATION_CHECKSUM = hashlib.sha256(MIGRATION_CONTRACT.encode("utf-8")).hexdigest()

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
_required_base_tables = {
    "notices",
    "notice_versions",
    "evaluations",
    "user_decisions",
}


class MigrationError(RuntimeError):
    """Raised when an additive schema migration cannot be applied safely."""


def _applied_checksum(connection: Connection) -> str | None:
    return connection.execute(
        select(schema_migrations.c.checksum).where(
            schema_migrations.c.migration_id == MIGRATION_ID
        )
    ).scalar_one_or_none()


def pending_migrations(engine: Engine) -> list[str]:
    """Return pending migration IDs without creating or changing any table."""

    with engine.connect() as connection:
        if "schema_migrations" not in inspect(connection).get_table_names():
            return [MIGRATION_ID]
        applied = _applied_checksum(connection)
        if applied is None:
            return [MIGRATION_ID]
        if applied != MIGRATION_CHECKSUM:
            raise MigrationError(
                f"migration checksum mismatch for {MIGRATION_ID}; manual review is required"
            )
        return []


def apply_additive_migrations(engine: Engine) -> list[str]:
    """Create the v1 persistence tables and record an idempotent ledger row.

    This migration is intentionally additive: it never alters or drops an
    existing table. Existing application tables must already be present. A new
    installation should run ``Base.metadata.create_all`` first (the CLI exposes
    this as ``--create-base``).
    """

    with engine.begin() as connection:
        existing = set(inspect(connection).get_table_names())
        missing_base = sorted(_required_base_tables - existing)
        if missing_base:
            raise MigrationError(
                "base schema is missing required table(s): "
                + ", ".join(missing_base)
                + "; initialise a fresh database with --create-base"
            )

        schema_migrations.create(connection, checkfirst=True)
        applied = _applied_checksum(connection)
        if applied is not None:
            if applied != MIGRATION_CHECKSUM:
                raise MigrationError(
                    f"migration checksum mismatch for {MIGRATION_ID}; manual review is required"
                )
            return []

        for table in _migration_tables:
            table.create(connection, checkfirst=True)
        connection.execute(
            schema_migrations.insert().values(
                migration_id=MIGRATION_ID,
                checksum=MIGRATION_CHECKSUM,
            )
        )
    return [MIGRATION_ID]


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
