"""One-shot recovery survives concurrency, new attempt IDs and retention."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier

from pai_loop.models import IngestionJob, NoticeVersion
from pai_loop import quantitative_recovery_policy as policy
from test_failed_attachment_retry_scope import failed_case
from test_long_output_once import long_case


def test_reservation_is_atomic_across_versions_and_survives_retention(long_case):
    client, _, version_id, _ = long_case
    factory = client.app.state.session_factory
    with factory() as session:
        old = session.get(NoticeVersion, version_id)
        newer = NoticeVersion(notice_id=old.notice_id, version_no=old.version_no + 1,
                             file_sha256=old.file_sha256, source_payload=deepcopy(old.source_payload))
        session.add(newer)
        session.commit()
        ids = [version_id, newer.id]
        reservation_id = policy.claim_id(old)
        assert policy.claim_id(newer) == reservation_id
    barrier = Barrier(2)
    def worker(row_id):
        with factory() as session:
            with session.begin():
                version = session.get(NoticeVersion, row_id)
            barrier.wait()
            return policy.consume(session, version, lambda: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ids))
    assert results.count(True) == results.count(False) == 1
    with factory() as session:
        claim = session.get(IngestionJob, reservation_id)
        claim.completed_at = datetime.now(timezone.utc) - timedelta(days=100)
        session.add(IngestionJob(source="SYN-ORDINARY", mode="LIVE", status="COMPLETED",
                                window_json={}, request_json={}, completed_at=claim.completed_at))
        session.commit()
    result = client.post("/api/v1/operations/retention", json={"retention_days": 7, "dry_run": False})
    assert result.status_code == 200
    assert result.json()["deleted"]["ingestion_jobs"] == 1
    with factory() as session:
        assert session.get(IngestionJob, reservation_id) is not None
        assert policy.consumed(session, session.get(NoticeVersion, version_id))


def test_rejected_source_does_not_consume_reservation(long_case):
    client, _, version_id, _ = long_case
    with client.app.state.session_factory() as session:
        with session.begin():
            version = session.get(NoticeVersion, version_id)
        assert not policy.consume(session, version, lambda: False)
        assert not policy.consumed(session, version)
