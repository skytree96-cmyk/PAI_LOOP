from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence

from .database import Base, build_engine, build_session_factory
from .public_notice_seed import PublicNoticeSeedError, import_public_notice_seed
from .public_award_seed import import_public_award_seed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly upsert the packaged public procurement seed into the database "
            "identified by PAI_LOOP_DATABASE_URL. This command is never run at app startup."
        )
    )
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="Create the current SQLAlchemy schema before import (fresh demo DB only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get("PAI_LOOP_DATABASE_URL", "").strip()
    if not database_url:
        raise PublicNoticeSeedError("PAI_LOOP_DATABASE_URL is required")
    engine = build_engine(database_url)
    try:
        if args.create_schema:
            Base.metadata.create_all(engine)
        factory = build_session_factory(engine)
        with factory() as session:
            result = import_public_notice_seed(session)
            award_result = import_public_award_seed(session)
        report = {**result.aggregate_only(), **award_result.aggregate_only()}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
