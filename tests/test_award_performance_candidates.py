from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pai_loop.accounts import departments, router as accounts_router
from pai_loop.award_performance_candidates import router
from pai_loop.config import Settings
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import AwardPerformanceCandidate, CompanyPerformanceRecord

ORIGIN = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}
PATH = "/api/v1/award-performance-candidates"


def _award(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "scope": "service",
        "bid_notice_no": "R26BK00000001",
        "revision_no": "000",
        "classification_no": "0",
        "rebid_no": "0",
        "title": "SYN 공공기관 리더십 교육",
        "participant_count": 3,
        "winner_name": "사단법인 한국능률협회",
        "award_amount": 120000000.0,
        "award_rate": 89.5,
        "opened_at": "2026-08-01T11:00:00",
        "agency": "SYN 발주기관",
        "registered_at": "2026-08-01T12:30:00",
        "awarded_at": "2026-08-02T00:00:00",
    }
    record.update(overrides)
    return record


def _app(**settings: object) -> FastAPI:
    app = FastAPI()
    values: dict[str, object] = {
        "environment": "production",
        "department_accounts_enabled": True,
        "api_key": "SYN-candidates-bootstrap-only",
        "public_read_only": True,
        "public_manual_analysis_enabled": True,
    }
    values.update(settings)
    app.state.settings = Settings(**values)
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.include_router(accounts_router)
    app.include_router(router)
    return app


def _login(client: TestClient, *, paid: bool) -> str:
    # Synthetic account only, bootstrapped into this test's disposable database.
    password = "SYN-account-fixture-password-1012"
    accounts = [{"username": "SYN_CANDIDATE_DEPT", "password": password, "role": "DEPARTMENT",
                 "department_id": next(iter(departments())), "active": True,
                 "paid_analysis_allowed": paid}]
    server = {"X-PAI-LOOP-API-KEY": "SYN-candidates-bootstrap-only"}
    preview = client.post("/api/v1/accounts/bootstrap", headers=server, json={"accounts": accounts})
    assert preview.status_code == 200, preview.text
    applied = client.post("/api/v1/accounts/bootstrap", headers=server,
                          json={"accounts": accounts, "dry_run": False, "preview_id": preview.json()["preview_id"]})
    assert applied.status_code == 200, applied.text
    login = client.post("/api/v1/accounts/login", headers=ORIGIN,
                        json={"username": "SYN_CANDIDATE_DEPT", "password": password})
    assert login.status_code == 200, login.text
    return login.json()["csrf_token"]


@contextmanager
def _client(*, authenticated: bool = True, paid: bool = False, **settings: object):
    app = _app(**settings)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            if authenticated:
                client.headers["X-CSRF-Token"] = _login(client, paid=paid)
            yield client, app
    finally:
        app.state.engine.dispose()


def test_saving_requires_login_same_origin_and_csrf() -> None:
    with _client(authenticated=False) as (client, _app_):
        assert client.post(PATH, headers=ORIGIN, json=_award()).status_code == 401
        assert client.get(PATH, headers=ORIGIN).status_code == 401
        csrf = _login(client, paid=False)
        assert client.post(PATH, headers=ORIGIN, json=_award()).status_code == 403
        cross = client.post(PATH, headers={**ORIGIN, "Origin": "https://attacker.test", "X-CSRF-Token": csrf},
                            json=_award())
        assert cross.status_code == 403
        retired = client.post(PATH, headers={**ORIGIN, "X-CSRF-Token": csrf, "X-PAI-Manual-Token": "1357"},
                              json=_award())
        assert retired.status_code == 401
        ok = client.post(PATH, headers={**ORIGIN, "X-CSRF-Token": csrf}, json=_award())
    # Copying a shown public award runs no external lookup, so the
    # paid-analysis grant is not required.
    assert ok.status_code == 201, ok.text


def test_save_is_idempotent_and_never_touches_company_performance() -> None:
    with _client() as (client, app):
        first = client.post(PATH, headers=ORIGIN, json=_award())
        again = client.post(PATH, headers=ORIGIN, json=_award(title="SYN 다른 표시명"))
        listed = client.get(PATH, headers=ORIGIN)
        with app.state.session_factory() as session:
            candidates = session.scalar(select(func.count()).select_from(AwardPerformanceCandidate))
            performance = session.scalar(select(func.count()).select_from(CompanyPerformanceRecord))
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    candidate = first.json()["candidate"]
    assert candidate["candidate_status"] == "CANDIDATE"
    assert candidate["award_amount_krw"] == 120_000_000
    assert candidate["candidate_key"] == "award:service|R26BK00000001|000|0|0"
    # PPS wall-clock 11:00 KST is 02:00 UTC.
    assert candidate["opened_at"].startswith("2026-08-01T02:00:00")
    assert candidate["created_by"] == next(iter(departments().values()))
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["candidate"]["id"] == candidate["id"]
    assert listed.json()["count"] == 1
    assert listed.json()["counts"] == {"CANDIDATE": 1, "DISMISSED": 0}
    assert candidates == 1
    assert performance == 0


def test_only_the_default_company_award_can_be_kept() -> None:
    with _client() as (client, _app_):
        other = client.post(PATH, headers=ORIGIN, json=_award(winner_name="SYN 다른회사"))
        spaced = client.post(PATH, headers=ORIGIN, json=_award(winner_name="사단법인  한국능률협회 "))
    assert other.status_code == 422
    assert "한국능률협회 낙찰 건만" in other.json()["detail"]
    assert spaced.status_code == 201, spaced.text


def test_dismiss_and_restore_by_saving_again() -> None:
    with _client() as (client, _app_):
        created = client.post(PATH, headers=ORIGIN, json=_award()).json()["candidate"]
        dismissed = client.patch(f"{PATH}/{created['id']}", headers=ORIGIN, json={"status": "DISMISSED"})
        active = client.get(PATH, headers=ORIGIN).json()
        hidden = client.get(PATH, headers=ORIGIN, params={"status": "DISMISSED"}).json()
        restored = client.post(PATH, headers=ORIGIN, json=_award())
        invalid = client.patch(f"{PATH}/{created['id']}", headers=ORIGIN, json={"status": "VALIDATED"})
        missing = client.patch(f"{PATH}/SYN-missing", headers=ORIGIN, json={"status": "DISMISSED"})
    assert dismissed.status_code == 200
    assert dismissed.json()["candidate_status"] == "DISMISSED"
    assert dismissed.json()["revision"] == 2
    assert active["count"] == 0
    assert active["counts"] == {"CANDIDATE": 0, "DISMISSED": 1}
    assert hidden["count"] == 1
    assert restored.status_code == 200
    assert restored.json()["candidate"]["candidate_status"] == "CANDIDATE"
    assert restored.json()["candidate"]["revision"] == 3
    # A candidate can never be promoted to verified performance here.
    assert invalid.status_code == 422
    assert missing.status_code == 404


def test_hidden_when_manual_feature_is_disabled() -> None:
    with _client(authenticated=False, public_manual_analysis_enabled=False) as (client, _app_):
        response = client.get(PATH, headers=ORIGIN)
    assert response.status_code == 404
