from __future__ import annotations

import importlib.util
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop import teams_bot as bot
from pai_loop.account_models import AccountSession, DepartmentAccount
from pai_loop.accounts import CSRF_COOKIE, SESSION_COOKIE, _session_hash, departments, now_utc
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.teams_identity_models import TeamsLinkCode, TeamsRecipient, TeamsSessionLink

SYN_APP = "11111111-1111-4111-8111-111111111111"
SYN_TENANT = "22222222-2222-4222-8222-222222222222"
SYN_PERSON = "33333333-3333-4333-8333-333333333333"
SYN_PEER = "44444444-4444-4444-8444-444444444444"
SYN_SERVICE = "https://smba.trafficmanager.net/teams/"
SYN_SETTINGS = bot.TeamsBotSettings(SYN_APP, "SYN-client-secret-only", SYN_TENANT, "https://syn-app.invalid")
HEADERS = {"Origin": "http://testserver", "X-CSRF-Token": "SYN-csrf", "Sec-Fetch-Site": "same-origin"}


def activity(person=SYN_PERSON, text=""):
    return {"type": "message", "channelId": "msteams", "serviceUrl": SYN_SERVICE,
            "from": {"aadObjectId": person}, "conversation": {
                "id": "SYN-conversation-" + person, "conversationType": "personal", "tenantId": SYN_TENANT},
            "channelData": {"tenant": {"id": SYN_TENANT}}, "text": text}


@pytest.fixture
def signer(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="SYN-key", alg="RS256", endorsements=["msteams"])
    monkeypatch.setattr(bot, "_key_cache", (time.monotonic(), [public]))

    def signed(**overrides):
        claims = {"aud": SYN_APP, "iss": bot.CONNECTOR_ISSUER, "exp": int(time.time()) + 300,
                  "nbf": int(time.time()) - 5, "serviceurl": SYN_SERVICE}
        claims.update(overrides)
        claims = {name: value for name, value in claims.items() if value is not None}
        return "Bearer " + jwt.encode(claims, key, algorithm="RS256", headers={"kid": "SYN-key"})
    return signed


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setattr(bot.TeamsBotSettings, "from_env", classmethod(lambda cls: SYN_SETTINGS))
    engine = build_engine("sqlite:///" + (tmp_path / "SYN-teams.db").as_posix())
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.state.session_factory = build_session_factory(engine)
    app.state.settings = SimpleNamespace(department_accounts_enabled=True, environment="development")
    app.include_router(bot.router)

    @app.get("/syn-recipient")
    def resolve(request: Request):
        with app.state.session_factory() as session:
            return {"id": bot.recipient_for_request(request, session).id}

    with app.state.session_factory() as session:
        session.add(DepartmentAccount(id="SYN-account", username="SYN_TEAMS", role="DEPARTMENT",
            department_id=next(iter(departments())), password_hash="SYN-unused", active=True,
            paid_analysis_allowed=False, created_at=now_utc()))
        session.flush()
        for identifier, token in (("SYN-session", "SYN-login"), ("SYN-peer-session", "SYN-peer-login")):
            session.add(AccountSession(id=identifier, account_id="SYN-account", token_hash=_session_hash(token),
                csrf_hash=_session_hash("SYN-csrf"), created_at=now_utc(), expires_at=now_utc()+timedelta(hours=1)))
        session.commit()
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, "SYN-login")
        client.cookies.set(CSRF_COOKIE, "SYN-csrf")
        yield client
    engine.dispose()


def redeem(browser, signer, code, person=SYN_PERSON):
    return browser.post("/api/v1/teams/messages", headers={"Authorization": signer()},
                        json=activity(person, "연결 " + code))


def pair(browser, signer, person=SYN_PERSON):
    result = browser.post("/api/v1/teams/link-code", headers=HEADERS)
    assert result.status_code == 200
    assert redeem(browser, signer, result.json()["code"], person).status_code == 200


def test_settings_and_service_url_fail_closed(monkeypatch):
    for name in ("PAI_TEAMS_BOT_APP_ID", "PAI_TEAMS_BOT_APP_SECRET", "PAI_TEAMS_TENANT_ID", "PAI_TEAMS_PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert not bot.TeamsBotSettings.from_env().enabled
    assert bot.TeamsBotSettings().bot_chat_url is None
    assert "SYN-client-secret" not in repr(SYN_SETTINGS)
    assert SYN_SETTINGS.enabled
    assert bot.trusted_service_url(SYN_SERVICE.rstrip("/")) == SYN_SERVICE
    assert bot.trusted_service_url("https://smba.trafficmanager.net/amer-client-ss.msg/") == "https://smba.trafficmanager.net/amer-client-ss.msg/"
    for url in ("http://smba.trafficmanager.net/teams/", "https://localhost/teams/",
                "https://smba.trafficmanager.net.attacker.invalid/teams/", "https://smba.trafficmanager.net/teams/?q=1",
                "https://" + "SYN-user" + "@" + "smba.trafficmanager.net/teams/", "https://smba.trafficmanager.net:8443/teams/",
                "https://smba.trafficmanager.net/teams/../../private", "https://smba.trafficmanager.net/teams/#x",
                "https://smba.trafficmanager.net/../", "https://smba.trafficmanager.net/./"):
        with pytest.raises(HTTPException):
            bot.trusted_service_url(url)


def test_pairing_is_single_use_hashed_and_session_scoped(browser, signer):
    issued = browser.post("/api/v1/teams/link-code", headers=HEADERS)
    code = issued.json()["code"]
    assert len(code) == 32 and issued.headers["cache-control"] == "no-store"
    with browser.app.state.session_factory() as session:
        proof = session.scalar(select(TeamsLinkCode))
        assert proof.code_hash != code and len(proof.code_hash) == 64
        assert proof.session_id == "SYN-session" and proof.account_id == "SYN-account"
    assert redeem(browser, signer, code).status_code == 200
    connection = browser.get("/api/v1/teams/connection").json()
    assert connection["connected"] and connection["enabled"]
    assert SYN_PERSON not in str(connection) and "recipient_id" not in connection
    recipient_id = browser.get("/syn-recipient").json()["id"]
    assert redeem(browser, signer, code, SYN_PEER).status_code == 200
    assert browser.get("/syn-recipient").json()["id"] == recipient_id
    browser.cookies.set(SESSION_COOKIE, "SYN-peer-login")
    assert not browser.get("/api/v1/teams/connection").json()["connected"]
    assert browser.get("/syn-recipient").status_code == 409
    pair(browser, signer, SYN_PEER)
    assert browser.get("/syn-recipient").json()["id"] != recipient_id
    with browser.app.state.session_factory() as session:
        recipients = session.scalars(select(TeamsRecipient)).all()
        assert len(recipients) == 2
        assert {row.account_id for row in recipients} == {"SYN-account"}


@pytest.mark.parametrize("invalid", ["expired", "revoked-session", "inactive-account", "reissued"])
def test_invalid_pairing_proof_cannot_link(browser, signer, invalid):
    code = browser.post("/api/v1/teams/link-code", headers=HEADERS).json()["code"]
    with browser.app.state.session_factory() as session:
        if invalid == "expired":
            session.get(TeamsLinkCode, bot._code_hash(code)).expires_at = now_utc() - timedelta(seconds=1)
        elif invalid == "revoked-session":
            session.get(AccountSession, "SYN-session").revoked_at = now_utc()
        elif invalid == "inactive-account":
            session.get(DepartmentAccount, "SYN-account").active = False
        session.commit()
    if invalid == "reissued":
        assert browser.post("/api/v1/teams/link-code", headers=HEADERS).status_code == 200
    assert redeem(browser, signer, code).status_code == 200
    with browser.app.state.session_factory() as session:
        assert session.scalar(select(TeamsSessionLink)) is None


def test_browser_pairing_requires_login_csrf_and_no_trusted_client_identity(browser):
    assert browser.post("/api/v1/teams/link-code").status_code == 403
    assert browser.post("/api/v1/teams/link-code", headers={**HEADERS, "Origin": "https://evil.invalid"}).status_code == 403
    assert browser.get("/api/v1/teams/connection?aadObjectId=" + SYN_PERSON).json()["connected"] is False
    browser.cookies.clear()
    assert browser.get("/api/v1/teams/connection").status_code == 401
    assert browser.post("/api/v1/teams/link-code", headers=HEADERS).status_code == 401


def test_disconnect_stops_personal_destination_and_unlinks(browser, signer):
    pair(browser, signer)
    result = browser.post("/api/v1/teams/disconnect", headers=HEADERS)
    assert result.status_code == 200 and result.json() == {"connected": False}
    assert browser.get("/syn-recipient").status_code == 409
    with browser.app.state.session_factory() as session:
        assert not session.scalar(select(TeamsRecipient)).active
        assert session.scalar(select(TeamsSessionLink)) is None


def test_install_reference_is_inactive_until_verified_pairing(browser, signer):
    item = activity()
    item.update(type="installationUpdate", action="add")
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer()}, json=item).status_code == 200
    with browser.app.state.session_factory() as session:
        recipient = session.scalar(select(TeamsRecipient))
        assert not recipient.active and recipient.account_id is None
    assert browser.get("/syn-recipient").status_code == 409
    pair(browser, signer)
    with browser.app.state.session_factory() as session:
        recipient = session.scalar(select(TeamsRecipient))
        assert recipient.active and recipient.account_id == "SYN-account"


def test_simultaneous_code_redemption_has_one_winner(browser, signer):
    code = browser.post("/api/v1/teams/link-code", headers=HEADERS).json()["code"]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda person: redeem(browser, signer, code, person), [SYN_PERSON, SYN_PEER]))
    assert all(result.status_code == 200 for result in results)
    with browser.app.state.session_factory() as session:
        assert len(session.scalars(select(TeamsRecipient)).all()) == 1
        assert len(session.scalars(select(TeamsSessionLink)).all()) == 1
        assert session.get(TeamsLinkCode, bot._code_hash(code)).consumed_at is not None


def test_unconfigured_bot_exposes_status_but_rejects_link_and_callback(browser, monkeypatch):
    monkeypatch.setattr(bot.TeamsBotSettings, "from_env", classmethod(lambda cls: bot.TeamsBotSettings()))
    assert browser.get("/api/v1/teams/connection").json() == {"enabled": False, "connected": False, "bot_chat_url": None}
    assert browser.post("/api/v1/teams/link-code", headers=HEADERS).status_code == 503
    assert browser.post("/api/v1/teams/messages", json=activity()).status_code == 503


def test_correct_claims_with_wrong_signature_are_rejected(browser, signer):
    valid = signer().split(" ", 1)[1]
    claims = jwt.decode(valid, options={"verify_signature": False})
    forged = jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048),
                        algorithm="RS256", headers={"kid": "SYN-key"})
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": "Bearer " + forged}, json=activity()).status_code == 401


def test_documented_and_sdk_service_url_claims_must_both_match(browser, signer):
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer(serviceUrl=SYN_SERVICE)}, json=activity()).status_code == 200
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer(serviceurl=None, serviceUrl=SYN_SERVICE)}, json=activity()).status_code == 200
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer(serviceUrl="https://evil.invalid/")}, json=activity()).status_code == 403
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer(serviceurl=None)}, json=activity()).status_code == 403


def test_bot_remove_and_service_reference_refresh(browser, signer):
    pair(browser, signer)
    updated = activity()
    updated["conversation"]["id"] = "SYN-new-conversation"
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer()}, json=updated).status_code == 200
    with browser.app.state.session_factory() as session:
        assert session.scalar(select(TeamsRecipient)).conversation_id == "SYN-new-conversation"
    updated.update(type="installationUpdate", action="remove")
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer()}, json=updated).status_code == 200
    assert not browser.get("/api/v1/teams/connection").json()["connected"]


@pytest.mark.parametrize("remove_action", ["remove", "remove-upgrade"])
def test_uninstall_without_aad_id_revokes_only_exact_conversation(browser, signer, remove_action):
    pair(browser, signer)
    item = activity()
    item.update(type="installationUpdate", action=remove_action, **{"from": {}})
    item["conversation"]["id"] = "SYN-other-person-conversation"
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer()}, json=item).status_code == 200
    assert browser.get("/api/v1/teams/connection").json()["connected"]
    item["conversation"]["id"] = activity()["conversation"]["id"]
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer()}, json=item).status_code == 200
    assert not browser.get("/api/v1/teams/connection").json()["connected"]


def test_signed_regional_service_reference_can_pair_and_send(browser, signer, monkeypatch):
    service_url = "https://smba.trafficmanager.net/amer-client-ss.msg/"
    code = browser.post("/api/v1/teams/link-code", headers=HEADERS).json()["code"]
    item = activity(text="연결 " + code)
    item["serviceUrl"] = service_url
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer(serviceurl=service_url)}, json=item).status_code == 200
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"access_token": "SYN-token", "id": "SYN-regional-message"})
    mock_transport(monkeypatch, handler)
    with browser.app.state.session_factory() as session:
        recipient = session.scalar(select(TeamsRecipient))
        assert bot.send_personal_card(recipient, {}, SYN_SETTINGS) == "SYN-regional-message"
    assert seen[1].startswith(service_url + "v3/conversations/")


@pytest.mark.parametrize("overrides", [{"aud": "SYN-wrong-app"}, {"iss": "https://evil.invalid"},
    {"exp": 1}, {"nbf": 9999999999}, {"serviceUrl": "https://evil.invalid/"}])
def test_connector_claim_validation(browser, signer, overrides):
    result = browser.post("/api/v1/teams/messages", headers={"Authorization": signer(**overrides)}, json=activity())
    assert result.status_code in {401, 403}
    assert "SYN-wrong-app" not in result.text


@pytest.mark.parametrize("variation", ["tenant", "group", "channel", "bad-url", "missing-auth", "hmac", "endorsement"])
def test_callback_rejects_forged_destinations(browser, signer, monkeypatch, variation):
    item = activity()
    authorization = signer()
    if variation == "tenant":
        item["channelData"]["tenant"]["id"] = SYN_PEER
    elif variation == "group":
        item["conversation"]["conversationType"] = "groupChat"
    elif variation == "channel":
        item["channelId"] = "webchat"
    elif variation == "bad-url":
        item["serviceUrl"] = "https://localhost/teams/"
        authorization = signer(serviceUrl=item["serviceUrl"])
    elif variation == "missing-auth":
        authorization = ""
    elif variation == "hmac":
        authorization = "Bearer " + jwt.encode({"aud": SYN_APP}, "SYN-unsafe-key", algorithm="HS256")
    elif variation == "endorsement":
        keys = deepcopy(bot._key_cache[1])
        keys[0]["endorsements"] = ["webchat"]
        monkeypatch.setattr(bot, "_key_cache", (time.monotonic(), keys))
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": authorization}, json=item).status_code in {401, 403}


def test_callback_body_limit_and_unknown_sender(browser, signer):
    assert browser.post("/api/v1/teams/messages", content="x" * 65537).status_code == 413
    assert browser.post("/api/v1/teams/messages", content="[").status_code == 400
    assert browser.post("/api/v1/teams/messages", json=[]).status_code == 400
    item = activity()
    item["from"] = {}
    assert browser.post("/api/v1/teams/messages", headers={"Authorization": signer()}, json=item).status_code == 200


def mock_transport(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(bot.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))


def test_jwks_refresh_is_fixed_and_endorsed(monkeypatch):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()))
    public.update(kid="SYN-fresh-key", endorsements=["msteams"])
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"keys": [public]})
    mock_transport(monkeypatch, handler)
    monkeypatch.setattr(bot, "_key_cache", (0, []))
    assert bot._signing_key("SYN-fresh-key")["kid"] == "SYN-fresh-key"
    with pytest.raises(HTTPException):
        bot._signing_key("SYN-random-key")
    assert seen == [bot.CONNECTOR_KEYS_URL]


@pytest.mark.parametrize("kind,expected", [("success", None), ("429", "RETRY"), ("500", "RETRY"),
    ("403", "FAILED"), ("redirect", "FAILED"), ("timeout", "UNKNOWN"), ("invalid-json", "UNKNOWN"),
    ("token-error", "RETRY")])
def test_outbound_safe_destinations_and_ambiguous_delivery(monkeypatch, kind, expected):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.host == "login.microsoftonline.com":
            if kind == "token-error":
                raise httpx.ConnectError("SYN-secret-sensitive-error", request=request)
            return httpx.Response(200, json={"access_token": "SYN-access-token"})
        if kind == "timeout":
            raise httpx.ReadTimeout("SYN-secret-sensitive-error", request=request)
        if kind == "invalid-json":
            return httpx.Response(200, text="SYN-invalid")
        status = int(kind) if kind.isdigit() else (302 if kind == "redirect" else 200)
        return httpx.Response(status, json={"id": "SYN-activity"}, headers={"Retry-After": "123", "Location": "https://evil.invalid"})
    mock_transport(monkeypatch, handler)
    recipient = TeamsRecipient(tenant_id=SYN_TENANT, active=True, service_url=SYN_SERVICE,
                               conversation_id="SYN-id/with?reserved")
    if expected:
        with pytest.raises(bot.TeamsDeliveryError) as error:
            bot.send_personal_card(recipient, {"type": "AdaptiveCard"}, SYN_SETTINGS)
        assert error.value.outcome == expected
        assert error.value.ambiguous == (expected == "UNKNOWN")
        assert "secret" not in str(error.value)
        if kind == "429":
            assert error.value.retry_after_seconds == 123
    else:
        assert bot.send_personal_card(recipient, {"type": "AdaptiveCard"}, SYN_SETTINGS) == "SYN-activity"
    assert seen[0].url.host == "login.microsoftonline.com"
    assert SYN_TENANT in str(seen[0].url)
    if len(seen) > 1:
        assert seen[1].url.host == "smba.trafficmanager.net"
        assert b"SYN-id%2Fwith%3Freserved" in seen[1].url.raw_path
        assert seen[1].headers["authorization"] == "Bearer SYN-access-token"


def test_outbound_refuses_untrusted_or_inactive_recipient(monkeypatch):
    monkeypatch.setattr(bot.httpx, "Client", lambda **kwargs: pytest.fail("must not reach network"))
    for recipient in (TeamsRecipient(active=False, tenant_id=SYN_TENANT),
                      TeamsRecipient(active=True, tenant_id=SYN_PEER),
                      TeamsRecipient(active=True, tenant_id=SYN_TENANT, service_url="https://evil.invalid/teams/")):
        with pytest.raises(bot.TeamsDeliveryError) as error:
            bot.send_personal_card(recipient, {}, SYN_SETTINGS)
        assert error.value.outcome == "FAILED"


def test_personal_package_requires_actual_inputs_and_contains_personal_scopes(tmp_path):
    spec = importlib.util.spec_from_file_location("teams_personal_builder", Path(__file__).parents[1] / "scripts/build_teams_personal_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "SYN-personal.zip"
    with pytest.raises(ValueError):
        module.build_package("PLACEHOLDER", "https://syn-app.invalid", output)
    with pytest.raises(ValueError):
        module.build_package(SYN_APP, "http://localhost", output)
    module.build_package(SYN_APP, "https://syn-app.invalid", output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"manifest.json", "color.png", "outline.png"}
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["bots"][0]["botId"] == SYN_APP
    assert manifest["bots"][0]["scopes"] == ["personal"]
    assert manifest["staticTabs"][0]["scopes"] == ["personal"]
    assert manifest["staticTabs"][0]["contentUrl"] == "https://syn-app.invalid/?host=teams"
    assert "packageName" not in manifest
    assert all("supportedPlatform" not in tab for tab in manifest.get("configurableTabs", []))
    assert "SYN-client-secret" not in str(manifest)
