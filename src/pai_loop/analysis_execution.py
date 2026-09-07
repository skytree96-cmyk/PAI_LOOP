"""PostgreSQL execution arbitration for bounded, leased analysis chunks."""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


# Keep the legacy gate key: old workers and generic/manual requests take it
# exclusively, while leased single-notice work takes it shared.
ANALYSIS_EXECUTION_GATE_KEY = 0x50414945
_LANE_KEYS = (0x5041494530, 0x5041494531)
ANALYSIS_EXECUTION_ACQUIRE_SECONDS = 10.0


class AnalysisExecutionBusy(RuntimeError):
    """No execution slot was acquired before any claim or provider work began."""


def _notice_execution_key(notice_key: str) -> int:
    digest = hashlib.sha256(
        ("pai-loop:analysis-notice:" + notice_key).encode("utf-8")
    ).digest()
    # A separate high-bit namespace cannot collide with the gate or lane keys.
    return (0x6 << 60) | (int.from_bytes(digest[:8], "big") & ((1 << 60) - 1))


@contextmanager
def postgres_analysis_execution_slot(
    engine: Engine, *, notice_key: str | None = None, chunk_index: int | None = None,
) -> Iterator[None]:
    """Hold one dedicated connection until every acquired session lock is gone.

    Queue chunks share the legacy gate and serialize by notice and lane.
    Generic/manual requests use the legacy exclusive gate. Acquisition errors
    may mean the server acquired a lock but its response was lost; invalidating
    the connection is therefore required even before a lock enters ``held``.
    """
    if (notice_key is None) != (chunk_index is None):
        raise ValueError("notice and chunk index must be supplied together")
    if chunk_index is not None and (type(chunk_index) is not int or chunk_index < 0):
        raise ValueError("chunk index must be a nonnegative integer")
    queued = notice_key is not None
    locks = [("shared" if queued else "exclusive", ANALYSIS_EXECUTION_GATE_KEY)]
    if queued:
        locks.extend([
            ("exclusive", _notice_execution_key(notice_key)),
            ("exclusive", _LANE_KEYS[chunk_index % 2]),
        ])
    connection = engine.connect()
    held: list[tuple[str, int]] = []
    try:
        try:
            # Session locks survive commits; a long provider call must not leave
            # this dedicated connection idle inside a PostgreSQL transaction.
            connection = connection.execution_options(isolation_level="AUTOCOMMIT")
            prior_timeout = connection.scalar(text("SHOW lock_timeout"))
            acquisition_deadline = time.monotonic() + ANALYSIS_EXECUTION_ACQUIRE_SECONDS
            for mode, key in locks:
                remaining = acquisition_deadline - time.monotonic()
                if remaining <= 0:
                    raise AnalysisExecutionBusy("analysis execution slots are busy")
                connection.execute(
                    text("SELECT set_config('lock_timeout', :timeout, false)"),
                    {"timeout": f"{max(1, int(remaining * 1000))}ms"},
                )
                function = "pg_advisory_lock_shared" if mode == "shared" else "pg_advisory_lock"
                connection.execute(text(f"SELECT {function}(:lock_key)"), {"lock_key": key})
                held.append((mode, key))
            connection.execute(
                text("SELECT set_config('lock_timeout', :timeout, false)"),
                {"timeout": prior_timeout},
            )
        except DBAPIError as error:
            connection.invalidate()
            if (
                getattr(error.orig, "sqlstate", None) == "55P03"
                or getattr(error.orig, "pgcode", None) == "55P03"
            ):
                raise AnalysisExecutionBusy("analysis execution slots are busy") from None
            raise
        except BaseException:
            connection.invalidate()
            raise
        try:
            yield
        finally:
            try:
                for mode, key in reversed(held):
                    function = "pg_advisory_unlock_shared" if mode == "shared" else "pg_advisory_unlock"
                    released = connection.scalar(text(f"SELECT {function}(:lock_key)"), {"lock_key": key})
                    if released is not True:
                        raise RuntimeError("analysis execution lock release was not confirmed")
            except BaseException:
                # close() alone returns a pooled connection; session locks would
                # survive rollback. Invalidation discards its physical session.
                connection.invalidate()
                raise
    finally:
        connection.close()
