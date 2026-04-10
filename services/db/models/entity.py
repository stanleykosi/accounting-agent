"""
Purpose: Define entity workspace and membership persistence models.
Scope: Entity settings, autonomous-routing defaults, and per-entity user membership rows.
Dependencies: Shared workflow enums plus the SQLAlchemy DB base helpers.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from services.common.enums import AutonomyMode
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import CHAR, Boolean, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS = {
    "classification": 0.85,
    "coding": 0.85,
    "reconciliation": 0.9,
    "posting": 0.95,
}
DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS_SQL = (
    "'{\"classification\":0.85,\"coding\":0.85,\"reconciliation\":0.9,\"posting\":0.95}'::jsonb"
)


class EntityStatus(StrEnum):
    """Enumerate the supported workspace lifecycle states."""

    ACTIVE = "active"
    ARCHIVED = "archived"


def build_default_confidence_thresholds() -> dict[str, float]:
    """Return a fresh copy of the canonical entity-level confidence defaults."""

    return dict(DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS)


class Entity(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one accounting workspace, which always maps to exactly one entity."""

    __tablename__ = "entities"
    __table_args__ = (
        build_text_choice_check(
            column_name="autonomy_mode",
            values=AutonomyMode.values(),
            constraint_name="autonomy_mode_valid",
        ),
        build_text_choice_check(
            column_name="status",
            values=tuple(status.value for status in EntityStatus),
            constraint_name="status_valid",
        ),
        Index("ix_entities_name", "name"),
        Index("ix_entities_status", "status"),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String, nullable=True)
    base_currency: Mapped[str] = mapped_column(
        CHAR(3),
        nullable=False,
        default="NGN",
        server_default="NGN",
    )
    country_code: Mapped[str] = mapped_column(
        CHAR(2),
        nullable=False,
        default="NG",
        server_default="NG",
    )
    timezone: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="Africa/Lagos",
        server_default="Africa/Lagos",
    )
    accounting_standard: Mapped[str | None] = mapped_column(String, nullable=True)
    autonomy_mode: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=AutonomyMode.HUMAN_REVIEW.value,
        server_default=AutonomyMode.HUMAN_REVIEW.value,
    )
    default_confidence_thresholds: Mapped[dict[str, float]] = mapped_column(
        JSONB,
        nullable=False,
        default=build_default_confidence_thresholds,
        server_default=text(DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS_SQL),
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=EntityStatus.ACTIVE.value,
        server_default=EntityStatus.ACTIVE.value,
    )


class EntityMembership(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one user's membership and default-actor preference for an entity workspace."""

    __tablename__ = "entity_memberships"
    __table_args__ = (
        UniqueConstraint("entity_id", "user_id", name="uq_entity_memberships_entity_user"),
    )

    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    is_default_actor: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )


__all__ = [
    "DEFAULT_ENTITY_CONFIDENCE_THRESHOLDS",
    "Entity",
    "EntityMembership",
    "EntityStatus",
    "build_default_confidence_thresholds",
]
