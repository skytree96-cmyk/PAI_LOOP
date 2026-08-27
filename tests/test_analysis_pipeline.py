from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pai_loop.analysis_pipeline import (
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
from pai_loop.integrations.openai_extraction import PROMPT_VERSION, SCHEMA_VERSION
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
        source_payload={
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
        },
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
    assert scores["competition.risk"].status == "MODEL_ESTIMATE"
    assert scores["pricing.award_rate_prediction"].status == "MODEL_ESTIMATE"
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


def test_pipeline_derives_competition_and_profitability_only_from_stored_award_basis(
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
    assert business_risk.basis_json["evidenced_axis_count"] == 6
    assert business_risk.basis_json["axis_basis"]["competition"]["source"] == (
        "STORED_3Y_AWARD_HISTORY"
    )
    assert business_risk.basis_json["axis_basis"]["profitability"]["source"] == (
        "STORED_3Y_AWARD_RATE_PREDICTION"
    )


def test_new_risk_semantics_have_versioned_non_reusable_idempotency(
    db_session: Session,
) -> None:
    assert PIPELINE_VERSION == "analysis-pipeline-0.6.2"
    assert MATERIALIZATION_VERSION == "atomic-materializer-0.3.0"
    assert SNAPSHOT_VERSION == "analysis-snapshot-0.2.0"
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


def test_attachment_local_absence_is_resolved_only_by_an_accepted_sibling(
    db_session: Session,
) -> None:
    notice = _notice(
        db_session,
        notice_key="SIBLING-COVERAGE",
        title="공고문과 제안요청서가 분리된 공급 용역",
    )
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

    assert result.status == "COMPLETED"
    assert result.eligibility == "PASS"
    assert result.reason_code == "PASS_MATCH"
    assert "SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING" in result.warnings
    assert "AGGREGATE_GAPS_UNRESOLVED" not in result.warnings


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
