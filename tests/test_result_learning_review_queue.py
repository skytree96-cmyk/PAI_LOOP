"""Result records list recorded notices first and isolate automatic results to review."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pai_loop.models import BidOutcome, Notice

from test_department_accounts import _login, account_client  # noqa: F401 - fixture


ENDPOINT = "/api/v1/result-learning"
NOW = datetime.now(timezone.utc)
AUTO_EVIDENCE = {"exact_match": {"verified": True, "bid_notice_no": "SYN", "revision_no": "000"}}


def _ended(session, label: str, days: int) -> Notice:
    notice = Notice(
        notice_key=f"PPS-SYN-REVIEW-{label}", bid_notice_no=f"SYN-REVIEW-{label}", revision_no="000",
        title=f"SYN 결과 검토 {label}", agency="SYN 기관", status="CLOSED",
        deadline=NOW - timedelta(days=days),
    )
    session.add(notice)
    session.flush()
    return notice


def _automatic(notice: Notice, status: str, observed: datetime, **evidence) -> BidOutcome:
    return BidOutcome(
        notice_id=notice.id, outcome_key=f"pps-final-award:{notice.notice_key}", status=status,
        source="PPS_AUTO_FEEDBACK", source_reference="PPS:SYN", winner_name="SYN 낙찰사",
        evidence_json={**AUTO_EVIDENCE, **evidence}, observed_at=observed,
        created_at=observed, updated_at=observed,
    )


def _department(notice: Notice, department_id: str, at: datetime) -> BidOutcome:
    return BidOutcome(
        notice_id=notice.id, outcome_key=f"department:{department_id}:{notice.notice_key}", status="LOST",
        department_id=department_id, department_name=department_id, department_revision=1,
        source="MANUAL_UI", source_reference="SYN 공문",
        evidence_json={"_workflow": {"record_status": "VALIDATED", "revision": 1, "human_reviewed": True}},
        observed_at=at, created_at=at, updated_at=at,
    )


def _seed(client, own_department: str, other_department: str) -> None:
    with client.app.state.session_factory() as session:
        unrecorded = _ended(session, "UNRECORDED", 1)
        automatic_won = _ended(session, "AUTO-WON", 10)
        own = _ended(session, "OWN", 20)
        reviewed_elsewhere = _ended(session, "OTHER-REVIEWED", 5)
        session.add(_automatic(
            automatic_won, "WON", NOW - timedelta(hours=1),
            demo_seed={"marker": "DEMO_SEED", "purpose": "presentation"},
        ))
        session.add(_department(own, own_department, NOW - timedelta(minutes=5)))
        session.add(_automatic(reviewed_elsewhere, "WON", NOW - timedelta(hours=3)))
        session.add(_department(reviewed_elsewhere, other_department, NOW - timedelta(hours=2)))
        del unrecorded
        session.commit()


def _keys(payload) -> list[str]:
    return [row["notice_key"].removeprefix("PPS-SYN-REVIEW-") for row in payload["records"]]


def test_recorded_notices_lead_and_counts_follow_the_viewer_department(account_client):
    client = account_client
    headers, first = _login(client)
    with client.__class__(client.app) as peer:
        _, second = _login(peer, "SYN_KMA2")
    own_department = first["account"]["department_id"]
    _seed(client, own_department, second["account"]["department_id"])

    listing = client.get(ENDPOINT)
    assert listing.status_code == 200, listing.text
    payload = listing.json()
    # Own record (newest) first, then provider results by observation time,
    # then the unrecorded ended notice.
    assert _keys(payload) == ["OWN", "AUTO-WON", "OTHER-REVIEWED", "UNRECORDED"]
    assert payload["with_outcome_count"] == 3
    # The other department's review does not settle this department's queue.
    assert payload["auto_review_count"] == 2
    pending = {key: row["auto_review_pending"] for key, row in zip(_keys(payload), payload["records"])}
    assert pending == {"OWN": False, "AUTO-WON": True, "OTHER-REVIEWED": True, "UNRECORDED": False}

    queue = client.get(ENDPOINT, params={"queue": "AUTO_REVIEW"}).json()
    assert _keys(queue) == ["AUTO-WON", "OTHER-REVIEWED"]
    assert queue["total"] == 2 and queue["auto_review_count"] == 2
    demo = {row["notice_key"]: row["latest_outcome"]["demo"] for row in queue["records"]}
    assert demo == {"PPS-SYN-REVIEW-AUTO-WON": True, "PPS-SYN-REVIEW-OTHER-REVIEWED": False}

    exact = client.get(f"{ENDPOINT}/notices/PPS-SYN-REVIEW-AUTO-WON").json()
    assert exact["auto_review_pending"] is True and exact["latest_outcome"]["demo"] is True

    dashboard = client.get("/api/v1/dashboard", params={"department_id": own_department})
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["result_review_count"] == 2
    assert client.get("/api/v1/dashboard").json()["result_review_count"] == 1
    assert client.get(ENDPOINT, params={"queue": "SYN-UNKNOWN"}).status_code == 422
    del headers


def test_a_department_review_after_the_provider_result_clears_its_queue(account_client):
    client = account_client
    _, first = _login(client)
    own_department = first["account"]["department_id"]
    with client.app.state.session_factory() as session:
        notice = _ended(session, "REVIEWED", 3)
        session.add(_automatic(notice, "WON", NOW - timedelta(hours=4)))
        session.add(_department(notice, own_department, NOW - timedelta(hours=1)))
        session.commit()
    payload = client.get(ENDPOINT, params={"queue": "AUTO_REVIEW"}).json()
    assert payload["records"] == [] and payload["auto_review_count"] == 0
    # A newer provider observation reopens the review for this department.
    with client.app.state.session_factory() as session:
        row = session.query(BidOutcome).filter(BidOutcome.source == "PPS_AUTO_FEEDBACK").one()
        row.observed_at = NOW
        session.commit()
    assert _keys(client.get(ENDPOINT, params={"queue": "AUTO_REVIEW"}).json()) == ["REVIEWED"]


def test_admin_viewer_treats_any_department_review_as_settled(account_client):
    client = account_client
    _, first = _login(client)
    with client.__class__(client.app) as peer:
        _, second = _login(peer, "SYN_KMA2")
    _seed(client, first["account"]["department_id"], second["account"]["department_id"])
    _login(client, "SYN_ADMIN")
    payload = client.get(ENDPOINT).json()
    assert payload["auto_review_count"] == 1
    assert _keys(client.get(ENDPOINT, params={"queue": "AUTO_REVIEW"}).json()) == ["AUTO-WON"]
