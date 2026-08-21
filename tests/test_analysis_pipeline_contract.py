from __future__ import annotations

import copy

import pytest
from sqlalchemy import func, select

from pai_loop.analysis_pipeline import (
    AnalysisPipelineSourceError,
    AnalysisPipelineTransactionError,
    run_analysis_pipeline,
)
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import (
    AnalysisRun,
    AtomicRequirement,
    Evaluation,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    RequirementResultSnapshot,
    ScoreSnapshot,
)
from pai_loop.public_notice_seed import import_public_notice_seed
from pai_loop.reference_registry import (
    sync_packaged_reference_data,
    sync_public_company_profile,
)


NOTICE_KEY = "MANUAL-INCHON-2025-17"


def _database():
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        sync_packaged_reference_data(session)
        sync_public_company_profile(session)
        session.commit()
        import_public_notice_seed(session)
    return engine, factory


def _notice_id(session) -> str:
    notice_id = session.scalar(select(Notice.id).where(Notice.notice_key == NOTICE_KEY))
    assert notice_id
    session.rollback()
    return notice_id


def test_pipeline_materializes_evaluates_snapshots_and_reuses() -> None:
    engine, factory = _database()
    try:
        with factory() as session:
            notice_id = _notice_id(session)
            first = run_analysis_pipeline(session, notice_id=notice_id)
            assert first.reused is False
            assert first.source_count == 1
            assert first.accepted_source_count == 1
            assert first.materialized_requirement_count >= 1
            assert first.requirement_snapshot_count == 23
            assert first.score_snapshot_count == 8

            repeated = run_analysis_pipeline(session, notice_id=notice_id)
            assert repeated.reused is True
            assert repeated.analysis_run_id == first.analysis_run_id

            assert session.scalar(select(func.count(AnalysisRun.id))) == 1
            assert session.scalar(select(func.count(Evaluation.id))) == 1
            assert session.scalar(select(func.count(RequirementResultSnapshot.id))) == 23
            assert session.scalar(select(func.count(ScoreSnapshot.id))) == 8
            assert session.scalar(select(func.count(RecommendationSnapshot.id))) >= 1
            run = session.get(AnalysisRun, first.analysis_run_id)
            assert run is not None
            assert run.basis_versions["active_reference_versions"]
            assert run.input_manifest["reference_content_sha256s"]
    finally:
        engine.dispose()


def test_pipeline_merges_latest_sources_by_attachment() -> None:
    engine, factory = _database()
    try:
        with factory() as session:
            notice_id = _notice_id(session)
            source = session.scalar(
                select(NoticeVersion).where(NoticeVersion.notice_id == notice_id)
            )
            assert source and isinstance(source.source_payload, dict)
            payload = copy.deepcopy(source.source_payload)
            payload["attachment_id"] = "ATT-SECOND"
            payload["document_sha256"] = "a" * 64
            for requirement in payload["result"]["requirements"]:
                for anchor in requirement["evidence"]:
                    anchor["attachment_id"] = "ATT-SECOND"
            session.add(
                NoticeVersion(
                    notice_id=notice_id,
                    version_no=source.version_no + 1,
                    file_sha256="a" * 64,
                    document_complete=True,
                    extraction_status="ACCEPTED",
                    extraction_confidence=0.99,
                    source_payload=payload,
                )
            )
            session.commit()

            result = run_analysis_pipeline(session, notice_id=notice_id)
            assert result.source_count == 2
            assert result.accepted_source_count == 2
            assert result.requirement_snapshot_count == 23
    finally:
        engine.dispose()


def test_pipeline_review_attempt_produces_partial_r07_snapshot() -> None:
    engine, factory = _database()
    try:
        with factory() as session:
            notice_id = _notice_id(session)
            source = session.scalar(
                select(NoticeVersion).where(NoticeVersion.notice_id == notice_id)
            )
            assert source and isinstance(source.source_payload, dict)
            payload = copy.deepcopy(source.source_payload)
            payload["attachment_id"] = payload["result"]["requirements"][0]["evidence"][0][
                "attachment_id"
            ]
            payload["status"] = "REVIEW"
            payload["review_code"] = "R07"
            payload["error_code"] = "SCHEMA_INVALID"
            payload["result"] = None
            session.add(
                NoticeVersion(
                    notice_id=notice_id,
                    version_no=source.version_no + 1,
                    file_sha256=source.file_sha256,
                    document_complete=False,
                    extraction_status="REVIEW",
                    extraction_confidence=0,
                    source_payload=payload,
                )
            )
            session.commit()

            result = run_analysis_pipeline(session, notice_id=notice_id)
            assert result.status == "PARTIAL"
            assert result.reason_code == "R07"
            assert result.score_snapshot_count == 8
    finally:
        engine.dispose()


def test_pipeline_rejects_active_transaction_and_foreign_source() -> None:
    engine, factory = _database()
    try:
        with factory() as session:
            notice_id = session.scalar(select(Notice.id).where(Notice.notice_key == NOTICE_KEY))
            assert notice_id
            with pytest.raises(AnalysisPipelineTransactionError):
                run_analysis_pipeline(session, notice_id=notice_id)
            session.rollback()
            with pytest.raises(AnalysisPipelineSourceError):
                run_analysis_pipeline(
                    session,
                    notice_id=notice_id,
                    source_version_ids=["not-this-notice"],
                )
    finally:
        engine.dispose()


def test_pipeline_rolls_back_every_stage_on_failure() -> None:
    engine, factory = _database()
    try:
        with factory() as session:
            notice_id = _notice_id(session)
            versions_before = session.scalar(
                select(func.count(NoticeVersion.id)).where(NoticeVersion.notice_id == notice_id)
            )
            session.rollback()

            def fail_after_evaluation(stage: str) -> None:
                if stage == "after_evaluation":
                    raise RuntimeError("synthetic transaction failure")

            with pytest.raises(RuntimeError, match="synthetic transaction failure"):
                run_analysis_pipeline(
                    session,
                    notice_id=notice_id,
                    _stage_hook=fail_after_evaluation,
                )
            assert session.scalar(
                select(func.count(NoticeVersion.id)).where(NoticeVersion.notice_id == notice_id)
            ) == versions_before
            assert session.scalar(select(func.count(AtomicRequirement.id))) == 0
            assert session.scalar(select(func.count(Evaluation.id))) == 0
            assert session.scalar(select(func.count(AnalysisRun.id))) == 0
    finally:
        engine.dispose()
