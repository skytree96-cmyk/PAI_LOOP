"""The Render work app is closed until an active department account signs in."""
from __future__ import annotations

import secrets
import os

from fastapi import HTTPException, Request

from .accounts import authenticated_account, browser_request
from .auth import require_private_evidence_access


FRONTEND_PATHS = frozenset({
    "/", "/index.html", "/notices", "/reviews", "/urgent", "/fail",
    "/cancelled", "/result-missing", "/decisions", "/results", "/awards",
    "/prespec", "/performance",
    # Work-pipeline queues. A reload or a shared link on one of these must
    # reach the frontend instead of being refused before the login page.
    "/pending-decision", "/in-progress", "/urgent-in-progress", "/result-entry",
})
_ASSETS = frozenset({
    "/favicon.svg", "/styles.css", "/app.js", "/teams-icon.png",
    "/login-gate.css", "/login.js",
})
_ACCOUNT_ENTRY = frozenset({
    "/api/v1/accounts/me", "/api/v1/accounts/login", "/api/v1/accounts/logout",
})


def require_app_access(request: Request) -> None:
    """Check entry access; individual routes still enforce role, CSRF and paid grants."""
    path = request.url.path
    if request.method == "OPTIONS":
        return  # CORS preflight contains no application data.
    if request.method == "POST" and path == "/api/v1/teams/messages":
        return  # Exact callback validates Microsoft Connector JWT, audience and tenant itself.
    if (request.method in {"GET", "HEAD"} and path == "/teams-config.html"
            and os.getenv("PAI_TEAMS_TAB_AUTH_ENABLED", "").lower() == "true"):
        return  # Data-free tab configuration must load before account login.
    if request.method in {"GET", "HEAD"} and (
        path in FRONTEND_PATHS or path in _ASSETS or path == "/healthz"
    ):
        return  # Frontend routes choose a data-free login page themselves.
    if path.rstrip("/") in _ACCOUNT_ENTRY:
        return  # These endpoints validate their own session/origin/CSRF contract.

    if request.headers.get("x-pai-manual-token"):
        raise HTTPException(401, "부서 계정으로 로그인해 주세요.")
    candidate = request.headers.get("x-pai-loop-api-key", "")
    if candidate:
        if browser_request(request):
            raise HTTPException(403, "서버 키는 브라우저 계정 권한을 대체할 수 없습니다.")
        expected = request.app.state.settings.api_key
        if not expected or not secrets.compare_digest(candidate.encode(), expected.encode()):
            raise HTTPException(401, "유효한 서버 인증이 필요합니다.")
        return
    # Preserve the existing separate evidence authority, only on its own routes.
    if request.headers.get("x-pai-private-evidence-token") and (
        path == "/api/v1/performance-records" or path.startswith("/api/v1/performance-records/")
        or path == "/api/v1/operator-evidence" or path.startswith("/api/v1/operator-evidence/")
    ):
        require_private_evidence_access(request)
        return
    if not request.app.state.settings.department_accounts_enabled:
        raise HTTPException(401, "현재 계정 로그인을 사용할 수 없습니다.")
    request.state.app_identity = authenticated_account(request)
