"""Single-tenant Teams personal bot with Connector JWT and session pairing.

The browser never supplies a recipient or Microsoft identity. Only an authenticated
personal bot message can redeem a one-use proof issued to the browser's session.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .account_models import AccountSession, DepartmentAccount
from .accounts import authenticated_account, now_utc, serial_transaction
from .teams_identity_models import TeamsLinkCode, TeamsRecipient, TeamsSessionLink

router = APIRouter(prefix="/api/v1/teams", tags=["personal Teams"])
CONNECTOR_KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
CONNECTOR_ISSUER = "https://api.botframework.com"
LINK_TTL_SECONDS = 600
MAX_ACTIVITY_BYTES = 65536
_key_lock = threading.Lock()
_key_cache: tuple[float, list[dict]] = (0, [])
_entra_key_lock = threading.Lock()
_entra_key_cache: tuple[float, str, list[dict]] = (0, "", [])
MAX_SSO_TOKEN_BYTES = 8192


def _uuid(value: str) -> str:
    return str(UUID(value))


@dataclass(frozen=True)
class TeamsBotSettings:
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    tenant_id: str = ""
    public_base_url: str = ""

    @classmethod
    def from_env(cls) -> "TeamsBotSettings":
        return cls(os.getenv("PAI_TEAMS_BOT_APP_ID", "").strip(),
                   os.getenv("PAI_TEAMS_BOT_APP_SECRET", ""),
                   os.getenv("PAI_TEAMS_TENANT_ID", "").strip(),
                   os.getenv("PAI_TEAMS_PUBLIC_BASE_URL", "").strip().rstrip("/"))

    @property
    def enabled(self) -> bool:
        try:
            _uuid(self.app_id)
            _uuid(self.tenant_id)
            parsed = urlsplit(self.public_base_url)
            return bool(self.app_secret and parsed.scheme == "https" and parsed.hostname
                        and not parsed.username and not parsed.password and not parsed.query
                        and not parsed.fragment and parsed.path in {"", "/"} and parsed.port in {None, 443})
        except (ValueError, TypeError):
            return False

    @property
    def bot_chat_url(self) -> str | None:
        if not self.enabled:
            return None
        return "https://teams.microsoft.com/l/chat/0/0?users=" + quote("28:" + self.app_id, safe="")

    def bot_chat_url_with_message(self, message: str) -> str | None:
        """Open the personal bot chat with the pairing command already composed.

        Teams fills its compose box from `message`, so the person only presses
        send. The code still travels through their own bot conversation, so what
        proves the pairing is unchanged; only the copying is removed. A host that
        ignores the parameter simply opens an empty chat, and the copy button on
        the page remains the way through.
        """

        base = self.bot_chat_url
        return base + "&message=" + quote(message, safe="") if base else None

    @property
    def sso_audiences(self) -> frozenset[str]:
        """Both spellings Entra may put in `aud` for this API.

        An app that also ships a bot must register its identifier URI with the
        `botid-` prefix, so that exact string is the one Teams asks for. A v2
        token can carry the bare client id instead, and both name the same API.
        """

        if not self.enabled:
            return frozenset()
        host = urlsplit(self.public_base_url).hostname or ""
        return frozenset({self.app_id, f"api://{host}/botid-{self.app_id}"})

    @property
    def sso_issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def sso_keys_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"


def _settings() -> TeamsBotSettings:
    settings = TeamsBotSettings.from_env()
    if not settings.enabled:
        raise HTTPException(503, "Teams 개인 알림 연결을 준비 중입니다.")
    return settings


def _code_hash(code: str) -> str:
    return hashlib.sha256(("teams-session-link-v1:" + code).encode()).hexdigest()


def _linked_recipient(session: Session, session_id: str, account_id: str) -> TeamsRecipient | None:
    link = session.get(TeamsSessionLink, session_id)
    if not link or link.account_id != account_id:
        return None
    recipient = session.get(TeamsRecipient, link.recipient_id)
    return recipient if recipient and recipient.active else None


def recipient_for_request(request: Request, session: Session) -> TeamsRecipient:
    identity = authenticated_account(request)
    recipient = _linked_recipient(session, identity.session_id, identity.id)
    if recipient is None:
        raise HTTPException(409, "Teams 개인 알림을 먼저 연결해 주세요.")
    return recipient


@router.get("/connection")
def connection(request: Request, response: Response) -> dict:
    identity = authenticated_account(request)
    response.headers["Cache-Control"] = "no-store"
    settings = TeamsBotSettings.from_env()
    with request.app.state.session_factory() as session:
        recipient = _linked_recipient(session, identity.session_id, identity.id)
        return {"enabled": settings.enabled, "connected": bool(recipient), "bot_chat_url": settings.bot_chat_url}


@router.post("/link-code")
def link_code(request: Request, response: Response) -> dict:
    identity = authenticated_account(request, mutation=True)
    settings = _settings()
    now = now_utc()
    code = secrets.token_urlsafe(24)
    with request.app.state.session_factory() as session:
        serial_transaction(session, scope="teams-identities")
        session.execute(delete(TeamsLinkCode).where(
            (TeamsLinkCode.session_id == identity.session_id) | (TeamsLinkCode.expires_at <= now)))
        session.add(TeamsLinkCode(code_hash=_code_hash(code), session_id=identity.session_id,
                                 account_id=identity.id, created_at=now,
                                 expires_at=now + timedelta(seconds=LINK_TTL_SECONDS)))
        session.commit()
    command = "연결 " + code
    response.headers["Cache-Control"] = "no-store"
    return {"code": code, "command": command, "expires_in_seconds": LINK_TTL_SECONDS,
            "expires_at": (now + timedelta(seconds=LINK_TTL_SECONDS)).isoformat(),
            "bot_chat_url": settings.bot_chat_url,
            "bot_chat_command_url": settings.bot_chat_url_with_message(command)}


@router.post("/link-sso")
async def link_sso(request: Request, response: Response) -> dict:
    """Bind this session to the signed-in person, without a pairing code.

    The browser proves nothing here. A tab running inside Teams asks the host
    for a token that Entra issued for this API, and only the `oid` inside that
    signed token names the person. The bot already stored the conversation it
    opened when the app was installed, so the token is matched against that
    stored row; nothing about the recipient comes from the request body.

    A person without an installed app has no stored conversation. That is not
    an error: there is simply nowhere to deliver yet, and the pairing-code path
    remains the way in.
    """

    identity = authenticated_account(request, mutation=True)
    settings = _settings()
    response.headers["Cache-Control"] = "no-store"
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_SSO_TOKEN_BYTES:
            raise HTTPException(413, "Teams 로그인 토큰이 너무 큽니다.")
    try:
        payload = json.loads(body)
        token = payload["token"]
        if not isinstance(token, str) or not token:
            raise ValueError()
    except (ValueError, UnicodeDecodeError, KeyError, TypeError):
        raise HTTPException(400, "Teams 로그인 토큰 형식을 확인해 주세요.") from None
    claims = await run_in_threadpool(validate_tab_sso_token, token, settings)
    aad_id = claims["oid"]
    with request.app.state.session_factory() as session:
        serial_transaction(session, scope="teams-identities")
        recipient = session.scalar(select(TeamsRecipient).where(
            TeamsRecipient.tenant_id == _uuid(settings.tenant_id),
            TeamsRecipient.aad_object_id == aad_id))
        if recipient is None:
            # The bot has never been installed for this person, so no
            # conversation exists to deliver into.
            return {"connected": False, "reason": "BOT_NOT_INSTALLED",
                    "bot_chat_url": settings.bot_chat_url}
        recipient.active = True
        recipient.account_id = identity.id
        recipient.updated_at = now_utc()
        session.execute(delete(TeamsSessionLink).where(
            TeamsSessionLink.session_id == identity.session_id))
        session.add(TeamsSessionLink(session_id=identity.session_id, account_id=identity.id,
                                     recipient_id=recipient.id, created_at=now_utc()))
        session.execute(delete(TeamsLinkCode).where(
            TeamsLinkCode.session_id == identity.session_id))
        session.commit()
    return {"connected": True, "reason": "SSO_LINKED", "bot_chat_url": settings.bot_chat_url}


def validate_tab_sso_token(token: str, settings: TeamsBotSettings) -> dict:
    """Accept only a token Entra signed for this API, in this tenant, for a person."""

    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise jwt.InvalidTokenError()
        key = jwt.PyJWK.from_dict(_entra_signing_key(header["kid"], settings.sso_keys_url))
        claims = jwt.decode(token, key.key, algorithms=["RS256"],
                            audience=list(settings.sso_audiences), issuer=settings.sso_issuer,
                            leeway=60, options={"require": ["exp", "nbf", "iss", "aud", "sub"]})
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(401, "Teams 로그인 토큰을 확인할 수 없습니다.") from None
    try:
        # A guest or an app-only token carries no personal object id in this
        # tenant, and a delegated token must name the tenant it came from.
        if _uuid(claims["tid"]) != _uuid(settings.tenant_id):
            raise ValueError()
        claims["oid"] = _uuid(claims["oid"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(403, "허용된 조직의 Teams 계정만 연결할 수 있습니다.") from None
    return claims


@router.post("/disconnect")
def disconnect(request: Request, response: Response) -> dict:
    identity = authenticated_account(request, mutation=True)
    with request.app.state.session_factory() as session:
        serial_transaction(session, scope="teams-identities")
        recipient = _linked_recipient(session, identity.session_id, identity.id)
        if recipient:
            recipient.active = False
            recipient.updated_at = now_utc()
            session.execute(delete(TeamsSessionLink).where(TeamsSessionLink.recipient_id == recipient.id))
        session.execute(delete(TeamsLinkCode).where(TeamsLinkCode.session_id == identity.session_id))
        session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"connected": False}


# The Connector names its region first and may append the tenant that owns the
# conversation: /amer/ and /amer/<tenant>/ are both current. The tenant segment
# is matched as a UUID rather than as free text, so widening the path by one
# segment cannot admit a traversal or an arbitrary route.
_CONNECTOR_PATH_RE = re.compile(
    r"/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*"
    r"(?:/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})?"
    r"/?"
)


def trusted_service_url(value: str) -> str:
    """Public-cloud Connector destinations only; no redirects, credentials or query."""
    try:
        parsed = urlsplit(value)
        if (len(value) > 300 or parsed.scheme != "https" or parsed.hostname != "smba.trafficmanager.net"
                or parsed.port not in {None, 443} or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not _CONNECTOR_PATH_RE.fullmatch(parsed.path)):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(403, "Teams 서비스 주소를 확인할 수 없습니다.") from None
    return value.rstrip("/") + "/"


def _entra_signing_key(kid: str, keys_url: str) -> dict:
    """Fetch the tenant's own signing keys, cached and bounded like the Connector's.

    A tab SSO token is signed by Entra for this tenant, not by the Connector,
    so it has its own key set and its own cache. Sharing one cache would let
    either issuer's keys validate the other's token.
    """

    global _entra_key_cache
    with _entra_key_lock:
        fetched, cached_url, keys = _entra_key_cache
        age = time.monotonic() - fetched
        match = next((key for key in keys if key.get("kid") == kid), None)
        if cached_url != keys_url or not keys or age > 3600 or (match is None and age > 30):
            try:
                with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                    result = client.get(keys_url)
                    result.raise_for_status()
                    payload = result.json()
                keys = payload["keys"]
                if not isinstance(keys, list) or not keys or not all(isinstance(key, dict) for key in keys):
                    raise ValueError()
                _entra_key_cache = (time.monotonic(), keys_url, keys)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise HTTPException(503, "Teams 인증을 잠시 후 다시 시도해 주세요.") from None
            match = next((key for key in keys if key.get("kid") == kid), None)
        if not match or match.get("kty") != "RSA" or match.get("alg", "RS256") != "RS256":
            raise HTTPException(401, "Teams 로그인 토큰을 확인할 수 없습니다.")
        return match


def _signing_key(kid: str) -> dict:
    global _key_cache
    with _key_lock:
        fetched, keys = _key_cache
        age = time.monotonic() - fetched
        match = next((key for key in keys if key.get("kid") == kid), None)
        # Refresh expired keys or unknown IDs, bounded even under random-kid traffic.
        if not keys or age > 3600 or (match is None and age > 30):
            try:
                with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                    result = client.get(CONNECTOR_KEYS_URL)
                    result.raise_for_status()
                    payload = result.json()
                keys = payload["keys"]
                if not isinstance(keys, list) or not keys or not all(isinstance(key, dict) for key in keys):
                    raise ValueError()
                _key_cache = (time.monotonic(), keys)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise HTTPException(503, "Teams 인증을 잠시 후 다시 시도해 주세요.") from None
            match = next((key for key in keys if key.get("kid") == kid), None)
        if not match or match.get("kty") != "RSA" or match.get("alg", "RS256") != "RS256":
            raise HTTPException(401, "Teams 요청 인증이 필요합니다.")
        endorsements = match.get("endorsements", [])
        if not isinstance(endorsements, list) or "msteams" not in endorsements:
            raise HTTPException(403, "Teams 채널 서명을 확인할 수 없습니다.")
        return match


def validate_connector_activity(authorization: str, activity: dict, settings: TeamsBotSettings) -> dict:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 16384:
        raise HTTPException(401, "Teams 요청 인증이 필요합니다.")
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise jwt.InvalidTokenError()
        key = jwt.PyJWK.from_dict(_signing_key(header["kid"]))
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=settings.app_id,
                            issuer=CONNECTOR_ISSUER, leeway=60,
                            options={"require": ["exp", "nbf", "iss", "aud"]})
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(401, "Teams 요청 인증이 필요합니다.") from None
    if not isinstance(activity, dict) or activity.get("channelId") != "msteams":
        raise HTTPException(403, "Teams 개인 대화만 지원합니다.")
    service_url = activity.get("serviceUrl")
    # Microsoft SDK uses lower-case serviceurl; REST prose also documents serviceUrl.
    # Both are signed claims. If both occur, require both to match the activity.
    signed_urls = [claims[name] for name in ("serviceurl", "serviceUrl") if name in claims]
    if not isinstance(service_url, str) or not signed_urls or any(value != service_url for value in signed_urls):
        raise HTTPException(403, "Teams 서비스 주소를 확인할 수 없습니다.")
    trusted_service_url(service_url)
    try:
        conversation = activity["conversation"]
        tenant = _uuid(activity["channelData"]["tenant"]["id"])
        if tenant != _uuid(settings.tenant_id) or conversation["conversationType"] != "personal":
            raise ValueError()
        if conversation.get("tenantId") and _uuid(conversation["tenantId"]) != tenant:
            raise ValueError()
        conversation_id = conversation["id"]
        if not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 1024:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(403, "허용된 조직의 Teams 개인 대화만 지원합니다.") from None
    return claims


def _handle_activity(request: Request, activity: dict) -> None:
    """No announcement data or user profile fields are accepted from a bot activity."""
    tenant = _uuid(activity["channelData"]["tenant"]["id"])
    now = now_utc()
    activity_type = activity.get("type")
    if activity_type == "installationUpdate" and activity.get("action") in {"remove", "remove-upgrade"}:
        # Teams removal events can omit the human aadObjectId. The signed personal
        # tenant+conversation reference is sufficient only to revoke a destination.
        with request.app.state.session_factory() as session:
            serial_transaction(session, scope="teams-identities")
            recipients = session.scalars(select(TeamsRecipient).where(
                TeamsRecipient.tenant_id == tenant,
                TeamsRecipient.conversation_id == activity["conversation"]["id"])).all()
            for recipient in recipients:
                recipient.active = False
                recipient.updated_at = now
                session.execute(delete(TeamsSessionLink).where(TeamsSessionLink.recipient_id == recipient.id))
            session.commit()
        return
    sender = activity.get("from") or {}
    try:
        aad_id = _uuid(sender.get("aadObjectId", ""))
    except (ValueError, TypeError, AttributeError):
        # Bot-originated install updates may omit the human sender. They cannot pair.
        return
    text = activity.get("text", "")
    match = re.fullmatch(r"\s*(?:연결|link)\s+([A-Za-z0-9_-]{32})\s*", text, re.I) if isinstance(text, str) else None
    with request.app.state.session_factory() as session:
        serial_transaction(session, scope="teams-identities")
        recipient = session.scalar(select(TeamsRecipient).where(
            TeamsRecipient.tenant_id == tenant, TeamsRecipient.aad_object_id == aad_id))
        if not recipient and (activity_type == "conversationUpdate" or
                              (activity_type == "installationUpdate" and activity.get("action") in {"add", "add-upgrade"})):
            recipient = TeamsRecipient(tenant_id=tenant, aad_object_id=aad_id,
                conversation_id=activity["conversation"]["id"],
                service_url=trusted_service_url(activity["serviceUrl"]),
                created_at=now, updated_at=now, active=False)
            session.add(recipient)
            session.flush()
        if recipient:
            recipient.conversation_id = activity["conversation"]["id"]
            recipient.service_url = trusted_service_url(activity["serviceUrl"])
            recipient.updated_at = now
        if activity_type == "message" and match:
            proof = session.get(TeamsLinkCode, _code_hash(match.group(1)))
            login = session.get(AccountSession, proof.session_id) if proof else None
            account = session.get(DepartmentAccount, proof.account_id) if proof else None
            if (proof and not proof.consumed_at and proof.expires_at > now and login
                    and not login.revoked_at and login.expires_at > now and account and account.active
                    and login.account_id == proof.account_id):
                if not recipient:
                    recipient = TeamsRecipient(tenant_id=tenant, aad_object_id=aad_id,
                        account_id=proof.account_id, conversation_id=activity["conversation"]["id"],
                        service_url=trusted_service_url(activity["serviceUrl"]),
                        created_at=now, updated_at=now, active=True)
                    session.add(recipient)
                    session.flush()
                recipient.active = True
                if recipient.account_id is None:
                    recipient.account_id = proof.account_id
                link = session.get(TeamsSessionLink, proof.session_id)
                if link:
                    link.recipient_id = recipient.id
                    link.account_id = proof.account_id
                    link.created_at = now
                else:
                    session.add(TeamsSessionLink(session_id=proof.session_id, account_id=proof.account_id,
                                                 recipient_id=recipient.id, created_at=now))
                proof.consumed_at = now
        session.commit()


@router.post("/messages", status_code=200)
async def messages(request: Request) -> Response:
    settings = _settings()
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_ACTIVITY_BYTES:
            raise HTTPException(413, "Teams 요청이 너무 큽니다.")
    try:
        activity = json.loads(body)
        if not isinstance(activity, dict):
            raise ValueError()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Teams 요청 형식을 확인해 주세요.") from None
    await run_in_threadpool(validate_connector_activity, request.headers.get("authorization", ""), activity, settings)
    await run_in_threadpool(_handle_activity, request, activity)
    return Response(status_code=200)


class TeamsDeliveryError(RuntimeError):
    def __init__(self, outcome: str, *, status_code: int | None = None, retry_after_seconds: int | None = None):
        super().__init__("Teams delivery " + outcome.lower())
        self.outcome = outcome
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = outcome == "RETRY"
        self.ambiguous = outcome == "UNKNOWN"


def send_personal_card(recipient: TeamsRecipient, card: dict, settings: TeamsBotSettings | None = None) -> str:
    """Send one card; UNKNOWN outcomes must never be automatically retried."""
    settings = settings or TeamsBotSettings.from_env()
    if not settings.enabled or not recipient.active or recipient.tenant_id != _uuid(settings.tenant_id):
        raise TeamsDeliveryError("FAILED")
    try:
        service = trusted_service_url(recipient.service_url)
    except HTTPException:
        raise TeamsDeliveryError("FAILED") from None
    endpoint = service + "v3/conversations/" + quote(recipient.conversation_id, safe="") + "/activities"
    token_endpoint = "https://login.microsoftonline.com/" + _uuid(settings.tenant_id) + "/oauth2/v2.0/token"
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        try:
            token_result = client.post(token_endpoint, data={"grant_type": "client_credentials",
                "client_id": settings.app_id, "client_secret": settings.app_secret,
                "scope": "https://api.botframework.com/.default"})
            token_result.raise_for_status()
            token = token_result.json()["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError()
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            raise TeamsDeliveryError("RETRY") from None
        try:
            result = client.post(endpoint, headers={"Authorization": "Bearer " + token}, json={
                "type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]})
        except httpx.RequestError:
            # The connector may have accepted the activity before the connection failed.
            raise TeamsDeliveryError("UNKNOWN") from None
        if result.status_code == 429 or result.status_code >= 500:
            retry_after = result.headers.get("Retry-After", "")
            raise TeamsDeliveryError("RETRY", status_code=result.status_code,
                retry_after_seconds=min(3600, max(1, int(retry_after))) if retry_after.isdigit() else None)
        if not 200 <= result.status_code < 300:
            raise TeamsDeliveryError("FAILED", status_code=result.status_code)
        try:
            activity_id = result.json()["id"]
            if not isinstance(activity_id, str) or not activity_id:
                raise ValueError()
            return activity_id
        except (ValueError, KeyError, TypeError):
            raise TeamsDeliveryError("UNKNOWN") from None
