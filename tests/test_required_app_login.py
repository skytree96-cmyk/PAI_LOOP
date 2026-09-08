"""The work app never serves its data or page before account authentication."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.account_models import AccountSession, DepartmentAccount
from pai_loop.accounts import departments, now_utc
from pai_loop.main import create_app


SERVER = {"X-PAI-LOOP-API-KEY": "SYN-required-login-server-key"}
PASSWORD = "SYN-required-login-password"
ORIGIN = {"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}
NOTICE = "SYN-PRIVATE-ENTRY-001"
DATA_PATHS = [
    "/api/v1/runtime-profile", "/api/v1/dashboard", "/api/v1/notices",
    f"/api/v1/notices/{NOTICE}", f"/api/v1/notices/{NOTICE}/award-history",
    f"/api/v1/notices/{NOTICE}/award-intelligence", f"/api/v1/notices/{NOTICE}/quantitative-estimate",
    "/api/v1/company-profile", "/api/v1/departments/keyword-profiles",
    "/api/v1/performance", "/api/v1/performance/summary", "/api/v1/performance-records",
    "/api/v1/pre-specifications", "/api/v1/pre-specifications/SYN-PRE-001",
    "/api/docs", "/api/openapi.json", "/teams-config.html",
]


@pytest.fixture
def gated_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    app = create_app(database_url=f"sqlite:///{tmp_path / 'SYN-entry.db'}", seed_synthetic=False)
    app.state.settings = replace(app.state.settings, api_key=SERVER["X-PAI-LOOP-API-KEY"],
                                 department_accounts_enabled=True, public_read_only=True)
    with TestClient(app, base_url="https://testserver") as client:
        accounts = [{"username": "SYN_ENTRY", "password": PASSWORD, "role": "DEPARTMENT",
                     "department_id": next(iter(departments())), "active": True}]
        preview = client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": accounts})
        assert preview.status_code == 200
        assert client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": accounts,
            "dry_run": False, "preview_id": preview.json()["preview_id"]}).status_code == 200
        assert client.post("/api/v1/notices", headers=SERVER, json={"notice_key": NOTICE,
            "bid_notice_no": NOTICE, "revision_no": "00", "title": "SYN 로그인 뒤에만 보이는 업무",
            "agency": "SYN 기관", "deadline": "2027-01-01T00:00:00Z", "status": "OPEN"}).status_code == 201
        yield client


def login(client):
    response = client.post("/api/v1/accounts/login", headers=ORIGIN,
                           json={"username": "SYN_ENTRY", "password": PASSWORD})
    assert response.status_code == 200
    return {**ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


@pytest.mark.parametrize("path", DATA_PATHS)
def test_anonymous_data_and_schema_routes_are_closed(gated_client, path):
    response = gated_client.get(path)
    assert response.status_code == 401
    if path.startswith("/api/"):
        assert response.headers["cache-control"] == "no-store"
    assert NOTICE not in response.text and "SYN 로그인" not in response.text


@pytest.mark.parametrize("path", ["/", "/index.html", "/awards", "/performance", "/prespec", "/notices?notice=SYN-PRIVATE-ENTRY-001"])
def test_unauthenticated_html_contains_only_login(gated_client, path):
    response = gated_client.get(path)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert 'id="entryLoginForm"' in response.text
    assert "app.js" not in response.text and "app-shell" not in response.text
    assert "paiBotTeamsUrl" not in response.text and NOTICE not in response.text


@pytest.mark.parametrize("path", ["/login.js", "/login-gate.css", "/favicon.svg", "/app.js", "/styles.css"])
def test_required_assets_and_minimal_health_remain_available(gated_client, path):
    assert gated_client.get(path).status_code == 200
    assert gated_client.get("/healthz").status_code == 200


def test_cookie_reveals_app_and_sanitized_data_logout_closes_everything(gated_client):
    client = gated_client
    headers = login(client)
    html = client.get("/awards?notice=SYN-PRIVATE-ENTRY-001")
    assert 'class="app-shell"' in html.text and '<body hidden>' in html.text
    assert 'id="entryLoginForm"' not in html.text
    for path in ["/api/v1/dashboard", "/api/v1/notices", "/api/v1/company-profile",
                 "/api/v1/performance", "/api/v1/pre-specifications"]:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["cache-control"] == "no-store"
    assert client.get(f"/api/v1/notices/{NOTICE}").json()["decisions"] == []
    assert client.post("/api/v1/accounts/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/notices").status_code == 401
    assert 'id="entryLoginForm"' in client.get("/awards").text


@pytest.mark.parametrize("change", ["revoked", "inactive", "flag-off"])
def test_revocation_deactivation_and_flag_off_close_app(gated_client, change):
    client = gated_client
    login(client)
    if change == "flag-off":
        client.app.state.settings = replace(client.app.state.settings, department_accounts_enabled=False,
                                            public_manual_analysis_token="2468")
    else:
        with client.app.state.session_factory() as session:
            if change == "revoked": session.scalar(select(AccountSession)).revoked_at = now_utc()
            else: session.scalar(select(DepartmentAccount)).active = False
            session.commit()
    assert client.get("/api/v1/notices").status_code == 401
    assert client.get("/api/v1/notices", headers={"X-PAI-Manual-Token": "2468"}).status_code == 401
    assert 'id="entryLoginForm"' in client.get("/").text


def test_server_key_remains_server_only_and_paid_authority_is_unchanged(gated_client):
    client = gated_client
    assert client.get("/api/v1/notices", headers=SERVER).status_code == 200
    assert client.get("/api/v1/notices", headers={**SERVER, **ORIGIN}).status_code == 403
    assert client.get("/api/v1/notices", headers={"X-PAI-LOOP-API-KEY": "SYN-wrong-key"}).status_code == 401
    headers = login(client)
    response = client.get("/api/v1/accounts/me")
    assert not response.json()["account"]["paid_analysis_allowed"]
    assert client.post("/api/v1/ingestion/replay", headers=headers).status_code == 401
    assert client.get("/api/v1/operator-evidence/company-profile", headers=headers).status_code in {401, 404}
