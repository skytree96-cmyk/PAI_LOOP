from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .models import UTCDateTime, new_id


class PreSpecification(Base):
    """A public PPS pre-specification, kept outside the bid decision queue."""

    __tablename__ = "pre_specifications"
    __table_args__ = (
        Index("ix_pre_specifications_status_deadline", "status", "opinion_deadline"),
        Index("ix_pre_specifications_title", "title"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    registry_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    pre_specification_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    ordering_agency: Mapped[str | None] = mapped_column(String(255))
    demand_agency: Mapped[str | None] = mapped_column(String(255))
    business_division: Mapped[str | None] = mapped_column(String(80))
    reference_no: Mapped[str | None] = mapped_column(String(120))
    budget_amount: Mapped[float | None] = mapped_column(Float)
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    registered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    opinion_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime())
    delivery_due: Mapped[datetime | None] = mapped_column(UTCDateTime())
    software_business: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="OPEN_FOR_OPINION", index=True)
    linked_bid_notice_nos: Mapped[list[str]] = mapped_column(JSON, default=list)
    matched_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_digest: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )

    versions: Mapped[list["PreSpecificationVersion"]] = relationship(
        back_populates="pre_specification",
        cascade="all, delete-orphan",
        order_by="PreSpecificationVersion.version_no",
    )
    documents: Mapped[list["PreSpecificationDocument"]] = relationship(
        back_populates="pre_specification",
        cascade="all, delete-orphan",
        order_by="PreSpecificationDocument.created_at",
    )
    analysis_runs: Mapped[list["PreSpecificationAnalysisRun"]] = relationship(
        back_populates="pre_specification",
        cascade="all, delete-orphan",
        order_by="PreSpecificationAnalysisRun.created_at",
    )


class PreSpecificationVersion(Base):
    """Immutable allowlisted provider snapshot for one material change."""

    __tablename__ = "pre_specification_versions"
    __table_args__ = (
        UniqueConstraint(
            "pre_specification_id",
            "version_no",
            name="uq_pre_specification_version",
        ),
        UniqueConstraint(
            "pre_specification_id",
            "source_digest",
            name="uq_pre_specification_source_digest",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pre_specification_id: Mapped[str] = mapped_column(
        ForeignKey("pre_specifications.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    source_digest: Mapped[str] = mapped_column(String(64), index=True)
    provider_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )

    pre_specification: Mapped[PreSpecification] = relationship(back_populates="versions")
    documents: Mapped[list["PreSpecificationDocument"]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="PreSpecificationDocument.slot",
    )


class PreSpecificationDocument(Base):
    """Canonical document reference bound to an immutable pre-spec version."""

    __tablename__ = "pre_specification_documents"
    __table_args__ = (
        UniqueConstraint(
            "pre_specification_version_id",
            "slot",
            name="uq_pre_specification_document_slot",
        ),
        Index(
            "ix_pre_specification_documents_parent_digest",
            "pre_specification_id",
            "source_digest",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pre_specification_id: Mapped[str] = mapped_column(
        ForeignKey("pre_specifications.id", ondelete="CASCADE"), index=True
    )
    pre_specification_version_id: Mapped[str] = mapped_column(
        ForeignKey("pre_specification_versions.id", ondelete="CASCADE"), index=True
    )
    slot: Mapped[int] = mapped_column(Integer)
    safe_url: Mapped[str] = mapped_column(Text)
    source_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )

    pre_specification: Mapped[PreSpecification] = relationship(back_populates="documents")
    version: Mapped[PreSpecificationVersion] = relationship(back_populates="documents")


class PreSpecificationAnalysisRun(Base):
    """Sanitised result of one explicitly approved pre-spec document analysis."""

    __tablename__ = "pre_specification_analysis_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_pre_spec_analysis_idempotency"),
        Index(
            "ix_pre_spec_analysis_parent_created",
            "pre_specification_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pre_specification_id: Mapped[str] = mapped_column(
        ForeignKey("pre_specifications.id", ondelete="CASCADE"), index=True
    )
    source_digest: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    document_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    openai_calls: Mapped[int] = mapped_column(Integer, default=0)
    openai_telemetry: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    pre_specification: Mapped[PreSpecification] = relationship(
        back_populates="analysis_runs"
    )
