"""Durable outbox for the personal daily briefing.

A briefing is not tied to any one notice, so it cannot reuse
``teams_follow_deliveries`` whose ``follow_id`` is required. It keeps its own
row per recipient per KST day per kind, and that uniqueness is what stops a
restart, a second worker, or a same-day reconnect from sending twice.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import UTCDateTime, new_id


class TeamsBriefingDelivery(Base):
    __tablename__ = "teams_briefing_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "recipient_id", "kst_date", "kind", name="uq_teams_briefing_day"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recipient_id: Mapped[str] = mapped_column(
        ForeignKey("teams_recipients.id"), index=True
    )
    # Provenance for the department the briefing was scoped to. A recipient can
    # reconnect from another department account, so the scope is recorded per
    # delivery rather than read back from the recipient afterwards.
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("department_accounts.id"), nullable=True
    )
    department_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # SUBSCRIBED is the ping sent right after a pairing; DAILY is the 08:30 run.
    kind: Mapped[str] = mapped_column(String(16))
    kst_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    # A ping deliberately skips the daily readiness gate, so the card says which
    # basis it was built on and the row records it for later operator checks.
    readiness_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_code: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
