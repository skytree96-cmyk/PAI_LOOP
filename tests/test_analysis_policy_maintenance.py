from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.analysis_policy_maintenance import (
    MANUAL_ONLY_CLEANUP_CONFIRMATION,
)
from pai_loop.models import Notice, NoticeAnalysisPolicy, PpsNoticeAuthority


ACTIVE_KEY = "PPS-MAINTENANCE-ACTIVE"
CANCELLED_KEY = "PPS-MAINTENANCE-CANCELLED"
CLOSED_KEY = "PPS-MAINTENANCE-CLOSED"
EXPIRED_KEY = "PPS-MAINTENANCE-EXPIRED"
OTHER_POLICY_KEY = "PPS-MAINTENANCE-OTHER-POLICY"
ORPHAN_KEY = "PPS-MAINTENANCE-ORPHAN"
PATH = "/api/v1/maintenance/legacy-manual-only-policies"
API_KEY = "maintenance-test-key"


def _protect_endpoint(client: TestClient) -> dict[str, str]:
    client.app.state.settings = replace(
        client.app.state.settings,
        api_key=API_KEY,
    )
    return {"X-PAI-LOOP-API-KEY": API_KEY}


def _seed_policies(client: TestClient) -> None:
    now = datetime.now(timezone.utc)
    notice_specs = (
        (ACTIVE_KEY, "BID-ACTIVE", "OPEN", now + timedelta(days=4)),
        (CANCELLED_KEY, "BID-CANCELLED", "OPEN", now + timedelta(days=4)),
        (CLOSED_KEY, "BID-CLOSED", "CLOSED", now + timedelta(days=4)),
        (EXPIRED_KEY, "BID-EXPIRED", "OPEN", now - timedelta(minutes=1)),
        (OTHER_POLICY_KEY, "BID-OTHER", "OPEN", now + timedelta(days=4)),
    )
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                Notice(
                    notice_key=notice_key,
                    bid_notice_no=bid_notice_no,
                    revision_no="00",
                    title=f"maintenance {notice_key}",
                    agency="공공기관",
                    published_at=now - timedelta(days=1),
                    deadline=deadline,
                    status=notice_status,
                    category="용역",
                )
                for notice_key, bid_notice_no, notice_status, deadline in notice_specs
            ]
        )
        session.add_all(
            [
                NoticeAnalysisPolicy(
                    notice_key=notice_key,
                    bid_notice_no=bid_notice_no,
                    analysis_policy=(
                        "AUTOMATIC"
                        if notice_key == OTHER_POLICY_KEY
                        else "MANUAL_ONLY"
                    ),
                    policy_source="TEST",
                )
                for notice_key, bid_notice_no, _status, _deadline in notice_specs
            ]
        )
        session.add(
            NoticeAnalysisPolicy(
                notice_key=ORPHAN_KEY,
                bid_notice_no="BID-ORPHAN",
                analysis_policy="MANUAL_ONLY",
                policy_source="TEST",
            )
        )
        session.add(
            PpsNoticeAuthority(
                bid_notice_no="BID-CANCELLED",
                revision_no="00",
                event_kind="취소공고",
                disposition="CANCELLED",
                required_fields_complete=False,
                direct_contract_signal=False,
                published_at=now - timedelta(days=1),
                provider_changed_at=now,
                deadline=None,
                authority_sha256="c" * 64,
            )
        )
        session.commit()


def _policy_keys(client: TestClient) -> set[str]:
    with client.app.state.session_factory() as session:
        return set(session.scalars(select(NoticeAnalysisPolicy.notice_key)).all())


def test_maintenance_endpoint_requires_server_api_key(client: TestClient) -> None:
    headers = _protect_endpoint(client)

    assert client.post(PATH).status_code == 401
    assert client.post(
        PATH,
        headers={"X-PAI-LOOP-API-KEY": "wrong"},
    ).status_code == 401
    response = client.post(PATH, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is True


def test_default_dry_run_selects_only_active_noncancelled_manual_only(
    client: TestClient,
) -> None:
    headers = _protect_endpoint(client)
    _seed_policies(client)
    before = _policy_keys(client)

    response = client.post(PATH, headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["candidate_count"] == 1
    assert body["deleted_count"] == 0
    assert body["notice_keys"] == [ACTIVE_KEY]
    assert body["notice_keys_truncated"] is False
    assert len(body["candidate_notice_keys_sha256"]) == 64
    assert body["confirmation_required"] == MANUAL_ONLY_CLEANUP_CONFIRMATION
    assert _policy_keys(client) == before


def test_apply_requires_exact_confirmation_and_preserves_all_other_rows(
    client: TestClient,
) -> None:
    headers = _protect_endpoint(client)
    _seed_policies(client)
    before = _policy_keys(client)

    unconfirmed = client.post(PATH, headers=headers, json={"apply": True})
    wrong = client.post(
        PATH,
        headers=headers,
        json={"apply": True, "confirm": "DELETE_ALL"},
    )

    assert unconfirmed.status_code == 409
    assert wrong.status_code == 409
    assert _policy_keys(client) == before

    applied = client.post(
        PATH,
        headers=headers,
        json={
            "apply": True,
            "confirm": MANUAL_ONLY_CLEANUP_CONFIRMATION,
        },
    )

    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["dry_run"] is False
    assert body["applied"] is True
    assert body["candidate_count"] == 1
    assert body["deleted_count"] == 1
    assert body["notice_keys"] == [ACTIVE_KEY]
    assert _policy_keys(client) == before - {ACTIVE_KEY}

    repeated = client.post(
        PATH,
        headers=headers,
        json={
            "apply": True,
            "confirm": MANUAL_ONLY_CLEANUP_CONFIRMATION,
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["candidate_count"] == 0
    assert repeated.json()["deleted_count"] == 0
    assert _policy_keys(client) == before - {ACTIVE_KEY}
