"""An analysis lease nobody touched within its TTL no longer holds its notices."""
from datetime import datetime, timedelta, timezone

import pytest

from pai_loop.analysis_api import _reserved_backfill_keys
from pai_loop.models import IngestionJob

NOW = datetime(2030, 1, 10, 12, tzinfo=timezone.utc)


def _parent(session, key, *, created, status="PARTIAL"):
    parent = IngestionJob(source="ANALYSIS_BACKFILL", mode="LIVE", status=status,
                          window_json={"scope": "OPEN_NOT_SELECTED"}, request_json={"queue_name": "BACKFILL"},
                          matched=1, notice_keys=[key], warnings=[], created_at=created)
    session.add(parent)
    session.flush()
    return parent


@pytest.mark.parametrize(("age_hours", "reserved"), [(1, True), (5, True), (7, False), (24 * 12, False)])
def test_idle_parent_releases_its_notices_after_the_ttl(client, age_hours, reserved):
    with client.app.state.session_factory() as session:
        _parent(session, "SYN-IDLE-KEY", created=NOW - timedelta(hours=age_hours))
        session.commit()
        keys = _reserved_backfill_keys(session, now=NOW, ttl_hours=6)
    assert ("SYN-IDLE-KEY" in keys) is reserved


def test_recent_child_activity_keeps_an_old_parent_reserved(client):
    with client.app.state.session_factory() as session:
        parent = _parent(session, "SYN-BUSY-KEY", created=NOW - timedelta(days=3))
        session.add(IngestionJob(source="ANALYSIS", mode="LIVE", status="RUNNING", window_json={"scope": "NOTICE_KEYS"},
                                 request_json={"parent_job_id": parent.id, "chunk_index": 0},
                                 matched=1, notice_keys=["SYN-BUSY-KEY"], warnings=[],
                                 created_at=NOW - timedelta(minutes=10)))
        session.commit()
        assert "SYN-BUSY-KEY" in _reserved_backfill_keys(session, now=NOW, ttl_hours=6)
