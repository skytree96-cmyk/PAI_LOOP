from datetime import datetime, timedelta, timezone

import pytest

from pai_loop.analysis_api import AnalysisBackfillPlanRequest, _matching_active_backfill
from pai_loop.models import IngestionJob


@pytest.mark.parametrize(
    "lease_age_hours,request_token,expected_queue",
    [(None, "SYN-NEW", "DAILY"), (0, "SYN-NEW", "BACKFILL"),
     (7, "SYN-NEW", "DAILY"), (0, "SYN-OWNER", "DAILY")],
)
def test_any_poll_uses_idle_parent_without_breaking_exact_replay(
    client, lease_age_hours, request_token, expected_queue
) -> None:
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        for queue in ("BACKFILL", "DAILY"):
            config = {"queue_name": queue, "dry_run": False, "chunk_size": 1,
                      "retry_cooldown_hours": 24, "reservation_ttl_hours": 6}
            if queue == "DAILY" and lease_age_hours is not None:
                config.update(lease_id="SYN-LEASE", lease_request_token="SYN-OWNER",
                              lease_started_at=(now - timedelta(hours=lease_age_hours)).isoformat())
            session.add(IngestionJob(source="ANALYSIS_BACKFILL", mode="LIVE", status="PARTIAL",
                                     request_json=config, window_json={}, notice_keys=[],
                                     created_at=now, completed_at=None))
        session.commit()
        selected = _matching_active_backfill(
            session,
            AnalysisBackfillPlanRequest(queue_name="ANY", resume_only=True,
                                        request_token=request_token),
            now=now,
        )
        assert selected is not None
        assert selected.request_json["queue_name"] == expected_queue
        # Selection must not release or mutate the in-flight lease itself.
        if expected_queue == "DAILY" and lease_age_hours is not None:
            assert selected.request_json["lease_id"] == "SYN-LEASE"
