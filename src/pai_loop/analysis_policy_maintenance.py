from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .analysis_selection import MANUAL_ONLY_POLICY
from .auth import require_api_key
from .models import Notice, NoticeAnalysisPolicy
from .notice_freshness import authoritative_pps_cancelled_notice_keys


MANUAL_ONLY_CLEANUP_CONFIRMATION = "DELETE_ACTIVE_MANUAL_ONLY_POLICIES"
MAX_RETURNED_NOTICE_KEYS = 100


class ManualOnlyPolicyCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool = False
    confirm: str | None = Field(default=None, max_length=80)


class ManualOnlyPolicyCleanupResponse(BaseModel):
    dry_run: bool
    applied: bool
    confirmation_required: str
    candidate_count: int
    deleted_count: int
    notice_keys: list[str]
    notice_keys_truncated: bool
    candidate_notice_keys_sha256: str


router = APIRouter(
    prefix="/api/v1/maintenance",
    tags=["maintenance"],
    dependencies=[Depends(require_api_key)],
)


def _candidate_digest(notice_keys: list[str]) -> str:
    canonical = json.dumps(
        notice_keys,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@router.post(
    "/legacy-manual-only-policies",
    response_model=ManualOnlyPolicyCleanupResponse,
)
def cleanup_legacy_manual_only_policies(
    request: Request,
    payload: ManualOnlyPolicyCleanupRequest = Body(
        default_factory=ManualOnlyPolicyCleanupRequest
    ),
) -> ManualOnlyPolicyCleanupResponse:
    """Preview or remove only obsolete active-notice MANUAL_ONLY markers."""

    if payload.apply and payload.confirm != MANUAL_ONLY_CLEANUP_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "MAINTENANCE_CONFIRMATION_REQUIRED",
                "confirmation_required": MANUAL_ONLY_CLEANUP_CONFIRMATION,
            },
        )

    now = datetime.now(timezone.utc)
    with request.app.state.session_factory() as session:
        rows = session.execute(
            select(NoticeAnalysisPolicy, Notice)
            .join(
                Notice,
                Notice.notice_key == NoticeAnalysisPolicy.notice_key,
            )
            .where(
                NoticeAnalysisPolicy.analysis_policy == MANUAL_ONLY_POLICY,
                Notice.status == "OPEN",
                Notice.deadline >= now,
            )
            .order_by(NoticeAnalysisPolicy.notice_key)
        ).all()
        cancelled_keys = authoritative_pps_cancelled_notice_keys(
            session,
            [notice for _policy, notice in rows],
        )
        candidates = [
            policy
            for policy, notice in rows
            if notice.notice_key not in cancelled_keys
        ]
        candidate_keys = [policy.notice_key for policy in candidates]

        deleted_count = 0
        if payload.apply:
            for policy in candidates:
                session.delete(policy)
            session.commit()
            deleted_count = len(candidates)

    return ManualOnlyPolicyCleanupResponse(
        dry_run=not payload.apply,
        applied=payload.apply,
        confirmation_required=MANUAL_ONLY_CLEANUP_CONFIRMATION,
        candidate_count=len(candidate_keys),
        deleted_count=deleted_count,
        notice_keys=candidate_keys[:MAX_RETURNED_NOTICE_KEYS],
        notice_keys_truncated=len(candidate_keys) > MAX_RETURNED_NOTICE_KEYS,
        candidate_notice_keys_sha256=_candidate_digest(candidate_keys),
    )
