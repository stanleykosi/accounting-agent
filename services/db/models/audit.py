"""
Purpose: Define immutable review-action and audit-event persistence models.
Scope: Review approvals/rejections plus cross-surface audit events for privileged actions.
Dependencies: Workflow enums, SQLAlchemy ORM primitives, and shared DB base helpers.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from services.common.enums import AutonomyMode
from services.common.types import JsonObject
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class AuditSourceSurface(StrEnum):
    """Enumerate the runtime surfaces allowed to emit canonical audit events."""

    DESKTOP = "desktop"
    CLI = "cli"
    SYSTEM = "system"
    WORKER = "worker"
    INTEGRATION = "integration"


class ReviewAction(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one immutable review decision taken against a reviewable target."""

    __tablename__ = "review_actions"
    __table_args__ = (
        build_text_choice_check(
            column_name="autonomy_mode",
            values=AutonomyMode.values(),
            constraint_name="autonomy_mode_valid",
        ),
        Index(
            "ix_review_actions_close_run_id_target_type_target_id",
            "close_run_id",
            "target_type",
            "target_id",
        ),
    )

    close_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_runs.id"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )
    autonomy_mode: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    before_payload: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    after_payload: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)


class AuditEvent(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist an immutable audit event emitted by a user, worker, or integration surface."""

    __tablename__ = "audit_events"
    __table_args__ = (
        build_text_choice_check(
            column_name="source_surface",
            values=tuple(surface.value for surface in AuditSourceSurface),
            constraint_name="source_surface_valid",
        ),
        Index("ix_audit_events_entity_id_created_at", "entity_id", "created_at"),
        Index("ix_audit_events_event_type", "event_type"),
        Index("ix_audit_events_trace_id", "trace_id"),
    )

    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("entities.id"),
        nullable=False,
    )
    close_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("close_runs.id"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    source_surface: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)


__all__ = ["AuditEvent", "AuditSourceSurface", "ReviewAction"]
