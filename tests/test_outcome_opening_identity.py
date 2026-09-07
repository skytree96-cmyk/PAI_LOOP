from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.integrations.outcome_feedback import ExactNoticeAwardFetch
from pai_loop.main import create_app
from pai_loop.models import BidOutcome, Notice


NOTICE_KEY = "PPS-SYN-OPENING-IDENTITY"
NOTICE_NO = "SYN-OPENING-IDENTITY"
REFRESH_URL = "/api/v1/outcome-feedback/pps/refresh"


def _identity(**changes):
    return {
        "bid_notice_no": NOTICE_NO, "revision_no": "0",
        "classification_no": "1", "rebid_no": "2", **changes,
    }


def _row(**changes):
    row = {
        **_identity(), "winner_name": "SYN 다른 낙찰사",
        "company_business_number_match": False,
        "company_business_number_status": "PRESENT_VALID",
        "award_amount": 90_000.0, "award_rate": 90.0,
        "opened_at": datetime(2025, 1, 15, tzinfo=timezone.utc),
        "awarded_at": datetime(2025, 1, 16, tzinfo=timezone.utc),
        "provider_result_sha256": "a" * 64, **changes,
    }
    row["identity"] = "|".join(str(row[key]) for key in _identity())
    return row


class _FeedbackClient:
    rows = []

    def __init__(self, **_kwargs):
        self.request_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def fetch_exact_notice_awards(self, **_kwargs):
        self.request_count += 1
        return ExactNoticeAwardFetch(
            rows=self.rows, fetched_count=len(self.rows), mismatched_count=0,
            quarantined_count=0, api_calls=1, hit_page_limit=False,
            hit_time_limit=False,
        )


@pytest.fixture()
def identity_client(monkeypatch):
    monkeypatch.setenv("PPS_API_KEY", "SYN-provider-key")
    monkeypatch.setattr("pai_loop.outcome_feedback.PpsOutcomeFeedbackClient", _FeedbackClient)
    monkeypatch.setattr(_FeedbackClient, "rows", [_row()])
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add(Notice(
                notice_key=NOTICE_KEY, bid_notice_no=NOTICE_NO, revision_no="00",
                title="SYN 재입찰", agency="SYN 기관", status="CLOSED",
                deadline=datetime(2025, 1, 15, tzinfo=timezone.utc),
                published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ))
            session.commit()
        yield client


def _participation(client, identity, *, key="SYN-submission", status="SUBMITTED"):
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        evidence = {"_workflow": {"record_status": "VALIDATED", "human_reviewed": True}}
        if identity is not None:
            evidence["opening_identity"] = identity
        item = BidOutcome(
            notice_id=notice.id, outcome_key=key, status=status, source="MANUAL_UI",
            submitted_bid_amount=95_000, source_reference="SYN 제출 확인",
            evidence_json=evidence, observed_at=datetime.now(timezone.utc),
        )
        session.add(item)
        session.commit()
        return item.id


def _refresh(client, **extra):
    response = client.post(REFRESH_URL, json={"notice_keys": [NOTICE_KEY], **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _automatic_rows(client):
    with client.app.state.session_factory() as session:
        return [{"id": row.id, "key": row.outcome_key, "status": row.status,
                 "evidence": row.evidence_json, "amount": row.winning_bid_amount}
                for row in session.scalars(select(BidOutcome).where(
                    BidOutcome.source == "PPS_AUTO_FEEDBACK"))]


@pytest.mark.parametrize("identity", [
    None, _identity(classification_no="9"), _identity(rebid_no="1"),
    _identity(bid_notice_no="SYN-OTHER"), _identity(revision_no="1"),
])
def test_missing_or_different_opening_participation_never_creates_loss(identity_client, identity):
    _participation(identity_client, identity)
    result = _refresh(identity_client)
    assert result["items"][0]["result"] == "REVIEW"
    assert result["items"][0]["outcome_status"] is None
    assert _automatic_rows(identity_client) == []


@pytest.mark.parametrize("field", ["rebid_no", "classification_no"])
def test_different_openings_are_separate_idempotent_observations(identity_client, monkeypatch, field):
    _participation(identity_client, _identity())
    first = _refresh(identity_client)
    assert first["created"] == 1
    assert _refresh(identity_client)["unchanged"] == 1
    _participation(identity_client, _identity(**{field: "3"}), key="SYN-next-submission")
    monkeypatch.setattr(_FeedbackClient, "rows", [_row(**{field: "3", "award_amount": 80_000})])
    second = _refresh(identity_client)
    assert second["created"] == 1
    assert _refresh(identity_client)["unchanged"] == 1
    outcomes = _automatic_rows(identity_client)
    assert len(outcomes) == 2
    assert len({row["key"] for row in outcomes}) == 2
    assert {row["amount"] for row in outcomes} == {90_000, 80_000}


def test_legacy_notice_only_result_is_preserved_when_new_opening_is_recorded(identity_client):
    _participation(identity_client, _identity())
    old_key = "pps-final-award:" + hashlib.sha256(
        f"PPS|{NOTICE_NO}|0|FINAL_AWARD".encode()).hexdigest()[:40]
    with identity_client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        old = BidOutcome(
            notice_id=notice.id, outcome_key=old_key, status="LOST",
            source="PPS_AUTO_FEEDBACK", evidence_json={"SYN-legacy": "preserve"},
            winner_name="SYN 기존 관측", winning_bid_amount=70_000,
            observed_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        session.add(old)
        session.commit()
        old_id = old.id
    assert _refresh(identity_client)["created"] == 1
    outcomes = _automatic_rows(identity_client)
    legacy = next(row for row in outcomes if row["id"] == old_id)
    assert legacy["key"] == old_key
    assert legacy["amount"] == 70_000
    assert legacy["evidence"] == {"SYN-legacy": "preserve"}
    assert len(outcomes) == 2


def test_mixed_openings_do_not_select_an_old_company_win(identity_client, monkeypatch):
    monkeypatch.setattr(_FeedbackClient, "rows", [
        _row(rebid_no="1", company_business_number_match=True), _row(),
    ])
    result = _refresh(identity_client)
    assert result["items"][0]["result"] == "REVIEW"
    assert _automatic_rows(identity_client) == []


@pytest.mark.parametrize("field", ["bidNtceOrd", "bidClsfcNo", "rbidNo"])
def test_missing_provider_component_is_not_invented_as_zero(identity_client, monkeypatch, field):
    from pai_loop.integrations.outcome_feedback import _normalise_outcome_row

    raw = {
        "bidNtceNo": NOTICE_NO, "bidNtceOrd": "000", "bidClsfcNo": "000",
        "rbidNo": "000", "bidwinnrNm": "SYN 다른 낙찰사",
        "bidwinnrBizno": "0000000001",
    }
    raw.pop(field)
    projected = _normalise_outcome_row(raw, company_business_number="0000000000")
    _participation(identity_client, _identity(classification_no="0", rebid_no="0"))
    monkeypatch.setattr(_FeedbackClient, "rows", [projected])
    result = _refresh(identity_client)
    assert result["items"][0]["reason_code"] == "PPS_OPENING_IDENTITY_MISSING"
    assert _automatic_rows(identity_client) == []


def test_adapter_opening_identity_must_agree_with_projected_fields(identity_client, monkeypatch):
    _participation(identity_client, _identity())
    monkeypatch.setattr(_FeedbackClient, "rows", [_row(opening_identity=_identity(rebid_no="8"))])
    assert _refresh(identity_client)["items"][0]["reason_code"] == "PPS_OPENING_IDENTITY_MISMATCH"
    assert _automatic_rows(identity_client) == []


def test_cancelled_submission_with_old_bid_amount_is_not_participation(identity_client):
    _participation(identity_client, _identity(), status="CANCELLED")
    assert _refresh(identity_client)["items"][0]["result"] == "REVIEW"
    assert _automatic_rows(identity_client) == []


def _submission_payload(**changes):
    return {
        "notice_key": NOTICE_KEY, "idempotency_key": "SYN-opening-manual-request",
        "record_status": "VALIDATED", "status": "SUBMITTED",
        "submitted_bid_amount": 95_000, "source_reference": "SYN 동일 회차 제출 확인",
        "opening_identity": _identity(revision_no="000", classification_no="001", rebid_no="002"),
        **changes,
    }


def test_manual_identity_normalizes_and_survives_idempotency_and_cas_update(identity_client):
    payload = _submission_payload()
    created = identity_client.post("/api/v1/result-learning", json=payload)
    assert created.status_code == 201, created.text
    original = created.json()["outcome"]
    assert original["opening_identity"] == _identity()
    repeated = identity_client.post("/api/v1/result-learning", json=payload)
    assert repeated.status_code == 201
    assert repeated.json()["created"] is False
    collision = identity_client.post("/api/v1/result-learning", json={
        **payload, "opening_identity": _identity(rebid_no="3"),
    })
    assert collision.status_code == 409
    patched = identity_client.patch(f"/api/v1/result-learning/{original['id']}", json={
        "expected_updated_at": original["updated_at"],
        "opening_identity": _identity(rebid_no="3"),
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["outcome"]["opening_identity"] == _identity(rebid_no="3")
    stale = identity_client.patch(f"/api/v1/result-learning/{original['id']}", json={
        "expected_updated_at": original["updated_at"], "opening_identity": _identity(),
    })
    assert stale.status_code == 409
    assert _refresh(identity_client)["items"][0]["result"] == "REVIEW"


@pytest.mark.parametrize("identity", [
    _identity(bid_notice_no="SYN-OTHER"), _identity(revision_no="1"),
    {key: value for key, value in _identity().items() if key != "rebid_no"},
    _identity(rebid_no=""), _identity(classification_no="unknown"),
])
def test_manual_identity_rejects_incomplete_or_different_notice(identity_client, identity):
    response = identity_client.post("/api/v1/result-learning", json=_submission_payload(opening_identity=identity))
    assert response.status_code == 422


def test_confirmed_loss_exposes_identity_without_claiming_automation_was_human_reviewed(identity_client):
    response = identity_client.post("/api/v1/result-learning", json=_submission_payload())
    assert response.status_code == 201
    assert _refresh(identity_client)["items"][0]["outcome_status"] == "LOST"
    auto = _automatic_rows(identity_client)[0]
    assert auto["evidence"]["opening_identity"] == _identity()
    basis = auto["evidence"]["participation_basis"]
    assert basis["opening_identity"] == _identity()
    assert basis["outcome_id"] == response.json()["outcome"]["id"]
    assert basis["human_reviewed"] is True  # Refers to the actual manual submission.
    assert "_workflow" not in auto["evidence"]
    assert "human_reviewed" not in auto["evidence"]
    listing = identity_client.get("/api/v1/result-learning", params={"q": NOTICE_KEY}).json()
    assert listing["records"][0]["latest_outcome"]["record_status"] == "VALIDATED"
    assert listing["records"][0]["latest_outcome"]["opening_identity"] == _identity()


@pytest.mark.parametrize("workflow", [None, {"record_status": "VALIDATED", "human_reviewed": True}])
def test_unbound_legacy_loss_is_visible_for_review_without_rewriting_it(identity_client, workflow):
    from pai_loop.result_learning import _workflow

    with identity_client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        evidence = {
            "exact_match": {"verified": True},
            "participation_basis": {"record_status": "VALIDATED", "human_reviewed": True},
        }
        if workflow is not None:
            evidence["_workflow"] = workflow
        legacy = BidOutcome(
            notice_id=notice.id, outcome_key="SYN-unbound-legacy", status="LOST",
            source="PPS_AUTO_FEEDBACK", evidence_json=evidence,
        )
        session.add(legacy)
        session.commit()
        assert _workflow(legacy)["record_status"] == "DRAFT"
        session.refresh(legacy)
        assert legacy.evidence_json == evidence
        assert legacy.status == "LOST"


def test_zero_padding_does_not_create_a_second_opening_event(identity_client, monkeypatch):
    _participation(identity_client, _identity())
    assert _refresh(identity_client)["created"] == 1
    first = _automatic_rows(identity_client)[0]
    monkeypatch.setattr(_FeedbackClient, "rows", [_row(
        revision_no="000", classification_no="001", rebid_no="002",
    )])
    assert _refresh(identity_client)["created"] == 0
    rows = _automatic_rows(identity_client)
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["key"] == first["key"]


def test_automatic_refresh_cannot_overwrite_a_manual_owned_collision(identity_client, monkeypatch):
    _participation(identity_client, _identity())
    assert _refresh(identity_client)["created"] == 1
    auto = _automatic_rows(identity_client)[0]
    with identity_client.app.state.session_factory() as session:
        item = session.get(BidOutcome, auto["id"])
        item.source = "MANUAL_UI"
        session.commit()
    monkeypatch.setattr(_FeedbackClient, "rows", [_row(award_amount=1)])
    response = identity_client.post(REFRESH_URL, json={"notice_keys": [NOTICE_KEY]})
    assert response.status_code == 409
    with identity_client.app.state.session_factory() as session:
        item = session.get(BidOutcome, auto["id"])
        assert item.source == "MANUAL_UI"
        assert item.winning_bid_amount == 90_000
        assert item.evidence_json == auto["evidence"]
