from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pai_loop.analysis_pipeline import (
    AnalysisPipelineSourceError,
    AnalysisPipelineTransactionError,
    _digest,
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
    title: str = "2025 AI 활용 역량 강화 교육 운영 용역",
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
) -> NoticeVersion:
    digest = digest_char * 64
    result = None
    if requirements is not None:
        result = {
            "document_type": "NOTICE",
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
        extraction_confidence=0.98 if status == "ACCEPTED" else 0.0,
        source_payload={
            "kind": "OPENAI_REQUIREMENT_EXTRACTION",
            "attachment_id": attachment_id,
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
    assert scores["quantitative.total"].lower_value is not None
    assert scores["competition.risk"].status == "MODEL_ESTIMATE"
    assert scores["pricing.award_rate_prediction"].status == "MODEL_ESTIMATE"
    assert scores["pricing.submitted_bid_rate_prediction"].status == "INSUFFICIENT_DATA"
    assert scores["pricing.method"].status == "AVAILABLE"
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
    assert {item["reason_code"] for item in evaluation.atomic_results} == {"R07"}


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
    }
    notice.versions.append(
        NoticeVersion(
            version_no=3,
            file_sha256="3" * 64,
            extraction_status="METADATA",
            source_payload={
                "kind": "PPS_NOTICE_METADATA",
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
