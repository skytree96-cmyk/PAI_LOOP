from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Notice(Base, TimestampMixin):
    __tablename__ = "notices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    bid_notice_no: Mapped[str] = mapped_column(String(80), index=True)
    revision_no: Mapped[str] = mapped_column(String(20), default="00")
    title: Mapped[str] = mapped_column(String(500), index=True)
    agency: Mapped[str] = mapped_column(String(255), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="OPEN", index=True)
    category: Mapped[str | None] = mapped_column(String(120))
    estimated_amount: Mapped[float | None] = mapped_column(Float)
    source_url: Mapped[str | None] = mapped_column(Text)
    risk_dimensions: Mapped[dict[str, float] | None] = mapped_column(JSON)

    versions: Mapped[list["NoticeVersion"]] = relationship(
        back_populates="notice", cascade="all, delete-orphan", order_by="NoticeVersion.version_no"
    )
    evaluations: Mapped[list["Evaluation"]] = relationship(
        back_populates="notice", cascade="all, delete-orphan", order_by="Evaluation.evaluated_at"
    )
    decisions: Mapped[list["UserDecision"]] = relationship(
        back_populates="notice", cascade="all, delete-orphan", order_by="UserDecision.created_at"
    )
    mock_notifications: Mapped[list["MockNotification"]] = relationship(
        back_populates="notice",
        cascade="all, delete-orphan",
        order_by="MockNotification.created_at",
    )
    award_history: Mapped[list["AwardHistoryItem"]] = relationship(
        back_populates="target_notice",
        cascade="all, delete-orphan",
        order_by="AwardHistoryItem.awarded_at",
    )
    analysis_runs: Mapped[list["AnalysisRun"]] = relationship(
        back_populates="notice",
        cascade="all, delete-orphan",
        order_by="AnalysisRun.generated_at",
    )
    bid_outcomes: Mapped[list["BidOutcome"]] = relationship(
        back_populates="notice",
        cascade="all, delete-orphan",
        order_by="BidOutcome.observed_at",
    )


class NoticeVersion(Base):
    __tablename__ = "notice_versions"
    __table_args__ = (
        UniqueConstraint("notice_id", "version_no", name="uq_notice_version"),
        Index("ix_notice_versions_sha", "file_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64))
    document_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    extraction_status: Mapped[str] = mapped_column(String(32), default="COMPLETE")
    extraction_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    notice: Mapped[Notice] = relationship(back_populates="versions")
    requirements: Mapped[list["AtomicRequirement"]] = relationship(
        back_populates="notice_version", cascade="all, delete-orphan", order_by="AtomicRequirement.sequence"
    )
    analysis_runs: Mapped[list["AnalysisRun"]] = relationship(back_populates="notice_version")


class Evidence(Base, TimestampMixin):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evidence_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    evidence_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="VERIFIED", index=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_location: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    facts: Mapped[list["CompanyFact"]] = relationship(back_populates="evidence")


class CompanyFact(Base, TimestampMixin):
    __tablename__ = "company_facts"
    __table_args__ = (Index("ix_company_fact_asof", "fact_key", "effective_from", "effective_to"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    fact_key: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[Any] = mapped_column(JSON)
    value_label: Mapped[str | None] = mapped_column(String(500))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("evidence.id", ondelete="SET NULL"), index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(80), default="MANUAL")

    evidence: Mapped[Evidence | None] = relationship(back_populates="facts")


class AtomicRequirement(Base):
    __tablename__ = "atomic_requirements"
    __table_args__ = (
        UniqueConstraint("notice_version_id", "requirement_key", name="uq_version_requirement"),
        Index("ix_requirement_group_path", "notice_version_id", "group_key", "path_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_version_id: Mapped[str] = mapped_column(
        ForeignKey("notice_versions.id", ondelete="CASCADE"), index=True
    )
    requirement_key: Mapped[str] = mapped_column(String(120))
    group_key: Mapped[str] = mapped_column(String(120))
    path_key: Mapped[str] = mapped_column(String(120), default="default")
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(500))
    fact_key: Mapped[str] = mapped_column(String(120), index=True)
    operator: Mapped[str] = mapped_column(String(24), default="eq")
    required_value: Mapped[Any] = mapped_column(JSON)
    evidence_required: Mapped[bool] = mapped_column(Boolean, default=True)
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True)
    pass_rule_id: Mapped[str] = mapped_column(String(80))
    linked_review_code: Mapped[str | None] = mapped_column(String(16))
    review_trigger_value: Mapped[Any | None] = mapped_column(JSON)
    parse_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source_excerpt: Mapped[str | None] = mapped_column(Text)
    source_location: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    notice_version: Mapped[NoticeVersion] = relationship(back_populates="requirements")


class Evaluation(Base):
    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    notice_version_id: Mapped[str] = mapped_column(
        ForeignKey("notice_versions.id", ondelete="CASCADE"), index=True
    )
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    deadline_snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    eligibility: Mapped[str] = mapped_column(String(16), index=True)
    reason_code: Mapped[str] = mapped_column(String(32))
    readiness_score: Mapped[float] = mapped_column(Float)
    readiness_status: Mapped[str] = mapped_column(String(16), index=True)
    evidence_coverage: Mapped[float] = mapped_column(Float)
    risk_score: Mapped[float | None] = mapped_column(Float)
    risk_band: Mapped[str] = mapped_column(String(32))
    ruleset_version: Mapped[str] = mapped_column(String(32), default="2026.08-v2")
    atomic_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON)

    notice: Mapped[Notice] = relationship(back_populates="evaluations")
    decisions: Mapped[list["UserDecision"]] = relationship(back_populates="evaluation")
    analysis_runs: Mapped[list["AnalysisRun"]] = relationship(back_populates="evaluation")
    bid_outcomes: Mapped[list["BidOutcome"]] = relationship(back_populates="evaluation")


class UserDecision(Base):
    __tablename__ = "user_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluations.id", ondelete="CASCADE"), index=True)
    choice: Mapped[str] = mapped_column(String(32), index=True)
    actor_label: Mapped[str] = mapped_column(String(120), default="담당자")
    rationale: Mapped[str] = mapped_column(Text)
    conditions: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    notice: Mapped[Notice] = relationship(back_populates="decisions")
    evaluation: Mapped[Evaluation] = relationship(back_populates="decisions")
    bid_outcomes: Mapped[list["BidOutcome"]] = relationship(back_populates="decision")


class IngestionJob(Base):
    """Sanitised operational audit for a bounded source ingestion run.

    Raw provider payloads and credentials are deliberately not persisted here.
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), default="PPS", index=True)
    mode: Mapped[str] = mapped_column(String(24), default="LIVE", index=True)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    window_json: Mapped[dict[str, str]] = mapped_column(JSON)
    keyword: Mapped[str | None] = mapped_column(String(100))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    quarantined_count: Mapped[int] = mapped_column(Integer, default=0)
    notice_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MockNotification(Base):
    """Local Teams-shaped delivery record used until tenant approval exists."""

    __tablename__ = "mock_notifications"
    __table_args__ = (UniqueConstraint("correlation_id", name="uq_mock_notification_correlation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="teams")
    delivery_mode: Mapped[str] = mapped_column(String(16), default="mock")
    status: Mapped[str] = mapped_column(String(24), default="RECORDED", index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(120), index=True)
    card: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    notice: Mapped[Notice] = relationship(back_populates="mock_notifications")


class AwardHistoryItem(Base):
    """Public winning-bid fact linked as a similarity candidate to one notice."""

    __tablename__ = "award_history_items"
    __table_args__ = (
        UniqueConstraint("target_notice_id", "external_identity", name="uq_notice_award_identity"),
        Index("ix_award_history_target_date", "target_notice_id", "awarded_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_notice_id: Mapped[str] = mapped_column(
        ForeignKey("notices.id", ondelete="CASCADE"), index=True
    )
    external_identity: Mapped[str] = mapped_column(String(220))
    bid_notice_no: Mapped[str] = mapped_column(String(80), index=True)
    revision_no: Mapped[str] = mapped_column(String(20), default="000")
    title: Mapped[str] = mapped_column(String(500))
    agency: Mapped[str] = mapped_column(String(255), default="")
    winner_name: Mapped[str] = mapped_column(String(255))
    participant_count: Mapped[int | None] = mapped_column(Integer)
    award_amount: Mapped[float | None] = mapped_column(Float)
    award_rate: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    similarity_score: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="PPS")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    target_notice: Mapped[Notice] = relationship(back_populates="award_history")


class AnalysisRun(Base):
    """Immutable audit root for one materialised decision-support calculation.

    The manifest stores only hashes, identifiers, and version labels. Source
    documents, credentials, and provider payloads do not belong in this table.
    Child rows preserve the requirement, score, and recommendation outputs that
    were shown to a reviewer at ``generated_at``.
    """

    __tablename__ = "analysis_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_analysis_run_idempotency"),
        Index("ix_analysis_run_notice_generated", "notice_id", "generated_at"),
        Index("ix_analysis_run_input", "notice_id", "input_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    notice_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("notice_versions.id", ondelete="SET NULL"), index=True
    )
    evaluation_id: Mapped[str | None] = mapped_column(
        ForeignKey("evaluations.id", ondelete="SET NULL"), index=True
    )
    run_kind: Mapped[str] = mapped_column(String(48), default="FULL_REVIEW", index=True)
    status: Mapped[str] = mapped_column(String(24), default="COMPLETED", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    input_sha256: Mapped[str] = mapped_column(String(64))
    ruleset_version: Mapped[str | None] = mapped_column(String(160))
    company_profile_version: Mapped[str | None] = mapped_column(String(160))
    department_profile_version: Mapped[str | None] = mapped_column(String(160))
    quantitative_profile_version: Mapped[str | None] = mapped_column(String(160))
    pricing_profile_version: Mapped[str | None] = mapped_column(String(160))
    analytics_version: Mapped[str | None] = mapped_column(String(160))
    extraction_prompt_version: Mapped[str | None] = mapped_column(String(160))
    model_name: Mapped[str | None] = mapped_column(String(160))
    basis_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    notice: Mapped[Notice] = relationship(back_populates="analysis_runs")
    notice_version: Mapped[NoticeVersion | None] = relationship(back_populates="analysis_runs")
    evaluation: Mapped[Evaluation | None] = relationship(back_populates="analysis_runs")
    requirement_results: Mapped[list["RequirementResultSnapshot"]] = relationship(
        back_populates="analysis_run",
        cascade="all, delete-orphan",
        order_by="RequirementResultSnapshot.sequence",
    )
    scores: Mapped[list["ScoreSnapshot"]] = relationship(
        back_populates="analysis_run", cascade="all, delete-orphan", order_by="ScoreSnapshot.score_key"
    )
    recommendations: Mapped[list["RecommendationSnapshot"]] = relationship(
        back_populates="analysis_run",
        cascade="all, delete-orphan",
        order_by="RecommendationSnapshot.rank",
    )


class RequirementResultSnapshot(Base):
    """One immutable requirement/policy result emitted by an analysis run."""

    __tablename__ = "requirement_result_snapshots"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "result_key", name="uq_run_requirement_result"),
        Index("ix_requirement_result_outcome", "outcome", "reason_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    result_key: Mapped[str] = mapped_column(String(160))
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    requirement_key: Mapped[str | None] = mapped_column(String(160), index=True)
    policy_class: Mapped[str | None] = mapped_column(String(40), index=True)
    outcome: Mapped[str] = mapped_column(String(40), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(80), index=True)
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_state: Mapped[str | None] = mapped_column(String(80))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="requirement_results")


class ScoreSnapshot(Base):
    """Versioned numeric or banded score presented for an analysis run."""

    __tablename__ = "score_snapshots"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "score_key", name="uq_run_score"),
        Index("ix_score_snapshot_type_status", "score_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    score_key: Mapped[str] = mapped_column(String(120))
    score_type: Mapped[str] = mapped_column(String(48), index=True)
    value: Mapped[float | None] = mapped_column(Float)
    lower_value: Mapped[float | None] = mapped_column(Float)
    upper_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="AVAILABLE", index=True)
    band: Mapped[str | None] = mapped_column(String(40), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    method_version: Mapped[str | None] = mapped_column(String(160))
    basis_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="scores")


class RecommendationSnapshot(Base):
    """A ranked department or bid recommendation shown for one analysis run."""

    __tablename__ = "recommendation_snapshots"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "recommendation_key", name="uq_run_recommendation"),
        Index("ix_recommendation_department_rank", "department_id", "rank"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    recommendation_key: Mapped[str] = mapped_column(String(160))
    department_id: Mapped[str | None] = mapped_column(String(120), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    priority_score: Mapped[float | None] = mapped_column(Float)
    recommendation: Mapped[str] = mapped_column(String(40), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    risk_band: Mapped[str | None] = mapped_column(String(40))
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    analysis_run: Mapped[AnalysisRun] = relationship(back_populates="recommendations")


class ReferenceDataVersion(Base, TimestampMixin):
    """Versioned, publication-safe decision basis synchronised from a registry.

    ``payload_json`` may contain only reviewed reference data. Raw source files,
    credentials, local paths, and personal information stay outside the online
    database. Application services retire the previous ACTIVE row and activate
    the new version in one transaction; a portable partial-unique constraint is
    intentionally not used because SQLite and PostgreSQL differ here.
    """

    __tablename__ = "reference_data_versions"
    __table_args__ = (
        UniqueConstraint("dataset_key", "version", name="uq_reference_dataset_version"),
        Index("ix_reference_dataset_active", "dataset_key", "status", "effective_from"),
        Index("ix_reference_content_sha", "dataset_key", "content_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_key: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[str] = mapped_column(String(160))
    schema_version: Mapped[str | None] = mapped_column(String(160))
    content_sha256: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(64), default="PUBLIC_REVIEWED")
    source: Mapped[str] = mapped_column(String(120), default="GIT_PACKAGE")
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class BidOutcome(Base, TimestampMixin):
    """Observed bid/no-bid/win/loss facts used for decision feedback.

    One notice can have multiple append-style observations, while
    ``outcome_key`` lets source adapters idempotently update the same event.
    """

    __tablename__ = "bid_outcomes"
    __table_args__ = (
        UniqueConstraint("notice_id", "outcome_key", name="uq_notice_bid_outcome"),
        Index("ix_bid_outcome_notice_observed", "notice_id", "observed_at"),
        Index("ix_bid_outcome_status_occurred", "status", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    evaluation_id: Mapped[str | None] = mapped_column(
        ForeignKey("evaluations.id", ondelete="SET NULL"), index=True
    )
    decision_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_decisions.id", ondelete="SET NULL"), index=True
    )
    outcome_key: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(32), index=True)
    submitted_bid_amount: Mapped[float | None] = mapped_column(Float)
    submitted_bid_rate: Mapped[float | None] = mapped_column(Float)
    winning_bid_amount: Mapped[float | None] = mapped_column(Float)
    winning_bid_rate: Mapped[float | None] = mapped_column(Float)
    technical_score: Mapped[float | None] = mapped_column(Float)
    price_score: Mapped[float | None] = mapped_column(Float)
    total_score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    winner_name: Mapped[str | None] = mapped_column(String(255))
    reason_code: Mapped[str | None] = mapped_column(String(80), index=True)
    loss_reason: Mapped[str | None] = mapped_column(Text)
    risk_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(48), default="MANUAL", index=True)
    source_reference: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    notice: Mapped[Notice] = relationship(back_populates="bid_outcomes")
    evaluation: Mapped[Evaluation | None] = relationship(back_populates="bid_outcomes")
    decision: Mapped[UserDecision | None] = relationship(back_populates="bid_outcomes")
