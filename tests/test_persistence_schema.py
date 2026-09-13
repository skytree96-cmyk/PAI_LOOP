from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    JSON,
    MetaData,
    String,
    Table,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from pai_loop.database import Base, build_engine
from pai_loop.migrations import (
    AWARD_AGENCY_METADATA_MIGRATION_ID,
    AWARD_OPENING_RESULT_MIGRATION_ID,
    ACCOUNT_MIGRATION_ID,
    COMPANY_PERFORMANCE_MIGRATION_ID,
    INDEPENDENT_DECISION_MIGRATION_ID,
    COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_CHECKSUM,
    COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID,
    MIGRATION_CHECKSUM,
    MIGRATION_ID,
    NOTICE_ANALYSIS_POLICY_MIGRATION_ID,
    PERFORMANCE_NORMALIZATION_MIGRATION_ID,
    PRESPEC_MIGRATION_ID,
    MigrationError,
    apply_additive_migrations,
    main as migration_main,
    pending_migrations,
    schema_migrations,
)
from pai_loop.models import (
    AnalysisRun,
    AwardHistoryItem,
    BidOutcome,
    CompanyPerformanceRecord,
    Evaluation,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    ReferenceDataVersion,
    RequirementResultSnapshot,
    ScoreSnapshot,
    UserDecision,
)
from pai_loop.prespec_models import (
    PreSpecification,
    PreSpecificationAnalysisRun,
    PreSpecificationDocument,
    PreSpecificationVersion,
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

    assert pending_migrations(engine) == [
        MIGRATION_ID,
        NOTICE_ANALYSIS_POLICY_MIGRATION_ID,
        COMPANY_PERFORMANCE_MIGRATION_ID,
        COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID,
        PERFORMANCE_NORMALIZATION_MIGRATION_ID,
        PRESPEC_MIGRATION_ID,
        INDEPENDENT_DECISION_MIGRATION_ID,
        AWARD_OPENING_RESULT_MIGRATION_ID,
        ACCOUNT_MIGRATION_ID,
        AWARD_AGENCY_METADATA_MIGRATION_ID,
    ]
    assert apply_additive_migrations(engine) == [
        MIGRATION_ID,
        NOTICE_ANALYSIS_POLICY_MIGRATION_ID,
        COMPANY_PERFORMANCE_MIGRATION_ID,
        COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID,
        PERFORMANCE_NORMALIZATION_MIGRATION_ID,
        PRESPEC_MIGRATION_ID,
        INDEPENDENT_DECISION_MIGRATION_ID,
        AWARD_OPENING_RESULT_MIGRATION_ID,
        ACCOUNT_MIGRATION_ID,
        AWARD_AGENCY_METADATA_MIGRATION_ID,
    ]
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
        "notice_analysis_policies",
        "company_performance_records",
        "performance_normalization_batches",
        "performance_normalization_revisions",
        "pre_specifications",
        "pre_specification_versions",
        "pre_specification_documents",
        "pre_specification_analysis_runs",
        "department_accounts",
        "account_sessions",
        "account_login_buckets",
        "account_audit",
        "account_bootstrap_previews",
    } <= tables
    performance_columns = {
        column["name"]
        for column in inspect(engine).get_columns("company_performance_records")
    }
    assert {
        "gross_contract_amount_krw",
        "recognized_performance_amount_krw",
        "recognized_amount_is_net_of_share",
    } <= performance_columns
    engine.dispose()


def test_independent_decision_migration_adds_nullable_decision_columns() -> None:
    """A human decision must be storable without any evaluation to point at."""

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    assert INDEPENDENT_DECISION_MIGRATION_ID in pending_migrations(engine)
    assert INDEPENDENT_DECISION_MIGRATION_ID in apply_additive_migrations(engine)
    assert apply_additive_migrations(engine) == []

    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("user_decisions")
    }
    assert {"analysis_state_snapshot", "analysis_snapshot"} <= set(columns)
    assert columns["analysis_state_snapshot"]["nullable"] is True
    assert columns["analysis_snapshot"]["nullable"] is True
    assert columns["evaluation_id"]["nullable"] is True

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        notice = _notice(now)
        session.add(notice)
        session.flush()
        session.add(
            UserDecision(
                notice_id=notice.id,
                evaluation_id=None,
                choice="NO_GO",
                actor_label="KMA 입찰팀",
                rationale="분석 전 담당자 판단",
                analysis_state_snapshot="NOT_EVALUATED",
                analysis_snapshot={"analysis_state": "NOT_EVALUATED"},
            )
        )
        session.commit()
        stored = session.scalar(select(UserDecision))
        assert stored is not None
        assert stored.evaluation_id is None
        assert stored.analysis_state_snapshot == "NOT_EVALUATED"
    engine.dispose()


def test_independent_decision_migration_upgrades_a_legacy_sqlite_table() -> None:
    """Existing SQLite installations upgrade without recreating their database."""

    engine = build_engine("sqlite:///:memory:")
    legacy_user_decisions = Table(
        "user_decisions",
        MetaData(),
        Column("id", String(36), primary_key=True),
        Column("notice_id", String(36), nullable=False),
        Column("evaluation_id", String(36), nullable=False),
        Column("choice", String(32), nullable=False),
        Column("actor_label", String(120), nullable=False),
        Column("rationale", String(), nullable=False),
        Column("conditions", JSON),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
    )
    with engine.begin() as connection:
        Notice.__table__.create(connection)
        NoticeVersion.__table__.create(connection)
        Evaluation.__table__.create(connection)
        legacy_user_decisions.create(connection)

    assert INDEPENDENT_DECISION_MIGRATION_ID in apply_additive_migrations(engine)
    assert apply_additive_migrations(engine) == []
    columns = {column["name"]: column for column in inspect(engine).get_columns("user_decisions")}
    assert columns["evaluation_id"]["nullable"] is True
    assert {"analysis_state_snapshot", "analysis_snapshot"} <= columns.keys()
    engine.dispose()


def test_award_opening_result_migration_adds_nullable_columns() -> None:
    """Competitors and evaluation scores are addable without touching old rows."""

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    assert AWARD_OPENING_RESULT_MIGRATION_ID in pending_migrations(engine)
    assert AWARD_OPENING_RESULT_MIGRATION_ID in apply_additive_migrations(engine)
    assert apply_additive_migrations(engine) == []

    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("award_history_items")
    }
    for name in ("opening_results", "opening_results_status", "opening_results_read_at"):
        assert columns[name]["nullable"] is True

    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        notice = _notice(now)
        session.add(notice)
        session.flush()
        session.add(
            AwardHistoryItem(
                target_notice_id=notice.id,
                external_identity="SYN-AWARD-1|000|0|000",
                bid_notice_no="SYN-AWARD-1",
                title="SYN 낙찰 이력",
                agency="SYN 기관",
                winner_name="SYN-A",
                similarity_score=90.0,
            )
        )
        session.commit()
        stored = session.scalar(select(AwardHistoryItem))
        assert stored is not None
        # A pre-existing award keeps NULL: never collected, never an empty
        # competitor set and never a zero score.
        assert stored.opening_results is None
        assert stored.opening_results_status is None
        assert stored.opening_results_read_at is None
    engine.dispose()


def test_additive_migration_refuses_an_uninitialised_database() -> None:
    engine = build_engine("sqlite:///:memory:")
    with pytest.raises(MigrationError, match="--create-base"):
        apply_additive_migrations(engine)
    engine.dispose()


def test_notice_policy_migration_upgrades_a_legacy_migration_ledger() -> None:
    engine = build_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Notice.__table__.create(connection)
        NoticeVersion.__table__.create(connection)
        Evaluation.__table__.create(connection)
        UserDecision.__table__.create(connection)
        # The recorded base migration includes bid_outcomes; the account
        # upgrade now adds nullable attribution to that existing table too.
        BidOutcome.__table__.create(connection)
        schema_migrations.create(connection)
        connection.execute(
            schema_migrations.insert().values(
                migration_id=MIGRATION_ID,
                checksum=MIGRATION_CHECKSUM,
            )
        )

    expected = [
        NOTICE_ANALYSIS_POLICY_MIGRATION_ID,
        COMPANY_PERFORMANCE_MIGRATION_ID,
        COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID,
        PERFORMANCE_NORMALIZATION_MIGRATION_ID,
        PRESPEC_MIGRATION_ID,
        INDEPENDENT_DECISION_MIGRATION_ID,
        AWARD_OPENING_RESULT_MIGRATION_ID,
        ACCOUNT_MIGRATION_ID,
        AWARD_AGENCY_METADATA_MIGRATION_ID,
    ]
    assert pending_migrations(engine) == expected
    assert apply_additive_migrations(engine) == expected
    assert "notice_analysis_policies" in inspect(engine).get_table_names()
    assert pending_migrations(engine) == []
    engine.dispose()


def test_recognized_amount_migration_adds_columns_to_v1_table() -> None:
    engine = build_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Notice.__table__.create(connection)
        NoticeVersion.__table__.create(connection)
        Evaluation.__table__.create(connection)
        UserDecision.__table__.create(connection)
        connection.exec_driver_sql(
            "CREATE TABLE company_performance_records ("
            "id VARCHAR(36) PRIMARY KEY, record_key VARCHAR(180) NOT NULL)"
        )

    applied = apply_additive_migrations(engine)

    assert COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID in applied
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("company_performance_records")
    }
    assert {
        "gross_contract_amount_krw",
        "recognized_performance_amount_krw",
        "recognized_amount_is_net_of_share",
    } <= columns
    assert apply_additive_migrations(engine) == []
    engine.dispose()


@pytest.mark.parametrize(
    ("column_definition", "expected_message"),
    [
        (
            "gross_contract_amount_krw TEXT NULL",
            "gross_contract_amount_krw; expected BigInteger",
        ),
        (
            "recognized_amount_is_net_of_share BOOLEAN NOT NULL",
            "recognized_amount_is_net_of_share; the additive column must be nullable",
        ),
    ],
)
def test_recognized_amount_migration_rejects_incompatible_existing_target_column(
    column_definition: str,
    expected_message: str,
) -> None:
    engine = build_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Notice.__table__.create(connection)
        NoticeVersion.__table__.create(connection)
        Evaluation.__table__.create(connection)
        UserDecision.__table__.create(connection)
        connection.exec_driver_sql(
            "CREATE TABLE company_performance_records ("
            "id VARCHAR(36) PRIMARY KEY, record_key VARCHAR(180) NOT NULL, "
            f"{column_definition})"
        )

    with pytest.raises(MigrationError, match=expected_message):
        apply_additive_migrations(engine)

    with engine.connect() as connection:
        if "schema_migrations" in inspect(connection).get_table_names():
            assert connection.execute(
                select(schema_migrations.c.migration_id).where(
                    schema_migrations.c.migration_id
                    == COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID
                )
            ).scalar_one_or_none() is None
    engine.dispose()


def test_recorded_recognized_amount_migration_revalidates_physical_schema() -> None:
    engine = build_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Notice.__table__.create(connection)
        NoticeVersion.__table__.create(connection)
        Evaluation.__table__.create(connection)
        UserDecision.__table__.create(connection)
        schema_migrations.create(connection)
        connection.execute(
            schema_migrations.insert().values(
                migration_id=COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_ID,
                checksum=COMPANY_PERFORMANCE_RECOGNIZED_AMOUNT_MIGRATION_CHECKSUM,
            )
        )
        connection.exec_driver_sql(
            "CREATE TABLE company_performance_records ("
            "id VARCHAR(36) PRIMARY KEY, record_key VARCHAR(180) NOT NULL, "
            "gross_contract_amount_krw BIGINT NULL, "
            "recognized_performance_amount_krw TEXT NULL, "
            "recognized_amount_is_net_of_share BOOLEAN NULL)"
        )

    with pytest.raises(
        MigrationError,
        match="recognized_performance_amount_krw; expected BigInteger",
    ):
        pending_migrations(engine)
    with pytest.raises(
        MigrationError,
        match="recognized_performance_amount_krw; expected BigInteger",
    ):
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
        CompanyPerformanceRecord.__table__,
        PreSpecification.__table__,
        PreSpecificationVersion.__table__,
        PreSpecificationDocument.__table__,
        PreSpecificationAnalysisRun.__table__,
    ):
        sql = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in sql
