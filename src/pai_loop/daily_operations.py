from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from .auth import require_api_key
from .award_intelligence import build_award_intelligence
from .department_ranking import (
    rank_notice_across_departments,
    rank_notice_review_candidates,
    route_notice_across_regions,
)
from .models import (
    AnalysisRun,
    AwardHistoryItem,
    CompanyFact,
    Evaluation,
    IngestionJob,
    MockNotification,
    Notice,
)
from .notice_freshness import latest_current_analysis_run, latest_current_evaluation
from .quantitative_scoring import estimate_for_notice
from .pps_enrichment import public_analysis_reason


router = APIRouter(
    prefix="/api/v1/operations",
    tags=["operations"],
    dependencies=[Depends(require_api_key)],
)


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RetentionRequest(ApiModel):
    retention_days: int = Field(default=7, ge=1, le=30)
    dry_run: bool = True


class RetentionResult(ApiModel):
    cutoff_at: datetime
    retention_days: int
    dry_run: bool
    eligible: dict[str, int]
    deleted: dict[str, int]
    scope: list[str]
    preserved: list[str]
    note: str


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_evaluation(notice: Notice) -> Evaluation | None:
    return latest_current_evaluation(notice)


def _latest_analysis_snapshot(notice: Notice) -> dict[str, Any] | None:
    run = latest_current_analysis_run(notice)
    if run is None:
        return None
    return {
        "analysis_run_id": run.id,
        "status": run.status,
        "generated_at": run.generated_at,
        "input_sha256": run.input_sha256,
        "basis_versions": run.basis_versions,
        "output_summary": run.output_summary,
        "scores": [
            {
                "score_key": item.score_key,
                "value": item.value,
                "lower_value": item.lower_value,
                "upper_value": item.upper_value,
                "status": item.status,
                "band": item.band,
                "method_version": item.method_version,
            }
            for item in run.scores
        ],
        "recommendations": [
            {
                "recommendation_key": item.recommendation_key,
                "department_id": item.department_id,
                "rank": item.rank,
                "priority_score": item.priority_score,
                "recommendation": item.recommendation,
                "risk_band": item.risk_band,
            }
            for item in run.recommendations
        ],
    }


def _award_snapshot(items: list[AwardHistoryItem]) -> dict[str, Any]:
    """Return compact, decision-safe signals for the daily card.

    This is deliberately not a price prediction engine.  The richer pricing
    endpoint can be attached later; the daily workflow simply carries its
    result when present.  Here we expose only observations that already exist.
    """

    if not items:
        return {
            "observations": 0,
            "distinct_winners": 0,
            "dominant_winner": None,
            "dominant_share_pct": None,
            "median_award_rate_pct": None,
            "note": "비교 가능한 최근 낙찰 후보가 아직 없습니다.",
        }
    winners = Counter(item.winner_name for item in items if item.winner_name)
    dominant_winner = None
    dominant_share = None
    if winners:
        dominant_winner, dominant_count = winners.most_common(1)[0]
        dominant_share = round(100 * dominant_count / sum(winners.values()), 1)
    rates = [float(item.award_rate) for item in items if item.award_rate is not None]
    return {
        "observations": len(items),
        "distinct_winners": len(winners),
        "dominant_winner": dominant_winner,
        "dominant_share_pct": dominant_share,
        "median_award_rate_pct": round(median(rates), 3) if rates else None,
        "note": "제목 유사 후보의 관측 요약이며 동일 사업·독점 또는 적정 투찰가를 확정하지 않습니다.",
    }


def _briefing_notice(
    notice: Notice,
    *,
    as_of: datetime,
    company_facts: tuple[CompanyFact, ...] = (),
) -> dict[str, Any]:
    latest = _latest_evaluation(notice)
    source_kind = "PPS" if notice.notice_key.upper().startswith("PPS-") else "MANUAL"
    analysis_reason = public_analysis_reason(
        notice.versions,
        evaluated=latest is not None,
        source_kind=source_kind,
    )
    analysis_updated_at = max(
        (_as_utc(item.created_at) for item in notice.versions),
        default=None,
    )
    departments = rank_notice_across_departments(
        title=notice.title,
        agency=notice.agency,
        category=notice.category or "",
        limit=3,
    )
    department_review_candidates = rank_notice_review_candidates(
        title=notice.title,
        agency=notice.agency,
        category=notice.category or "",
        limit=3,
    )
    region_routing = route_notice_across_regions(
        title=notice.title,
        agency=notice.agency,
        category=notice.category or "",
        limit=2,
    )
    fit = {
        "eligibility": latest.eligibility if latest else "PENDING",
        "reason_code": latest.reason_code if latest else "NOT_EVALUATED",
        "readiness_score": latest.readiness_score if latest else None,
        "readiness_status": latest.readiness_status if latest else "GRAY",
        "evidence_coverage": latest.evidence_coverage if latest else None,
        "risk_score": latest.risk_score if latest else None,
        "risk_band": latest.risk_band if latest else "UNKNOWN",
    }
    eligibility_weight = {"PASS": 30, "REVIEW": 20, "PENDING": 10, "FAIL": 0}[fit["eligibility"]]
    department_score = float(departments[0]["score"]) if departments else 0.0
    readiness_score = float(fit["readiness_score"] or 0.0)
    priority_score = round(min(100.0, eligibility_weight + 0.4 * department_score + 0.3 * readiness_score), 1)
    pricing_intelligence = build_award_intelligence(
        notice.award_history,
        as_of=as_of,
        target_estimated_price=notice.estimated_amount,
    )
    return {
        "notice_key": notice.notice_key,
        "bid_notice_no": notice.bid_notice_no,
        "revision_no": notice.revision_no,
        "title": notice.title,
        "agency": notice.agency,
        "published_at": notice.published_at,
        "deadline": notice.deadline,
        "status": notice.status,
        "estimated_amount": notice.estimated_amount,
        "priority_score": priority_score,
        "fit": fit,
        "top_departments": departments,
        "department_review_candidates": department_review_candidates,
        "region_routing": region_routing,
        "award_snapshot": _award_snapshot(notice.award_history),
        "competition_risk": pricing_intelligence["competition_risk"],
        "quantitative_estimate": estimate_for_notice(
            notice,
            company_facts,
        ).model_dump(mode="json"),
        "pricing_intelligence": pricing_intelligence,
        "analysis_snapshot": _latest_analysis_snapshot(notice),
        "analysis_coverage": {
            "state": analysis_reason.state,
            "reason_code": analysis_reason.reason_code,
            "reason": analysis_reason.reason,
            "attempted": analysis_reason.attempted,
            "updated_at": analysis_updated_at,
        },
    }


@router.get("/daily-briefing")
def daily_briefing(
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=30)] = 7,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Build the stored-data daily feed without calling PPS, OpenAI, or Teams."""

    generated_at = _as_utc(as_of or datetime.now(timezone.utc))
    window_start = generated_at - timedelta(days=days)
    observed_at = func.coalesce(Notice.published_at, Notice.created_at)
    notices = list(
        session.scalars(
            select(Notice)
            .where(
                observed_at >= window_start,
                observed_at <= generated_at,
                Notice.status == "OPEN",
                Notice.deadline >= generated_at,
            )
            .options(
                selectinload(Notice.evaluations),
                selectinload(Notice.versions),
                selectinload(Notice.award_history),
                selectinload(Notice.analysis_runs).selectinload(AnalysisRun.scores),
                selectinload(Notice.analysis_runs).selectinload(AnalysisRun.recommendations),
            )
            .order_by(observed_at.desc())
        ).all()
    )
    company_facts = tuple(
        session.scalars(
            select(CompanyFact).options(selectinload(CompanyFact.evidence))
        ).all()
    )
    items = [
        _briefing_notice(
            notice,
            as_of=generated_at,
            company_facts=company_facts,
        )
        for notice in notices
    ]
    items.sort(
        key=lambda item: (
            -float(item["priority_score"]),
            _as_utc(item["deadline"]),
        )
    )
    selected = items[:limit]
    # Keep the operator-facing ranking separate from the bounded analysis queue.
    # The latter must advance through the backlog instead of repeatedly sending
    # the same top three newly-ingested notices to the enrichment pipeline.
    never_attempted = [
        item
        for item in items
        if item["analysis_coverage"]["reason_code"] == "NOT_SELECTED"
    ]
    retryable = [
        item
        for item in items
        if item["analysis_coverage"]["reason_code"]
        in {
            "HWPX_EXTRACT_FAILED",
            "PDF_EXTRACT_FAILED",
            "DOCUMENT_EXTRACT_FAILED",
            "OPENAI_REVIEW",
            "QUOTE_UNVERIFIED",
        }
        or (
            item["analysis_coverage"]["reason_code"] == "ANALYZED"
            and item["analysis_snapshot"] is None
        )
    ]
    retryable.sort(
        key=lambda item: item["analysis_coverage"]["updated_at"]
        or datetime.min.replace(tzinfo=timezone.utc)
    )
    pending_analysis = never_attempted + retryable
    bounded_pending_analysis = pending_analysis[:50]
    never_attempted_keys = {
        item["notice_key"] for item in never_attempted
    }
    bounded_never_attempted_notice_keys = [
        item["notice_key"]
        for item in bounded_pending_analysis
        if item["notice_key"] in never_attempted_keys
    ]
    bounded_retryable_notice_keys = [
        item["notice_key"]
        for item in bounded_pending_analysis
        if item["notice_key"] not in never_attempted_keys
    ]
    deferred_terminal_total = sum(
        item["analysis_coverage"]["reason_code"]
        in {"ATTACHMENT_NONE", "HWP_ONLY_UNSUPPORTED", "UNSUPPORTED_ATTACHMENT"}
        for item in items
    )
    eligibility_counts = dict(Counter(item["fit"]["eligibility"] for item in selected))
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "timezone": "Asia/Seoul",
        "window": {
            "days": days,
            "from": window_start,
            "to": generated_at,
        },
        "totals": {
            "observed": len(items),
            "included": len(selected),
            "pending_or_review": sum(
                item["fit"]["eligibility"] in {"PENDING", "REVIEW"} for item in selected
            ),
            "eligibility": eligibility_counts,
        },
        "notices": selected,
        "analysis_queue": {
            "policy": "NEVER_ATTEMPTED_THEN_OLDEST_RETRY",
            "pending_total": len(pending_analysis),
            "never_attempted_total": len(never_attempted),
            "retryable_total": len(retryable),
            "deferred_terminal_total": deferred_terminal_total,
            "notice_keys": [
                *bounded_never_attempted_notice_keys,
                *bounded_retryable_notice_keys,
            ],
            "never_attempted_notice_keys": bounded_never_attempted_notice_keys,
            "retryable_notice_keys": bounded_retryable_notice_keys,
            "limit": 50,
            "note": "미시도 공고를 먼저 처리하고 실패 건은 가장 오래된 시도부터 재검토합니다. 첨부 없음·미지원 형식은 manifest가 바뀔 때까지 자동 재시도하지 않습니다.",
        },
        "delivery": {
            "channel": "teams",
            "mode": "mock",
            "actual_push_sent": False,
        },
        "source_calls": {"pps": 0, "openai": 0, "teams": 0},
    }


@router.post("/retention", response_model=RetentionResult)
def apply_operational_retention(
    payload: RetentionRequest,
    session: DbSession,
) -> RetentionResult:
    """Preview or prune short-lived orchestration logs.

    Canonical notices, decisions, evaluations and three-year award observations
    remain available for audit and learning.  The seven-day rule applies to the
    morning feed window and disposable execution/notification logs only.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(days=payload.retention_days)
    completed_jobs = (
        IngestionJob.completed_at.is_not(None)
        & (IngestionJob.completed_at < cutoff)
        & (IngestionJob.status != "RUNNING")
    )
    old_notifications = MockNotification.created_at < cutoff
    eligible = {
        "ingestion_jobs": int(
            session.scalar(select(func.count(IngestionJob.id)).where(completed_jobs)) or 0
        ),
        "mock_notifications": int(
            session.scalar(select(func.count(MockNotification.id)).where(old_notifications)) or 0
        ),
    }
    deleted = {"ingestion_jobs": 0, "mock_notifications": 0}
    if not payload.dry_run:
        deleted["ingestion_jobs"] = int(
            session.execute(delete(IngestionJob).where(completed_jobs)).rowcount or 0
        )
        deleted["mock_notifications"] = int(
            session.execute(delete(MockNotification).where(old_notifications)).rowcount or 0
        )
        session.commit()

    return RetentionResult(
        cutoff_at=cutoff,
        retention_days=payload.retention_days,
        dry_run=payload.dry_run,
        eligible=eligible,
        deleted=deleted,
        scope=["completed ingestion_jobs", "mock_notifications"],
        preserved=[
            "notices",
            "evaluations",
            "user_decisions",
            "award_history_items",
            "pps_notice_authorities",
        ],
        note=(
            "dry-run: 삭제 대상 수만 계산했습니다."
            if payload.dry_run
            else "단기 운영 로그만 삭제했습니다. 핵심 의사결정 기록은 보존했습니다."
        ),
    )
