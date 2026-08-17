from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .auth import require_api_key
from .reference_registry import (
    active_reference_metadata,
    sync_packaged_reference_data,
    sync_public_company_profile,
)


router = APIRouter(
    prefix="/api/v1/reference-data",
    tags=["reference data"],
    dependencies=[Depends(require_api_key)],
)


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


@router.get("/versions")
def list_active_reference_versions(session: DbSession) -> dict[str, Any]:
    rows = active_reference_metadata(session)
    return {
        "schema_version": "reference-registry-1.0",
        "active_count": len(rows),
        "versions": rows,
        "payloads_exposed": False,
    }


@router.post("/sync")
def sync_reference_versions(session: DbSession) -> dict[str, Any]:
    reference = sync_packaged_reference_data(session)
    company_profile = sync_public_company_profile(session)
    session.commit()
    return {
        "status": "COMPLETED",
        "reference": reference,
        "company_profile": company_profile,
    }
