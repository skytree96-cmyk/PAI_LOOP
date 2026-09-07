from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.integrations.outcome_feedback import PpsOutcomeFeedbackClient
from pai_loop.main import create_app
from pai_loop.models import BidOutcome, Notice


OWN = "0000000000"
WINNER = "0000000001"
KEY = "PPS-SYN-EXACT-PARTICIPATION"
IDENTITY = {"bid_notice_no": "SYN-EXACT-PARTICIPATION", "revision_no": "0", "classification_no": "1", "rebid_no": "2"}
RAW_IDENTITY = {"bidNtceNo": IDENTITY["bid_notice_no"], "bidNtceOrd": "000", "bidClsfcNo": "001", "rbidNo": "002"}
URL = "/api/v1/outcome-feedback/pps/refresh"


def participant(number, **changes):
    return {**RAW_IDENTITY, "prcbdrNm": f"SYN 업체 {number[-2:]}", "prcbdrBizno": number,
            "bidprcAmt": "86,130,000", "opengRank": "2", "prcbdrCeoNm": "SYN-DO-NOT-RETAIN", **changes}


@pytest.fixture()
def feed(monkeypatch):
    monkeypatch.setenv("PPS_API_KEY", "SYN-provider-key")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "SYN-server-key")
    monkeypatch.setattr("pai_loop.outcome_feedback.DEFAULT_COMPANY_BUSINESS_NUMBER", OWN)
    data = SimpleNamespace(calls=[], participants=[participant(OWN), participant(WINNER)], total=None, hook=None)
    data.final = {**RAW_IDENTITY, "bidNtceNm": "SYN 정확한 최종 결과", "bidwinnrNm": "SYN 낙찰사",
                  "bidwinnrBizno": WINNER, "sucsfbidAmt": "90000000", "prtcptCnum": "2",
                  "rlOpengDt": "202501151100", "fnlSucsfDate": "20250116"}
    data.final_rows = None

    def handler(request):
        data.calls.append(request)
        page, size = int(request.url.params["pageNo"]), int(request.url.params["numOfRows"])
        if request.url.path.endswith("getOpengResultListInfoOpengCompt"):
            assert all(request.url.params[field] in (value, str(int(value))) if field != "bidNtceNo"
                       else request.url.params[field] == value for field, value in RAW_IDENTITY.items())
            if data.hook:
                callback, data.hook = data.hook, None
                callback()
            total = data.total if data.total is not None else len(data.participants)
            rows = deepcopy(data.participants[(page - 1) * size:page * size])
        else:
            assert request.url.path.endswith("getScsbidListSttusServcPPSSrch")
            rows = deepcopy(data.final_rows if data.final_rows is not None else [data.final])
            total = len(rows)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"},
            "body": {"items": rows, "totalCount": total, "pageNo": page, "numOfRows": size}}})

    monkeypatch.setattr("pai_loop.outcome_feedback.PpsOutcomeFeedbackClient", lambda **kwargs:
                        PpsOutcomeFeedbackClient(**kwargs, transport=httpx.MockTransport(handler)))
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app, headers={"X-PAI-LOOP-API-KEY": "SYN-server-key"}) as client:
        with app.state.session_factory() as session:
            session.add(Notice(notice_key=KEY, bid_notice_no=IDENTITY["bid_notice_no"], revision_no="00",
                title="SYN 실제 참여 확인", agency="SYN 기관", status="CLOSED",
                deadline=datetime(2025, 1, 15, tzinfo=timezone.utc), published_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
            session.commit()
        data.client = client
        yield data


def refresh(feed, **changes):
    response = feed.client.post(URL, json={"notice_keys": [KEY], "include_participation": True, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def stored(feed):
    with feed.client.app.state.session_factory() as session:
        return [dict(id=row.id, status=row.status, department_id=row.department_id, account_id=row.account_id,
                     decision_id=row.decision_id, amount=row.submitted_bid_amount, evidence=row.evidence_json)
                for row in session.scalars(select(BidOutcome).where(BidOutcome.source == "PPS_AUTO_FEEDBACK"))]


@pytest.mark.parametrize("won", [False, True])
def test_real_adapter_records_exact_participant_without_manual_submission_and_keeps_proof_private(feed, won):
    if won:
        feed.final["bidwinnrBizno"] = OWN
    result = refresh(feed)
    assert result["items"][0]["outcome_status"] == ("WON" if won else "LOST")
    assert result["api_calls"] == result["items"][0]["api_calls"] == len(feed.calls) == 2
    row = stored(feed)[0]
    assert row["amount"] == 86_130_000
    assert row["department_id"] is row["account_id"] is row["decision_id"] is None
    proof = row["evidence"]["participation_basis"]
    assert proof["kind"] == "PROVIDER_PARTICIPANT_EXACT" and proof["opening_identity"] == IDENTITY
    assert proof["company_identifier_match"] is True and proof["complete"] is True
    assert all(value not in str(row) for value in (OWN, WINNER, "SYN-DO-NOT-RETAIN", "SYN-provider-key", "prcbdrBizno"))
    visible = feed.client.get("/api/v1/result-learning").json()["records"][0]["latest_outcome"]
    assert visible["record_status"] == "VALIDATED" and visible["participation_verified"] is True
    assert "evidence_json" not in visible and "participation_basis" not in visible
    assert refresh(feed)["items"][0]["result"] == "UNCHANGED"
    assert len(stored(feed)) == 1


@pytest.mark.parametrize("failure", ["absent", "masked", "missing_id", "duplicate", "short_page", "winner_absent", "different_rebid", "missing_rebid", "blank_revision", "mixed_notice", "count_conflict", "final_duplicate", "final_missing_id"])
def test_incomplete_or_inconsistent_provider_sets_never_become_won_lost_or_no_bid(feed, failure):
    if failure == "absent": feed.participants[0] = participant("0000000002")
    elif failure == "masked": feed.participants[0]["prcbdrBizno"] = "000-**-*****"
    elif failure == "missing_id": feed.participants[0].pop("prcbdrBizno")
    elif failure == "duplicate": feed.participants[1] = participant("000-00-00000")
    elif failure == "short_page": feed.total = 3
    elif failure == "winner_absent": feed.participants[1] = participant("0000000002")
    elif failure == "different_rebid": feed.participants[0]["rbidNo"] = "3"
    elif failure == "missing_rebid": feed.participants[0].pop("rbidNo")
    elif failure == "blank_revision": feed.participants[0]["bidNtceOrd"] = ""
    elif failure == "mixed_notice": feed.participants[0]["bidNtceNo"] = "SYN-OTHER"
    elif failure == "count_conflict": feed.final["prtcptCnum"] = "7"
    elif failure == "final_duplicate": feed.final_rows = [feed.final, feed.final]
    elif failure == "final_missing_id": feed.final.pop("bidwinnrBizno")
    result = refresh(feed)
    assert result["items"][0]["result"] == "REVIEW"
    assert result["items"][0]["outcome_status"] is None
    assert stored(feed) == []


def test_second_participant_page_is_bounded_and_counts_with_final_award_request(feed):
    feed.participants = [participant(str(number).zfill(10)) for number in range(101)]
    feed.final["prtcptCnum"] = "101"
    assert refresh(feed)["items"][0]["result"] == "REVIEW"
    assert stored(feed) == []
    feed.calls.clear()
    result = refresh(feed, participation_max_pages=2)
    assert result["items"][0]["outcome_status"] == "LOST"
    assert result["api_calls"] == result["items"][0]["api_calls"] == 3
    assert [request.url.params["pageNo"] for request in feed.calls] == ["1", "1", "2"]
    assert feed.client.post(URL, json={"participation_max_pages": 3}).status_code == 422


@pytest.mark.parametrize("status,record_status", [("NO_BID", "VALIDATED"), ("CANCELLED", "VALIDATED"), ("SUBMITTED", "DRAFT")])
def test_human_correction_during_provider_fetch_is_rechecked_before_write(feed, status, record_status):
    def correction():
        response = feed.client.post("/api/v1/result-learning", json={
            "notice_key": KEY, "idempotency_key": "SYN-later-human-correction", "status": status,
            "record_status": record_status, "opening_identity": IDENTITY, "source_reference": "SYN human source",
        })
        assert response.status_code == 201, response.text
    feed.hook = correction
    assert refresh(feed)["items"][0]["reason_code"] == "HUMAN_PARTICIPATION_CONFLICT"
    assert stored(feed) == []
    assert feed.client.get("/api/v1/result-learning").json()["records"][0]["latest_outcome"]["status"] == status


def test_late_older_provider_response_cannot_replace_newer_completed_result(feed):
    def newer_refresh():
        feed.final["sucsfbidAmt"] = "95000000"
        assert refresh(feed)["items"][0]["result"] == "CREATED"
    feed.hook = newer_refresh
    assert refresh(feed)["items"][0]["reason_code"] == "NEWER_RESULT_PRESERVED"
    assert len(stored(feed)) == 1
    visible = feed.client.get("/api/v1/result-learning").json()["records"][0]["latest_outcome"]
    assert visible["winning_bid_amount"] == 95_000_000


def test_review_copy_inherits_identity_without_owning_provider_proof_and_retains_cas(feed):
    refresh(feed)
    original = stored(feed)[0]
    response = feed.client.post("/api/v1/result-learning", json={
        "notice_key": KEY, "idempotency_key": "SYN-review-provider-proof", "basis_outcome_id": original["id"],
        "status": "LOST", "record_status": "VALIDATED", "loss_reason": "SYN 담당자 결과 확인", "source_reference": "SYN human source",
    })
    assert response.status_code == 201, response.text
    review = response.json()["outcome"]
    assert review["opening_identity"] == IDENTITY and review["participation_verified"] is False
    assert review["source"] == "MANUAL_UI"
    update = {"expected_updated_at": review["updated_at"], "operator_note": "SYN 수정"}
    assert feed.client.patch(f"/api/v1/result-learning/{review['id']}", json=update).status_code == 200
    assert feed.client.patch(f"/api/v1/result-learning/{review['id']}", json=update).status_code == 409
    feed.final["sucsfbidAmt"] = "95000000"
    assert refresh(feed)["items"][0]["result"] == "UPDATED"
    assert len(stored(feed)[0]["evidence"]["_provider_history"]) == 1
    assert stored(feed)[0]["evidence"]["_provider_history"][0]["evidence"] == original["evidence"]
    assert refresh(feed)["items"][0]["result"] == "UNCHANGED"


def test_provider_proof_cannot_be_supplied_through_result_payload_and_option_is_off_by_default(feed):
    base = {"notice_key": KEY, "idempotency_key": "SYN-forged-proof-attempt", "status": "LOST"}
    for forged in ({"participation_verified": True}, {"evidence_json": {"participation_basis": {"kind": "PROVIDER_PARTICIPANT_EXACT"}}}, {"source": "PPS_AUTO_FEEDBACK"}):
        assert feed.client.post("/api/v1/result-learning", json={**base, **forged}).status_code == 422
    result = feed.client.post(URL, json={"notice_keys": [KEY]}).json()
    assert result["items"][0]["reason_code"] == "PARTICIPATION_OPENING_NOT_CONFIRMED"
    assert len(feed.calls) == 1
    assert stored(feed) == []


def test_dry_run_reads_participants_without_persisting_an_outcome(feed):
    assert refresh(feed, dry_run=True)["items"][0]["result"] == "DRY_RUN_CREATE"
    assert len(feed.calls) == 2 and stored(feed) == []


def test_legacy_refresh_cannot_erase_newer_exact_participation_proof(feed):
    feed.final["bidwinnrBizno"] = OWN
    refresh(feed)
    before = stored(feed)
    feed.final["sucsfbidAmt"] = "95000000"
    assert refresh(feed, include_participation=False)["items"][0]["result"] == "UNCHANGED"
    assert stored(feed) == before


def test_notice_cancelled_while_reading_provider_is_not_written(feed):
    def cancel():
        with feed.client.app.state.session_factory() as session:
            notice = session.scalar(select(Notice).where(Notice.notice_key == KEY))
            notice.status = "CANCELLED"
            session.commit()
    feed.hook = cancel
    assert refresh(feed)["items"][0]["reason_code"] == "NOTICE_CANCELLED"
    assert stored(feed) == []


def test_existing_server_namespace_rejects_forged_provider_proof(feed):
    response = feed.client.post(f"/api/v1/notices/{KEY}/outcomes", json={
        "status": "LOST", "source": "PPS_AUTO_FEEDBACK", "outcome_key": "pps-final-award:SYN-forgery",
        "evidence_json": {"participation_basis": {"kind": "PROVIDER_PARTICIPANT_EXACT"}},
    })
    assert response.status_code == 409
    assert stored(feed) == []


@pytest.mark.parametrize("won", [False, True])
def test_invalid_server_proof_cannot_claim_verified_even_with_a_legacy_workflow_flag(feed, won):
    if won: feed.final["bidwinnrBizno"] = OWN
    refresh(feed)
    with feed.client.app.state.session_factory() as session:
        row = session.scalar(select(BidOutcome).where(BidOutcome.source == "PPS_AUTO_FEEDBACK"))
        evidence = deepcopy(row.evidence_json)
        evidence["participation_basis"]["opening_identity"]["rebid_no"] = "9"
        evidence["_workflow"] = {"record_status": "VALIDATED", "human_reviewed": True}
        row.evidence_json = evidence
        session.commit()
    visible = feed.client.get("/api/v1/result-learning").json()["records"][0]["latest_outcome"]
    assert visible["record_status"] == "DRAFT" and visible["participation_verified"] is False


def test_latest_edit_of_an_older_human_record_is_not_hidden_by_another_submission(feed):
    payload = {"notice_key": KEY, "status": "SUBMITTED", "record_status": "VALIDATED",
               "submitted_bid_amount": 86_130_000, "opening_identity": IDENTITY, "source_reference": "SYN 제출 확인"}
    old = feed.client.post("/api/v1/result-learning", json={**payload, "idempotency_key": "SYN-first-submission"}).json()["outcome"]
    assert feed.client.post("/api/v1/result-learning", json={**payload, "idempotency_key": "SYN-second-submission"}).status_code == 201
    corrected = feed.client.patch(f"/api/v1/result-learning/{old['id']}", json={
        "expected_updated_at": old["updated_at"], "status": "NO_BID", "submitted_bid_amount": None,
    })
    assert corrected.status_code == 200, corrected.text
    assert refresh(feed)["items"][0]["reason_code"] == "HUMAN_PARTICIPATION_CONFLICT"
    assert stored(feed) == []
