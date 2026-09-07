"""Serialize human corrections and provider outcome writes for one notice."""
from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session

from .accounts import serial_transaction


def outcome_notice_lock_key(notice_key: str) -> int:
    return int.from_bytes(hashlib.sha256(f"result-notice:{notice_key}".encode()).digest()[:8], "big", signed=True)


def lock_outcome_notice(session: Session, notice_key: str) -> None:
    # Department CREATE takes its existing CAS lock first, then this shared lock.
    # Other callers start clean; SQLite's BEGIN IMMEDIATE already covers all writers.
    if not session.in_transaction():
        serial_transaction(session, scope=f"result-notice:{notice_key}")
    elif session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": outcome_notice_lock_key(notice_key)})
