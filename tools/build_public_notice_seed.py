from __future__ import annotations

import argparse
import json
from pathlib import Path

from pai_loop.public_notice_seed import (
    PublicNoticeSeedError,
    build_public_notice_seed_from_db,
    write_public_notice_seed,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the bounded, publication-safe Incheon procurement seed from an "
            "ignored SQLite database opened read-only. Only aggregate diagnostics "
            "are printed; notice text and evidence quotes are never printed."
        )
    )
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    source = args.source_db.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if source == output:
        raise PublicNoticeSeedError("output cannot overwrite the source database")
    result = build_public_notice_seed_from_db(source)
    write_public_notice_seed(result, output)
    print(json.dumps(result.aggregate_only(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
