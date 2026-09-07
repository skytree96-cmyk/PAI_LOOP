from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import threading

import pytest
from sqlalchemy import select

from pai_loop.models import BidOutcome, Notice
from pai_loop import result_learning


ENDPOINT = "/api/v1/result-learning"
NOTICE_KEY = "SYN-BID-RATE-001"
AUTO = {
    "mode": "AUTO",
    "basis_kind": "PLANNED_PRICE",
    "basis_amount": 200,
    "basis_reference": "SYN 개찰결과 예정가격 1쪽",
}


def _notice(client):
    response = client.post("/api/v1/notices", json={
        "notice_key": NOTICE_KEY, "bid_notice_no": NOTICE_KEY, "revision_no": "00",
        "title": "SYN 투찰률 계산", "agency": "SYN 기관",
        "deadline": "2025-01-01T00:00:00Z", "estimated_amount": 999999,
    })
    assert response.status_code == 201, response.text


def _payload(**changes):
    return {
        "notice_key": NOTICE_KEY, "idempotency_key": "SYN-rate-create-001",
        "status": "SUBMITTED", "submitted_bid_amount": 176.5433,
        "submitted_rate_calculation": AUTO, **changes,
    }


def _stored(client, outcome_id):
    with client.app.state.session_factory() as session:
        return session.get(BidOutcome, outcome_id).evidence_json


@pytest.mark.parametrize("amount,basis,expected", [
    (176.5433, 200, 88.2717),  # Exact half: the fifth decimal rounds up.
    (0, 200, 0), (2, 3, 66.6667), (400, 200, 200),
])
def test_auto_bid_rate_is_server_calculated_and_allowlisted(client, amount, basis, expected):
    _notice(client)
    response = client.post(ENDPOINT, json=_payload(
        submitted_bid_amount=amount, submitted_bid_rate=123,
        submitted_rate_calculation={**AUTO, "basis_amount": basis},
    ))
    assert response.status_code == 201, response.text
    row = response.json()["outcome"]
    assert row["submitted_bid_rate"] == expected  # Neither the supplied rate nor notice budget.
    assert row["submitted_rate_calculation"] == {**AUTO, "basis_amount": basis}
    stored = _stored(client, row["id"])["_submitted_bid_rate"]
    assert stored["history"][0]["before"] is None
    assert stored["history"][0]["after"]["rounding_policy"] == "DECIMAL_HALF_UP_4"
    with client.app.state.session_factory() as session:
        item = session.get(BidOutcome, row["id"])
        item.evidence_json = {**item.evidence_json, "SYN_private_evidence": "SYN-not-public"}
        session.commit()
    listing = client.get(ENDPOINT).json()
    assert "history" not in str(listing) and "SYN-not-public" not in str(listing)
    client.app.state.settings = replace(client.app.state.settings, public_read_only=True, api_key="SYN-rate-server-key")
    assert client.get(f"/api/v1/notices/{NOTICE_KEY}/outcomes").status_code == 401
    assert client.get(ENDPOINT).status_code == 401
    public = client.get(f"/api/v1/notices/{NOTICE_KEY}")
    assert public.status_code == 200, public.text
    assert "basis_reference" not in public.text and "SYN-not-public" not in public.text


@pytest.mark.parametrize("changes", [
    {"submitted_bid_amount": None},
    {"submitted_bid_amount": "Infinity"},
    {"submitted_bid_amount": "NaN"},
    {"submitted_bid_amount": 400.0001},
    {"submitted_rate_calculation": None},
    {"submitted_rate_calculation": {"mode": "AUTO"}},
    {"submitted_rate_calculation": {**AUTO, "basis_amount": None}},
    {"submitted_rate_calculation": {**AUTO, "basis_amount": 0}},
    {"submitted_rate_calculation": {**AUTO, "basis_amount": -1}},
    {"submitted_rate_calculation": {**AUTO, "basis_amount": "Infinity"}},
    {"submitted_rate_calculation": {**AUTO, "basis_kind": "NOTICE_ESTIMATED_AMOUNT"}},
    {"submitted_rate_calculation": {**AUTO, "basis_reference": "  "}},
    {"submitted_rate_calculation": {**AUTO, "history": [{"SYN": "forged"}]}},
])
def test_auto_bid_rate_rejects_missing_invalid_or_untrusted_basis(client, changes):
    _notice(client)
    response = client.post(ENDPOINT, json=_payload(**changes))
    assert response.status_code == 422, response.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(BidOutcome)) is None


def test_auto_bid_rate_replay_binds_basis_even_when_ratio_is_unchanged(client):
    _notice(client)
    payload = _payload()
    created = client.post(ENDPOINT, json=payload).json()["outcome"]
    repeated = client.post(ENDPOINT, json=payload)
    assert repeated.status_code == 201 and repeated.json()["created"] is False
    for change in ({"basis_kind": "BASE_AMOUNT"}, {"basis_reference": "SYN different source"}):
        assert client.post(ENDPOINT, json={**payload, "submitted_rate_calculation": {**AUTO, **change}}).status_code == 409
    assert len(_stored(client, created["id"])["_submitted_bid_rate"]["history"]) == 1


def test_auto_bid_rate_legacy_patch_keeps_basis_and_appends_before_after_history(client):
    _notice(client)
    original = client.post(ENDPOINT, json=_payload()).json()["outcome"]
    path = f"{ENDPOINT}/{original['id']}"
    # Legacy clients omit the new field and may send the previous rate with a new amount.
    edited = client.patch(path, json={
        "expected_updated_at": original["updated_at"], "submitted_bid_amount": 100,
        "submitted_bid_rate": original["submitted_bid_rate"],
    })
    assert edited.status_code == 200, edited.text
    row = edited.json()["outcome"]
    assert row["submitted_bid_rate"] == 50 and row["submitted_rate_calculation"] == AUTO
    assert client.patch(path, json={"expected_updated_at": original["updated_at"], "submitted_bid_amount": 80}).status_code == 409
    audit_before = _stored(client, row["id"])["_submitted_bid_rate"]["history"]
    assert len(audit_before) == 2
    assert audit_before[1]["before"]["submitted_bid_rate"] == 88.2717
    assert audit_before[1]["before"]["submitted_bid_amount"] == 176.5433
    assert audit_before[1]["after"]["submitted_bid_rate"] == 50
    assert audit_before[1]["revision"] == 2 and audit_before[1]["changed_at"]

    note = client.patch(path, json={"expected_updated_at": row["updated_at"], "operator_note": "SYN note only"})
    assert note.status_code == 200
    row = note.json()["outcome"]
    assert _stored(client, row["id"])["_submitted_bid_rate"]["history"] == audit_before
    changed_basis = {**AUTO, "basis_kind": "BASE_AMOUNT", "basis_amount": 125, "basis_reference": "SYN 기초금액 2쪽"}
    row = client.patch(path, json={"expected_updated_at": row["updated_at"], "submitted_rate_calculation": changed_basis}).json()["outcome"]
    assert row["submitted_bid_rate"] == 80 and row["submitted_rate_calculation"] == changed_basis
    row = client.patch(path, json={"expected_updated_at": row["updated_at"], "submitted_rate_calculation": {"mode": "MANUAL"}, "submitted_bid_rate": 77.1234}).json()["outcome"]
    assert row["submitted_bid_rate"] == 77.1234
    assert row["submitted_rate_calculation"]["basis_amount"] is None
    history = _stored(client, row["id"])["_submitted_bid_rate"]["history"]
    assert len(history) == 4 and history[:2] == audit_before
    assert history[-1]["before"]["calculation"] == changed_basis
    assert history[-1]["after"]["calculation"]["mode"] == "MANUAL"
    # Omitted metadata on manual records continues to mean manual entry.
    updated = client.patch(path, json={"expected_updated_at": row["updated_at"], "submitted_bid_rate": 0})
    assert updated.status_code == 200 and updated.json()["outcome"]["submitted_bid_rate"] == 0


def test_auto_to_manual_without_rate_preserves_dates_sources_and_history(client):
    _notice(client)
    response = client.post(ENDPOINT, json=_payload(
        occurred_at="2026-09-07T16:30:45.123456Z", source_reference="SYN result evidence",
    ))
    assert response.status_code == 201, response.text
    row = response.json()["outcome"]
    history_before = _stored(client, row["id"])["_submitted_bid_rate"]["history"]
    path = f"{ENDPOINT}/{row['id']}"
    changed = client.patch(path, json={
        "expected_updated_at": row["updated_at"], "submitted_rate_calculation": {"mode": "MANUAL"},
    })
    assert changed.status_code == 200, changed.text
    manual = changed.json()["outcome"]
    assert manual["submitted_bid_rate"] == row["submitted_bid_rate"]
    assert manual["submitted_rate_calculation"] == {
        "mode": "MANUAL", "basis_kind": None, "basis_amount": None, "basis_reference": None,
    }
    # A legacy amount-only PATCH must keep the explicit manual rate after the transition.
    changed = client.patch(path, json={
        "expected_updated_at": manual["updated_at"], "submitted_bid_amount": 120,
    })
    assert changed.status_code == 200, changed.text
    final = changed.json()["outcome"]
    assert final["submitted_bid_rate"] == row["submitted_bid_rate"]
    assert final["occurred_at"] == row["occurred_at"]
    assert final["source_reference"] == row["source_reference"]
    history = _stored(client, row["id"])["_submitted_bid_rate"]["history"]
    assert len(history) == 3 and history[:1] == history_before
    assert history[1]["before"]["calculation"] == AUTO
    assert history[1]["after"]["calculation"]["mode"] == "MANUAL"
    assert history[2]["before"] == history[1]["after"]
    assert history[2]["after"]["submitted_bid_amount"] == 120


def test_legacy_row_without_metadata_roundtrips_and_keeps_original_evidence(client):
    _notice(client)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        item = BidOutcome(notice_id=notice.id, outcome_key="SYN-legacy", source="MANUAL_UI", status="SUBMITTED",
                          submitted_bid_rate=87.65, evidence_json={"SYN_original": {"retained": True}}, observed_at=datetime.now(timezone.utc))
        session.add(item)
        session.commit()
        outcome_id = item.id
    row = client.get(ENDPOINT).json()["records"][0]["latest_outcome"]
    assert row["submitted_rate_calculation"]["mode"] == "MANUAL"
    patched = client.patch(f"{ENDPOINT}/{outcome_id}", json={"expected_updated_at": row["updated_at"], "submitted_bid_rate": 0})
    assert patched.status_code == 200, patched.text
    stored = _stored(client, outcome_id)
    assert stored["SYN_original"] == {"retained": True}
    assert stored["_submitted_bid_rate"]["history"][0]["before"]["submitted_bid_rate"] == 87.65
    created_payload = _payload(submitted_bid_amount=None, submitted_bid_rate=None)
    created_payload.pop("submitted_rate_calculation")
    created = client.post(ENDPOINT, json=created_payload)
    assert created.status_code == 201 and created.json()["outcome"]["submitted_bid_rate"] is None
    assert client.post(ENDPOINT, json=created_payload).json()["created"] is False


def test_auto_bid_rate_concurrent_patch_keeps_only_winning_snapshot(client, monkeypatch):
    _notice(client)
    row = client.post(ENDPOINT, json=_payload()).json()["outcome"]
    barrier = threading.Barrier(2)
    original = result_learning._same_version

    def same_version(actual, expected):
        matches = original(actual, expected)
        barrier.wait(timeout=5)
        return matches

    monkeypatch.setattr(result_learning, "_same_version", same_version)

    def save(amount):
        return client.patch(f"{ENDPOINT}/{row['id']}", json={
            "expected_updated_at": row["updated_at"], "submitted_bid_amount": amount,
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(save, [100, 120]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()["outcome"] for response in responses if response.status_code == 200)
    history = _stored(client, row["id"])["_submitted_bid_rate"]["history"]
    assert len(history) == 2
    assert history[-1]["before"]["submitted_bid_rate"] == row["submitted_bid_rate"]
    assert history[-1]["after"]["submitted_bid_rate"] == winner["submitted_bid_rate"]
    assert history[-1]["after"]["submitted_bid_amount"] == winner["submitted_bid_amount"]


def test_no_bid_requires_explicit_exit_from_auto_and_external_source_is_immutable(client):
    _notice(client)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        source = BidOutcome(notice_id=notice.id, outcome_key="SYN-external", source="PPS_AUTO_FEEDBACK", status="WON",
                            winning_bid_amount=190, evidence_json={"SYN_original": True}, observed_at=datetime.now(timezone.utc))
        session.add(source)
        session.commit()
        source_id, source_version = source.id, source.updated_at.isoformat()
    assert client.patch(f"{ENDPOINT}/{source_id}", json={"expected_updated_at": source_version, "submitted_rate_calculation": AUTO}).status_code == 409
    row = client.post(ENDPOINT, json=_payload(basis_outcome_id=source_id)).json()["outcome"]
    assert row["basis_outcome_id"] == source_id
    path = f"{ENDPOINT}/{row['id']}"
    payload = {"expected_updated_at": row["updated_at"], "status": "NO_BID", "submitted_bid_amount": None, "submitted_bid_rate": None}
    assert client.patch(path, json=payload).status_code == 422
    changed = client.patch(path, json={**payload, "submitted_rate_calculation": {"mode": "MANUAL"}})
    assert changed.status_code == 200, changed.text
    assert changed.json()["outcome"]["submitted_bid_rate"] is None
    assert changed.json()["outcome"]["basis_outcome_id"] == source_id
    assert _stored(client, source_id) == {"SYN_original": True}
