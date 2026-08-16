from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status


def require_api_key(request: Request) -> None:
    """Protect all `/api/v1` routes with a constant-time server key check.

    Development/test without a configured key stays local-demo friendly.
    Production cannot start without a key. This is an interim server-to-server
    boundary; browser clients must use a trusted backend/BFF until Teams Entra
    SSO and role-based authorization replace it.
    """
    settings = request.app.state.settings
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

