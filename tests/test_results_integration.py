"""Synthetic cross-contract coverage for accounts, rates and opening evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import threading

import pytest
from sqlalchemy import select

from pai_loop import result_learning
from pai_loop.models import BidOutcome, Notice
from test_department_accounts import account_client, _login, _outcome, _peer, NOTICE, SERVER
from test_outcome_opening_identity import _FeedbackClient, _row


AUTO = {"mode": "AUTO", "basis_kind": "BASE_AMOUNT", "basis_amount": 200,
        "basis_reference": "SYN 기준가격 출처"}


def _opening(notice_no=NOTICE, rebid="2"):
    return {"bid_notice_no": notice_no, "revision_no": "0", "classification_no": "1", "rebid_no": rebid}


def _evidence(client, outcome_id):
    with client.app.state.session_factory() as session:
        return deepcopy(session.get(BidOutcome, outcome_id).evidence_json)


def _submission(client, headers, **changes):
    response = _outcome(client, headers, **{
        "status": "SUBMITTED", "record_status": "VALIDATED", "source_reference": "SYN 제출 출처",
        "submitted_bid_amount": 100, "submitted_rate_calculation": AUTO,
        "opening_identity": _opening(), **changes,
    })
    assert response.status_code == 201, response.text
    return response.json()["outcome"]


def test_department_rate_and_opening_histories_preserve_actor_basis_and_cas(account_client):
    headers, me = _login(account_client)
    with account_client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE))
        original = BidOutcome(notice_id=notice.id, outcome_key="SYN-original-feedback", source="PPS_AUTO_FEEDBACK",
                              status="WON", evidence_json={"SYN-original": True})
        session.add(original)
        session.commit()
        original_id = original.id
    row = _submission(account_client, headers, basis_outcome_id=original_id)
    assert _submission(account_client, headers, basis_outcome_id=original_id)["id"] == row["id"]
    before = _evidence(account_client, row["id"])
    path = f"/api/v1/result-learning/{row['id']}"
    change = {"expected_updated_at": row["updated_at"], "opening_identity": _opening(rebid="3"),
              "submitted_bid_amount": 176.5433}  # Old UI omits calculation metadata.
    other = _peer(account_client)
    other_headers, _ = _login(other, "SYN_KMA2")
    assert other.patch(path, headers=other_headers, json=change).status_code == 403
    assert _peer(account_client).patch(path, headers=SERVER, json=change).status_code == 403
    assert _evidence(account_client, row["id"]) == before
    response = account_client.patch(path, headers=headers, json=change)
    assert response.status_code == 200, response.text
    updated = response.json()["outcome"]
    assert updated["submitted_bid_rate"] == 88.2717
    assert updated["opening_identity"] == _opening(rebid="3")
    assert updated["department_id"] == row["department_id"]
    assert updated["basis_outcome_id"] == original_id
    assert account_client.patch(path, headers=headers, json=change).status_code == 409
    stored = _evidence(account_client, row["id"])
    openings = stored["_opening_identity_history"]
    assert len(openings) == 2 and openings[:1] == before["_opening_identity_history"]
    assert all(entry["actor"] == me["account"]["department_name"] for entry in openings)
    rates = stored["_submitted_bid_rate"]["history"]
    assert len(rates) == 2 and rates[:1] == before["_submitted_bid_rate"]["history"]
    assert all(entry["actor_id"] == me["account"]["id"] for entry in rates)
    assert stored["_workflow"]["updated_by"] == me["account"]["department_name"]
    assert stored["_workflow"]["basis_outcome"]["id"] == original_id
    # Neither identity nor calculation is omitted from create-idempotency comparison.
    assert _outcome(account_client, headers, status="SUBMITTED", submitted_bid_amount=176.5433,
                    record_status="VALIDATED", source_reference="SYN 제출 출처",
                    submitted_rate_calculation=AUTO, opening_identity=_opening(rebid="4"),
                    basis_outcome_id=original_id).status_code == 409
    cleared = account_client.patch(path, headers=headers, json={
        "expected_updated_at": updated["updated_at"], "opening_identity": None,
        "submitted_rate_calculation": {"mode": "MANUAL"},
    })
    assert cleared.status_code == 200, cleared.text
    final = _evidence(account_client, row["id"])
    assert final["_opening_identity_history"][:-1] == stored["_opening_identity_history"]
    assert final["_opening_identity_history"][-1]["after"] is None
    assert final["_submitted_bid_rate"]["history"][:-1] == rates
    assert final["_submitted_bid_rate"]["history"][-1]["after"]["calculation"]["mode"] == "MANUAL"
    note = account_client.patch(path, headers=headers, json={
        "expected_updated_at": cleared.json()["outcome"]["updated_at"], "operator_note": "SYN 일반 메모",
    })
    assert note.status_code == 200, note.text
    after_note = _evidence(account_client, row["id"])
    assert after_note["_opening_identity_history"] == final["_opening_identity_history"]
    assert after_note["_submitted_bid_rate"] == final["_submitted_bid_rate"]
    assert "_opening_identity_history" not in note.text and "_submitted_bid_rate" not in note.text
    assert _evidence(account_client, original_id) == {"SYN-original": True}
    public = _peer(account_client).get(f"/api/v1/notices/{NOTICE}")
    assert public.status_code == 200
    assert "opening_identity" not in public.text and "basis_reference" not in public.text


def test_department_concurrent_rate_and_opening_change_records_one_complete_winner(account_client, monkeypatch):
    headers, me = _login(account_client)
    row = _submission(account_client, headers)
    barrier = threading.Barrier(2)
    original = result_learning._same_version

    def same_version(actual, expected):
        matches = original(actual, expected)
        barrier.wait(timeout=10)
        return matches

    monkeypatch.setattr(result_learning, "_same_version", same_version)

    def save(number):
        return account_client.patch(f"/api/v1/result-learning/{row['id']}", headers=headers, json={
            "expected_updated_at": row["updated_at"], "opening_identity": _opening(rebid=str(number)),
            "submitted_bid_amount": number * 20,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, [3, 4]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()["outcome"] for response in responses if response.status_code == 200)
    stored = _evidence(account_client, row["id"])
    openings, rates = stored["_opening_identity_history"], stored["_submitted_bid_rate"]["history"]
    assert len(openings) == len(rates) == 2
    assert openings[-1]["after"] == winner["opening_identity"]
    assert rates[-1]["after"]["submitted_bid_rate"] == winner["submitted_bid_rate"]
    assert int(openings[-1]["after"]["rebid_no"]) * 10 == rates[-1]["after"]["submitted_bid_rate"]
    assert openings[-1]["actor"] == me["account"]["department_name"]
    assert rates[-1]["actor_id"] == me["account"]["id"]


@pytest.mark.parametrize("status,record_status", [
    ("NO_BID", "VALIDATED"), ("CANCELLED", "VALIDATED"), ("SUBMITTED", "DRAFT"),
])
def test_latest_department_correction_blocks_older_auto_rate_participation(account_client, monkeypatch, status, record_status):
    key = "PPS-SYN-RESULTS-INTEGRATION"
    server = _peer(account_client)
    created = server.post("/api/v1/notices", headers=SERVER, json={
        "notice_key": key, "bid_notice_no": key, "revision_no": "00", "title": "SYN 회차 통합",
        "agency": "SYN 기관", "deadline": "2025-01-15T00:00:00Z", "status": "CLOSED",
    })
    assert created.status_code == 201, created.text
    account_client.app.state.settings = replace(account_client.app.state.settings, pps_api_key="SYN-provider-only")
    monkeypatch.setattr("pai_loop.outcome_feedback.PpsOutcomeFeedbackClient", _FeedbackClient)
    monkeypatch.setattr(_FeedbackClient, "rows", [_row(bid_notice_no=key)])
    headers, _ = _login(account_client)
    first = _submission(account_client, headers, notice_key=key, opening_identity=_opening(key))
    correction = _submission(account_client, headers, notice_key=key, opening_identity=_opening(key),
                             idempotency_key="SYN-opening-correction", expected_outcome_id=first["id"],
                             status=status, record_status=record_status, submitted_bid_amount=None,
                             submitted_rate_calculation={"mode": "MANUAL"})

    def refresh():
        response = server.post("/api/v1/outcome-feedback/pps/refresh", headers=SERVER, json={"notice_keys": [key]})
        assert response.status_code == 200, response.text
        return response.json()

    blocked = refresh()
    assert blocked["items"][0]["reason_code"] == "PARTICIPATION_OPENING_NOT_CONFIRMED"
    assert blocked["created"] == 0
    later = _submission(account_client, headers, notice_key=key, opening_identity=_opening(key),
                        idempotency_key="SYN-opening-resubmitted", expected_outcome_id=correction["id"])
    # A separate round's cancellation and an unreviewed provider record cannot cancel this submission.
    _submission(account_client, headers, notice_key=key, opening_identity=_opening(key, rebid="3"),
                idempotency_key="SYN-other-opening-cancelled", expected_outcome_id=later["id"],
                status="CANCELLED", submitted_bid_amount=None, submitted_rate_calculation={"mode": "MANUAL"})
    with account_client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == key))
        session.add(BidOutcome(notice_id=notice.id, outcome_key="SYN-provider-unreviewed", source="PPS_IMPORT",
                               status="CANCELLED", observed_at=datetime.now(timezone.utc),
                               evidence_json={"opening_identity": _opening(key)}))
        session.commit()
    assert refresh()["items"][0]["outcome_status"] == "LOST"
    with account_client.app.state.session_factory() as session:
        auto = session.scalar(select(BidOutcome).where(BidOutcome.source == "PPS_AUTO_FEEDBACK"))
        assert auto.evidence_json["participation_basis"]["outcome_id"] == later["id"]
        assert auto.evidence_json["opening_identity"] == _opening(key)
        assert session.get(BidOutcome, first["id"]).submitted_bid_rate == 50
