from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AnalysisRun,
    Evaluation,
    Notice,
    NoticeVersion,
    PpsNoticeAuthority,
)


PPS_METADATA_KIND = "PPS_NOTICE_METADATA"


def authoritative_pps_cancelled_notice_keys(
    session: Session,
    notices: list[Notice],
) -> set[str]:
    """Return PPS Notice rows whose current logical authority is cancelled.

    Cancellation is scoped by ``bid_notice_no``. Every retained historical
    row for that logical notice is therefore write-ineligible even though only
    one current representative receives the public cancellation projection.
    """

    pps_notices = [
        notice
        for notice in notices
        if notice.notice_key.upper().startswith("PPS-") and notice.bid_notice_no
    ]
    notice_nos = {notice.bid_notice_no for notice in pps_notices}
    if not notice_nos:
        return set()
    cancelled_notice_nos = {
        authority.bid_notice_no
        for authority in session.scalars(
            select(PpsNoticeAuthority)
            .where(
                PpsNoticeAuthority.bid_notice_no.in_(notice_nos),
                PpsNoticeAuthority.disposition == "CANCELLED",
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    return {
        notice.notice_key
        for notice in pps_notices
        if notice.bid_notice_no in cancelled_notice_nos
    }


def authoritative_pps_notice_is_cancelled(
    session: Session,
    notice: Notice,
) -> bool:
    return notice.notice_key in authoritative_pps_cancelled_notice_keys(
        session,
        [notice],
    )


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


def _current_pps_attachment_audit_is_complete(notice: Notice) -> bool:
    """Require current extraction contracts before exposing current analysis.

    Evaluations and runs remain immutable history.  A prompt, schema, processing,
    or validator bump can nevertheless invalidate every current attachment
    attempt without creating a new PPS metadata version.  In that state an old
    evaluation shares the latest metadata basis but is not a current result.

    Empty legacy manifests retain their historical compatibility.  Once PPS
    declares at least one attachment slot, the public current axis is available
    only after the current all-attachment audit reaches ANALYZED.
    """

    metadata = _latest_pps_metadata_version(notice)
    if metadata is None or not isinstance(metadata.source_payload, dict):
        return True
    manifest = metadata.source_payload.get("attachment_manifest")
    if not isinstance(manifest, list) or not manifest:
        return True

    # Local import keeps freshness primitives independent at module import
    # time while reusing the single prompt/schema/validator-aware audit policy.
    from .pps_enrichment import public_analysis_reason

    return (
        public_analysis_reason(
            notice.versions,
            evaluated=False,
            source_kind="PPS",
        ).state
        == "ANALYZED"
    )


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


def has_current_independent_failure(notice: Notice, evaluation: Evaluation) -> bool:
    """Expose the server's mandatory failure proof only against current sources.

    This grants no current AnalysisRun, quantitative score or PASS. A changed
    attachment attempt, manifest, deadline or policy invalidates the proof.
    """
    return _has_current_proof(notice, evaluation, "FAIL", "independent_failure", "requirement_keys")


def has_current_independent_pass(notice: Notice, evaluation: Evaluation) -> bool:
    """Expose a gated PASS whose attachments were all read (or are format twins).

    The pipeline records the proof only when every current attachment was read
    completely or is another file format of a fully read one. It grants no
    AnalysisRun or quantitative score and expires exactly like the failure proof.
    """
    return _has_current_proof(notice, evaluation, "PASS", "independent_pass", "attachment_ids")


def _has_current_proof(
    notice: Notice, evaluation: Evaluation, eligibility: str, proof_key: str, list_key: str,
    *, any_versions: bool = False,
) -> bool:
    if evaluation.eligibility != eligibility or not analysis_basis_is_current(notice, evaluation.notice_version_id):
        return False
    if _as_utc(evaluation.deadline_snapshot_at) != _as_utc(notice.deadline):
        return False
    basis = next((v for v in notice.versions if v.id == evaluation.notice_version_id), None)
    payload = basis.source_payload if basis is not None else None
    proof = payload.get(proof_key) if isinstance(payload, dict) and payload.get("kind") == "ANALYSIS_PIPELINE_MATERIALIZATION" else None
    if not isinstance(proof, dict) or not isinstance(proof.get(list_key), list) or not proof[list_key]:
        return False
    from .analysis_pipeline import PIPELINE_VERSION, PROMPT_VERSION, _select_source_versions_from_list
    from .eligibility_policy import POLICY_VERSION
    if not any_versions and (proof.get("pipeline_version") != PIPELINE_VERSION
                             or proof.get("policy_version") != POLICY_VERSION):
        return False
    selected = _select_source_versions_from_list(notice.versions, prompt_version=PROMPT_VERSION)
    return bool(selected) and proof.get("source_version_ids") == sorted(v.id for v in selected)


def previous_version_proof_evaluation(notice: Notice) -> Evaluation | None:
    """The newest PASS/FAIL whose proof covers today's sources under an older policy.

    A policy or pipeline release invalidates every proof at once, so between
    deploy and the free reanalysis the board loses most decisions (2026-10-07:
    3.5 hours). This returns that superseded decision for display only, labelled
    as pending reanalysis. It is never a current evaluation: work queues,
    reuse and recomputation keep using ``latest_current_evaluation``.
    """

    evaluations = sorted(notice.evaluations, key=lambda item: _as_utc(item.evaluated_at), reverse=True)
    for evaluation in evaluations:
        if not analysis_basis_is_current(notice, evaluation.notice_version_id):
            continue
        if _as_utc(evaluation.deadline_snapshot_at) != _as_utc(notice.deadline):
            continue
        if has_current_independent_failure(notice, evaluation) or has_current_independent_pass(notice, evaluation):
            return None  # a current proof exists; nothing is pending
        if (_has_current_proof(notice, evaluation, "FAIL", "independent_failure", "requirement_keys", any_versions=True)
                or _has_current_proof(notice, evaluation, "PASS", "independent_pass", "attachment_ids", any_versions=True)):
            return evaluation
        return None
    return None


def latest_current_evaluation(notice: Notice) -> Evaluation | None:
    has_pps_material = _latest_pps_metadata_version(notice) is not None
    audit_complete = not has_pps_material or _current_pps_attachment_audit_is_complete(notice)
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
        basis = next((v for v in notice.versions if v.id == evaluation.notice_version_id), None)
        proof = (basis.source_payload or {}).get("independent_failure") if basis else None
        if (proof or not audit_complete) and not (
            has_current_independent_failure(notice, evaluation)
            or has_current_independent_pass(notice, evaluation)
        ):
            return None
        return evaluation
    return None


def latest_current_analysis_run(notice: Notice) -> AnalysisRun | None:
    if (
        _latest_pps_metadata_version(notice) is not None
        and not _current_pps_attachment_audit_is_complete(notice)
    ):
        return None
    runs = sorted(
        notice.analysis_runs,
        key=lambda item: _as_utc(item.generated_at),
        reverse=True,
    )
    for run in runs:
        if analysis_basis_is_current(notice, run.notice_version_id):
            return run
    return None


def latest_quantitative_snapshot_run(notice: Notice) -> AnalysisRun | None:
    """The run whose quantitative snapshot the notice's score view reads.

    Mirrors ``_stored_public_quantitative_projection``: the newest run on the
    current PPS metadata basis, without requiring every attachment to be
    audited. A partial-source subtotal is labeled as such on its own, so the
    dashboard counts what each notice actually shows.
    """

    metadata = _latest_pps_metadata_version(notice)
    runs = list(notice.analysis_runs)
    if metadata is not None:
        current_ids = {item.id for item in notice.versions if item.version_no >= metadata.version_no}
        runs = [run for run in runs if run.notice_version_id in current_ids]
    if not runs:
        return None
    # Only id, notice_version_id and generated_at are loaded on the dashboard path.
    return max(runs, key=lambda item: (_as_utc(item.generated_at), str(item.id)))


def analysis_run_versions_are_current(
    run: AnalysisRun,
    *,
    pipeline_version: str,
    policy_version: str,
    quantitative_engine_version: str,
) -> bool:
    """Return whether a stored snapshot used the active calculation policy.

    Source-material freshness and calculation-policy freshness are separate
    axes.  A PPS notice can still point at the current metadata/attachments
    while its immutable snapshot was produced by an older pipeline,
    requirement policy, or quantitative engine. Keep the expected versions
    explicit at the caller so this module does not import calculation modules
    and create a cycle.
    """

    basis = run.basis_versions if isinstance(run.basis_versions, dict) else {}
    return (
        basis.get("pipeline") == pipeline_version
        and basis.get("requirement_policy") == policy_version
        and basis.get("quantitative_engine") == quantitative_engine_version
    )


def analysis_version_refresh_required(
    notice: Notice,
    *,
    pipeline_version: str,
    policy_version: str,
    quantitative_engine_version: str,
) -> bool:
    """Return true only for a current-source snapshot using stale policy code.

    A notice with no current run remains ordinary never-attempted/retry work;
    this predicate is deliberately limited to an existing current-source run
    so queue partitions stay disjoint.
    """

    run = latest_current_analysis_run(notice)
    return run is not None and not analysis_run_versions_are_current(
        run,
        pipeline_version=pipeline_version,
        policy_version=policy_version,
        quantitative_engine_version=quantitative_engine_version,
    )
