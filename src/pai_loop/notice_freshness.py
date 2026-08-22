from __future__ import annotations

from datetime import datetime, timezone

from .models import AnalysisRun, Evaluation, Notice, NoticeVersion


PPS_METADATA_KIND = "PPS_NOTICE_METADATA"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_pps_metadata_version(notice: Notice) -> NoticeVersion | None:
    metadata = [
        version
        for version in notice.versions
        if isinstance(version.source_payload, dict)
        and version.source_payload.get("kind") == PPS_METADATA_KIND
    ]
    return max(metadata, key=lambda item: item.version_no) if metadata else None


def analysis_basis_is_current(notice: Notice, notice_version_id: str | None) -> bool:
    """Return whether an immutable analysis basis covers current PPS material.

    Historical evaluations and recommendations remain stored, but a newer PPS
    metadata version makes them unsuitable for the active board and briefing
    until the refresh queue creates a new materialized analysis version.
    """

    current_metadata = _latest_pps_metadata_version(notice)
    if current_metadata is None:
        return True
    if not notice_version_id:
        return False
    basis = next(
        (item for item in notice.versions if item.id == notice_version_id),
        None,
    )
    return basis is not None and basis.version_no >= current_metadata.version_no


def latest_current_evaluation(notice: Notice) -> Evaluation | None:
    has_pps_material = _latest_pps_metadata_version(notice) is not None
    evaluations = sorted(
        notice.evaluations,
        key=lambda item: _as_utc(item.evaluated_at),
        reverse=True,
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


def latest_current_analysis_run(notice: Notice) -> AnalysisRun | None:
    runs = sorted(
        notice.analysis_runs,
        key=lambda item: _as_utc(item.generated_at),
        reverse=True,
    )
    for run in runs:
        if analysis_basis_is_current(notice, run.notice_version_id):
            return run
    return None
