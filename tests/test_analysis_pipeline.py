from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pai_loop.analysis_pipeline import (
    AnalysisPipelineError,
    AnalysisPipelineSourceError,
    AnalysisPipelineTransactionError,
    MATERIALIZATION_VERSION,
    PIPELINE_VERSION,
    SNAPSHOT_VERSION,
    _digest,
    _system_bid_recommendation,
    run_analysis_pipeline,
)
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.evaluator import evaluate_notice
from pai_loop.integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ExtractionPayload,
)
from pai_loop.models import (
    AnalysisRun,
    AtomicRequirement,
    AwardHistoryItem,
    CompanyFact,
    Evaluation,
    Evidence,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    ReferenceDataVersion,
    RequirementResultSnapshot,
    ScoreSnapshot,
)
from pai_loop.pps_enrichment import (
    PPS_METADATA_SCHEMA,
    PPS_PROCESSING_VERSION,
)
from pai_loop.quantitative_rule_extraction import (
    validate_quantitative_attachment_extraction,
)


DEADLINE = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
RFP_PRICING_SHA = "53b24e9dae63328d4f692e4cbe21e7148e0f24614dedbec5c356e2adbfc84648"


@pytest.fixture
def db_session() -> Session:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _notice(
    session: Session,
    *,
    notice_key: str = "R25BK00764725-000",
    title: str = "2025 AI 리터러시 역량 강화 교육 운영 용역",
) -> Notice:
    notice = Notice(
        notice_key=notice_key,
        bid_notice_no=notice_key,
        revision_no="000",
        title=title,
        agency="공개 테스트 기관",
        published_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        deadline=DEADLINE,
        status="OPEN",
        category="AI 역량강화 교육 컨설팅 용역",
        estimated_amount=100_000_000,
        risk_dimensions={
            "qualification": 20,
            "execution": 30,
            "competition": 40,
            "profitability": 25,
            "operation": 20,
            "document": 10,
        },
    )
    session.add(notice)
    session.flush()
    return notice


def _requirement(
    requirement_id: str,
    condition: str,
    *,
    attachment_id: str,
    category: str = "ENTITY",
    mandatory: bool = True,
    confidence: float = 0.98,
) -> dict[str, Any]:
    return {
        "requirement_id": requirement_id,
        "category": category,
        "logic": "SINGLE",
        "normalized_condition": condition,
        "mandatory": mandatory,
        "deadline_basis": "입찰 마감일",
        "evidence": [
            {
                "attachment_id": attachment_id,
                "page": 2,
                "section": "입찰참가자격",
                "quote": f"공개 근거 {requirement_id}",
                "confidence": confidence,
            }
        ],
        "ambiguity_reason": None,
    }


def _quantitative_table(attachment_id: str) -> dict[str, Any]:
    criterion_literal = "용역수행 실적 건수 (10점)"
    row_literal = "5건 이상 10점, 미충족 0점"
    return {
        "table_id": "Q-PERFORMANCE",
        "label": "정량적 평가 세부기준",
        "criteria": [
            {
                "criterion_id": "Q-PERFORMANCE-COUNT",
                "label": "용역수행 실적 건수",
                "criterion_literal": criterion_literal,
                "max_points": 10,
                "scoring_method": "THRESHOLD",
                "metric": "PERFORMANCE_COUNT",
                "unit": "건",
                "brackets": [],
                "threshold": {
                    "literal": row_literal,
                    "operator": "GTE",
                    "threshold_value": 5,
                    "points_if_met": 10,
                    "points_if_not_met": 0,
                    "evidence": {
                        "attachment_id": attachment_id,
                        "page": 33,
                        "section": "정량적 평가 세부기준",
                        "quote": row_literal,
                        "confidence": 0.98,
                    },
                },
                "formula_literal": None,
                "cases": [],
                "recognition_conditions": [],
                "required_evidence": ["company.performance.count"],
                "evidence": {
                    "attachment_id": attachment_id,
                    "page": 33,
                    "section": "정량적 평가 세부기준",
                    "quote": criterion_literal,
                    "confidence": 0.98,
                },
                "ambiguity_reason": None,
            }
        ],
        "total_points": 10,
        "total_evidence": {
            "attachment_id": attachment_id,
            "page": 33,
            "section": "정량적 평가 세부기준",
            "quote": "정량평가 합계 10점",
            "confidence": 0.98,
        },
        "minimum_score": None,
        "minimum_evidence": None,
        "ambiguity_reason": None,
    }


def _quantitative_source_text() -> str:
    return "\n".join(
        (
            "정량평가표",
            "용역수행 실적 건수 (10점)",
            "5건 이상 10점, 미충족 0점",
            "정량평가 합계 10점",
        )
    )


def _source_version(
    notice: Notice,
    *,
    version_no: int,
    attachment_id: str,
    digest_char: str,
    requirements: list[dict[str, Any]] | None,
    status: str = "ACCEPTED",
    document_complete: bool = True,
    prompt_version: str = PROMPT_VERSION,
    include_missing_field: bool = True,
    missing: list[str] | None = None,
    document_type: str = "NOTICE",
    extraction_confidence: float | None = None,
    source_label: str | None = None,
    quantitative_tables: list[dict[str, Any]] | None = None,
    include_quantitative_validation_record: bool = True,
) -> NoticeVersion:
    digest = digest_char * 64
    result = None
    if requirements is not None:
        result = {
            "document_type": document_type,
            "requirements": requirements,
            "summary": "공개 테스트 추출",
        }
        if include_missing_field:
            result["missing_or_unreadable"] = missing or []
        if quantitative_tables is not None:
            result["quantitative_tables"] = quantitative_tables
    source_payload: dict[str, Any] = {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "attachment_id": attachment_id,
        "source_label": source_label,
        "document_sha256": digest,
        "status": status,
        "review_code": None if status == "ACCEPTED" else "R07",
        "error_code": None if status == "ACCEPTED" else "MODEL_REFUSAL",
        "model": "test-extractor",
        "prompt_version": prompt_version,
        "schema_version": SCHEMA_VERSION,
        "result": result,
    }
    if (
        result is not None
        and quantitative_tables is not None
        and include_quantitative_validation_record
    ):
        current_manifest_sha256 = "f" * 64
        quantitative_record = validate_quantitative_attachment_extraction(
            ExtractionPayload.model_validate(result),
            source_text=_quantitative_source_text(),
            attachment_id=attachment_id,
            document_sha256=digest,
            manifest_sha256=current_manifest_sha256,
        )
        source_payload.update(
            {
                "manifest_sha256": current_manifest_sha256,
                "current_manifest_sha256": current_manifest_sha256,
                "quantitative_validation_record": quantitative_record.model_dump(
                    mode="json"
                ),
            }
        )
    version = NoticeVersion(
        notice_id=notice.id,
        version_no=version_no,
        file_sha256=digest,
        document_complete=document_complete,
        extraction_status=status,
        extraction_confidence=(
            extraction_confidence
            if extraction_confidence is not None
            else 0.98 if status == "ACCEPTED" else 0.0
        ),
        source_payload=source_payload,
    )
    notice.versions.append(version)
    return version


def _verified_boolean_fact(session: Session, key: str, *, evidence_required: bool = True) -> None:
    evidence = None
    if evidence_required:
        evidence = Evidence(
            evidence_key=f"E-{key}",
            name="공개 검증 증빙",
            evidence_type="PUBLIC_TEST",
            status="VERIFIED",
            issued_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
            sha256="e" * 64,
        )
        session.add(evidence)
        session.flush()
    session.add(
        CompanyFact(
            fact_key=key,
            value=True,
            effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
            evidence_id=evidence.id if evidence else None,
            verified=True,
            source="PUBLIC_TEST",
        )
    )


def _seed_reference_versions(session: Session) -> None:
    versions = {
        "company_public_profile": "company-ref-v1",
        "department_keyword_profiles": "department-ref-v1",
        "quantitative_notice_profiles": "quantitative-ref-v1",
        "pricing_method_profiles": "pricing-ref-v1",
    }
    for index, (dataset_key, version) in enumerate(versions.items(), start=1):
        session.add(
            ReferenceDataVersion(
                dataset_key=dataset_key,
                version=version,
                schema_version="test-v1",
                content_sha256=str(index) * 64,
                classification="PUBLIC_REVIEWED",
                source="TEST_PACKAGE",
                status="ACTIVE",
                payload_json={},
                effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )


def _seed_awards(session: Session, notice: Notice) -> None:
    for index in range(6):
        session.add(
            AwardHistoryItem(
                target_notice_id=notice.id,
                external_identity=f"PUBLIC-AWARD-{index}",
                bid_notice_no=f"PUB-{index}",
                revision_no="000",
                title="AI 교육 용역",
                agency="공개 발주기관",
                winner_name=("공개기관A" if index < 3 else f"공개기관{index}"),
                participant_count=2 + (index % 3),
                award_amount=80_000_000 + index * 1_000_000,
                award_rate=80 + index,
                opened_at=datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
                awarded_at=datetime(2025, 1, 2, tzinfo=timezone.utc) + timedelta(days=index),
                similarity_score=80,
                source="PUBLIC_TEST",
            )
        )


def test_pipeline_merges_sources_and_persists_full_immutable_snapshot(db_session: Session) -> None:
    notice = _notice(db_session)
    notice.deadline = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    first = _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-QUAL",
        digest_char="a",
        requirements=[
            _requirement(
                "REQ-QUAL",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-QUAL",
            )
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-PROCEDURE",
        digest_char="b",
        requirements=[
            _requirement(
                "REQ-E-BID",
                "나라장터 전자입찰서 제출 가능",
                attachment_id="ATT-PROCEDURE",
                category="SUBMISSION",
            )
        ],
        include_missing_field=False,
    )
    notice.versions.append(
        NoticeVersion(
            version_no=3,
            file_sha256=RFP_PRICING_SHA,
            document_complete=True,
            extraction_status="REFERENCE",
            extraction_confidence=1.0,
            source_payload={"kind": "PUBLIC_DOCUMENT_REFERENCE"},
        )
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    _seed_reference_versions(db_session)
    _seed_awards(db_session, notice)
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "COMPLETED"
    assert result.reused is False
    assert result.source_count == 2
    assert result.accepted_source_count == 2
    assert result.materialized_requirement_count == 1
    assert result.requirement_snapshot_count == 2
    assert result.score_snapshot_count == 8
    assert result.recommendation_snapshot_count > 0
    assert result.eligibility == "PASS"

    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert run is not None
    assert run.company_profile_version == "company-ref-v1"
    assert run.department_profile_version == "department-ref-v1"
    assert run.quantitative_profile_version == "quantitative-ref-v1"
    assert run.pricing_profile_version == "pricing-ref-v1"
    assert run.analytics_version == "award-intelligence-1.1.0"
    assert len(run.requirement_results) == 2
    assert {item.policy_class for item in run.requirement_results} == {
        "ELIGIBILITY",
        "INFORMATION",
    }
    assert len(run.scores) == 8
    scores = {item.score_key: item for item in run.scores}
    assert scores["quantitative.total"].value is None
    assert scores["quantitative.total"].lower_value == 9.7
    assert scores["quantitative.total"].upper_value == 20
    assert scores["quantitative.total"].basis_json["confirmed_points"] == 0
    public_criteria = scores["quantitative.total"].basis_json["public_criteria"]
    assert public_criteria["schema_version"] == (
        "public-quantitative-criteria-1.0.0"
    )
    assert public_criteria["items"]
    assert all(
        set(item)
        == {
            "display_code",
            "max_points",
            "estimated_points",
            "lower_points",
            "upper_points",
            "status",
        }
        for item in public_criteria["items"]
    )
    public_criteria_text = str(public_criteria)
    for forbidden_field in (
        "criterion_id",
        "category",
        "label",
        "formula",
        "rationale",
        "assumptions",
        "condition",
        "source_anchor",
        "quote",
        "evidence",
        "fact_binding",
        "attachment",
        "sha256",
    ):
        assert forbidden_field not in public_criteria_text
    # These legacy awards have no verified demand-agency evidence. Keep them
    # as stored audit records without using them to estimate competition/prices.
    assert scores["competition.risk"].status == "UNKNOWN"
    assert scores["competition.risk"].value is None
    assert scores["pricing.award_rate_prediction"].status == "INSUFFICIENT_DATA"
    assert scores["pricing.award_rate_prediction"].value is None
    assert run.input_manifest["award_history_ids"] == []
    assert scores["pricing.submitted_bid_rate_prediction"].status == "INSUFFICIENT_DATA"
    assert scores["pricing.method"].status == "AVAILABLE"
    assert scores["business.risk"].value == 23.5
    assert scores["business.risk"].status == "AVAILABLE"
    assert scores["business.risk"].method_version == "business-risk-2.0.0"
    assert scores["business.risk"].basis_json["evidenced_axis_count"] == 6
    assert scores["business.risk"].basis_json["axis_basis"]["qualification"]["source"] == (
        "NOTICE_RISK_DIMENSIONS_AUTHORITATIVE_OVERRIDE"
    )
    assert len(run.recommendations) == result.recommendation_snapshot_count
    system_opinion = next(
        item for item in run.recommendations if item.recommendation_key == "bid:system"
    )
    assert system_opinion.rank == 0
    assert system_opinion.recommendation in {"GO", "HOLD", "NO_GO"}
    assert system_opinion.detail_json["decision_boundary"].startswith("SYSTEM_ADVISORY_ONLY")
    department_recommendations = [
        item for item in run.recommendations if item.recommendation_key != "bid:system"
    ]
    assert department_recommendations
    assert department_recommendations[0].rank == 1
    manifest_text = str(run.input_manifest)
    assert "경쟁입찰참가자격" not in manifest_text
    assert "공개 근거" not in manifest_text
    assert first.id in run.input_manifest["source_version_ids"]
    db_session.rollback()

    repeated = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert repeated.reused is True
    assert repeated.analysis_run_id == result.analysis_run_id

    db_session.add(
        NoticeVersion(
            notice_id=notice.id,
            version_no=5,
            file_sha256=first.file_sha256,
            document_complete=first.document_complete,
            extraction_status=first.extraction_status,
            extraction_confidence=first.extraction_confidence,
            source_payload=first.source_payload,
        )
    )
    db_session.commit()
    duplicate_source_retry = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert duplicate_source_retry.reused is True
    assert duplicate_source_retry.analysis_run_id == result.analysis_run_id
    assert db_session.scalar(select(func.count(AnalysisRun.id))) == 1


def test_company_declaration_does_not_invent_an_evidence_requirement(db_session: Session) -> None:
    notice = _notice(db_session, notice_key="DECLARATION", title="유죄 사실 확인 교육 용역")
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-DECLARATION",
        digest_char="c",
        requirements=[
            _requirement(
                "REQ-CONVICTION",
                "조세포탈 유죄판결 사실이 없어야 함",
                attachment_id="ATT-DECLARATION",
                category="ENTITY",
            )
        ],
    )
    _verified_boolean_fact(db_session, "conviction_clear", evidence_required=False)
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert result.status == "COMPLETED"
    assert result.eligibility == "PASS"
    requirement = db_session.scalar(
        select(AtomicRequirement).where(
            AtomicRequirement.notice_version_id == result.notice_version_id
        )
    )
    assert requirement is not None
    assert requirement.fact_key == "conviction_clear"
    assert requirement.evidence_required is False


def test_missing_industry_code_is_persisted_as_fail_not_review(db_session: Session) -> None:
    notice = _notice(db_session, notice_key="INDUSTRY-MISSING", title="업종코드 제한 용역")
    notice.deadline = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-INDUSTRY",
        digest_char="9",
        requirements=[
            _requirement(
                "REQ-INDUSTRY-MISSING",
                "국내여행업(업종코드 1263) 등록업체에 한함",
                attachment_id="ATT-INDUSTRY",
                category="INDUSTRY_CODE",
            )
        ],
    )
    evidence = Evidence(
        evidence_key="E-INDUSTRY-INVENTORY",
        name="공개 업종 등록 전체 스냅샷",
        evidence_type="PUBLIC_TEST",
        status="VERIFIED",
        valid_from=datetime(2026, 8, 5, tzinfo=timezone.utc),
        valid_until=datetime(2026, 11, 30, tzinfo=timezone.utc),
        sha256="f" * 64,
    )
    db_session.add(evidence)
    db_session.flush()
    db_session.add(
        CompanyFact(
            fact_key="industry_code_inventory",
            value=["1169", "1261"],
            effective_from=datetime(2026, 8, 5, tzinfo=timezone.utc),
            effective_to=datetime(2026, 11, 30, tzinfo=timezone.utc),
            evidence_id=evidence.id,
            verified=True,
            source="PUBLIC_TEST",
        )
    )
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    requirement_row = db_session.scalar(
        select(AtomicRequirement).where(
            AtomicRequirement.notice_version_id == result.notice_version_id
        )
    )

    assert result.eligibility == "FAIL"
    assert result.reason_code == "DF-000"
    assert requirement_row is not None
    assert requirement_row.fact_key == "industry_code_inventory"
    assert requirement_row.operator == "contains"
    assert requirement_row.required_value == "1263"


def test_pipeline_excludes_unverified_agency_awards_from_competition_and_profitability(
    db_session: Session,
) -> None:
    notice = _notice(db_session, notice_key="DERIVED-RISK", title="AI 교육 용역")
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-RISK",
        digest_char="6",
        requirements=[
            _requirement(
                "REQ-RISK",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-RISK",
            )
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    _seed_awards(db_session, notice)
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert run is not None
    business_risk = next(item for item in run.scores if item.score_key == "business.risk")

    assert business_risk.status == "AVAILABLE"
    assert business_risk.value is not None
    assert business_risk.basis_json["evidenced_axis_count"] == 4
    assert "competition" not in business_risk.basis_json["axis_basis"]
    assert "profitability" not in business_risk.basis_json["axis_basis"]
    assert run.input_manifest["award_history_ids"] == []
    assert db_session.scalar(select(func.count()).select_from(AwardHistoryItem)) == 6


def test_new_risk_semantics_have_versioned_non_reusable_idempotency(
    db_session: Session,
) -> None:
    assert PIPELINE_VERSION == "analysis-pipeline-0.6.6"
    assert MATERIALIZATION_VERSION == "atomic-materializer-0.3.1"
    assert SNAPSHOT_VERSION == "analysis-snapshot-0.3.0"
    notice = _notice(db_session, notice_key="RISK-VERSION", title="AI 리터러시 교육 용역")
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-VERSION",
        digest_char="5",
        requirements=[
            _requirement(
                "REQ-VERSION",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-VERSION",
            )
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    previous = run_analysis_pipeline(
        db_session,
        notice_id=notice.id,
        ruleset_version="2026.08-v1",
    )
    current = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert current.reused is False
    assert current.analysis_run_id != previous.analysis_run_id
    assert current.input_sha256 != previous.input_sha256
    assert db_session.scalar(
        select(func.count(AnalysisRun.id)).where(AnalysisRun.notice_id == notice.id)
    ) == 2
    run = db_session.get(AnalysisRun, current.analysis_run_id)
    assert run is not None
    assert run.ruleset_version == "2026.08-v2"
    assert run.basis_versions["business_risk"] == "business-risk-2.0.0"


def test_action_is_reviewed_but_checklist_is_snapshot_only(db_session: Session) -> None:
    notice = _notice(db_session, notice_key="ACTION", title="제안설명회 교육 용역")
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-ACTION",
        digest_char="d",
        requirements=[
            _requirement(
                "REQ-ACTION",
                "제안설명회 참여 필수이며 불참 시 입찰 불가",
                attachment_id="ATT-ACTION",
                category="SUBMISSION",
            ),
            _requirement(
                "REQ-VISIT",
                "제안서는 직접 방문 접수",
                attachment_id="ATT-ACTION",
                category="SUBMISSION",
            ),
        ],
    )
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert result.eligibility == "REVIEW"
    assert result.reason_code == "REVIEW_MATCH"
    assert result.materialized_requirement_count == 1
    snapshots = list(
        db_session.scalars(
            select(RequirementResultSnapshot).where(
                RequirementResultSnapshot.analysis_run_id == result.analysis_run_id
            )
        ).all()
    )
    assert {item.policy_class for item in snapshots} == {"ACTION_REQUIRED", "CHECKLIST"}
    action = next(item for item in snapshots if item.policy_class == "ACTION_REQUIRED")
    checklist = next(item for item in snapshots if item.policy_class == "CHECKLIST")
    assert action.reason_code == "R04"
    assert action.blocking is True
    assert checklist.blocking is False


def test_complete_nonblocking_requirements_do_not_become_r07(db_session: Session) -> None:
    notice = _notice(db_session, notice_key="NONBLOCKING", title="일반 운영 안내 용역")
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-NONBLOCKING",
        digest_char="6",
        requirements=[
            _requirement(
                "REQ-INFO",
                "용역기간은 계약체결일부터 12개월",
                attachment_id="ATT-NONBLOCKING",
                category="OTHER",
            ),
            _requirement(
                "REQ-CHECK",
                "제안서는 직접 방문 접수",
                attachment_id="ATT-NONBLOCKING",
                category="SUBMISSION",
            ),
        ],
    )
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "COMPLETED"
    assert result.eligibility == "PASS"
    assert result.reason_code == "NO_BLOCKING_REQUIREMENTS"
    assert result.materialized_requirement_count == 0
    assert "NO_BLOCKING_REQUIREMENTS_VERIFIED" in result.warnings
    assert "NO_ELIGIBILITY_OR_ACTION_REQUIREMENTS" not in result.warnings
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation is not None
    assert evaluation.readiness_status == "GREEN"
    assert evaluation.risk_score is not None
    assert evaluation.risk_band == "GO"
    snapshots = list(
        db_session.scalars(
            select(RequirementResultSnapshot).where(
                RequirementResultSnapshot.analysis_run_id == result.analysis_run_id
            )
        ).all()
    )
    assert {item.policy_class for item in snapshots} == {"CHECKLIST", "INFORMATION"}
    assert all(item.blocking is False for item in snapshots)


def test_review_never_becomes_system_no_go_from_uncertainty_risk() -> None:
    assert _system_bid_recommendation(
        eligibility="REVIEW",
        readiness_status="GRAY",
        business_risk_band="NO_GO",
        quantitative_status="REVIEW",
        quantitative_band="GRAY",
        competition_band=None,
    ) == "HOLD"


def test_partial_extraction_merges_accepted_content_but_forces_r07(db_session: Session) -> None:
    notice = _notice(db_session, notice_key="PARTIAL", title="부분 추출 교육 용역")
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-GOOD",
        digest_char="e",
        requirements=[
            _requirement(
                "REQ-GOOD",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-GOOD",
            )
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-FAILED",
        digest_char="f",
        requirements=None,
        status="REVIEW",
        document_complete=False,
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert result.status == "PARTIAL"
    assert result.reason_code == "R07"
    assert result.accepted_source_count == 1
    assert "SOURCE_STATUS_NOT_ACCEPTED" in result.warnings
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation is not None
    assert evaluation.eligibility == "REVIEW"
    assert evaluation.risk_score is None
    assert evaluation.risk_band == "UNKNOWN"
    assert evaluation.explanation["risk"]["status"] == "WITHHELD_R07"
    assert {item["reason_code"] for item in evaluation.atomic_results} == {"R07"}


def test_known_non_eligibility_gap_can_release_r07_after_strict_eligibility_checks(
    db_session: Session,
) -> None:
    notice = _notice(db_session, notice_key="PARTIAL-SAFE", title="가격표 일부 누락 교육 용역")
    notice.deadline = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-SAFE",
        digest_char="7",
        requirements=[
            _requirement(
                "REQ-SAFE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-SAFE",
            )
        ],
        missing=["가격산정 세부표 일부"],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == "PASS"
    assert result.reason_code == "PASS_MATCH"
    assert "NON_ELIGIBILITY_PARTIAL_GATE_APPLIED" in result.warnings
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert run is not None and evaluation is not None
    assert run.error_code is None
    assert run.output_summary["eligibility_gate_applied"] is True
    assert evaluation.explanation["document_gate"]["strict_document_quality_ok"] is False
    assert evaluation.explanation["analysis_pipeline"]["eligibility_gate_applied"] is True
    assert {item["reason_code"] for item in evaluation.atomic_results} == {"P-ENTITY"}
    assert evaluation.risk_score == 35.3
    assert evaluation.risk_band == "CONDITIONAL_GO"
    business_risk = next(item for item in run.scores if item.score_key == "business.risk")
    assert business_risk.status == "AVAILABLE"
    assert business_risk.basis_json["evidenced_axes"] == [
        "qualification",
        "execution",
        "operation",
        "document",
    ]
    assert business_risk.basis_json["axis_basis"]["document"]["run_status"] == "PARTIAL"


def test_quantitative_absence_is_not_resolved_by_document_presence_alone(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="SIBLING-COVERAGE",
        title="공고문과 제안요청서가 분리된 공급 용역",
    )
    notice.deadline = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-NOTICE",
        digest_char="a",
        requirements=[
            _requirement(
                "REQ-SIBLING",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-NOTICE",
            )
        ],
        document_complete=False,
        document_type="NOTICE",
        missing=[
            "입찰공고 본문에는 제안요청서의 세부 요구사항과 정량 평가표가 포함되지 않음."
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-RFP",
        digest_char="b",
        requirements=[],
        document_type="RFP",
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == "PASS"
    assert result.reason_code == "PASS_MATCH"
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" not in result.warnings
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


@pytest.mark.parametrize(
    ("rfp_confidence", "expected_status", "expected_eligibility"),
    ((None, "COMPLETED", "PASS"), (0, "PARTIAL", "REVIEW")),
    ids=("requirement-confidence", "quantitative-anchor-confidence"),
)
@pytest.mark.parametrize(
    ("gap_kind", "notice_gap"),
    (
        (
            "production",
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 "
            "포함되어 있지 않음",
        ),
        (
            "missing-body",
            "제안요청서(붙임) 본문이 제공되지 않아 세부 평가배점표"
            "(정량평가 기준)를 확인할 수 없음",
        ),
        (
            "separate-rfp",
            "별도 제안요청서의 평가배점표는 이 첨부에 포함되지 않음",
        ),
    ),
)
def test_busan_cross_references_and_qualitative_exclusion_form_complete_closure(
    db_session: Session,
    rfp_confidence: float | None,
    expected_status: str,
    expected_eligibility: str,
    gap_kind: str,
    notice_gap: str,
) -> None:
    notice = _notice(
        db_session,
        notice_key=(
            f"BUSAN-CROSS-REFERENCE-CLOSURE-{expected_eligibility}-{gap_kind}"
        ),
        title="공고문과 제안요청서가 상호 참조하는 교육 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-BUSAN-NOTICE",
        digest_char="1",
        requirements=[
            _requirement(
                "REQ-BUSAN-CLOSURE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-BUSAN-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문(재공고).pdf",
        missing=[notice_gap],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-BUSAN-RFP",
        digest_char="2",
        requirements=[],
        document_type="RFP",
        source_label="2026 부산교육한마당 제안요청서.hwp",
        extraction_confidence=rfp_confidence,
        quantitative_tables=[_quantitative_table("ATT-BUSAN-RFP")],
        missing=[
            "정성적 평가(80점) 세부 평가항목은 등급 척도"
            "(매우우수/우수/보통/미흡)만 제시되어 있어 정성 판단 항목으로 "
            "정량 테이블에서 제외됨",
            "입찰공고문(제출기한 등 구체 일정) 원문은 본 첨부에 "
            "포함되어 있지 않음",
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == expected_status
    assert result.eligibility == expected_eligibility
    if expected_eligibility == "PASS":
        assert result.reason_code == "PASS_MATCH"
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" in result.warnings
    assert "AGGREGATE_GAPS_UNRESOLVED" not in result.warnings
    if rfp_confidence == 0:
        assert "LOW_EXTRACTION_CONFIDENCE" in result.warnings


def test_unreadable_table_in_technical_sibling_keeps_busan_closure_incomplete(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="BUSAN-CROSS-REFERENCE-UNREADABLE",
        title="판독 불가 표가 남은 교육 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-BUSAN-NOTICE-UNREADABLE",
        digest_char="3",
        requirements=[
            _requirement(
                "REQ-BUSAN-UNREADABLE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-BUSAN-NOTICE-UNREADABLE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문(재공고).pdf",
        missing=[
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 "
            "포함되어 있지 않음"
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-BUSAN-RFP-UNREADABLE",
        digest_char="4",
        requirements=[],
        document_type="RFP",
        source_label="2026 부산교육한마당 제안요청서.hwp",
        quantitative_tables=[
            _quantitative_table("ATT-BUSAN-RFP-UNREADABLE")
        ],
        missing=[
            "정성적 평가(80점) 세부 평가항목은 등급 척도"
            "(매우우수/우수/보통/미흡)만 제시되어 있어 정성 판단 항목으로 "
            "정량 테이블에서 제외됨",
            "입찰공고문(제출기한 등 구체 일정) 원문은 본 첨부에 "
            "포함되어 있지 않음",
            "평가표 일부 행이 흐려 판독 불가",
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


def test_mutually_incomplete_empty_documents_cannot_close_by_type_alone(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="EMPTY-CROSS-REFERENCE-CYCLE",
        title="내용 없는 상호 참조 문서",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-EMPTY-NOTICE",
        digest_char="5",
        requirements=[
            _requirement(
                "REQ-EMPTY-CYCLE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-EMPTY-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[
            "입찰공고 본문에는 제안요청서의 세부 요구사항과 정량 평가표가 "
            "포함되어 있지 않음"
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-EMPTY-RFP",
        digest_char="6",
        requirements=[],
        document_type="RFP",
        source_label="제안요청서.hwp",
        quantitative_tables=[_quantitative_table("ATT-EMPTY-RFP")],
        include_quantitative_validation_record=False,
        missing=[
            "입찰공고문(제출기한 등 구체 일정) 원문은 본 첨부에 "
            "포함되어 있지 않음"
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


def test_review_quantitative_record_cannot_seed_cross_reference_closure(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="REVIEW-TABLE-CROSS-REFERENCE-CYCLE",
        title="검토 표가 있는 상호 참조 문서",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-REVIEW-NOTICE",
        digest_char="7",
        requirements=[
            _requirement(
                "REQ-REVIEW-CYCLE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-REVIEW-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 "
            "포함되어 있지 않음"
        ],
    )
    review_table = _quantitative_table("ATT-REVIEW-RFP")
    review_table["ambiguity_reason"] = "원문에서 적용 표를 확정할 수 없음"
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-REVIEW-RFP",
        digest_char="8",
        requirements=[],
        document_type="RFP",
        source_label="제안요청서.hwp",
        quantitative_tables=[review_table],
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


def test_available_table_inside_review_record_supplies_named_rfp_gap(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="MIXED-REVIEW-TABLE-CROSS-REFERENCE",
        title="확정 표와 검토 표가 함께 있는 교육 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-MIXED-NOTICE",
        digest_char="7",
        requirements=[
            _requirement(
                "REQ-MIXED-REVIEW",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-MIXED-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 "
            "포함되어 있지 않음"
        ],
    )
    available_table = _quantitative_table("ATT-MIXED-RFP")
    review_table = copy.deepcopy(available_table)
    review_table["table_id"] = "Q-PERFORMANCE-REVIEW"
    review_table["criteria"][0]["criterion_id"] = "Q-PERFORMANCE-COUNT-REVIEW"
    review_table["ambiguity_reason"] = "두 평가표 중 적용 대상을 원문에서 확정할 수 없음"
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-MIXED-RFP",
        digest_char="8",
        requirements=[],
        document_type="RFP",
        source_label="제안요청서.hwp",
        quantitative_tables=[available_table, review_table],
        missing=[
            "입찰공고문(제출기한 등 구체 일정) 원문은 본 첨부에 "
            "포함되어 있지 않음"
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "COMPLETED"
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" in result.warnings
    assert "AGGREGATE_GAPS_UNRESOLVED" not in result.warnings
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert run is not None
    quantitative = next(
        item for item in run.scores if item.score_key == "quantitative.total"
    )
    assert quantitative.status == "REVIEW"
    assert quantitative.basis_json["activation_status"] != "AUTO_ACTIVE"


def test_exact_rfp_label_can_supply_when_extracted_type_is_other(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="OTHER-TYPE-RFP-LABEL-CLOSURE",
        title="파일명으로 역할을 확정한 교육 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-OTHER-NOTICE",
        digest_char="5",
        requirements=[
            _requirement(
                "REQ-OTHER-RFP",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-OTHER-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=["별도 제안요청서의 평가배점표는 이 첨부에 포함되지 않음"],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-OTHER-RFP",
        digest_char="6",
        requirements=[],
        document_type="OTHER",
        source_label="제안요청서.hwp",
        quantitative_tables=[_quantitative_table("ATT-OTHER-RFP")],
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "COMPLETED"
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" in result.warnings
    assert "AGGREGATE_GAPS_UNRESOLVED" not in result.warnings


def test_rfp_table_gap_cannot_reverse_direction_to_a_notice_supplier(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="RFP-GAP-DIRECTION-NOTICE-SUPPLIER",
        title="결손 문서 방향을 보존하는 교육 용역",
    )
    notice.risk_dimensions = None
    gap = "공고문에는 제안요청서 본문이 제공되지 않아 평가배점표를 확인할 수 없음"
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-DIRECTION-RFP",
        digest_char="4",
        requirements=[
            _requirement(
                "REQ-DIRECTION-RFP",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-DIRECTION-RFP",
            )
        ],
        document_type="RFP",
        source_label="제안요청서.hwp",
        missing=[gap],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-DIRECTION-NOTICE",
        digest_char="5",
        requirements=[],
        document_type="NOTICE",
        source_label="공고문.pdf",
        quantitative_tables=[_quantitative_table("ATT-DIRECTION-NOTICE")],
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" not in result.warnings


def test_compound_non_quantitative_gap_cannot_close_by_notice_presence(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="COMPOUND-NON-QUANTITATIVE-GAP",
        title="다른 누락이 함께 남은 교육 용역",
    )
    notice.risk_dimensions = None
    gap = "공고문은 본 제안요청서에 포함되지 않음. 안전관리계획도 누락"
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-COMPOUND-RFP",
        digest_char="6",
        requirements=[
            _requirement(
                "REQ-COMPOUND-NON-QUANT",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-COMPOUND-RFP",
            )
        ],
        document_type="RFP",
        source_label="제안요청서.hwp",
        missing=[gap],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-COMPOUND-NOTICE",
        digest_char="7",
        requirements=[],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" not in result.warnings


@pytest.mark.parametrize(
    ("supplier_type", "supplier_label", "expected_status"),
    (
        ("OTHER", "제안요청\u200b서.hwp", "COMPLETED"),
        ("RFP", "제안요청서 작성양식.hwp", "PARTIAL"),
        ("RFP", "제안요청서(안).hwp", "PARTIAL"),
    ),
)
def test_supplier_label_format_controls_and_auxiliary_rfp_roles(
    db_session: Session,
    supplier_type: str,
    supplier_label: str,
    expected_status: str,
) -> None:
    notice = _notice(
        db_session,
        notice_key=f"RFP-LABEL-PROVENANCE-{expected_status}-{supplier_type}",
        title="문서명 출처 검증 교육 용역",
    )
    notice.risk_dimensions = None
    gap = "별도 제안요청서의 평가배점표는 이 첨부에 포함되지 않음"
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-LABEL-NOTICE",
        digest_char="8",
        requirements=[
            _requirement(
                "REQ-LABEL-PROVENANCE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-LABEL-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[gap],
    )
    supplier_id = "ATT-LABEL-SUPPLIER"
    _source_version(
        notice,
        version_no=2,
        attachment_id=supplier_id,
        digest_char="9",
        requirements=[],
        document_type=supplier_type,
        source_label=supplier_label,
        quantitative_tables=[_quantitative_table(supplier_id)],
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == expected_status
    if expected_status == "COMPLETED":
        assert "AGGREGATE_GAPS_UNRESOLVED" not in result.warnings
    else:
        assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


def test_raw_quantitative_table_cannot_seed_cross_reference_closure(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="RAW-TABLE-CROSS-REFERENCE",
        title="검증 레코드 없는 표가 포함된 교육 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-RAW-NOTICE",
        digest_char="9",
        requirements=[
            _requirement(
                "REQ-RAW-TABLE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-RAW-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 "
            "포함되어 있지 않음"
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-RAW-RFP",
        digest_char="a",
        requirements=[],
        document_type="RFP",
        source_label="제안요청서.hwp",
        quantitative_tables=[_quantitative_table("ATT-RAW-RFP")],
        include_quantitative_validation_record=False,
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" not in result.warnings


@pytest.mark.parametrize(
    ("supplier_type", "supplier_label", "gap"),
    (
        (
            "FORM",
            "인력 배치 평가표.hwp",
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 포함되어 있지 않음",
        ),
        (
            "FORM",
            "제안요청서 서식.hwp",
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 포함되어 있지 않음",
        ),
        (
            "RFP",
            "제안요청서 서식.hwp",
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 포함되어 있지 않음",
        ),
        (
            "RFP",
            "제안요청서 참고용.hwp",
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 포함되어 있지 않음",
        ),
        (
            "RFP",
            "부록 제안요청서.hwp",
            "별도 제안요청서의 평가배점표는 이 첨부에 포함되지 않음",
        ),
        (
            "RFP",
            "제안요청서 초안.hwp",
            "제안요청서(붙임) 본문이 제공되지 않아 세부 평가배점표"
            "(정량평가 기준)를 확인할 수 없음",
        ),
        (
            "SCOPE",
            "과업지시서.hwp",
            "제안 요청서의 정량 평가표는 본 공고문에 포함되어 있지 않음",
        ),
        (
            "RFP",
            "과업지시서.hwp",
            "기술평가 세부 배점표(제안요청서 내 배점기준)는 본 공고문에 포함되어 있지 않음",
        ),
    ),
)
def test_unrelated_validated_table_cannot_satisfy_named_rfp_gap(
    db_session: Session,
    supplier_type: str,
    supplier_label: str,
    gap: str,
) -> None:
    notice = _notice(
        db_session,
        notice_key=f"UNRELATED-TABLE-{supplier_type}",
        title="다른 문서 역할의 평가표가 포함된 교육 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id=f"ATT-UNRELATED-NOTICE-{supplier_type}",
        digest_char="d",
        requirements=[
            _requirement(
                f"REQ-UNRELATED-{supplier_type}",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id=f"ATT-UNRELATED-NOTICE-{supplier_type}",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[gap],
    )
    supplier_id = f"ATT-UNRELATED-{supplier_type}"
    _source_version(
        notice,
        version_no=2,
        attachment_id=supplier_id,
        digest_char="e",
        requirements=[],
        document_type=supplier_type,
        source_label=supplier_label,
        quantitative_tables=[_quantitative_table(supplier_id)],
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


@pytest.mark.parametrize(
    ("compound_gap", "expected_eligibility", "expected_reason"),
    (
        (
            "입찰참가자격 세부요건과 정량 평가표는 제안요청서에 있어 "
            "본 공고문에 포함되어 있지 않음",
            "REVIEW",
            "R07",
        ),
        (
            "제안요청서 본문이 제공되지 않아 평가배점표와 수행계획을 확인할 수 없음",
            "PASS",
            "PASS_MATCH",
        ),
        (
            "별도 제안요청서의 평가배점표와 사업일정은 이 첨부에 포함되지 않음",
            "PASS",
            "PASS_MATCH",
        ),
        (
            "제안요청서 본문이 제공되지 않아 평가배점표 및 안전관리계획을 확인할 수 없음",
            "PASS",
            "PASS_MATCH",
        ),
    ),
)
def test_compound_requirement_and_table_gap_cannot_close_by_table_capability(
    db_session: Session,
    compound_gap: str,
    expected_eligibility: str,
    expected_reason: str,
) -> None:
    notice = _notice(
        db_session,
        notice_key="COMPOUND-TABLE-CROSS-REFERENCE",
        title="참가자격과 표가 함께 누락된 교육 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-COMPOUND-NOTICE",
        digest_char="b",
        requirements=[
            _requirement(
                "REQ-COMPOUND",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-COMPOUND-NOTICE",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[compound_gap],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-COMPOUND-RFP",
        digest_char="c",
        requirements=[],
        document_type="RFP",
        source_label="제안요청서.hwp",
        quantitative_tables=[_quantitative_table("ATT-COMPOUND-RFP")],
        missing=[],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == expected_eligibility
    assert result.reason_code == expected_reason
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


@pytest.mark.parametrize(
    ("suffix", "notice_gap"),
    (
        (
            "COMPOUND",
            "입찰참가자격 세부요건과 정량 평가표는 제안요청서에 있어 "
            "본 공고문에 포함되어 있지 않음",
        ),
        (
            "QUALITATIVE",
            "제안요청서의 정성 평가표가 본 공고문에 포함되어 있지 않음",
        ),
    ),
)
def test_available_quantitative_record_covers_only_quantitative_table_gap(
    db_session: Session,
    suffix: str,
    notice_gap: str,
) -> None:
    notice = _notice(
        db_session,
        notice_key=f"NON-QUANTITATIVE-CROSS-REFERENCE-{suffix}",
        title="정량표로 대체할 수 없는 상호 참조 문서",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id=f"ATT-NON-QUANT-NOTICE-{suffix}",
        digest_char="9",
        requirements=[
            _requirement(
                f"REQ-NON-QUANT-{suffix}",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id=f"ATT-NON-QUANT-NOTICE-{suffix}",
            )
        ],
        document_type="NOTICE",
        source_label="공고문.pdf",
        missing=[notice_gap],
    )
    rfp_attachment_id = f"ATT-NON-QUANT-RFP-{suffix}"
    _source_version(
        notice,
        version_no=2,
        attachment_id=rfp_attachment_id,
        digest_char="a",
        requirements=[],
        document_type="RFP",
        source_label="제안요청서.hwp",
        quantitative_tables=[_quantitative_table(rfp_attachment_id)],
        missing=[
            "입찰공고문(제출기한 등 구체 일정) 원문은 본 첨부에 "
            "포함되어 있지 않음"
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


def test_attachment_does_not_satisfy_its_own_missing_document_gap(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="SELF-COVERAGE",
        title="제안요청서 단일 첨부 공급 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-ONLY-RFP",
        digest_char="c",
        requirements=[
            _requirement(
                "REQ-SELF",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-ONLY-RFP",
            )
        ],
        document_complete=False,
        document_type="RFP",
        missing=["제안요청서의 평가 세부항목 일부가 본문에 포함되지 않음."],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.reason_code == "R07"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


def test_incomplete_sibling_does_not_clear_an_attachment_local_gap(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="INCOMPLETE-SIBLING",
        title="불완전한 제안요청서가 있는 공급 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-NOTICE-INCOMPLETE",
        digest_char="e",
        requirements=[
            _requirement(
                "REQ-INCOMPLETE-SIBLING",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-NOTICE-INCOMPLETE",
            )
        ],
        document_complete=False,
        document_type="NOTICE",
        missing=["제안요청서의 세부 요구사항은 본문에 포함되지 않음."],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-RFP-INCOMPLETE",
        digest_char="f",
        requirements=[],
        document_complete=False,
        document_type="RFP",
        missing=["평가표 일부가 흐려 판독 불가"],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.reason_code == "R07"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings


def test_combined_scope_and_rfp_sibling_covers_each_named_missing_document(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="COMBINED-SIBLING-COVERAGE",
        title="과업지시서와 제안요청서가 결합된 공급 용역",
    )
    notice.deadline = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-COMBINED-NOTICE",
        digest_char="7",
        requirements=[
            _requirement(
                "REQ-COMBINED-SIBLING",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-COMBINED-NOTICE",
            )
        ],
        document_complete=False,
        document_type="NOTICE",
        source_label="입찰공고서.pdf",
        missing=[
            "과업내용서, 내역서 및 제안요청서의 세부 규격·평가항목은 "
            "입찰공고 본문에 포함되지 않아 확인할 수 없음"
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-COMBINED-RFP",
        digest_char="8",
        requirements=[],
        document_type="RFP",
        source_label="과업지시서 및 제안요청서.hwp",
    )
    _source_version(
        notice,
        version_no=3,
        attachment_id="ATT-COMBINED-SPEC",
        digest_char="6",
        requirements=[],
        document_complete=False,
        document_type="OTHER",
        source_label="세부사양서.hwp",
        missing=["입찰 제출서류, 계약기간, 납품기한 등 절차 정보가 없음."],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "COMPLETED"
    assert result.eligibility == "PASS"
    assert result.reason_code == "PASS_MATCH"
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" in result.warnings
    assert "AGGREGATE_GAPS_UNRESOLVED" not in result.warnings


def test_generic_rfp_sibling_does_not_claim_a_missing_scope_document(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="GENERIC-RFP-NO-SCOPE-COVERAGE",
        title="과업내용서가 별도로 누락된 공급 용역",
    )
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-NO-SCOPE-NOTICE",
        digest_char="9",
        requirements=[
            _requirement(
                "REQ-NO-SCOPE-SIBLING",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-NO-SCOPE-NOTICE",
            )
        ],
        document_complete=False,
        document_type="NOTICE",
        source_label="입찰공고서.pdf",
        missing=[
            "과업내용서 및 제안요청서의 세부 규격은 입찰공고 본문에 "
            "포함되지 않아 확인할 수 없음"
        ],
    )
    _source_version(
        notice,
        version_no=2,
        attachment_id="ATT-GENERIC-RFP",
        digest_char="0",
        requirements=[],
        document_type="RFP",
        source_label="제안요청서.hwp",
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert "AGGREGATE_GAPS_UNRESOLVED" in result.warnings
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" not in result.warnings


def test_requirement_anchor_confidence_is_not_lowered_by_document_average(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="ANCHOR-CONFIDENCE",
        title="요건 근거와 문서 평균을 분리하는 용역",
    )
    notice.deadline = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-CONFIDENCE",
        digest_char="d",
        requirements=[
            _requirement(
                "REQ-HIGH-ANCHOR",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-CONFIDENCE",
                confidence=0.98,
            )
        ],
        extraction_confidence=0.85,
        document_complete=False,
        missing=["가격산정 세부표 일부"],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == "PASS"
    assert result.reason_code == "PASS_MATCH"
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation is not None
    assert {item["reason_code"] for item in evaluation.atomic_results} == {"P-ENTITY"}


def test_known_non_eligibility_gap_keeps_medium_confidence_as_review_only(
    db_session: Session,
) -> None:
    notice = _notice(db_session, notice_key="PARTIAL-MEDIUM", title="배점표 일부 누락 교육 용역")
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-MEDIUM",
        digest_char="3",
        requirements=[
            _requirement(
                "REQ-MEDIUM",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-MEDIUM",
                confidence=0.85,
            )
        ],
        missing=["가격산정 세부표 일부"],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == "REVIEW"
    assert result.reason_code == "REVIEW_MATCH"
    assert "NON_ELIGIBILITY_PARTIAL_GATE_APPLIED" in result.warnings
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation is not None
    assert {item["reason_code"] for item in evaluation.atomic_results} == {
        "R07_LOW_CONFIDENCE"
    }
    assert evaluation.explanation["risk"]["status"] == "AVAILABLE"
    system_opinion = db_session.scalar(
        select(RecommendationSnapshot).where(
            RecommendationSnapshot.analysis_run_id == result.analysis_run_id,
            RecommendationSnapshot.recommendation_key == "bid:system",
        )
    )
    assert system_opinion is not None
    assert system_opinion.recommendation == "HOLD"


def test_partial_gate_stays_fail_closed_when_an_eligibility_anchor_is_unverified(
    db_session: Session,
) -> None:
    notice = _notice(db_session, notice_key="PARTIAL-ANCHOR", title="자격 근거 미검증 용역")
    notice.risk_dimensions = None
    unanchored = _requirement(
        "REQ-NO-ANCHOR",
        "경쟁입찰참가자격 등록을 완료한 업체여야 함",
        attachment_id="ATT-NO-ANCHOR",
    )
    unanchored["evidence"] = []
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-NO-ANCHOR",
        digest_char="8",
        requirements=[unanchored],
        missing=["가격산정 세부표 일부"],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == "REVIEW"
    assert result.reason_code == "R07"
    assert "NON_ELIGIBILITY_PARTIAL_GATE_APPLIED" not in result.warnings
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation is not None
    assert evaluation.risk_score is None
    assert evaluation.risk_band == "UNKNOWN"
    assert evaluation.explanation["risk"]["status"] == "WITHHELD_R07"


def test_partial_gate_stays_fail_closed_for_a_missing_blocking_action(
    db_session: Session,
) -> None:
    notice = _notice(db_session, notice_key="PARTIAL-ACTION-GAP", title="설명회 교육 용역")
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-ACTION-GAP",
        digest_char="4",
        requirements=[
            _requirement(
                "REQ-ACTION-GAP",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-ACTION-GAP",
            )
        ],
        missing=["제안설명회 일정 및 참여 안내 일부"],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == "REVIEW"
    assert result.reason_code == "R07"
    assert "NON_ELIGIBILITY_PARTIAL_GATE_APPLIED" not in result.warnings
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation is not None
    assert evaluation.risk_score is None
    assert evaluation.risk_band == "UNKNOWN"
    assert evaluation.explanation["risk"]["status"] == "WITHHELD_R07"
    assert "operation" not in evaluation.explanation["risk"]["axis_basis"]
    assert "operation" in evaluation.explanation["risk"]["missing_axes"]


def test_known_non_eligibility_gap_preserves_company_evidence_review(
    db_session: Session,
) -> None:
    notice = _notice(db_session, notice_key="PARTIAL-EVIDENCE", title="회사 증빙 미완료 용역")
    notice.risk_dimensions = None
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-EVIDENCE",
        digest_char="9",
        requirements=[
            _requirement(
                "REQ-EVIDENCE",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-EVIDENCE",
            )
        ],
        missing=["가격산정 세부표 일부"],
    )
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)

    assert result.status == "PARTIAL"
    assert result.eligibility == "REVIEW"
    assert result.reason_code == "REVIEW_MATCH"
    assert "NON_ELIGIBILITY_PARTIAL_GATE_APPLIED" in result.warnings
    evaluation = db_session.get(Evaluation, result.evaluation_id)
    assert evaluation is not None
    assert {item["reason_code"] for item in evaluation.atomic_results} == {"R04"}
    assert len(
        evaluation.explanation["document_gate"]["verified_requirement_keys_applied"]
    ) == 1
    assert evaluation.explanation["risk"]["status"] == "AVAILABLE"


def test_no_extraction_source_is_a_persisted_fail_closed_r07(db_session: Session) -> None:
    notice = _notice(db_session, notice_key="NO-SOURCE", title="추출 없는 교육 용역")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert result.status == "FAILED"
    assert result.eligibility == "REVIEW"
    assert result.reason_code == "R07"
    assert result.materialized_requirement_count == 0
    assert result.requirement_snapshot_count == 0
    assert "NO_EXTRACTION_SOURCES" in result.warnings
    assert "NO_ELIGIBILITY_OR_ACTION_REQUIREMENTS" in result.warnings


@pytest.mark.parametrize(
    "stage",
    ["after_materialization", "after_evaluation", "after_snapshots"],
)
def test_pipeline_rolls_back_every_derived_row_on_failure(
    db_session: Session,
    stage: str,
) -> None:
    notice = _notice(db_session, notice_key=f"ROLLBACK-{stage}", title="롤백 교육 용역")
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-ROLLBACK",
        digest_char="1",
        requirements=[
            _requirement(
                "REQ-ROLLBACK",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-ROLLBACK",
            )
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    def fail_at(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"synthetic failure at {stage}")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        run_analysis_pipeline(db_session, notice_id=notice.id, _stage_hook=fail_at)

    assert db_session.scalar(select(func.count(NoticeVersion.id))) == 1
    assert db_session.scalar(select(func.count(AtomicRequirement.id))) == 0
    assert db_session.scalar(select(func.count(Evaluation.id))) == 0
    assert db_session.scalar(select(func.count(AnalysisRun.id))) == 0
    assert db_session.scalar(select(func.count(RequirementResultSnapshot.id))) == 0
    assert db_session.scalar(select(func.count(ScoreSnapshot.id))) == 0
    assert db_session.scalar(select(func.count(RecommendationSnapshot.id))) == 0


def test_pipeline_rolls_back_when_public_criteria_snapshot_is_invalid(
    db_session: Session,
    monkeypatch,
) -> None:
    notice = _notice(
        db_session,
        notice_key="PUBLIC-CRITERIA-INVARIANT",
        title="공개 정량 스냅샷 불변식",
    )
    _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-PUBLIC-CRITERIA-INVARIANT",
        digest_char="9",
        requirements=[
            _requirement(
                "REQ-PUBLIC-CRITERIA-INVARIANT",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id="ATT-PUBLIC-CRITERIA-INVARIANT",
            )
        ],
    )
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()
    monkeypatch.setattr(
        "pai_loop.analysis_pipeline.build_public_quantitative_criteria_snapshot",
        lambda _result: None,
    )

    with pytest.raises(
        AnalysisPipelineError,
        match="public criteria snapshot invariant failed",
    ):
        run_analysis_pipeline(db_session, notice_id=notice.id)

    assert db_session.scalar(select(func.count(NoticeVersion.id))) == 1
    assert db_session.scalar(select(func.count(AtomicRequirement.id))) == 0
    assert db_session.scalar(select(func.count(Evaluation.id))) == 0
    assert db_session.scalar(select(func.count(AnalysisRun.id))) == 0
    assert db_session.scalar(select(func.count(RequirementResultSnapshot.id))) == 0
    assert db_session.scalar(select(func.count(ScoreSnapshot.id))) == 0
    assert db_session.scalar(select(func.count(RecommendationSnapshot.id))) == 0


def test_source_selection_and_transaction_contract_fail_before_writes(db_session: Session) -> None:
    notice = _notice(db_session, notice_key="SOURCE-CONTRACT")
    source = _source_version(
        notice,
        version_no=1,
        attachment_id="ATT-PROMPT",
        digest_char="2",
        requirements=[],
        prompt_version="old-prompt",
    )
    db_session.commit()

    with pytest.raises(AnalysisPipelineSourceError, match="different prompt version"):
        run_analysis_pipeline(
            db_session,
            notice_id=notice.id,
            source_version_ids=[source.id],
        )
    assert db_session.scalar(select(func.count(AnalysisRun.id))) == 0
    db_session.rollback()


def test_latest_pps_manifest_excludes_superseded_attachment_from_pipeline(
    db_session: Session,
) -> None:
    notice = _notice(db_session, notice_key="PPS-MANIFEST-SOURCE")
    attachment_a = {
        "attachment_id": "PPS-ATT-aaaaaaaaaaaaaaaaaaaaaaaa",
        "file_name": "구공고.pdf",
        "media_type": "application/pdf",
        "url": "https://www.g2b.go.kr/old",
        "slot": 1,
    }
    attachment_b = {
        "attachment_id": "PPS-ATT-bbbbbbbbbbbbbbbbbbbbbbbb",
        "file_name": "정정공고.pdf",
        "media_type": "application/pdf",
        "url": "https://www.g2b.go.kr/current",
        "slot": 1,
    }
    notice.versions.append(
        NoticeVersion(
            version_no=1,
            file_sha256="1" * 64,
            extraction_status="METADATA",
            source_payload={
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": [attachment_a],
            },
        )
    )
    source_a = _source_version(
        notice,
        version_no=2,
        attachment_id=attachment_a["attachment_id"],
        digest_char="a",
        requirements=[
            _requirement(
                "REQ-OLD",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함",
                attachment_id=attachment_a["attachment_id"],
            )
        ],
    )
    source_a.source_payload = {
        **source_a.source_payload,
            "source_kind": "PPS_PUBLIC_ATTACHMENT",
            "manifest_sha256": _digest(attachment_a),
            "current_manifest_sha256": _digest([attachment_a]),
            "processing_version": PPS_PROCESSING_VERSION,
    }
    notice.versions.append(
        NoticeVersion(
            version_no=3,
            file_sha256="3" * 64,
            extraction_status="METADATA",
            source_payload={
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": [attachment_b],
            },
        )
    )
    source_b = _source_version(
        notice,
        version_no=4,
        attachment_id=attachment_b["attachment_id"],
        digest_char="b",
        requirements=[
            _requirement(
                "REQ-CURRENT",
                "입찰참가자격 등록을 완료한 업체만 참여 가능",
                attachment_id=attachment_b["attachment_id"],
            )
        ],
    )
    source_b.source_payload = {
        **source_b.source_payload,
            "source_kind": "PPS_PUBLIC_ATTACHMENT",
            "manifest_sha256": _digest(attachment_b),
            "current_manifest_sha256": _digest([attachment_b]),
            "processing_version": PPS_PROCESSING_VERSION,
    }
    _verified_boolean_fact(db_session, "bidder_registration")
    db_session.commit()

    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert result.materialized_requirement_count == 1
    with db_session.begin():
        labels = list(
            db_session.scalars(
                select(AtomicRequirement.label).where(
                    AtomicRequirement.notice_version_id == result.notice_version_id
                )
            ).all()
        )
    assert labels == ["입찰참가자격 등록을 완료한 업체만 참여 가능"]

    db_session.execute(select(Notice.id))
    with pytest.raises(AnalysisPipelineTransactionError, match="active transaction"):
        run_analysis_pipeline(db_session, notice_id=notice.id)
    db_session.rollback()


def test_accepted_extraction_status_is_valid_evaluator_input() -> None:
    notice = Notice(
        notice_key="ACCEPTED-QUALITY",
        bid_notice_no="ACCEPTED-QUALITY",
        revision_no="000",
        title="accepted quality",
        agency="public test",
        deadline=DEADLINE,
        status="OPEN",
    )
    version = NoticeVersion(
        version_no=1,
        file_sha256="3" * 64,
        document_complete=True,
        extraction_status="ACCEPTED",
        extraction_confidence=0.98,
    )
    requirement = AtomicRequirement(
        requirement_key="Q-ACCEPTED",
        group_key="G-ACCEPTED",
        path_key="PATH-PRIMARY",
        sequence=1,
        label="accepted",
        fact_key="accepted_fact",
        operator="eq",
        required_value=True,
        evidence_required=False,
        mandatory=True,
        pass_rule_id="P-ENTITY",
        parse_confidence=0.98,
        active=True,
    )
    fact = CompanyFact(
        fact_key="accepted_fact",
        value=True,
        effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
        verified=True,
    )

    evaluated = evaluate_notice(notice, version, [requirement], [fact])
    assert evaluated.eligibility.value == "PASS"
    assert evaluated.explanation["document_quality_ok"] is True


@pytest.mark.parametrize("supplier", ("NOTICE", "RFP", "FORM", None))
def test_exact_notice_date_reference_requires_effective_notice_sibling(supplier: str | None) -> None:
    from pai_loop.analysis_pipeline import _gap_is_covered_by_aggregate_sources
    gap = (
        "제안서 제출기한의 구체적 날짜는 본문에 명시되지 않고 '공고문 명시'로만 "
        "표기되어 있어 실제 마감일자는 확인 불가"
    )
    def covered(value: str) -> bool:
        return _gap_is_covered_by_aggregate_sources(
            value, current_document_type="RFP", available_types={supplier} if supplier else set(),
            sibling_document_labels=set(), validated_quantitative_supplier_roles=set(),
        )
    assert covered(gap) is (supplier == "NOTICE")
    assert covered(gap + ". 참가자격도 확인할 수 없음") is False
    assert covered("직접생산 자격이 명시되지 않아 공고문 확인 필요") is False



def test_statutory_compound_materializes_two_anchored_and_gates() -> None:
    from pai_loop.analysis_pipeline import _MergedRequirement, _policy_items, _atomic_requirement
    from pai_loop.eligibility_policy import load_public_company_profile
    from pai_loop.integrations.openai_extraction import ExtractedRequirement
    clause = ("지방계약법 시행령 제13조·시행규칙 제14조의 자격요건을 구비하고 "
              "시행령 제92조(부정당업자 제재) 해당사항이 없는 업체여야 함")
    extracted = ExtractedRequirement.model_validate(_requirement("SYN-STATUTORY", clause, attachment_id="SYN-ATTACHMENT"))
    extracted.evidence[0].quote = clause
    merged = _MergedRequirement(requirement_key="ai-synthetic-statutory", requirement=extracted,
                                attachment_ids={"SYN-ATTACHMENT"}, source_document_sha256s={"a" * 64},
                                anchors=list(extracted.evidence), source_confidences=[0.98])
    notice = Notice(notice_key="SYN-STATUTORY-NOTICE", bid_notice_no="SYN-STATUTORY-NOTICE",
                    title="합성 자격 조건", agency="합성 기관", deadline=datetime(2026, 9, 6, tzinfo=timezone.utc))
    pairs = _policy_items([merged], notice=notice, profile=load_public_company_profile())
    atomics = [_atomic_requirement(item, policy, sequence=i) for i, (item, policy) in enumerate(pairs, 1)]
    assert len(atomics) == 2
    assert {item.fact_key for item in atomics} == {"bidder_registration", "sanction_clear"}
    assert all(item.mandatory and item.path_key == "PATH-PRIMARY" for item in atomics)
    assert len({item.requirement_key for item in atomics}) == 2
    assert len({item.group_key for item in atomics}) == 2
    assert all(item.source_excerpt == clause for item in atomics)
    assert all(item.source_location == "SYN-ATTACHMENT#page=2:입찰참가자격" for item in atomics)
    assert all(item.anchors == merged.anchors for item, _policy in pairs)



@pytest.mark.parametrize("sanction_present", (True, False))
def test_statutory_compound_pipeline_cannot_pass_without_both_facts(db_session: Session, sanction_present: bool) -> None:
    notice = _notice(db_session, notice_key="SYN-STATUTORY-PIPELINE", title="합성 복합 자격 검증")
    notice.deadline = datetime(2026, 9, 6, tzinfo=timezone.utc)
    clause = ("지방계약법 시행령 제13조·시행규칙 제14조의 자격요건을 구비하고 "
              "시행령 제92조(부정당업자 제재) 해당사항이 없는 업체여야 함")
    extracted = _requirement("SYN-STATUTORY-PIPELINE-REQ", clause, attachment_id="SYN-STATUTORY-ATT")
    extracted["evidence"][0]["quote"] = clause
    _source_version(notice, version_no=1, attachment_id="SYN-STATUTORY-ATT", digest_char="a", requirements=[extracted])
    _verified_boolean_fact(db_session, "bidder_registration")
    if sanction_present:
        _verified_boolean_fact(db_session, "sanction_clear")
    db_session.commit()
    result = run_analysis_pipeline(db_session, notice_id=notice.id)
    assert result.materialized_requirement_count == 2
    assert (result.eligibility == "PASS") is sanction_present


@contextmanager
def _current_pps_confidence_source(*, eligibility_confidence=0.98, other_confidence=0.72,
                                   missing=None, company_fact=True, other_eligibility=False):
    """Persist a real current-contract extraction using only synthetic local transports."""
    import httpx
    import json
    from types import SimpleNamespace
    from test_pps_enrichment import _single_hwpx_reuse_case
    from pai_loop.integrations.openai_extraction import OpenAIExtractionClient, OpenAITelemetry
    from pai_loop.pps_enrichment import enrich_notice_from_pps

    lines = ["경쟁입찰참가자격 등록을 완료한 업체여야 함",
             "경쟁입찰참가자격을 등록한 업체여야 함" if other_eligibility
             else "제안서 분량은 20페이지 내외를 권장한다"]
    class SyntheticClient:
        calls = 0
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract(self, *, document_text, allowed_attachment_ids):
            type(self).calls += 1
            attachment_id = next(iter(allowed_attachment_ids))
            requirements = []
            for index, (condition, category, mandatory, confidence) in enumerate([
                (lines[0], "ENTITY", True, eligibility_confidence),
                (lines[1], "ENTITY" if other_eligibility else "OTHER", other_eligibility, other_confidence),
            ]):
                requirements.append({
                    "requirement_id": f"SYN-CONFIDENCE-{index}", "category": category,
                    "logic": "SINGLE", "normalized_condition": condition,
                    "mandatory": mandatory, "deadline_basis": "입찰 마감일",
                    "evidence": [{"attachment_id": attachment_id, "page": 1,
                                  "section": "합성 공고", "quote": condition,
                                  "confidence": confidence}], "ambiguity_reason": None,
                })
            data = {"document_type": "NOTICE", "requirements": requirements,
                    "quantitative_tables": [], "quantitative_table_not_applicable": None,
                    "missing_or_unreadable": missing or [], "summary": "합성 추출 검증"}
            def forbidden_network(request):
                raise AssertionError("Provider transport must never execute")
            with OpenAIExtractionClient(
                api_key="synthetic-only", provider="openai",
                transport=httpx.MockTransport(forbidden_network),
            ) as boundary:
                return boundary._validate_response(
                    {"status": "completed", "output_text": json.dumps(data, ensure_ascii=False)},
                    document_text=document_text, allowed_attachment_ids=allowed_attachment_ids,
                    api_calls=1, openai_telemetry=OpenAITelemetry(),
                )

    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-SYN-CONFIDENCE-GATE", source_text="\n".join(lines),
    )
    try:
        with factory() as session:
            if company_fact is not None:
                _verified_boolean_fact(session, "bidder_registration")
                session.flush()
                fact = session.scalar(select(CompanyFact).where(CompanyFact.fact_key == "bidder_registration"))
                fact.value = company_fact
                session.commit()
        with factory() as session:
            extracted = enrich_notice_from_pps(
                session, notice_id=notice_id, openai_api_key="synthetic-only",
                openai_model="synthetic", transport=transport,
                openai_client_factory=SyntheticClient,
            )
        with factory() as session:
            yield SimpleNamespace(session=session, notice_id=notice_id,
                                  source_id=extracted.version_id, client=SyntheticClient)
    finally:
        engine.dispose()


@pytest.mark.parametrize("other_confidence", [0.1, 0.72, 0.89])
def test_current_pps_eligibility_uses_own_confidence_without_paid_redo(other_confidence):
    with _current_pps_confidence_source(other_confidence=other_confidence) as case:
        source = case.session.get(NoticeVersion, case.source_id)
        stored_payload = copy.deepcopy(source.source_payload)
        source_confidence = source.extraction_confidence
        case.session.commit()
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        evaluation = case.session.get(Evaluation, result.evaluation_id)
        assert result.eligibility == "PASS"
        assert {item["reason_code"] for item in evaluation.atomic_results} == {"P-ENTITY"}
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" in result.warnings
        assert source.source_payload == stored_payload
        assert source.extraction_confidence == source_confidence
        assert source.document_complete is True
        assert case.client.calls == 1
        case.session.commit()
        assert run_analysis_pipeline(case.session, notice_id=case.notice_id).reused is True
        assert case.client.calls == 1


def test_normal_current_pps_success_does_not_gain_confidence_gate_warning():
    with _current_pps_confidence_source(other_confidence=0.98) as case:
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.status == "COMPLETED"
        assert result.eligibility == "PASS"
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" not in result.warnings
        assert "NON_ELIGIBILITY_PARTIAL_GATE_APPLIED" not in result.warnings


@pytest.mark.parametrize("confidence", [0, 0.79, 0.85, 0.899])
def test_current_pps_weak_mandatory_eligibility_remains_r07(confidence):
    with _current_pps_confidence_source(eligibility_confidence=confidence) as case:
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "REVIEW"
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" not in result.warnings
        evaluation = case.session.get(Evaluation, result.evaluation_id)
        assert {item["reason_code"] for item in evaluation.atomic_results} == {"R07"}


@pytest.mark.parametrize("fact,expected", [(False, "FAIL"), (None, "REVIEW")])
def test_confidence_gate_never_invents_company_qualification(fact, expected):
    with _current_pps_confidence_source(company_fact=fact) as case:
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == expected
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" in result.warnings


@pytest.mark.parametrize("mutation", ["incomplete", "bad_fingerprint", "bad_document_digest", "foreign_anchor", "empty_anchor"])
def test_confidence_gate_rejects_unproven_or_incomplete_current_source(mutation):
    with _current_pps_confidence_source() as case:
        version = case.session.get(NoticeVersion, case.source_id)
        payload = copy.deepcopy(version.source_payload)
        if mutation == "incomplete":
            version.document_complete = False
        elif mutation == "bad_fingerprint":
            payload["quantitative_validation_record"]["validation_fingerprint_sha256"] = "f" * 64
        elif mutation == "bad_document_digest":
            payload["document_sha256"] = "f" * 64
        elif mutation == "foreign_anchor":
            payload["result"]["requirements"][0]["evidence"][0]["attachment_id"] = "SYN-FOREIGN-ATTACHMENT"
        else:
            payload["result"]["requirements"][0]["evidence"] = []
        version.source_payload = payload
        case.session.commit()
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "REVIEW"
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" not in result.warnings


@pytest.mark.parametrize("missing", [["입찰참가자격 원문 일부 누락"], ["일부 내용을 읽을 수 없음"]])
def test_confidence_gate_cannot_close_actual_or_unknown_source_gap(missing):
    with _current_pps_confidence_source(missing=missing) as case:
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "REVIEW"
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" not in result.warnings


def test_confidence_fix_recalculates_old_pipeline_run_once_without_extraction(monkeypatch):
    import pai_loop.analysis_pipeline as pipeline_module
    with _current_pps_confidence_source() as case:
        with monkeypatch.context() as old:
            old.setattr(pipeline_module, "PIPELINE_VERSION", "analysis-pipeline-0.6.4")
            old.setattr(pipeline_module, "_current_complete_pps_evidence", lambda *args: False)
            previous = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert previous.eligibility == "REVIEW"
        current = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert current.eligibility == "PASS"
        assert current.analysis_run_id != previous.analysis_run_id
        assert not current.reused
        assert run_analysis_pipeline(case.session, notice_id=case.notice_id).reused
        assert case.client.calls == 1
        assert case.session.get(AnalysisRun, current.analysis_run_id).basis_versions["pipeline"] == "analysis-pipeline-0.6.6"


@pytest.mark.parametrize("malformed", [None, {}, "invalid", "__MISSING__"])
def test_latest_malformed_pps_manifest_never_restores_older_source(malformed):
    from pai_loop.analysis_pipeline import _current_pps_manifest_basis, _select_source_versions
    with _current_pps_confidence_source() as case:
        notice = case.session.get(Notice, case.notice_id)
        metadata = next(v for v in notice.versions if v.source_payload.get("kind") == "PPS_NOTICE_METADATA")
        payload = copy.deepcopy(metadata.source_payload)
        if malformed == "__MISSING__":
            payload.pop("attachment_manifest")
        else:
            payload["attachment_manifest"] = malformed
        newest = NoticeVersion(notice_id=notice.id, version_no=max(v.version_no for v in notice.versions)+1,
                               file_sha256=_digest(payload), document_complete=False,
                               extraction_status="COMPLETE", extraction_confidence=1, source_payload=payload)
        case.session.add(newest); case.session.commit()
        versions = list(case.session.scalars(select(NoticeVersion).where(NoticeVersion.notice_id == notice.id)).all())
        basis = _current_pps_manifest_basis(versions, prompt_version=PROMPT_VERSION)
        assert basis["metadata_version_id"] == newest.id
        assert basis["coverage_complete"] is False
        assert basis["accepted_attachment_ids"] == basis["selected_attempt_ids"] == []
        assert _select_source_versions(case.session, notice_id=case.notice_id,
                                       prompt_version=PROMPT_VERSION, source_version_ids=None) == []
        explicit = _select_source_versions(case.session, notice_id=case.notice_id,
                                           prompt_version=PROMPT_VERSION,
                                           source_version_ids=[case.source_id])
        assert [row.id for row in explicit] == [case.source_id]
        case.session.commit()
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "REVIEW"
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" not in result.warnings


def test_confidence_gate_keeps_exact_existing_threshold():
    with _current_pps_confidence_source(eligibility_confidence=0.90) as case:
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "PASS"
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" in result.warnings


def test_another_weak_mandatory_eligibility_clause_cannot_hide_behind_strong_one():
    with _current_pps_confidence_source(other_eligibility=True) as case:
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "REVIEW"
        assert "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED" not in result.warnings


@pytest.mark.parametrize("gaps", [[], ["가격산정 세부표 일부"]])
def test_irrelevant_gap_no_longer_determines_strong_eligibility_outcome(gaps):
    with _current_pps_confidence_source(missing=gaps) as case:
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "PASS"
        assert case.client.calls == 1
