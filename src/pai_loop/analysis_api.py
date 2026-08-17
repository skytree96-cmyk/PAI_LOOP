from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .analysis_pipeline import AnalysisPipelineError, run_analysis_pipeline
from .auth import require_api_key
from .models import AnalysisRun, IngestionJob, Notice, NoticeVersion


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AnalysisBatchRequest(ApiModel):
    notice_keys: list[str] = Field(min_length=1, max_length=20)
    dry_run: bool = False
    force: Literal[False] = False

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


@router.post("/notices/analysis/batch", response_model=AnalysisBatchResponse)
def run_notice_analysis_batch(
    payload: AnalysisBatchRequest,
    request: Request,
) -> AnalysisBatchResponse:
    """Materialise stored extraction evidence and persist immutable snapshots.

    This endpoint never sends document text to OpenAI. It consumes only stored,
    validated extraction versions. Missing documents are represented as R07
    analysis evidence rather than silently promoted to PASS.
    """

    job_id = _create_batch_job(request, payload)
    rows: list[AnalysisBatchItemOut] = []
    completed = skipped = failed = 0
    materialized = evaluations = snapshots = 0

    if payload.dry_run:
        for notice_key in payload.notice_keys:
            item = _dry_run_item(request, notice_key)
            rows.append(item)
            if item.status == "FAILED":
                failed += 1
            else:
                skipped += 1
    else:
        for notice_key in payload.notice_keys:
            with request.app.state.session_factory() as session:
                notice_id = session.scalar(
                    select(Notice.id).where(Notice.notice_key == notice_key)
                )
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
                    continue
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
                            warnings=[type(exc).__name__],
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
                            warnings=["INTERNAL_PIPELINE_ERROR"],
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
                    warnings=list(result.warnings),
                )
            )

    warnings = sorted({warning for row in rows for warning in row.warnings})
    partial = failed > 0 or any(
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
        openai_calls=0,
        results=rows,
        warnings=warnings,
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
