"""Department identities: never infer ownership from legacy display labels."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import UTCDateTime, new_id


class DepartmentAccount(Base):
    __tablename__ = "department_accounts"
    __table_args__ = (CheckConstraint("(role = 'DEPARTMENT' AND department_id IS NOT NULL) OR (role = 'ADMIN' AND department_id IS NULL)", name="ck_account_role_department"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    department_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_analysis_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AccountSession(Base):
    __tablename__ = "account_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("department_accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AccountLoginBucket(Base):
    __tablename__ = "account_login_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class AccountAudit(Base):
    __tablename__ = "account_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_account_id: Mapped[str | None] = mapped_column(String(36))
    target_id: Mapped[str | None] = mapped_column(String(36))
    event: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class AccountBootstrapPreview(Base):
    __tablename__ = "account_bootstrap_previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
