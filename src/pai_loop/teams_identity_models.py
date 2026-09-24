"""Personal Teams destinations and short-lived browser-session pairing proofs."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import UTCDateTime, new_id


class TeamsRecipient(Base):
    __tablename__ = "teams_recipients"
    __table_args__ = (UniqueConstraint("tenant_id", "aad_object_id", name="uq_teams_recipient_person"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Provenance only: a department account is shared and never identifies a person.
    account_id: Mapped[str | None] = mapped_column(ForeignKey("department_accounts.id"), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(36))
    aad_object_id: Mapped[str] = mapped_column(String(36))
    conversation_id: Mapped[str] = mapped_column(Text)
    service_url: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    # NULL means the person never chose, which reads as subscribed. Pairing is
    # the opt-in, so a recipient that predates this column keeps receiving the
    # briefing and only an explicit False stops it. A choice survives
    # disconnecting and pairing again: turning it off is deliberate.
    briefing_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TeamsSessionLink(Base):
    __tablename__ = "teams_session_links"
    session_id: Mapped[str] = mapped_column(ForeignKey("account_sessions.id"), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("department_accounts.id"))
    recipient_id: Mapped[str] = mapped_column(ForeignKey("teams_recipients.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TeamsLinkCode(Base):
    __tablename__ = "teams_link_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("account_sessions.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("department_accounts.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
