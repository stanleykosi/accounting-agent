"""
Purpose: Define generic ownership, last-touch, and in-progress lock persistence.
Scope: One canonical ownership target table for documents, recommendations,
review targets, close runs, and entity workspaces as those domain tables arrive.
Dependencies: Canonical ownership enums, SQLAlchemy ORM primitives, and shared DB base helpers.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from services.common.enums import OwnershipTargetType
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class OwnershipTarget(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one reviewable target's owner, in-progress lock, and last-touch metadata."""

    __tablename__ = "ownership_targets"
    __table_args__ = (
        build_text_choice_check(
            column_name="target_type",
            values=OwnershipTargetType.values(),
            constraint_name="target_type_valid",
        ),
        CheckConstraint(
            "(locked_by_user_id IS NULL AND locked_at IS NULL) "
            "OR (locked_by_user_id IS NOT NULL AND locked_at IS NOT NULL)",
            name="lock_metadata_valid",
        ),
        CheckConstraint(
            "(last_touched_by_user_id IS NULL AND last_touched_at IS NULL) "
            "OR (last_touched_by_user_id IS NOT NULL AND last_touched_at IS NOT NULL)",
            name="last_touch_metadata_valid",
        ),
        UniqueConstraint("target_type", "target_id", name="uq_ownership_targets_type_target"),
        Index("ix_ownership_targets_entity_id", "entity_id"),
        Index("ix_ownership_targets_close_run_id", "close_run_id"),
        Index("ix_ownership_targets_locked_by_user_id", "locked_by_user_id"),
    )

    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), nullable=False)
    close_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("close_runs.id"), nullable=True)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    locked_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_touched_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    last_touched_at: Mapped[datetime | None] = mapped_column(nullable=True)
    lock_note: Mapped[str | None] = mapped_column(String, nullable=True)


__all__ = ["OwnershipTarget"]
