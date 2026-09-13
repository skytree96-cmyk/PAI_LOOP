from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.models import BidOutcome

from test_department_accounts import NOTICE, _login, _outcome, account_client


ENDPOINT = "/api/v1/result-learning"
NOTICE_ENDPOINT = f"{ENDPOINT}/notices/{NOTICE}"


def _won_payload(**changes):
    return {
        "record_status": "DRAFT",
        "status": "WON",
        "winning_bid_amount": 35_501_500,
        "winning_bid_rate": 95,
        "technical_score": 82.73,
        "price_score": 7.37,
        "total_score": 90.1,
        "rank": 1,
        "winner_name": "SYN 낙찰사",
        "occurred_at": "2026-01-27T00:00:00+09:00",
        "source_reference": None,
        **changes,
    }


def test_exact_result_entry_lookup_requires_login_and_is_independent_of_list_filters(account_client):
    client = account_client
    assert client.get(NOTICE_ENDPOINT).status_code == 401
    assert client.get(f"{ENDPOINT}/notices/SYN-MISSING").status_code == 401
    _login(client)
    # The current open notice is excluded from the default ended list. A row
    # action must still resolve its exact notice without searches or pagination.
    assert client.get(ENDPOINT).json()["records"] == []
    result = client.get(NOTICE_ENDPOINT)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["notice_key"] == NOTICE
    assert result.json()["latest_outcome"] is None
    assert result.json()["outcomes"] == []
    assert client.get(f"{ENDPOINT}/notices/SYN-MISSING").status_code == 404


def test_exact_result_entry_projection_matches_list_and_includes_department_versions(account_client):
    client = account_client
    headers, first_session = _login(client)
    own = _outcome(client, headers, **_won_payload()).json()["outcome"]
    with TestClient(client.app) as peer:
        peer_headers, second_session = _login(peer, "SYN_KMA2")
        other = _outcome(peer, peer_headers).json()["outcome"]
    with client.app.state.session_factory() as session:
        stored = session.get(BidOutcome, own["id"])
        stored.evidence_json = {**stored.evidence_json, "SYN_private_marker": "SYN-not-for-response"}
        session.commit()
    exact = client.get(NOTICE_ENDPOINT)
    listing = client.get(ENDPOINT, params={"scope": "ALL", "q": NOTICE}).json()["records"][0]
    assert exact.status_code == 200, exact.text
    assert exact.json() == listing
    rows = {row["id"]: row for row in exact.json()["outcomes"]}
    assert set(rows) == {own["id"], other["id"]}
    assert rows[own["id"]]["department_id"] == first_session["account"]["department_id"]
    assert rows[other["id"]]["department_id"] == second_session["account"]["department_id"]
    assert rows[own["id"]]["updated_at"] and rows[own["id"]]["department_revision"] == 1
    assert "evidence_json" not in exact.text and "SYN-not-for-response" not in exact.text


def test_result_entry_draft_saves_completed_scores_and_requires_source_only_for_validation(account_client):
    client = account_client
    headers, _ = _login(client)
    saved = _outcome(client, headers, **_won_payload())
    assert saved.status_code == 201, saved.text
    row = saved.json()["outcome"]
    assert row["record_status"] == "DRAFT" and row["source_reference"] is None
    for field in ("winning_bid_amount", "winning_bid_rate", "technical_score", "price_score", "total_score", "rank"):
        assert row[field] == _won_payload()[field]
    path = f"{ENDPOINT}/{row['id']}"
    invalid = client.patch(path, headers=headers, json={
        "expected_updated_at": row["updated_at"], "record_status": "VALIDATED",
    })
    assert invalid.status_code == 422, invalid.text
    assert "출처 또는 근거 참조" in invalid.json()["detail"]
    assert client.get(NOTICE_ENDPOINT).json()["latest_outcome"] == row
    validated = client.patch(path, headers=headers, json={
        "expected_updated_at": row["updated_at"],
        "record_status": "VALIDATED", "source_reference": "SYN 평가결과 공문 1쪽",
    })
    assert validated.status_code == 200, validated.text
    assert validated.json()["outcome"]["record_status"] == "VALIDATED"


def test_result_entry_invalid_create_has_actionable_errors_without_persisting(account_client):
    client = account_client
    headers, _ = _login(client)
    for changes, expected_message in (
        ({"status": "NO_BID"}, "미참여 결과에는"),
        ({"record_status": "VALIDATED"}, "출처 또는 근거 참조"),
    ):
        rejected = _outcome(client, headers, **_won_payload(**changes))
        assert rejected.status_code == 422, rejected.text
        assert expected_message in rejected.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(BidOutcome)) is None
    # Correcting the selection can reuse the same request id because the
    # rejected requests never created a result record.
    corrected = _outcome(client, headers, **_won_payload())
    assert corrected.status_code == 201, corrected.text


def test_exact_result_entry_internal_projection_matches_list_when_accounts_disabled(client):
    result = client.post("/api/v1/notices", json={
        "notice_key": NOTICE, "bid_notice_no": NOTICE, "revision_no": "00",
        "title": "SYN 결과 직접 입력", "agency": "SYN 기관", "deadline": "2025-01-01T00:00:00Z",
    })
    assert result.status_code == 201
    created = client.post(ENDPOINT, json={
        "notice_key": NOTICE, "idempotency_key": "SYN-direct-entry-001", **_won_payload(),
    })
    assert created.status_code == 201, created.text
    exact = client.get(NOTICE_ENDPOINT)
    assert exact.status_code == 200
    assert exact.json() == client.get(ENDPOINT).json()["records"][0]
    assert exact.json()["latest_outcome"]["id"] == created.json()["outcome"]["id"]
    assert exact.json()["outcomes"] == []
