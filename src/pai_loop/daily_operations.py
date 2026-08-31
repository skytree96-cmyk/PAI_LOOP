from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, load_only, selectinload

from .analysis_pipeline import PIPELINE_VERSION
from .auth import require_api_key
from .analysis_selection import manual_only_notice_keys
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
    CompanyPerformanceRecord,
    Evaluation,
    IngestionJob,
    MockNotification,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    ScoreSnapshot,
)
from .eligibility_policy import POLICY_VERSION
from .notice_freshness import (
    analysis_basis_is_current,
    analysis_run_versions_are_current,
)
from .quantitative_scoring import estimate_for_notice
from .pps_enrichment import PPS_METADATA_KIND, public_analysis_reason


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


_DAILY_BRIEFING_BATCH_SIZE = 25


def _latest_evaluation(
    notice: Notice,
    evaluations: list[Evaluation],
) -> Evaluation | None:
    has_pps_material = any(
        isinstance(version.source_payload, dict)
        and version.source_payload.get("kind") == PPS_METADATA_KIND
        for version in notice.versions
    )
    for evaluation in evaluations:
        if not analysis_basis_is_current(notice, evaluation.notice_version_id):
            continue
        if has_pps_material and _as_utc(evaluation.deadline_snapshot_at) != _as_utc(
            notice.deadline
        ):
            continue
        return evaluation
    return None


def _latest_analysis_snapshot(run: AnalysisRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    basis_versions = (
        run.basis_versions if isinstance(run.basis_versions, dict) else {}
    )
    return {
        "analysis_run_id": run.id,
        "status": run.status,
        "generated_at": run.generated_at,
        "input_sha256": run.input_sha256,
        "basis_versions": basis_versions,
        "pipeline_version": basis_versions.get("pipeline"),
        "policy_version": basis_versions.get("requirement_policy"),
        "version_current": analysis_run_versions_are_current(
            run,
            pipeline_version=PIPELINE_VERSION,
            policy_version=POLICY_VERSION,
        ),
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
    latest_evaluation: Evaluation | None,
    latest_analysis_run: AnalysisRun | None,
    company_facts: tuple[CompanyFact, ...] = (),
    performance_records: tuple[CompanyPerformanceRecord, ...] = (),
) -> dict[str, Any]:
    latest = latest_evaluation
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
        "quantitative_estimate": (
            estimate_for_notice(notice, company_facts, performance_records)
            if performance_records
            else estimate_for_notice(notice, company_facts)
        ).model_dump(mode="json"),
        "pricing_intelligence": pricing_intelligence,
        "analysis_snapshot": _latest_analysis_snapshot(latest_analysis_run),
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
    notice_ids = list(
        session.scalars(
            select(Notice.id)
            .where(
                observed_at >= window_start,
                observed_at <= generated_at,
                Notice.status == "OPEN",
                Notice.deadline >= generated_at,
            )
            .order_by(observed_at.desc())
        ).all()
    )
    company_facts = tuple(
        session.scalars(
            select(CompanyFact).options(selectinload(CompanyFact.evidence))
        ).all()
    )
    performance_records = tuple(
        session.scalars(
            select(CompanyPerformanceRecord).where(
                CompanyPerformanceRecord.record_status == "VALIDATED"
            )
        ).all()
    )
    items: list[dict[str, Any]] = []
    for start in range(0, len(notice_ids), _DAILY_BRIEFING_BATCH_SIZE):
        batch_ids = notice_ids[start : start + _DAILY_BRIEFING_BATCH_SIZE]
        notices = list(
            session.scalars(
                select(Notice)
                .where(Notice.id.in_(batch_ids))
                .options(
                    selectinload(Notice.versions).load_only(
                        NoticeVersion.id,
                        NoticeVersion.notice_id,
                        NoticeVersion.version_no,
                        NoticeVersion.file_sha256,
                        NoticeVersion.document_complete,
                        NoticeVersion.extraction_status,
                        NoticeVersion.source_payload,
                        NoticeVersion.created_at,
                    ),
                    selectinload(Notice.award_history),
                )
            ).all()
        )
        notices_by_id = {notice.id: notice for notice in notices}

        evaluation_rows = list(
            session.scalars(
                select(Evaluation)
                .where(Evaluation.notice_id.in_(batch_ids))
                .options(
                    load_only(
                        Evaluation.id,
                        Evaluation.notice_id,
                        Evaluation.notice_version_id,
                        Evaluation.evaluated_at,
                        Evaluation.deadline_snapshot_at,
                        Evaluation.eligibility,
                        Evaluation.reason_code,
                        Evaluation.readiness_score,
                        Evaluation.readiness_status,
                        Evaluation.evidence_coverage,
                        Evaluation.risk_score,
                        Evaluation.risk_band,
                    )
                )
                .order_by(Evaluation.evaluated_at.desc())
            ).all()
        )
        evaluations_by_notice: dict[str, list[Evaluation]] = {
            notice_id: [] for notice_id in batch_ids
        }
        for evaluation in evaluation_rows:
            evaluations_by_notice[evaluation.notice_id].append(evaluation)
        latest_evaluations = {
            notice_id: _latest_evaluation(
                notices_by_id[notice_id],
                evaluations_by_notice[notice_id],
            )
            for notice_id in batch_ids
        }

        latest_run_ids: dict[str, str] = {}
        run_references = session.execute(
            select(
                AnalysisRun.id,
                AnalysisRun.notice_id,
                AnalysisRun.notice_version_id,
                AnalysisRun.generated_at,
            )
            .where(AnalysisRun.notice_id.in_(batch_ids))
            .order_by(AnalysisRun.generated_at.desc())
        ).all()
        for run_id, notice_id, notice_version_id, _generated_at in run_references:
            if notice_id in latest_run_ids:
                continue
            if analysis_basis_is_current(
                notices_by_id[notice_id],
                notice_version_id,
            ):
                latest_run_ids[notice_id] = run_id

        selected_runs = (
            list(
                session.scalars(
                    select(AnalysisRun)
                    .where(AnalysisRun.id.in_(latest_run_ids.values()))
                    .options(
                        load_only(
                            AnalysisRun.id,
                            AnalysisRun.notice_id,
                            AnalysisRun.notice_version_id,
                            AnalysisRun.status,
                            AnalysisRun.input_sha256,
                            AnalysisRun.basis_versions,
                            AnalysisRun.output_summary,
                            AnalysisRun.generated_at,
                        ),
                        selectinload(AnalysisRun.scores).load_only(
                            ScoreSnapshot.id,
                            ScoreSnapshot.analysis_run_id,
                            ScoreSnapshot.score_key,
                            ScoreSnapshot.value,
                            ScoreSnapshot.lower_value,
                            ScoreSnapshot.upper_value,
                            ScoreSnapshot.status,
                            ScoreSnapshot.band,
                            ScoreSnapshot.method_version,
                        ),
                        selectinload(AnalysisRun.recommendations).load_only(
                            RecommendationSnapshot.id,
                            RecommendationSnapshot.analysis_run_id,
                            RecommendationSnapshot.recommendation_key,
                            RecommendationSnapshot.department_id,
                            RecommendationSnapshot.rank,
                            RecommendationSnapshot.priority_score,
                            RecommendationSnapshot.recommendation,
                            RecommendationSnapshot.risk_band,
                        ),
                    )
                ).all()
            )
            if latest_run_ids
            else []
        )
        runs_by_id = {run.id: run for run in selected_runs}

        for notice_id in batch_ids:
            notice = notices_by_id[notice_id]
            items.append(
                _briefing_notice(
                    notice,
                    as_of=generated_at,
                    latest_evaluation=latest_evaluations[notice_id],
                    latest_analysis_run=runs_by_id.get(
                        latest_run_ids.get(notice_id, "")
                    ),
                    company_facts=company_facts,
                    performance_records=performance_records,
                )
            )
        session.expunge_all()
    items.sort(
        key=lambda item: (
            -float(item["priority_score"]),
            _as_utc(item["deadline"]),
        )
    )
    selected = items[:limit]
    manual_only_keys = manual_only_notice_keys(
        session,
        (item["notice_key"] for item in items),
    )
    # Keep the operator-facing ranking separate from the bounded analysis queue.
    # The latter must advance through the backlog instead of repeatedly sending
    # the same top three newly-ingested notices to the enrichment pipeline.
    never_attempted = [
        item
        for item in items
        if item["notice_key"] not in manual_only_keys
        if item["analysis_coverage"]["reason_code"] == "NOT_SELECTED"
    ]
    retryable = [
        item
        for item in items
        if item["notice_key"] not in manual_only_keys
        if item["analysis_coverage"]["reason_code"]
        in {
            "ATTACHMENT_COVERAGE_INCOMPLETE",
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
        or (
            item["analysis_snapshot"] is not None
            and not item["analysis_snapshot"]["version_current"]
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
            "note": (
                "미시도 공고를 먼저 처리하고 구버전 분석은 현재 정책으로 점진 갱신하며, "
                "실패 건은 가장 오래된 시도부터 재검토합니다. 첨부 없음·미지원 형식은 "
                "manifest가 바뀔 때까지 자동 재시도하지 않습니다."
            ),
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
            "notice_analysis_policies",
        ],
        note=(
            "dry-run: 삭제 대상 수만 계산했습니다."
            if payload.dry_run
            else "단기 운영 로그만 삭제했습니다. 핵심 의사결정 기록은 보존했습니다."
        ),
    )
