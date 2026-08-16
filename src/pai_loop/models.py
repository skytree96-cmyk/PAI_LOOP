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
    ruleset_version: Mapped[str] = mapped_column(String(32), default="2026.08-v1")
    atomic_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON)

    notice: Mapped[Notice] = relationship(back_populates="evaluations")
    decisions: Mapped[list["UserDecision"]] = relationship(back_populates="evaluation")


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

