"""Opt-in, narrowly scoped department cookie authentication.

Cookies never satisfy the broad API-key or private-evidence dependencies.
Only the explicit operator routes below opt into department permissions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from .account_models import AccountAudit, AccountBootstrapPreview, AccountLoginBucket, AccountSession, DepartmentAccount
from .department_ranking import load_department_keyword_profiles

SESSION_COOKIE = "pai_department_session"
CSRF_COOKIE = "pai_department_csrf"
# Reject pre-cutover cookies on the new app. Initial activation also revokes
# their stored sessions. Login buckets keep their existing hash namespace.
SESSION_HASH_NAMESPACE = "account-login-cutover-v1:"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_BUCKET_LIMIT = 1024
router = APIRouter(prefix="/api/v1/accounts", tags=["department accounts"])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def departments() -> dict[str, str]:
    return {row["id"]: row["name"] for row in load_department_keyword_profiles()["departments"]}


def enabled(request: Request) -> bool:
    return request.app.state.settings.department_accounts_enabled


def _enabled(request: Request) -> None:
    if not enabled(request):
        raise HTTPException(404, "부서 계정 기능이 비활성화되어 있습니다.")


def browser_request(request: Request) -> bool:
    return bool(request.headers.get("origin") or request.headers.get("sec-fetch-site") or request.cookies.get(SESSION_COOKIE))


def same_origin(request: Request) -> None:
    # Import lazily: manual analysis also uses the narrow account dependency.
    from .manual_analysis import _same_origin_request
    if not _same_origin_request(request):
        raise HTTPException(403, "동일한 출처에서만 요청할 수 있습니다.")


def _read_origin(request: Request) -> None:
    site = request.headers.get("sec-fetch-site", "").casefold()
    if site and site not in {"same-origin", "none"}:
        raise HTTPException(403, "동일한 출처에서만 조회할 수 있습니다.")
    if request.headers.get("origin"):
        same_origin(request)


def require_server_bootstrap(request: Request) -> None:
    expected = request.app.state.settings.api_key
    supplied = request.headers.get("x-pai-loop-api-key", "")
    if browser_request(request):
        raise HTTPException(403, "서버 전용 작업입니다.")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(401, "서버 인증이 필요합니다.")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_hash(value: str) -> str:
    return _hash(SESSION_HASH_NAMESPACE + value)


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt-v1${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        version, salt, expected = encoded.split("$")
        if version != "scrypt-v1" or len(salt) != 32 or len(expected) != 64:
            return False
        derived = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
        return secrets.compare_digest(derived.hex(), expected)
    except (ValueError, TypeError):
        return False


# An absent username still performs the same KDF. This is not a usable credential.
_DUMMY_HASH = "scrypt-v1$" + "00" * 16 + "$" + "00" * 32


def serial_transaction(session: Session, *, scope: str = "accounts") -> None:
    if session.in_transaction():
        raise RuntimeError("account transaction requires a clean Session")
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    elif dialect == "postgresql":
        key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    else:
        raise RuntimeError("unsupported account database")


def audit(session: Session, event: str, *, actor: str | None = None, target: str | None = None) -> None:
    session.add(AccountAudit(event=event, actor_account_id=actor, target_id=target, created_at=now_utc()))


@dataclass(frozen=True)
class Identity:
    id: str
    username: str
    role: str
    department_id: str | None
    department_name: str | None
    paid_analysis_allowed: bool
    session_id: str

    @property
    def actor_label(self) -> str:
        return self.department_name or "개발자 관리자"


def authenticated_account(request: Request, *, mutation: bool = False, department_write: bool = False, admin: bool = False, paid: bool = False) -> Identity:
    _enabled(request)
    if request.headers.get("x-pai-loop-api-key"):
        raise HTTPException(403, "부서 계정 경로에는 서버 키를 사용할 수 없습니다.")
    _read_origin(request)
    if mutation:
        same_origin(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or len(token) > 128:
        raise HTTPException(401, "부서 계정 로그인이 필요합니다.")
    with request.app.state.session_factory() as session:
        row = session.scalar(select(AccountSession).where(AccountSession.token_hash == _session_hash(token)))
        account = session.get(DepartmentAccount, row.account_id) if row else None
        if not row or row.revoked_at or row.expires_at <= now_utc() or not account or not account.active:
            raise HTTPException(401, "로그인이 만료되었습니다. 다시 로그인해 주세요.")
        name = departments().get(account.department_id) if account.department_id else None
        if account.role not in {"ADMIN", "DEPARTMENT"} or (account.role == "DEPARTMENT" and name is None):
            raise HTTPException(403, "사용할 수 없는 부서 계정입니다.")
        if mutation:
            csrf = request.headers.get("x-csrf-token", "")
            cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
            if not csrf or len(csrf) > 128 or not secrets.compare_digest(csrf, cookie_csrf) or not secrets.compare_digest(_session_hash(csrf), row.csrf_hash):
                raise HTTPException(403, "요청 인증을 새로 확인해 주세요.")
        if department_write and account.role != "DEPARTMENT":
            raise HTTPException(403, "부서 계정만 자기 부서 기록을 작성할 수 있습니다.")
        if admin and account.role != "ADMIN":
            raise HTTPException(403, "관리자 권한이 필요합니다.")
        if paid and not account.paid_analysis_allowed:
            raise HTTPException(403, "유료 분석 권한이 없습니다.")
        return Identity(account.id, account.username, account.role, account.department_id, name, account.paid_analysis_allowed, row.id)


def account_payload(identity: Identity | None, *, account_enabled: bool, csrf: str | None = None, manual_enabled: bool = False) -> dict:
    return {
        "enabled": account_enabled,
        "authenticated": identity is not None,
        "account": ({"id": identity.id, "username": identity.username, "role": identity.role, "department_id": identity.department_id, "department_name": identity.department_name, "actor_label": identity.actor_label, "paid_analysis_allowed": identity.paid_analysis_allowed} if identity else None),
        "csrf_token": csrf,
        "capabilities": {
            "read_department_records": identity is not None,
            "write_decisions": bool(identity and identity.role == "DEPARTMENT"),
            "write_results": bool(identity and identity.role == "DEPARTMENT"),
            "manage_accounts": bool(identity and identity.role == "ADMIN"),
            "request_paid_analysis": bool(identity and identity.paid_analysis_allowed and manual_enabled),
            "recompute_analysis": bool(identity and manual_enabled),
            "private_evidence": False,
        },
    }


def _me(request: Request, identity: Identity | None, *, csrf: str | None = None) -> dict:
    from .manual_analysis import _manual_feature_enabled
    return account_payload(identity, account_enabled=enabled(request), csrf=csrf, manual_enabled=_manual_feature_enabled(request))


@router.get("/me")
def me(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if not enabled(request):
        return _me(request, None)
    if not request.cookies.get(SESSION_COOKIE):
        _read_origin(request)
        return _me(request, None)
    identity = authenticated_account(request)
    return _me(request, identity, csrf=request.cookies.get(CSRF_COOKIE))


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(InputModel):
    username: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    password: SecretStr = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def canonical_username(cls, value: str) -> str:
        return value.upper()


def _throttle(session: Session, request: Request, username: str) -> list[AccountLoginBucket]:
    now = now_utc()
    session.execute(delete(AccountLoginBucket).where(AccountLoginBucket.expires_at <= now))
    ip = request.client.host if request.client else "unknown"
    # Never trust X-Forwarded-For supplied by a browser. Trusted proxy settings
    # belong to the server. Account and global budgets remain across IP changes.
    keys = [(_hash("global"), 100), (_hash("ip:" + ip), 20), (_hash("username:" + username), 5)]
    rows = [(session.get(AccountLoginBucket, key), key, limit) for key, limit in keys]
    count = session.scalar(select(func.count()).select_from(AccountLoginBucket)) or 0
    if any(row and row.attempts >= limit for row, _, limit in rows) or count + sum(row is None for row, _, _ in rows) > LOGIN_BUCKET_LIMIT:
        session.commit()
        raise HTTPException(429, "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.", headers={"Retry-After": str(LOGIN_WINDOW_SECONDS)})
    # The caller holds the serial transaction through password verification.
    # Charge only failures; success must not consume or reset earlier failures.
    return [row if row is not None else AccountLoginBucket(key=key, attempts=0, expires_at=now + timedelta(seconds=LOGIN_WINDOW_SECONDS)) for row, key, _ in rows]


@router.post("/login")
def login(payload: Login, request: Request, response: Response) -> dict:
    _enabled(request)
    same_origin(request)
    if request.headers.get("x-pai-loop-api-key"):
        raise HTTPException(403, "로그인에는 서버 키를 사용할 수 없습니다.")
    with request.app.state.session_factory() as session:
        serial_transaction(session)
        failure_buckets = _throttle(session, request, payload.username)
        account = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == payload.username))
        valid = verify_password(payload.password.get_secret_value(), account.password_hash if account else _DUMMY_HASH)
        if not valid or not account or not account.active or (account.role == "DEPARTMENT" and account.department_id not in departments()):
            for bucket in failure_buckets:
                bucket.attempts += 1
                session.add(bucket)
            audit(session, "LOGIN_FAILED")
            session.commit()
            raise HTTPException(401, "아이디 또는 비밀번호를 확인해 주세요.")
        now = now_utc()
        session.execute(delete(AccountSession).where(AccountSession.expires_at < now - timedelta(days=1)))
        old = request.cookies.get(SESSION_COOKIE, "")
        if old:
            previous = session.scalar(select(AccountSession).where(AccountSession.token_hash == _session_hash(old)))
            if previous:
                previous.revoked_at = now
        live = list(session.scalars(select(AccountSession).where(AccountSession.account_id == account.id, AccountSession.revoked_at.is_(None), AccountSession.expires_at > now).order_by(AccountSession.created_at)).all())
        for previous in live[:-7] if len(live) >= 8 else []:
            previous.revoked_at = now
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        row = AccountSession(account_id=account.id, token_hash=_session_hash(token), csrf_hash=_session_hash(csrf), created_at=now, expires_at=now + timedelta(seconds=SESSION_SECONDS))
        session.add(row)
        session.flush()
        identity = Identity(account.id, account.username, account.role, account.department_id, departments().get(account.department_id), account.paid_analysis_allowed, row.id)
        audit(session, "LOGIN_SUCCEEDED", actor=account.id, target=row.id)
        session.commit()
    secure = request.url.scheme == "https" or request.app.state.settings.environment.casefold() == "production"
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=secure, samesite="strict", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=SESSION_SECONDS, httponly=False, secure=secure, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return _me(request, identity, csrf=csrf)


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    _enabled(request)
    same_origin(request)
    try:
        identity = authenticated_account(request, mutation=True)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        identity = None  # An expired/revoked cookie should still be cleared.
    if identity:
        with request.app.state.session_factory() as session:
            row = session.get(AccountSession, identity.session_id)
            row.revoked_at = now_utc()
            audit(session, "LOGOUT", actor=identity.id, target=row.id)
            session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"logged_out": True}


class BootstrapAccount(Login):
    password: SecretStr = Field(min_length=12, max_length=256)
    role: Literal["DEPARTMENT", "ADMIN"]
    department_id: str | None = Field(default=None, max_length=120)
    active: bool = False
    paid_analysis_allowed: bool = False

    @model_validator(mode="after")
    def valid_department(self):
        if self.role == "DEPARTMENT" and self.department_id not in departments():
            raise ValueError("허용된 부서 식별자가 필요합니다.")
        if self.role == "ADMIN" and self.department_id is not None:
            raise ValueError("관리자는 부서 신원을 가질 수 없습니다.")
        return self


class Bootstrap(InputModel):
    dry_run: bool = True
    preview_id: str | None = Field(default=None, max_length=36)
    accounts: list[BootstrapAccount] = Field(min_length=1, max_length=25)


@router.post("/bootstrap")
def bootstrap(payload: Bootstrap, request: Request) -> dict:
    # Registration remains available before rollout, but only to an explicit
    # server credential. Neither PIN, development mode nor admin cookies count.
    require_server_bootstrap(request)
    names = [item.username for item in payload.accounts]
    ids = [item.department_id for item in payload.accounts if item.department_id]
    if len(set(names)) != len(names) or len(set(ids)) != len(ids):
        raise HTTPException(422, "중복 계정 또는 부서가 있습니다.")
    plan = [{**item.model_dump(exclude={"password"}), "password": item.password.get_secret_value()} for item in payload.accounts]
    digest = hmac.new(request.app.state.settings.api_key.encode(), json.dumps(plan, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    with request.app.state.session_factory() as session:
        serial_transaction(session)
        now = now_utc()
        session.execute(delete(AccountBootstrapPreview).where(AccountBootstrapPreview.expires_at <= now))
        if not payload.dry_run:
            preview = session.get(AccountBootstrapPreview, payload.preview_id) if payload.preview_id else None
            if not preview or preview.consumed_at or preview.expires_at <= now or not secrets.compare_digest(preview.digest, digest):
                raise HTTPException(409, "동일한 등록 내용으로 먼저 dry_run을 실행해 주세요.")
            preview.consumed_at = now
        results = []
        for item in payload.accounts:
            existing = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == item.username))
            if existing:
                results.append({"username": item.username, "status": "EXISTS_UNCHANGED"})
                continue
            if item.department_id and session.scalar(select(DepartmentAccount.id).where(DepartmentAccount.department_id == item.department_id)):
                raise HTTPException(409, "이미 등록된 부서입니다.")
            results.append({"username": item.username, "status": "WOULD_CREATE" if payload.dry_run else "CREATED"})
            if not payload.dry_run:
                row = DepartmentAccount(**item.model_dump(exclude={"password"}), password_hash=password_hash(item.password.get_secret_value()), created_at=now)
                session.add(row)
                session.flush()
                audit(session, "ACCOUNT_BOOTSTRAPPED", target=row.id)
        preview_id = None
        if payload.dry_run:
            if (session.scalar(select(func.count()).select_from(AccountBootstrapPreview)) or 0) >= 100:
                raise HTTPException(429, "등록 미리보기가 많습니다. 잠시 후 다시 시도해 주세요.")
            preview = AccountBootstrapPreview(digest=digest, expires_at=now + timedelta(minutes=15))
            session.add(preview)
            session.flush()
            preview_id = preview.id
        session.commit()
    return {"dry_run": payload.dry_run, "preview_id": preview_id, "accounts": results}


class InitialAdminActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    dry_run: bool = True
    preview_id: str | None = Field(default=None, max_length=36)

    @field_validator("username")
    @classmethod
    def canonical_username(cls, value: str) -> str:
        return value.upper()


@router.post("/initial-admin-activation")
def activate_initial_admin(payload: InitialAdminActivation, request: Request) -> dict:
    """Activate an unchanged, inactive initial admin with a single-use preview.

    This is a server-only escape from the no-active-admin bootstrap deadlock,
    not an account reset or a way to reactivate deliberately disabled users.
    """
    require_server_bootstrap(request)
    now = now_utc()
    with request.app.state.session_factory() as session:
        serial_transaction(session)
        row = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == payload.username))
        if row is None or row.role != "ADMIN" or row.department_id is not None:
            raise HTTPException(409, "등록된 초기 관리자 정보를 확인해 주세요.")
        if row.active:
            if not payload.dry_run:
                raise HTTPException(409, "이미 활성화되어 있습니다. 조회로 상태를 확인해 주세요.")
            return {"dry_run": True, "status": "ALREADY_ACTIVE", "preview_id": None, "account": _admin_account(row)}
        active_admins = session.scalar(select(func.count()).select_from(DepartmentAccount).where(DepartmentAccount.role == "ADMIN", DepartmentAccount.active.is_(True)))
        if active_admins or row.revision != 1 or row.paid_analysis_allowed:
            raise HTTPException(409, "초기 활성화 조건이 변경되었습니다. 기존 관리자에게 문의해 주세요.")
        # Separate from registration preview digests; bind the exact saved row
        # and revision without asking for, returning, or changing its password.
        plan = {"action": "INITIAL_ADMIN_ACTIVATION_V1", "account": _admin_account(row)}
        digest = hmac.new(request.app.state.settings.api_key.encode(), json.dumps(plan, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        session.execute(delete(AccountBootstrapPreview).where(AccountBootstrapPreview.expires_at <= now))
        if payload.dry_run:
            count = session.scalar(select(func.count()).select_from(AccountBootstrapPreview))
            if count >= 100:
                raise HTTPException(429, "등록 미리보기가 많습니다. 잠시 후 다시 시도해 주세요.")
            preview = AccountBootstrapPreview(digest=digest, expires_at=now + timedelta(minutes=15))
            session.add(preview)
            session.flush()
            result = {"dry_run": True, "status": "WOULD_ACTIVATE", "preview_id": preview.id, "account": _admin_account(row)}
        else:
            preview = session.get(AccountBootstrapPreview, payload.preview_id) if payload.preview_id else None
            if not preview or preview.consumed_at or preview.expires_at <= now or not hmac.compare_digest(preview.digest, digest):
                raise HTTPException(409, "유효한 초기 활성화 미리보기가 필요합니다.")
            preview.consumed_at = now
            row.active = True
            row.revision += 1
            # Revoke persisted pre-cutover sessions too, so reverting an app
            # version cannot resurrect those cookies after activation.
            for saved in session.scalars(select(AccountSession).where(AccountSession.revoked_at.is_(None))):
                saved.revoked_at = now
            audit(session, "INITIAL_ADMIN_ACTIVATED", target=row.id)
            result = {"dry_run": False, "status": "ACTIVATED", "preview_id": None, "account": _admin_account(row)}
        session.commit()
        return result


def _admin_account(row: DepartmentAccount) -> dict:
    return {"id": row.id, "username": row.username, "role": row.role, "department_id": row.department_id, "department_name": departments().get(row.department_id), "active": row.active, "paid_analysis_allowed": row.paid_analysis_allowed, "revision": row.revision}


@router.get("")
def list_accounts(request: Request) -> dict:
    authenticated_account(request, admin=True)
    with request.app.state.session_factory() as session:
        return {"accounts": [_admin_account(row) for row in session.scalars(select(DepartmentAccount).order_by(DepartmentAccount.username)).all()]}


class AccountUpdate(InputModel):
    expected_revision: int = Field(ge=1)
    active: bool | None = None
    paid_analysis_allowed: bool | None = None
    password: SecretStr | None = Field(default=None, min_length=12, max_length=256)


@router.patch("/{account_id}")
def update_account(account_id: str, payload: AccountUpdate, request: Request) -> dict:
    identity = authenticated_account(request, mutation=True, admin=True)
    with request.app.state.session_factory() as session:
        serial_transaction(session)
        row = session.get(DepartmentAccount, account_id)
        if not row:
            raise HTTPException(404, "계정을 찾을 수 없습니다.")
        if row.revision != payload.expected_revision:
            raise HTTPException(409, "계정 정보가 갱신되었습니다.")
        if row.role == "ADMIN" and row.active and payload.active is False:
            active_admins = session.scalar(select(func.count()).select_from(DepartmentAccount).where(DepartmentAccount.role == "ADMIN", DepartmentAccount.active.is_(True)))
            if active_admins <= 1:
                raise HTTPException(409, "마지막 활성 관리자는 비활성화할 수 없습니다.")
        if payload.active is not None:
            row.active = payload.active
        if payload.paid_analysis_allowed is not None:
            row.paid_analysis_allowed = payload.paid_analysis_allowed
        if payload.password is not None:
            row.password_hash = password_hash(payload.password.get_secret_value())
        row.revision += 1
        for item in session.scalars(select(AccountSession).where(AccountSession.account_id == row.id, AccountSession.revoked_at.is_(None))).all():
            item.revoked_at = now_utc()
        audit(session, "ACCOUNT_UPDATED_SESSIONS_REVOKED", actor=identity.id, target=row.id)
        result = _admin_account(row)
        session.commit()
        return result


@router.get("/sessions/list")
def list_sessions(request: Request) -> dict:
    authenticated_account(request, admin=True)
    with request.app.state.session_factory() as session:
        return {"sessions": [{"id": row.id, "account_id": row.account_id, "created_at": row.created_at, "expires_at": row.expires_at, "revoked_at": row.revoked_at} for row in session.scalars(select(AccountSession).order_by(AccountSession.created_at.desc()).limit(200)).all()]}


@router.post("/sessions/{session_id}/revoke")
def revoke_session(session_id: str, request: Request) -> dict:
    identity = authenticated_account(request, mutation=True, admin=True)
    with request.app.state.session_factory() as session:
        row = session.get(AccountSession, session_id)
        if not row:
            raise HTTPException(404, "세션을 찾을 수 없습니다.")
        row.revoked_at = now_utc()
        audit(session, "SESSION_REVOKED", actor=identity.id, target=row.id)
        session.commit()
    return {"revoked": True}


@router.get("/audit/list")
def list_audit(request: Request) -> dict:
    authenticated_account(request, admin=True)
    with request.app.state.session_factory() as session:
        return {"events": [{"id": row.id, "event": row.event, "actor_account_id": row.actor_account_id, "target_id": row.target_id, "created_at": row.created_at} for row in session.scalars(select(AccountAudit).order_by(AccountAudit.created_at.desc()).limit(200)).all()]}
