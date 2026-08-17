from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from pai_loop.database import Base, build_engine
from pai_loop.migrations import (
    MIGRATION_CHECKSUM,
    MIGRATION_ID,
    MigrationError,
    apply_additive_migrations,
    main as migration_main,
    pending_migrations,
    schema_migrations,
)
from pai_loop.models import (
    AnalysisRun,
    BidOutcome,
    Evaluation,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    ReferenceDataVersion,
    RequirementResultSnapshot,
    ScoreSnapshot,
    UserDecision,
)


def _notice(now: datetime) -> Notice:
    return Notice(
        notice_key="PERSISTENCE-001",
        bid_notice_no="20260817-001",
        revision_no="00",
        title="분석 결과 영속화 테스트",
        agency="테스트기관",
        published_at=now,
        deadline=now + timedelta(days=10),
        status="OPEN",
    )


def test_analysis_snapshots_and_bid_outcome_round_trip() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        notice = _notice(now)
        version = NoticeVersion(
            notice=notice,
            version_no=1,
            file_sha256="a" * 64,
            document_complete=True,
            extraction_status="ACCEPTED",
            extraction_confidence=0.98,
            source_payload={"kind": "TEST", "status": "ACCEPTED"},
        )
        session.add_all([notice, version])
        session.flush()
        evaluation = Evaluation(
            notice=notice,
            notice_version_id=version.id,
            deadline_snapshot_at=notice.deadline,
            eligibility="PASS",
            reason_code="P-ENTITY",
            readiness_score=82.0,
            readiness_status="GREEN",
            evidence_coverage=0.9,
            risk_score=21.0,
            risk_band="LOW",
            ruleset_version="2026.08-v2.1",
            atomic_results=[],
            explanation={"summary": "테스트"},
        )
        session.add(evaluation)
        session.flush()

        run = AnalysisRun(
            notice=notice,
            notice_version=version,
            evaluation=evaluation,
            run_kind="FULL_REVIEW",
            status="COMPLETED",
            idempotency_key="analysis:PERSISTENCE-001:v1:audit-v1",
            input_sha256="b" * 64,
            ruleset_version="2026.08-v2.1",
            company_profile_version="2026.08.17-v1",
            basis_versions={"department_keywords": "2026.08.17-1"},
            input_manifest={"document_sha256": "a" * 64, "notice_version": 1},
            output_summary={"eligibility": "PASS", "recommendation": "GO"},
            generated_at=now,
            requirement_results=[
                RequirementResultSnapshot(
                    result_key="R-001",
                    sequence=1,
                    requirement_key="ENTITY-001",
                    policy_class="ELIGIBILITY",
                    outcome="PASS",
                    reason_code="P-ENTITY",
                    blocking=False,
                    evidence_state="VERIFIED",
                    result_json={"evidence_key": "PUBLIC-EVIDENCE-1"},
                )
            ],
            scores=[
                ScoreSnapshot(
                    score_key="readiness",
                    score_type="READINESS",
                    value=82.0,
                    unit="points",
                    status="AVAILABLE",
                    band="GREEN",
                    confidence=0.9,
                    method_version="2026.08-v2.1",
                    basis_json={"evidence_coverage": 0.9},
                )
            ],
            recommendations=[
                RecommendationSnapshot(
                    recommendation_key="department:consulting",
                    department_id="consulting",
                    rank=1,
                    priority_score=88.0,
                    recommendation="GO",
                    confidence=0.85,
                    risk_band="LOW",
                    detail_json={"matched_keywords": ["컨설팅"]},
                )
            ],
        )
        outcome = BidOutcome(
            notice=notice,
            evaluation=evaluation,
            outcome_key="PPS:20260817-001:OPENING-1",
            status="WON",
            submitted_bid_amount=95_000_000,
            submitted_bid_rate=94.7,
            winning_bid_amount=95_000_000,
            winning_bid_rate=94.7,
            technical_score=88.5,
            price_score=9.2,
            total_score=97.7,
            rank=1,
            winner_name="공개 법인명",
            source="PPS",
            evidence_json={"public_record_key": "award-1"},
            occurred_at=now,
            observed_at=now,
        )
        reference = ReferenceDataVersion(
            dataset_key="company_public_profile",
            version="2026.08.17-v1",
            schema_version="1.0",
            content_sha256="c" * 64,
            classification="PUBLIC_REVIEWED",
            source="GIT_PACKAGE",
            status="ACTIVE",
            payload_json={"profile_version": "2026.08.17-v1", "facts": {}},
            effective_from=now,
        )
        session.add_all([run, outcome, reference])
        session.commit()

        stored = session.scalar(select(AnalysisRun).where(AnalysisRun.id == run.id))
        assert stored is not None
        assert stored.input_manifest["document_sha256"] == "a" * 64
        assert stored.requirement_results[0].outcome == "PASS"
        assert stored.scores[0].value == 82.0
        assert stored.recommendations[0].department_id == "consulting"
        assert session.scalar(select(BidOutcome)).status == "WON"  # type: ignore[union-attr]
        assert session.scalar(select(ReferenceDataVersion)).status == "ACTIVE"  # type: ignore[union-attr]
    engine.dispose()


def test_analysis_idempotency_and_reference_version_constraints() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        notice = _notice(now)
        session.add(notice)
        session.flush()
        session.add(
            AnalysisRun(
                notice_id=notice.id,
                idempotency_key="same-run",
                input_sha256="d" * 64,
            )
        )
        session.commit()
        session.add(
            AnalysisRun(
                notice_id=notice.id,
                idempotency_key="same-run",
                input_sha256="d" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        reference = ReferenceDataVersion(
            dataset_key="department_keywords",
            version="v1",
            content_sha256="e" * 64,
            payload_json={"version": "v1"},
            effective_from=now,
        )
        session.add(reference)
        session.commit()
        session.add(
            ReferenceDataVersion(
                dataset_key="department_keywords",
                version="v1",
                content_sha256="e" * 64,
                payload_json={"version": "v1"},
                effective_from=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


def test_additive_migration_upgrades_an_existing_base_schema_idempotently() -> None:
    engine = build_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Notice.__table__.create(connection)
        NoticeVersion.__table__.create(connection)
        Evaluation.__table__.create(connection)
        UserDecision.__table__.create(connection)

    assert pending_migrations(engine) == [MIGRATION_ID]
    assert apply_additive_migrations(engine) == [MIGRATION_ID]
    assert apply_additive_migrations(engine) == []
    assert pending_migrations(engine) == []
    tables = set(inspect(engine).get_table_names())
    assert {
        "schema_migrations",
        "analysis_runs",
        "requirement_result_snapshots",
        "score_snapshots",
        "recommendation_snapshots",
        "reference_data_versions",
        "bid_outcomes",
    } <= tables
    engine.dispose()


def test_additive_migration_refuses_an_uninitialised_database() -> None:
    engine = build_engine("sqlite:///:memory:")
    with pytest.raises(MigrationError, match="--create-base"):
        apply_additive_migrations(engine)
    engine.dispose()


def test_migration_checksum_mismatch_fails_closed() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        schema_migrations.create(connection, checkfirst=True)
        connection.execute(
            schema_migrations.insert().values(
                migration_id=MIGRATION_ID,
                checksum="0" * 64,
            )
        )
    with pytest.raises(MigrationError, match="checksum mismatch"):
        pending_migrations(engine)
    with pytest.raises(MigrationError, match="checksum mismatch"):
        apply_additive_migrations(engine)
    assert MIGRATION_CHECKSUM != "0" * 64
    engine.dispose()


def test_migration_cli_create_base_and_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv("PAI_LOOP_DATABASE_URL", f"sqlite:///{database_path}")
    assert migration_main(["--create-base"]) == 0
    applied = capsys.readouterr().out
    assert MIGRATION_ID in applied
    assert migration_main(["--check"]) == 0
    assert '"pending": []' in capsys.readouterr().out


def test_migration_cli_check_reports_pending_and_requires_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "pending.db"
    monkeypatch.setenv("PAI_LOOP_DATABASE_URL", f"sqlite:///{database_path}")
    assert migration_main(["--check"]) == 1
    assert MIGRATION_ID in capsys.readouterr().out
    monkeypatch.delenv("PAI_LOOP_DATABASE_URL")
    with pytest.raises(MigrationError, match="PAI_LOOP_DATABASE_URL"):
        migration_main([])


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_new_tables_compile_for_sqlite_and_postgresql(dialect: object) -> None:
    for table in (
        AnalysisRun.__table__,
        RequirementResultSnapshot.__table__,
        ScoreSnapshot.__table__,
        RecommendationSnapshot.__table__,
        ReferenceDataVersion.__table__,
        BidOutcome.__table__,
    ):
        sql = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in sql
