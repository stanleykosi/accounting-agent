"""
Purpose: Define the canonical authentication and session persistence models.
Scope: Local users, signed desktop/web sessions, and CLI personal access tokens.
Dependencies: SQLAlchemy ORM, PostgreSQL-specific types, and shared DB base helpers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column


class UserStatus(StrEnum):
    """Enumerate the supported local-auth lifecycle states."""

    ACTIVE = "active"
    DISABLED = "disabled"


class User(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one locally authenticated operator account."""

    __tablename__ = "users"
    __table_args__ = (
        build_text_choice_check(
            column_name="status",
            values=tuple(status.value for status in UserStatus),
            constraint_name="status_valid",
        ),
    )

    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=UserStatus.ACTIVE.value,
        server_default=UserStatus.ACTIVE.value,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Session(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist a revocable signed session issued to the desktop or web surface."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET(), nullable=True)


class ApiToken(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist a hashed personal access token issued for CLI authentication."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        Index("ix_api_tokens_user_id", "user_id"),
        Index(
            "ix_api_tokens_active_user_id",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    scope: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)


__all__ = ["ApiToken", "Session", "User", "UserStatus"]
