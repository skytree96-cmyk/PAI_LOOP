from __future__ import annotations

import uuid
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .analysis_pipeline import AnalysisPipelineError, run_analysis_pipeline
from .auth import require_api_key
from .models import AnalysisRun, IngestionJob, Notice, NoticeVersion
from .pps_enrichment import (
    PpsEnrichmentResult,
    enrich_notice_from_pps,
    has_current_accepted_pps_extraction,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AnalysisBatchRequest(ApiModel):
    notice_keys: list[str] = Field(min_length=1, max_length=20)
    dry_run: bool = False
    force: Literal[False] = False
    enrich_missing: bool = False
    max_notices: int = Field(default=3, ge=1, le=3)
    max_attachments_per_notice: int = Field(default=1, ge=1, le=1)

    @field_validator("notice_keys")
    @classmethod
    def validate_notice_keys(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 160 for item in cleaned):
            raise ValueError("notice_keys must contain 1..160 character values")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("notice_keys must be unique")
        return cleaned


class AnalysisBatchItemOut(ApiModel):
    notice_key: str
    status: Literal["COMPLETED", "SKIPPED", "FAILED"]
    document_status: str
    evaluation_status: str
    snapshot_status: str
    analysis_run_id: str | None = None
    evaluation_id: str | None = None
    notice_version_id: str | None = None
    input_sha256: str | None = None
    reused: bool = False
    materialized_requirements: int = 0
    requirement_snapshots: int = 0
    score_snapshots: int = 0
    recommendation_snapshots: int = 0
    warnings: list[str] = Field(default_factory=list)


class AnalysisEnrichmentOut(ApiModel):
    requested: int = 0
    attempted: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    attachments_discovered: int = 0
    attachments_processed: int = 0
    openai_calls: int = 0
    warnings: list[str] = Field(default_factory=list)


class AnalysisBatchResponse(ApiModel):
    job_id: str
    status: Literal["COMPLETED", "PARTIAL"]
    dry_run: bool
    requested: int
    processed: int
    completed: int
    skipped: int
    failed: int
    document_materialized: int
    evaluations_created: int
    snapshots_refreshed: int
    openai_calls: int
    results: list[AnalysisBatchItemOut]
    warnings: list[str]
    enrichment: AnalysisEnrichmentOut


router = APIRouter(
    prefix="/api/v1",
    tags=["analysis persistence"],
    dependencies=[Depends(require_api_key)],
)


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _create_batch_job(request: Request, payload: AnalysisBatchRequest) -> str:
    job_id = str(uuid.uuid4())
    with request.app.state.session_factory() as session:
        session.add(
            IngestionJob(
                id=job_id,
                source="ANALYSIS",
                mode="DRY_RUN" if payload.dry_run else "LIVE",
                status="RUNNING",
                window_json={"scope": "NOTICE_KEYS"},
                request_json={
                    "notice_count": len(payload.notice_keys),
                    "dry_run": payload.dry_run,
                    "force": False,
                    "enrich_missing": payload.enrich_missing,
                    "max_notices": payload.max_notices,
                    "max_attachments_per_notice": payload.max_attachments_per_notice,
                },
                matched=len(payload.notice_keys),
                notice_keys=list(payload.notice_keys),
            )
        )
        session.commit()
    return job_id


def _finish_batch_job(
    request: Request,
    *,
    job_id: str,
    status_value: str,
    completed: int,
    skipped: int,
    failed: int,
    warnings: list[str],
) -> None:
    with request.app.state.session_factory() as session:
        job = session.get(IngestionJob, job_id)
        if job is None:  # pragma: no cover - database invariant
            raise RuntimeError("analysis batch audit job disappeared")
        job.status = status_value
        job.fetched = completed + skipped + failed
        job.created_count = completed
        job.duplicate_count = skipped
        job.quarantined_count = failed
        job.warnings = warnings
        job.completed_at = datetime.now(timezone.utc)
        session.commit()


def _dry_run_item(request: Request, notice_key: str) -> AnalysisBatchItemOut:
    with request.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        if notice is None:
            return AnalysisBatchItemOut(
                notice_key=notice_key,
                status="FAILED",
                document_status="NOTICE_NOT_FOUND",
                evaluation_status="NOT_RUN",
                snapshot_status="NOT_RUN",
                warnings=["NOTICE_NOT_FOUND"],
            )
        versions = list(
            session.scalars(
                select(NoticeVersion).where(NoticeVersion.notice_id == notice.id)
            ).all()
        )
        accepted = [
            item
            for item in versions
            if isinstance(item.source_payload, dict)
            and item.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
            and item.source_payload.get("status") == "ACCEPTED"
        ]
        return AnalysisBatchItemOut(
            notice_key=notice_key,
            status="SKIPPED",
            document_status="READY" if accepted else "EXTRACTION_MISSING",
            evaluation_status="NOT_RUN",
            snapshot_status="NOT_RUN",
            warnings=["DRY_RUN_NO_WRITES"]
            + ([] if accepted else ["NO_ACCEPTED_EXTRACTION"]),
        )


def _has_accepted_pps_extraction(request: Request, notice_id: str) -> bool:
    with request.app.state.session_factory() as session:
        return has_current_accepted_pps_extraction(session, notice_id)


def _enrich_one_notice(
    request: Request,
    *,
    notice_id: str,
    payload: AnalysisBatchRequest,
) -> PpsEnrichmentResult:
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        return enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model,
            max_attachments=payload.max_attachments_per_notice,
            dry_run=payload.dry_run,
            # One notice is bounded to roughly one minute. Together with the
            # batch wall-clock guard this keeps the n8n 240 second request
            # contract below its hard timeout and returns PARTIAL instead of
            # allowing an unbounded provider retry chain.
            download_timeout_seconds=12,
            openai_timeout_seconds=45,
            openai_max_retries=0,
        )


@router.post("/notices/analysis/batch", response_model=AnalysisBatchResponse)
def run_notice_analysis_batch(
    payload: AnalysisBatchRequest,
    request: Request,
) -> AnalysisBatchResponse:
    """Boundedly enrich missing PPS documents, then persist analysis snapshots."""
    job_id = _create_batch_job(request, payload)
    try:
        return _execute_notice_analysis_batch(payload, request, job_id=job_id)
    except Exception:
        # A provider/library failure must not leave the operational audit in a
        # permanent RUNNING state. Per-notice failures are handled below and
        # normally produce a 200 PARTIAL response; this is the final boundary.
        _finish_batch_job(
            request,
            job_id=job_id,
            status_value="FAILED",
            completed=0,
            skipped=0,
            failed=len(payload.notice_keys),
            warnings=["INTERNAL_ANALYSIS_BATCH_ERROR"],
        )
        raise


def _execute_notice_analysis_batch(
    payload: AnalysisBatchRequest,
    request: Request,
    *,
    job_id: str,
) -> AnalysisBatchResponse:
    rows: list[AnalysisBatchItemOut] = []
    completed = skipped = failed = 0
    materialized = evaluations = snapshots = 0
    enrichment_requested = min(len(payload.notice_keys), payload.max_notices) if payload.enrich_missing else 0
    enrichment_attempted = enrichment_completed = enrichment_skipped = enrichment_failed = 0
    enrichment_discovered = enrichment_processed = enrichment_openai_calls = 0
    enrichment_warnings: list[str] = []
    enrichment_deadline = time.monotonic() + 205

    for index, notice_key in enumerate(payload.notice_keys):
        enrichment_targeted = payload.enrich_missing and index < payload.max_notices
        if enrichment_targeted:
            # attempted is the processed target partition (including precheck
            # reuse/not-found/timeout), not merely outbound provider calls.
            # Workflow 10 validates attempted == completed+skipped+failed.
            enrichment_attempted += 1
        with request.app.state.session_factory() as session:
            notice_id = session.scalar(select(Notice.id).where(Notice.notice_key == notice_key))
            session.rollback()
        if notice_id is None:
            rows.append(
                AnalysisBatchItemOut(
                    notice_key=notice_key,
                    status="FAILED",
                    document_status="NOTICE_NOT_FOUND",
                    evaluation_status="NOT_RUN",
                    snapshot_status="NOT_RUN",
                    warnings=["NOTICE_NOT_FOUND"],
                )
            )
            failed += 1
            if enrichment_targeted:
                enrichment_skipped += 1
                enrichment_warnings.append("NOTICE_NOT_FOUND")
            continue

        enrichment_result: PpsEnrichmentResult | None = None
        should_enrich = (
            enrichment_targeted
            and not _has_accepted_pps_extraction(request, notice_id)
        )
        if enrichment_targeted and not should_enrich:
            enrichment_completed += 1
        elif should_enrich and time.monotonic() >= enrichment_deadline:
            enrichment_result = PpsEnrichmentResult(
                status="SKIPPED",
                warnings=["ENRICHMENT_TOTAL_TIMEOUT"],
            )
        elif should_enrich:
            try:
                enrichment_result = _enrich_one_notice(
                    request,
                    notice_id=notice_id,
                    payload=payload,
                )
            except Exception:  # pragma: no cover - provider fail-closed boundary
                enrichment_result = PpsEnrichmentResult(
                    status="REVIEW",
                    warnings=["INTERNAL_ENRICHMENT_ERROR"],
                )

        item_enrichment_warnings: list[str] = []
        if enrichment_result is not None:
            enrichment_discovered += enrichment_result.attachments_discovered
            enrichment_processed += enrichment_result.attachments_processed
            enrichment_openai_calls += enrichment_result.openai_calls
            item_enrichment_warnings.extend(enrichment_result.warnings)
            enrichment_warnings.extend(enrichment_result.warnings)
            if enrichment_result.status in {"COMPLETED", "REUSED"}:
                enrichment_completed += 1
            elif enrichment_result.status in {"PLANNED", "SKIPPED"}:
                enrichment_skipped += 1
            else:
                enrichment_failed += 1

        if payload.dry_run:
            item = _dry_run_item(request, notice_key)
            updates: dict[str, Any] = {
                "warnings": sorted(set([*item.warnings, *item_enrichment_warnings])),
            }
            if enrichment_result is not None and enrichment_result.status == "PLANNED":
                updates["document_status"] = "ENRICHMENT_PLANNED"
            item = item.model_copy(update=updates)
            rows.append(item)
            if item.status == "FAILED":
                failed += 1
            else:
                skipped += 1
            continue

        with request.app.state.session_factory() as session:
            try:
                result = run_analysis_pipeline(session, notice_id=notice_id)
            except AnalysisPipelineError as exc:
                rows.append(
                    AnalysisBatchItemOut(
                        notice_key=notice_key,
                        status="FAILED",
                        document_status="PIPELINE_REJECTED",
                        evaluation_status="NOT_CREATED",
                        snapshot_status="NOT_CREATED",
                        warnings=sorted(set([type(exc).__name__, *item_enrichment_warnings])),
                    )
                )
                failed += 1
                continue
            except Exception:  # pragma: no cover - operational fail-closed boundary
                rows.append(
                    AnalysisBatchItemOut(
                        notice_key=notice_key,
                        status="FAILED",
                        document_status="PIPELINE_ERROR",
                        evaluation_status="NOT_CREATED",
                        snapshot_status="NOT_CREATED",
                        warnings=sorted(set(["INTERNAL_PIPELINE_ERROR", *item_enrichment_warnings])),
                    )
                )
                failed += 1
                continue

        completed += 1
        if not result.reused:
            materialized += 1
            evaluations += 1
            snapshots += 1
        rows.append(
            AnalysisBatchItemOut(
                notice_key=notice_key,
                status="COMPLETED",
                document_status=result.status,
                evaluation_status="REUSED" if result.reused else "CREATED",
                snapshot_status="REUSED" if result.reused else "CREATED",
                analysis_run_id=result.analysis_run_id,
                evaluation_id=result.evaluation_id,
                notice_version_id=result.notice_version_id,
                input_sha256=result.input_sha256,
                reused=result.reused,
                materialized_requirements=result.materialized_requirement_count,
                requirement_snapshots=result.requirement_snapshot_count,
                score_snapshots=result.score_snapshot_count,
                recommendation_snapshots=result.recommendation_snapshot_count,
                warnings=sorted(set([*result.warnings, *item_enrichment_warnings])),
            )
        )

    warnings = sorted({warning for row in rows for warning in row.warnings})
    partial = failed > 0 or enrichment_failed > 0 or any(
        row.document_status not in {"READY", "COMPLETED"} for row in rows
    )
    status_value = "PARTIAL" if partial else "COMPLETED"
    if status_value == "PARTIAL" and not warnings:
        warnings = ["PARTIAL_ANALYSIS"]
    _finish_batch_job(
        request,
        job_id=job_id,
        status_value=status_value,
        completed=completed,
        skipped=skipped,
        failed=failed,
        warnings=warnings,
    )
    return AnalysisBatchResponse(
        job_id=job_id,
        status=status_value,
        dry_run=payload.dry_run,
        requested=len(payload.notice_keys),
        processed=completed + skipped + failed,
        completed=completed,
        skipped=skipped,
        failed=failed,
        document_materialized=materialized,
        evaluations_created=evaluations,
        snapshots_refreshed=snapshots,
        openai_calls=enrichment_openai_calls,
        results=rows,
        warnings=warnings,
        enrichment=AnalysisEnrichmentOut(
            requested=enrichment_requested,
            attempted=enrichment_attempted,
            completed=enrichment_completed,
            skipped=enrichment_skipped,
            failed=enrichment_failed,
            attachments_discovered=enrichment_discovered,
            attachments_processed=enrichment_processed,
            openai_calls=enrichment_openai_calls,
            warnings=sorted(set(enrichment_warnings)),
        ),
    )


@router.get("/notices/{notice_key}/analysis-runs")
def list_notice_analysis_runs(
    notice_key: str,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    runs = list(
        session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.notice_id == notice.id)
            .options(
                selectinload(AnalysisRun.requirement_results),
                selectinload(AnalysisRun.scores),
                selectinload(AnalysisRun.recommendations),
            )
            .order_by(AnalysisRun.generated_at.desc())
            .limit(limit)
        ).all()
    )
    return {
        "notice_key": notice_key,
        "count": len(runs),
        "runs": [
            {
                "id": run.id,
                "status": run.status,
                "run_kind": run.run_kind,
                "input_sha256": run.input_sha256,
                "generated_at": run.generated_at,
                "evaluation_id": run.evaluation_id,
                "notice_version_id": run.notice_version_id,
                "basis_versions": run.basis_versions,
                "output_summary": run.output_summary,
                "requirement_results": [
                    {
                        "result_key": item.result_key,
                        "sequence": item.sequence,
                        "requirement_key": item.requirement_key,
                        "policy_class": item.policy_class,
                        "outcome": item.outcome,
                        "reason_code": item.reason_code,
                        "blocking": item.blocking,
                        "evidence_state": item.evidence_state,
                        "result": item.result_json,
                    }
                    for item in run.requirement_results
                ],
                "scores": [
                    {
                        "score_key": item.score_key,
                        "score_type": item.score_type,
                        "value": item.value,
                        "lower_value": item.lower_value,
                        "upper_value": item.upper_value,
                        "unit": item.unit,
                        "status": item.status,
                        "band": item.band,
                        "confidence": item.confidence,
                        "method_version": item.method_version,
                        "basis": item.basis_json,
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
                        "confidence": item.confidence,
                        "risk_band": item.risk_band,
                        "detail": item.detail_json,
                    }
                    for item in run.recommendations
                ],
            }
            for run in runs
        ],
    }
