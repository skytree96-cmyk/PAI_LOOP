from __future__ import annotations

import secrets
import re

from fastapi import HTTPException, Request, status


_PUBLIC_SAFE_GET_PATHS = {
    "/api/v1/runtime-profile",
    "/api/v1/dashboard",
    # Department coverage repeats the already-public keyword profile and the
    # notice counts behind it. It carries no company fact and no evidence.
    "/api/v1/dashboard/departments",
    "/api/v1/departments/keyword-profiles",
    "/api/v1/company-profile",
    "/api/v1/performance",
    "/api/v1/performance/summary",
    "/api/v1/notices",
    "/api/v1/pre-specifications",
}
_PUBLIC_SAFE_GET_PATTERNS = (
    re.compile(r"^/api/v1/notices/[^/]+$"),
    re.compile(r"^/api/v1/notices/[^/]+/award-history$"),
    re.compile(r"^/api/v1/notices/[^/]+/award-intelligence$"),
    re.compile(r"^/api/v1/notices/[^/]+/quantitative-estimate$"),
    re.compile(r"^/api/v1/notices/[^/]+/analysis/requirement-policy$"),
    re.compile(r"^/api/v1/pre-specifications/[A-Za-z0-9_-]{1,40}$"),
)


def public_read_allowed(request: Request) -> bool:
    """Select the redacted browser representation, never grant anonymous access."""

    settings = request.app.state.settings
    if request.method.upper() != "GET":
        return False
    configured_key: str | None = settings.api_key
    candidate = request.headers.get("X-PAI-LOOP-API-KEY", "")
    if configured_key and candidate and secrets.compare_digest(
        candidate.encode("utf-8"), configured_key.encode("utf-8")
    ):
        return False
    path = request.url.path.rstrip("/") or "/"
    return path in _PUBLIC_SAFE_GET_PATHS or any(pattern.fullmatch(path) for pattern in _PUBLIC_SAFE_GET_PATTERNS)


def require_api_key(request: Request) -> None:
    """Protect all `/api/v1` routes with a constant-time server key check.

    Development/test without a configured key stays local-demo friendly.
    Production cannot start without a key. This remains server-to-server;
    browser clients use the explicit department cookie routes.
    """
    settings = request.app.state.settings
    if public_read_allowed(request):
        from .accounts import authenticated_account
        authenticated_account(request)
        return
    if request.headers.get("x-pai-manual-token"):
        raise HTTPException(status_code=401, detail="부서 계정으로 다시 로그인해 주세요.")
    from .accounts import browser_request
    if request.headers.get("x-pai-loop-api-key") and browser_request(request):
        raise HTTPException(status_code=403, detail="서버 키는 브라우저 계정 권한을 대체할 수 없습니다.")
    configured_key: str | None = settings.api_key
    auth_required = settings.department_accounts_enabled or settings.environment.casefold() == "production" or bool(configured_key)
    if not auth_required:
        return
    candidate = request.headers.get("X-PAI-LOOP-API-KEY", "")
    if not configured_key or not secrets.compare_digest(
        candidate.encode("utf-8"), configured_key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효한 서버 인증이 필요합니다.",
            headers={"WWW-Authenticate": "PAI-Loop-ApiKey"},
        )


def require_private_evidence_access(request: Request) -> None:
    """Protect private company evidence with server or high-entropy auth.

    Department cookies and the retired demo PIN do not grant private data
    access or replacement of the authoritative performance register.
    """

    settings = request.app.state.settings
    if request.headers.get("X-PAI-LOOP-API-KEY") and settings.api_key:
        require_api_key(request)
        return
    expected = (
        settings.private_evidence_token
        if settings.private_evidence_token_valid
        else ""
    )
    candidate = request.headers.get("X-PAI-Private-Evidence-Token", "")
    if not expected or not candidate or not secrets.compare_digest(
        candidate.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="비공개 증빙 전용 인증이 필요합니다.",
            headers={"WWW-Authenticate": "PAI-Private-Evidence"},
        )
