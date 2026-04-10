"""
Purpose: Define the canonical persistence model for external integration connections.
Scope: Provider identity, encrypted OAuth credentials, connection lifecycle state,
and sync metadata for entity-scoped integrations.
Dependencies: SQLAlchemy ORM, PostgreSQL JSONB support, and shared DB base helpers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class IntegrationProvider(StrEnum):
    """Enumerate the external systems supported by the canonical integration boundary."""

    QUICKBOOKS_ONLINE = "quickbooks_online"


class IntegrationConnectionStatus(StrEnum):
    """Enumerate the supported lifecycle states for an entity-scoped connection."""

    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"


class IntegrationConnection(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one entity-scoped connection with encrypted provider credentials."""

    __tablename__ = "integration_connections"
    __table_args__ = (
        build_text_choice_check(
            column_name="provider",
            values=tuple(provider.value for provider in IntegrationProvider),
            constraint_name="provider_valid",
        ),
        build_text_choice_check(
            column_name="status",
            values=tuple(status.value for status in IntegrationConnectionStatus),
            constraint_name="status_valid",
        ),
        UniqueConstraint(
            "entity_id",
            "provider",
            name="uq_integration_connections_entity_provider",
        ),
        Index("ix_integration_connections_status", "status"),
        Index("ix_integration_connections_last_sync_at", "last_sync_at"),
    )

    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=IntegrationConnectionStatus.CONNECTED.value,
        server_default=IntegrationConnectionStatus.CONNECTED.value,
    )
    encrypted_credentials: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    external_realm_id: Mapped[str] = mapped_column(String, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(nullable=True)


__all__ = [
    "IntegrationConnection",
    "IntegrationConnectionStatus",
    "IntegrationProvider",
]
