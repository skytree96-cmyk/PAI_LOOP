"""Durable award-only coverage and request reservations, separate from analysis."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import UTCDateTime, new_id


class AwardRefreshState(Base):
    __tablename__ = "award_refresh_states"
    __table_args__ = (Index("ix_award_refresh_due", "status", "next_attempt_at"),)

    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(64))
    basis_sha256: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cycle_attempts: Mapped[int] = mapped_column(Integer, default=0)
    records: Mapped[int] = mapped_column(Integer, default=0)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    last_job_id: Mapped[str | None] = mapped_column(String(36))
    refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    leased_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_token: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AwardRefreshAttempt(Base):
    __tablename__ = "award_refresh_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reserved_calls: Mapped[int] = mapped_column(Integer)
    # Null means actual usage is unknown: the full reservation remains charged.
    api_calls: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
