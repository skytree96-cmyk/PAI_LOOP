from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from conftest import internal_server_client
from sqlalchemy import select

from pai_loop.integrations.outcome_feedback import (
    ExactNoticeAwardFetch,
    PpsOutcomeFeedbackClient,
    canonical_pps_revision,
)
from pai_loop.integrations.pps import PpsApiError
from pai_loop.main import create_app
from pai_loop.models import BidOutcome, IngestionJob, Notice, PpsNoticeAuthority
from pai_loop.outcome_feedback import _select_provider_result


SERVER_HEADERS = {"X-PAI-LOOP-API-KEY": "server-only-secret"}
_PUBLIC_COMPANY_BUSINESS_NUMBER = "105\u002d82\u002d01810"


def _provider_row(
    *,
    bid_notice_no: str = "20250101001",
    revision_no: str = "000",
    winner_name: str = "사단법인 한국능률협회",
    winner_business_number: str = _PUBLIC_COMPANY_BUSINESS_NUMBER,
) -> dict[str, object]:
    return {
        "bidNtceNo": bid_notice_no,
        "bidNtceOrd": revision_no,
        "bidClsfcNo": "0",
        "rbidNo": "000",
        "bidNtceNm": "정확한 낙찰 결과",
        "bidwinnrNm": winner_name,
        "bidwinnrBizno": winner_business_number,
        "prtcptCnum": "7",
        "sucsfbidAmt": "98,000,000",
        "sucsfbidRate": "88.125",
        "rlOpengDt": "202501151100",
        "rgstDt": "202501161000",
        "fnlSucsfDate": "20250116",
        "dminsttNm": "공개 발주기관",
        "opengRgstNo": "must-not-leak",
        "bidwinnrTelNo": "must-not-leak",
    }


def _envelope(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "OK"},
            "body": {"totalCount": len(items), "items": items},
        }
    }


def test_pps_outcome_adapter_post_filters_exact_identity_and_drops_identifiers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["bidNtceNo"] == "20250101001"
        assert request.url.params["inqryDiv"] == "3"
        return httpx.Response(
            200,
            json=_envelope(
                [
                    _provider_row(),
                    _provider_row(bid_notice_no="OTHER-NOTICE"),
                    _provider_row(revision_no="001"),
                ]
            ),
        )

    with PpsOutcomeFeedbackClient(
        service_key="server-secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        fetched = client.fetch_exact_notice_awards(
            bid_notice_no="20250101001",
            revision_no="00",
            start=date(2025, 1, 1),
            end=date(2025, 1, 2),
            company_business_number="1058201810",
        )

    assert fetched.fetched_count == 3
    assert fetched.mismatched_count == 2
    assert len(fetched.rows) == 1
    assert fetched.rows[0]["company_business_number_match"] is True
    serialised = str(fetched.rows)
    assert _PUBLIC_COMPANY_BUSINESS_NUMBER not in serialised
    assert "must-not-leak" not in serialised
    assert "server-secret" not in serialised


def test_pps_outcome_adapter_reports_quarantine_page_and_time_bounds() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        row = _provider_row(winner_business_number="invalid-business-number")
        row["bidwinnrNm"] = ""
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "OK"},
                    "body": {"totalCount": 2, "items": [row]},
                }
            },
        )

    assert canonical_pps_revision(" 00 ") == "0"
    assert canonical_pps_revision(" A  1 ") == "a 1"
    with PpsOutcomeFeedbackClient(
        service_key="server-secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="bid_notice_no"):
            client.fetch_exact_notice_awards(
                bid_notice_no=" ", revision_no="00", start=date(2025, 1, 1),
                end=date(2025, 1, 1), company_business_number="1058201810",
            )
        with pytest.raises(ValueError, match="rows"):
            client.fetch_exact_notice_awards(
                bid_notice_no="20250101001", revision_no="00", start=date(2025, 1, 1),
                end=date(2025, 1, 1), company_business_number="1058201810", rows=0,
            )
        with pytest.raises(ValueError, match="max_pages_per_window"):
            client.fetch_exact_notice_awards(
                bid_notice_no="20250101001", revision_no="00", start=date(2025, 1, 1),
                end=date(2025, 1, 1), company_business_number="1058201810",
                max_pages_per_window=0,
            )
        page_limited = client.fetch_exact_notice_awards(
            bid_notice_no="20250101001",
            revision_no="00",
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            company_business_number="1058201810",
            rows=1,
            max_pages_per_window=1,
        )
        time_limited = client.fetch_exact_notice_awards(
            bid_notice_no="20250101001",
            revision_no="00",
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            company_business_number="1058201810",
            deadline_monotonic=0,
        )

    assert calls == 1
    assert page_limited.hit_page_limit is True
    assert page_limited.quarantined_count == 1
    assert page_limited.rows == []
    assert time_limited.hit_time_limit is True
    assert time_limited.api_calls == 0


def _exact_fetch(
    *,
    bid_notice_no: str,
    revision_no: str = "000",
    company_match: bool | None = True,
    winner_name: str = "사단법인 한국능률협회",
) -> ExactNoticeAwardFetch:
    row = {
        "identity": f"{bid_notice_no}|{revision_no}|0|000",
        "bid_notice_no": bid_notice_no,
        "revision_no": revision_no,
        "classification_no": "0",
        "rebid_no": "000",
        "title": "정확한 낙찰 결과",
        "participant_count": 7,
        "winner_name": winner_name,
        "award_amount": 98_000_000.0,
        "award_rate": 88.125,
        "opened_at": datetime(2025, 1, 15, 2, tzinfo=timezone.utc),
        "registered_at": datetime(2025, 1, 16, 1, tzinfo=timezone.utc),
        "awarded_at": datetime(2025, 1, 15, 15, tzinfo=timezone.utc),
        "agency": "공개 발주기관",
        "company_business_number_match": company_match,
        "company_business_number_status": (
            "ABSENT" if company_match is None else "PRESENT_VALID"
        ),
        "provider_result_sha256": "a" * 64,
    }
    return ExactNoticeAwardFetch(
        rows=[row],
        fetched_count=1,
        mismatched_count=0,
        quarantined_count=0,
        api_calls=1,
        hit_page_limit=False,
        hit_time_limit=False,
    )


def test_present_but_invalid_business_number_never_uses_name_fallback() -> None:
    fetched = _exact_fetch(
        bid_notice_no="20250101099",
        company_match=None,
        winner_name="사단법인 한국능률협회",
    )
    fetched.rows[0]["company_business_number_status"] = "PRESENT_INVALID"
    selected, company_won, reason = _select_provider_result(fetched.rows)
    assert selected is None
    assert company_won is False
    assert reason == "COMPANY_IDENTITY_INVALID"


class _WinningFeedbackClient:
    def __init__(self, **_kwargs: object) -> None:
        self.request_count = 0

    def __enter__(self) -> "_WinningFeedbackClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def fetch_exact_notice_awards(self, **kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        return _exact_fetch(bid_notice_no=str(kwargs["bid_notice_no"]))


def _create_ended_pps_notice(
    client: TestClient,
    *,
    notice_key: str,
    bid_notice_no: str,
    revision_no: str = "00",
    status: str = "CLOSED",
    deadline: str = "2025-01-15T17:00:00+09:00",
    headers: dict[str, str] | None = None,
) -> None:
    response = client.post(
        "/api/v1/notices",
        headers=headers,
        json={
            "notice_key": notice_key,
            "bid_notice_no": bid_notice_no,
            "revision_no": revision_no,
            "title": "자동 환류 대상 용역",
            "agency": "공개 발주기관",
            "published_at": "2025-01-01T09:00:00+09:00",
            "deadline": deadline,
            "status": status,
        },
    )
    assert response.status_code == 201, response.text


def test_exact_company_win_is_idempotently_fed_back_without_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _WinningFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-WIN",
            bid_notice_no="20250101001",
        )
        first = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-WIN"]},
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "COMPLETED"
        assert first.json()["created"] == 1
        assert first.json()["openai_calls"] == 0
        assert first.json()["items"][0]["outcome_status"] == "WON"

        second = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-WIN"]},
        )
        assert second.status_code == 200
        assert second.json()["unchanged"] == 1

        outcomes = client.get("/api/v1/notices/PPS-OUTCOME-WIN/outcomes")
        assert outcomes.status_code == 200
        assert len(outcomes.json()) == 1
        outcome = outcomes.json()[0]
        assert outcome["status"] == "WON"
        assert outcome["source"] == "PPS_AUTO_FEEDBACK"
        assert outcome["winning_bid_amount"] == 98_000_000
        assert outcome["evidence_json"]["exact_match"]["verified"] is True
        assert outcome["evidence_json"]["company_match_basis"] == "BUSINESS_NUMBER_EXACT"
        assert "1058201810" not in str(outcome)

        jobs = client.get("/api/v1/ingestion/jobs").json()
        audit = next(item for item in jobs if item["source"] == "PPS_OUTCOME")
        assert audit["status"] == "COMPLETED"
        assert audit["api_calls"] == 1


class _OtherWinnerFeedbackClient(_WinningFeedbackClient):
    def fetch_exact_notice_awards(self, **kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        return _exact_fetch(
            bid_notice_no=str(kwargs["bid_notice_no"]),
            company_match=False,
            winner_name="다른 공개 낙찰사",
        )


class _IdentityBoundaryFeedbackClient(_WinningFeedbackClient):
    def fetch_exact_notice_awards(self, **kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        notice_no = str(kwargs["bid_notice_no"])
        if notice_no.endswith("21"):
            return _exact_fetch(
                bid_notice_no=notice_no,
                company_match=False,
                winner_name="사단법인 한국능률협회",
            )
        return _exact_fetch(
            bid_notice_no=notice_no,
            company_match=None,
            winner_name=" 사단법인  한국능률협회 ",
        )


def test_company_identity_conflict_is_reviewed_and_name_fallback_can_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _IdentityBoundaryFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-CONFLICT",
            bid_notice_no="20250101021",
        )
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-NAME-FALLBACK",
            bid_notice_no="20250101022",
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={
                "notice_keys": [
                    "PPS-OUTCOME-CONFLICT",
                    "PPS-OUTCOME-NAME-FALLBACK",
                ]
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["review"] == 1
        assert body["created"] == 1
        by_key = {item["notice_key"]: item for item in body["items"]}
        assert by_key["PPS-OUTCOME-CONFLICT"]["reason_code"] == "COMPANY_IDENTITY_CONFLICT"
        assert by_key["PPS-OUTCOME-NAME-FALLBACK"]["outcome_status"] == "WON"
        outcome = client.get(
            "/api/v1/notices/PPS-OUTCOME-NAME-FALLBACK/outcomes"
        ).json()[0]
        assert outcome["evidence_json"]["company_match_basis"] == "COMPANY_NAME_EXACT"


def test_other_winner_becomes_lost_only_after_stored_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _OtherWinnerFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-LOSS",
            bid_notice_no="20250101002",
        )
        unknown = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-LOSS"]},
        )
        assert unknown.status_code == 200
        assert unknown.json()["review"] == 1
        assert unknown.json()["items"][0]["reason_code"] == "PARTICIPATION_OPENING_NOT_CONFIRMED"
        assert client.get("/api/v1/notices/PPS-OUTCOME-LOSS/outcomes").json() == []

        draft = client.post(
            "/api/v1/result-learning",
            json={
                "notice_key": "PPS-OUTCOME-LOSS",
                "idempotency_key": "draft-submission-does-not-prove-participation",
                "record_status": "DRAFT",
                "status": "SUBMITTED",
                "submitted_bid_amount": 97_000_000,
            },
        )
        assert draft.status_code == 201
        still_unknown = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-LOSS"]},
        )
        assert still_unknown.status_code == 200
        assert still_unknown.json()["items"][0]["reason_code"] == "PARTICIPATION_OPENING_NOT_CONFIRMED"

        submitted = client.post(
            "/api/v1/result-learning",
            json={
                "notice_key": "PPS-OUTCOME-LOSS",
                "idempotency_key": "validated-submission-proves-participation",
                "record_status": "VALIDATED",
                "status": "SUBMITTED",
                "submitted_bid_amount": 97_000_000,
                "source_reference": "담당자 검증 투찰 기록",
                "opening_identity": {
                    "bid_notice_no": "20250101002", "revision_no": "0",
                    "classification_no": "0", "rebid_no": "0",
                },
            },
        )
        assert submitted.status_code == 201
        submission_key = submitted.json()["outcome"]["outcome_key"]
        refreshed = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-LOSS"]},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["created"] == 1
        assert refreshed.json()["items"][0]["outcome_status"] == "LOST"
        outcomes = client.get("/api/v1/notices/PPS-OUTCOME-LOSS/outcomes").json()
        automatic = next(item for item in outcomes if item["source"] == "PPS_AUTO_FEEDBACK")
        assert automatic["status"] == "LOST"
        assert (
            automatic["evidence_json"]["participation_basis"]["outcome_key"]
            == submission_key
        )
        assert automatic["evidence_json"]["participation_basis"]["record_status"] == "VALIDATED"
        assert automatic["evidence_json"]["participation_basis"]["human_reviewed"] is True
        learning = client.get(
            "/api/v1/result-learning",
            params={"scope": "WITH_OUTCOME", "q": "PPS-OUTCOME-LOSS"},
        ).json()
        assert learning["records"][0]["latest_outcome"]["record_status"] == "VALIDATED"


class _MismatchingFeedbackClient(_WinningFeedbackClient):
    def fetch_exact_notice_awards(self, **kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        return _exact_fetch(
            bid_notice_no=str(kwargs["bid_notice_no"]),
            revision_no="999",
        )


def test_service_boundary_rejects_adapter_revision_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _MismatchingFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-MISMATCH",
            bid_notice_no="20250101003",
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-MISMATCH"]},
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["result"] == "NO_RESULT"
        assert "PROVIDER_IDENTITY_MISMATCH_DROPPED" in response.json()["items"][0]["warnings"]
        assert client.get("/api/v1/notices/PPS-OUTCOME-MISMATCH/outcomes").json() == []


def test_dry_run_reports_would_create_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _WinningFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-DRY-RUN",
            bid_notice_no="20250101007",
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-DRY-RUN"], "dry_run": True},
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["result"] == "DRY_RUN_CREATE"
        assert response.json()["created"] == 1
        assert client.get("/api/v1/notices/PPS-OUTCOME-DRY-RUN/outcomes").json() == []


class _MutableWinningFeedbackClient(_WinningFeedbackClient):
    award_amount = 98_000_000.0

    def fetch_exact_notice_awards(self, **kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        fetched = _exact_fetch(bid_notice_no=str(kwargs["bid_notice_no"]))
        fetched.rows[0]["award_amount"] = self.award_amount
        fetched.rows[0]["provider_result_sha256"] = (
            "a" if self.award_amount == 98_000_000 else "c"
        ) * 64
        return fetched


def test_dry_run_update_is_non_mutating_then_live_update_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _MutableWinningFeedbackClient,
    )
    monkeypatch.setattr(_MutableWinningFeedbackClient, "award_amount", 98_000_000.0)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-MUTABLE",
            bid_notice_no="20250101023",
        )
        created = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-MUTABLE"]},
        )
        assert created.json()["items"][0]["result"] == "CREATED"

        monkeypatch.setattr(_MutableWinningFeedbackClient, "award_amount", 99_000_000.0)
        preview = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-MUTABLE"], "dry_run": True},
        )
        assert preview.json()["items"][0]["result"] == "DRY_RUN_UPDATE"
        stored = client.get("/api/v1/notices/PPS-OUTCOME-MUTABLE/outcomes").json()
        assert stored[0]["winning_bid_amount"] == 98_000_000

        updated = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-MUTABLE"]},
        )
        assert updated.json()["items"][0]["result"] == "UPDATED"
        stored = client.get("/api/v1/notices/PPS-OUTCOME-MUTABLE/outcomes").json()
        assert stored[0]["winning_bid_amount"] == 99_000_000

        unchanged = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-MUTABLE"]},
        )
        assert unchanged.json()["items"][0]["result"] == "UNCHANGED"


def test_full_opening_key_preserves_legacy_schema_key_and_remains_schema_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _WinningFeedbackClient,
    )
    bid_notice_no = "20250101024"
    legacy_digest = hashlib.sha256(
        f"PPS|{bid_notice_no}|0|pai-loop-pps-outcome-feedback-1.0.0".encode()
    ).hexdigest()[:40]
    legacy_key = f"pps-final-award:{legacy_digest}"
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-LEGACY-KEY",
            bid_notice_no=bid_notice_no,
        )
        with client.app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(
                    Notice.notice_key == "PPS-OUTCOME-LEGACY-KEY"
                )
            )
            assert notice is not None
            session.add(
                BidOutcome(
                    notice_id=notice.id,
                    outcome_key=legacy_key,
                    status="WON",
                    source="PPS_AUTO_FEEDBACK",
                    winner_name="사단법인 한국능률협회",
                    winning_bid_amount=98_000_000,
                    observed_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

        migrated = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-LEGACY-KEY"]},
        )
        assert migrated.status_code == 200
        assert migrated.json()["items"][0]["result"] == "CREATED"
        stable_key = migrated.json()["items"][0]["outcome_key"]
        assert stable_key != legacy_key
        outcomes = client.get(
            "/api/v1/notices/PPS-OUTCOME-LEGACY-KEY/outcomes"
        ).json()
        assert len(outcomes) == 2
        legacy = next(item for item in outcomes if item["outcome_key"] == legacy_key)
        assert legacy["evidence_json"] == {}

        monkeypatch.setattr(
            "pai_loop.outcome_feedback.OUTCOME_FEEDBACK_SCHEMA",
            "pai-loop-pps-outcome-feedback-9.9.9",
        )
        schema_refresh = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-LEGACY-KEY"]},
        )
        assert schema_refresh.status_code == 200
        outcomes = client.get(
            "/api/v1/notices/PPS-OUTCOME-LEGACY-KEY/outcomes"
        ).json()
        assert len(outcomes) == 2
        assert next(item for item in outcomes if item["outcome_key"] == legacy_key) == legacy
        current = next(item for item in outcomes if item["outcome_key"] == stable_key)
        assert current["evidence_json"]["schema_version"].endswith("9.9.9")


def test_automatic_batches_rotate_past_recently_checked_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _WinningFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-ROTATE-A",
            bid_notice_no="20250101011",
        )
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-ROTATE-B",
            bid_notice_no="20250101012",
        )
        first = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"max_notices": 1},
        )
        second = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"max_notices": 1},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["items"][0]["notice_key"] != second.json()["items"][0]["notice_key"]
        assert first.json()["created"] == second.json()["created"] == 1


def test_automatic_rotation_is_oldest_first_across_the_twenty_four_hour_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _WinningFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    now = datetime.now(timezone.utc)
    keys = [f"PPS-OUTCOME-ROUND-{index:02d}" for index in range(12)]
    with internal_server_client(app) as client:
        for index, key in enumerate(keys):
            _create_ended_pps_notice(
                client,
                notice_key=key,
                bid_notice_no=f"20250102{index:03d}",
                deadline=(now - timedelta(days=30 + index)).isoformat(),
            )
        with app.state.session_factory() as session:
            checked_25_hours_ago = now - timedelta(hours=25)
            checked_49_hours_ago = now - timedelta(hours=49)
            session.add_all(
                [
                    IngestionJob(
                        source="PPS_OUTCOME",
                        mode="LIVE",
                        status="COMPLETED",
                        window_json={"from": "2025-01-01", "to": "2026-08-24"},
                        keyword=None,
                        request_json={},
                        notice_keys=keys[:10],
                        warnings=[],
                        created_at=checked_25_hours_ago,
                        completed_at=checked_25_hours_ago,
                    ),
                    IngestionJob(
                        source="PPS_OUTCOME",
                        mode="LIVE",
                        status="COMPLETED",
                        window_json={"from": "2025-01-01", "to": "2026-08-24"},
                        keyword=None,
                        request_json={},
                        notice_keys=keys[10:],
                        warnings=[],
                        created_at=checked_49_hours_ago,
                        completed_at=checked_49_hours_ago,
                    ),
                ]
            )
            session.commit()

        oldest = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"max_notices": 2},
        )
        assert oldest.status_code == 200
        assert [item["notice_key"] for item in oldest.json()["items"]] == keys[10:]

        next_oldest = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"max_notices": 2},
        )
        assert next_oldest.status_code == 200
        assert [item["notice_key"] for item in next_oldest.json()["items"]] == keys[:2]


class _MustNotRunClient:
    def __init__(self, **_kwargs: object) -> None:
        raise AssertionError("ineligible/public request must not construct PPS client")


class _NoFetchFeedbackClient(_WinningFeedbackClient):
    def fetch_exact_notice_awards(self, **_kwargs: object) -> ExactNoticeAwardFetch:
        raise AssertionError("ineligible notices must not call the PPS provider")


def test_non_pps_open_and_invalid_authority_notices_are_ineligible_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _NoFetchFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="MANUAL-NOT-PPS",
            bid_notice_no="20250101031",
        )
        future = client.post(
            "/api/v1/notices",
            json={
                "notice_key": "PPS-OUTCOME-FUTURE",
                "bid_notice_no": "20250101032",
                "revision_no": "00",
                "title": "아직 마감되지 않은 공고",
                "agency": "공개 발주기관",
                "published_at": "2026-01-01T09:00:00+09:00",
                "deadline": "2099-01-15T17:00:00+09:00",
                "status": "OPEN",
            },
        )
        assert future.status_code == 201
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-AUTH-CANCELLED",
            bid_notice_no="20250101033",
        )
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-AUTH-INVALID",
            bid_notice_no="20250101034",
        )
        with app.state.session_factory() as session:
            for number, disposition in [
                ("20250101033", "CANCELLED"),
                ("20250101034", "QUARANTINED"),
            ]:
                session.add(
                    PpsNoticeAuthority(
                        bid_notice_no=number,
                        revision_no="00",
                        event_kind="취소공고" if disposition == "CANCELLED" else "원공고",
                        disposition=disposition,
                        required_fields_complete=True,
                        direct_contract_signal=False,
                        published_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                        provider_changed_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                        deadline=datetime(2025, 1, 20, tzinfo=timezone.utc),
                        authority_sha256=("d" if disposition == "CANCELLED" else "e") * 64,
                    )
                )
            session.commit()
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={
                "notice_keys": [
                    "MANUAL-NOT-PPS",
                    "PPS-OUTCOME-FUTURE",
                    "PPS-OUTCOME-AUTH-CANCELLED",
                    "PPS-OUTCOME-AUTH-INVALID",
                ]
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["api_calls"] == 0
        assert body["processed_count"] == 0
        assert body["skipped"] == 4
        assert {item["reason_code"] for item in body["items"]} == {
            "NOT_PPS_NOTICE",
            "NOTICE_NOT_ENDED",
            "PPS_AUTHORITY_CANCELLED",
            "PPS_AUTHORITY_NOT_VALID",
        }


def test_cancelled_notice_is_skipped_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _MustNotRunClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-CANCELLED",
            bid_notice_no="20250101004",
            status="CANCELLED",
        )
        # The batch still constructs one shared client after selecting notices;
        # use a real no-op fake to assert the fetch itself is skipped instead.
        monkeypatch.setattr(
            "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
            _WinningFeedbackClient,
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-CANCELLED"]},
        )
        assert response.status_code == 200
        assert response.json()["skipped"] == 1
        assert response.json()["api_calls"] == 0
        assert response.json()["items"][0]["reason_code"] == "NOTICE_CANCELLED"


def test_superseded_stored_revision_is_skipped_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _WinningFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-SUPERSEDED",
            bid_notice_no="20250101008",
            revision_no="00",
        )
        with app.state.session_factory() as session:
            session.add(
                PpsNoticeAuthority(
                    bid_notice_no="20250101008",
                    revision_no="01",
                    event_kind="변경공고",
                    disposition="VALID",
                    required_fields_complete=True,
                    direct_contract_signal=False,
                    published_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                    provider_changed_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                    deadline=datetime(2025, 1, 20, tzinfo=timezone.utc),
                    authority_sha256="b" * 64,
                )
            )
            session.commit()
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-SUPERSEDED"]},
        )
        assert response.status_code == 200
        assert response.json()["api_calls"] == 0
        assert response.json()["items"][0]["reason_code"] == "PPS_REVISION_SUPERSEDED"


class _PartialFeedbackClient(_WinningFeedbackClient):
    def fetch_exact_notice_awards(self, **kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        if str(kwargs["bid_notice_no"]).endswith("5"):
            raise PpsApiError("safe synthetic failure")
        return _exact_fetch(bid_notice_no=str(kwargs["bid_notice_no"]))


class _LimitedFeedbackClient(_WinningFeedbackClient):
    def fetch_exact_notice_awards(self, **kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        fetched = _exact_fetch(bid_notice_no=str(kwargs["bid_notice_no"]))
        second = {
            **fetched.rows[0],
            "identity": f"{kwargs['bid_notice_no']}|000|0|001",
            "rebid_no": "001",
            "winner_name": "다른 공개 낙찰사",
            "company_business_number_match": False,
        }
        return ExactNoticeAwardFetch(
            rows=[fetched.rows[0], second],
            fetched_count=4,
            mismatched_count=1,
            quarantined_count=1,
            api_calls=1,
            hit_page_limit=True,
            hit_time_limit=True,
        )


class _AllFailFeedbackClient(_WinningFeedbackClient):
    def fetch_exact_notice_awards(self, **_kwargs: object) -> ExactNoticeAwardFetch:
        self.request_count += 1
        raise PpsApiError("bounded synthetic provider outage")


class _FatalFeedbackClient(_WinningFeedbackClient):
    def __enter__(self) -> "_FatalFeedbackClient":
        raise RuntimeError("synthetic client construction failure")


def test_batch_reports_partial_provider_failure_and_keeps_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _PartialFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-ERROR",
            bid_notice_no="20250101005",
        )
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-SUCCESS",
            bid_notice_no="20250101006",
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={
                "notice_keys": ["PPS-OUTCOME-ERROR", "PPS-OUTCOME-SUCCESS"],
                "max_notices": 2,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "PARTIAL"
        assert body["errors"] == 1
        assert body["created"] == 1
        assert [item["result"] for item in body["items"]] == ["ERROR", "CREATED"]
        assert len(client.get("/api/v1/notices/PPS-OUTCOME-SUCCESS/outcomes").json()) == 1


def test_provider_limits_are_partial_and_preserve_sanitised_audit_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _LimitedFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-LIMITED",
            bid_notice_no="20250101041",
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-LIMITED"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "PARTIAL"
        assert body["created"] == 0
        assert body["review"] == 1
        assert body["errors"] == 0
        assert body["warnings"] == ["PAGE_LIMIT_REACHED", "WALL_TIME_LIMIT"]
        assert set(body["items"][0]["warnings"]) >= {
            "PAGE_LIMIT_REACHED",
            "WALL_TIME_LIMIT",
            "PROVIDER_IDENTITY_MISMATCH_DROPPED",
            "INCOMPLETE_PROVIDER_RESULT_DROPPED",
            "MULTIPLE_EXACT_RESULTS",
        }
        assert body["items"][0]["result"] == "REVIEW"
        assert body["items"][0]["reason_code"] == "PPS_FINAL_RESULT_INCOMPLETE"
        assert client.get("/api/v1/notices/PPS-OUTCOME-LIMITED/outcomes").json() == []


def test_all_provider_failures_report_failed_without_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _AllFailFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-ALL-FAILED",
            bid_notice_no="20250101042",
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-ALL-FAILED"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "FAILED"
        assert body["errors"] == 1
        assert body["api_calls"] == 1
        assert body["openai_calls"] == 0
        assert body["items"][0]["reason_code"] == "PPS_AWARD_API_ERROR"
        audit = next(
            item
            for item in client.get("/api/v1/ingestion/jobs").json()
            if item["source"] == "PPS_OUTCOME"
        )
        assert audit["status"] == "FAILED"
        assert audit["error_code"] == "PPS_OUTCOME_ALL_FAILED"


def test_unexpected_client_failure_marks_durable_audit_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _FatalFeedbackClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app, raise_server_exceptions=False) as client:
        _create_ended_pps_notice(
            client,
            notice_key="PPS-OUTCOME-FATAL",
            bid_notice_no="20250101043",
        )
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-OUTCOME-FATAL"]},
        )
        assert response.status_code == 500
        audit = next(
            item
            for item in client.get("/api/v1/ingestion/jobs").json()
            if item["source"] == "PPS_OUTCOME"
        )
        assert audit["status"] == "FAILED"
        assert audit["error_code"] == "PPS_OUTCOME_CLIENT_ERROR"
        assert "synthetic client construction failure" not in str(audit)


def test_refresh_requires_server_side_pps_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PPS_API_KEY", raising=False)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        response = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-NOT-PRESENT"]},
        )
        assert response.status_code == 503
        assert "PPS_API_KEY" in response.json()["detail"]


def test_public_read_only_requires_server_key_for_outcome_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    monkeypatch.setenv("PPS_API_KEY", "server-side-pps-key")
    monkeypatch.setattr(
        "pai_loop.outcome_feedback.PpsOutcomeFeedbackClient",
        _MustNotRunClient,
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        without_key = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            json={"notice_keys": ["PPS-NOT-PRESENT"]},
        )
        assert without_key.status_code == 401
        with_key = client.post(
            "/api/v1/outcome-feedback/pps/refresh",
            headers=SERVER_HEADERS,
            json={"notice_keys": ["PPS-NOT-PRESENT"]},
        )
        assert with_key.status_code == 200
        assert with_key.json()["skipped"] == 1
        assert with_key.json()["processed_count"] == 0
        assert with_key.json()["openai_calls"] == 0
        assert with_key.json()["items"] == [
            {
                "notice_key": "PPS-NOT-PRESENT",
                "bid_notice_no": None,
                "revision_no": None,
                "result": "SKIPPED",
                "outcome_status": None,
                "outcome_key": None,
                "exact_result_count": 0,
                "api_calls": 0,
                "reason_code": "NOTICE_NOT_FOUND",
                "warnings": [],
            }
        ]
