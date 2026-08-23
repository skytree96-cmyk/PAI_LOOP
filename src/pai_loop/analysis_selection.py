from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import NoticeAnalysisPolicy


MANUAL_ONLY_POLICY = "MANUAL_ONLY"
_POLICY_LOOKUP_BATCH_SIZE = 500


def manual_only_notice_keys(
    session: Session,
    candidate_keys: Iterable[str],
) -> set[str]:
    """Return durable operator-selected notices excluded from automation.

    A manual PPS save writes this policy before exposing the notice. The
    dedicated table is outside operational-log retention and is queried only
    for the bounded candidate keys supplied by daily/backfill planners. The
    explicit same-origin manual-analysis endpoint still loads Notice directly.
    """

    candidates = set(candidate_keys)
    if not candidates:
        return set()
    ordered = sorted(candidates)
    marked: set[str] = set()
    for offset in range(0, len(ordered), _POLICY_LOOKUP_BATCH_SIZE):
        batch = ordered[offset : offset + _POLICY_LOOKUP_BATCH_SIZE]
        statement = select(NoticeAnalysisPolicy.notice_key).where(
            NoticeAnalysisPolicy.analysis_policy == MANUAL_ONLY_POLICY,
            NoticeAnalysisPolicy.notice_key.in_(batch),
        )
        marked.update(session.scalars(statement).all())
    return marked
