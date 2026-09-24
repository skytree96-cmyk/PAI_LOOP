"""Both rate calculations remain independently sourced, audited and server-owned."""

import pytest

from test_result_learning_bid_rate import AUTO, ENDPOINT, _notice, _payload, _stored


@pytest.mark.parametrize("amount,basis,expected", [(176.5433, 200, 88.2717), (0, 200, 0), (2, 3, 66.6667), (400, 200, 200)])
def test_both_rates_use_independent_bases_and_server_rounding(client, amount, basis, expected):
    _notice(client)
    payload = _payload(winning_bid_amount=amount, winning_bid_rate=123,
                       winning_rate_calculation={**AUTO, "basis_kind": "BASE_AMOUNT", "basis_amount": basis})
    response = client.post(ENDPOINT, json=payload)
    assert response.status_code == 201, response.text
    row = response.json()["outcome"]
    assert row["submitted_bid_rate"] == 88.2717
    assert row["winning_bid_rate"] == expected
    assert row["winning_rate_calculation"]["basis_kind"] == "BASE_AMOUNT"
    audit = _stored(client, row["id"])["_winning_bid_rate"]
    assert audit["history"][0]["before"] is None
    assert audit["history"][0]["after"]["rounding_policy"] == "DECIMAL_HALF_UP_4"
    assert client.post(ENDPOINT, json=payload).json()["created"] is False
    assert client.post(ENDPOINT, json={**payload, "winning_rate_calculation": {
        **payload["winning_rate_calculation"], "basis_reference": "SYN other source",
    }}).status_code == 409


@pytest.mark.parametrize("changes", [
    {"winning_bid_amount": None}, {"winning_bid_amount": "NaN"},
    {"winning_bid_amount": 401}, {"winning_rate_calculation": None},
    {"winning_rate_calculation": {"mode": "AUTO"}},
    {"winning_rate_calculation": {**AUTO, "basis_amount": 0}},
    {"winning_rate_calculation": {**AUTO, "basis_amount": "Infinity"}},
    {"winning_rate_calculation": {**AUTO, "basis_reference": " "}},
    {"winning_rate_calculation": {**AUTO, "basis_kind": "NOTICE_ESTIMATED_AMOUNT"}},
    {"winning_rate_calculation": {**AUTO, "history": []}},
])
def test_winner_auto_rejects_unverified_or_invalid_basis(client, changes):
    _notice(client)
    response = client.post(ENDPOINT, json=_payload(
        **{"winning_bid_amount": 180, "winning_rate_calculation": AUTO, **changes},
    ))
    assert response.status_code == 422, response.text


def test_legacy_patch_preserves_winner_basis_and_records_only_changed_rate(client):
    _notice(client)
    row = client.post(ENDPOINT, json=_payload(winning_bid_amount=180, winning_rate_calculation=AUTO)).json()["outcome"]
    path = f"{ENDPOINT}/{row['id']}"
    changed = client.patch(path, json={"expected_updated_at": row["updated_at"], "winning_bid_amount": 170, "winning_bid_rate": 90})
    assert changed.status_code == 200, changed.text
    updated = changed.json()["outcome"]
    assert updated["winning_bid_rate"] == 85
    assert updated["winning_rate_calculation"] == AUTO
    audit = _stored(client, row["id"])
    assert len(audit["_submitted_bid_rate"]["history"]) == 1
    history = audit["_winning_bid_rate"]["history"]
    assert len(history) == 2
    assert history[-1]["before"]["winning_bid_rate"] == 90
    assert history[-1]["after"]["winning_bid_rate"] == 85
    assert client.patch(path, json={"expected_updated_at": row["updated_at"], "winning_bid_amount": 160}).status_code == 409
    manual = client.patch(path, json={"expected_updated_at": updated["updated_at"], "winning_rate_calculation": {"mode": "MANUAL"}, "winning_bid_rate": 84.25})
    assert manual.status_code == 200, manual.text
    assert manual.json()["outcome"]["winning_bid_rate"] == 84.25
    assert manual.json()["outcome"]["winning_rate_calculation"]["basis_amount"] is None
