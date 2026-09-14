"""Personal interest subscriptions and a durable, audited Teams delivery outbox."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import UTCDateTime, new_id


class TeamsFollow(Base):
    __tablename__ = "teams_follows"
    __table_args__ = (UniqueConstraint("recipient_id", "notice_id", name="uq_teams_follow_recipient_notice"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("teams_recipients.id"), index=True)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notices.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("department_accounts.id"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    subscribed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deadline_snapshot: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TeamsFollowDelivery(Base):
    __tablename__ = "teams_follow_deliveries"
    __table_args__ = (
        UniqueConstraint("follow_id", "generation", "event_kind", name="uq_teams_follow_delivery_event"),
        Index("ix_teams_follow_delivery_due", "status", "scheduled_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    follow_id: Mapped[str] = mapped_column(ForeignKey("teams_follows.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    event_kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deadline_snapshot: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_code: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
