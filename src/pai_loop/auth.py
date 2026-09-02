from __future__ import annotations

import secrets
import re

from fastapi import HTTPException, Request, status


_PUBLIC_SAFE_GET_PATHS = {
    "/api/v1/runtime-profile",
    "/api/v1/dashboard",
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
    """Return true only for the publication-safe contest-demo GET surface."""

    settings = request.app.state.settings
    if not settings.public_read_only or request.method.upper() != "GET":
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
    Production cannot start without a key. This is an interim server-to-server
    boundary; browser clients must use a trusted backend/BFF until Teams Entra
    SSO and role-based authorization replace it.
    """
    settings = request.app.state.settings
    if public_read_allowed(request):
        return
    configured_key: str | None = settings.api_key
    auth_required = settings.environment.casefold() == "production" or bool(configured_key)
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

    The four-digit demo operator PIN is intentionally not accepted here. It is
    suitable only for bounded public-demo actions, not private data access or
    replacement of the authoritative performance register.
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
