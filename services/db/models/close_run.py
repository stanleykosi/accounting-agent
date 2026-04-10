"""
Purpose: Define close-run and workflow-phase persistence models.
Scope: Period-bound close runs, lifecycle state, version lineage, and per-phase gate tracking.
Dependencies: Canonical workflow enums and the shared SQLAlchemy DB base helpers.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from services.common.enums import CloseRunPhaseStatus, CloseRunStatus, WorkflowPhase
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import CHAR, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class CloseRun(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one entity-specific accounting period and its canonical lifecycle state."""

    __tablename__ = "close_runs"
    __table_args__ = (
        build_text_choice_check(
            column_name="status",
            values=CloseRunStatus.values(),
            constraint_name="status_valid",
        ),
        CheckConstraint("period_end >= period_start", name="period_range_valid"),
        CheckConstraint("current_version_no >= 1", name="current_version_no_positive"),
        UniqueConstraint(
            "entity_id",
            "period_start",
            "period_end",
            "current_version_no",
            name="uq_close_runs_entity_period_version",
        ),
        Index("ix_close_runs_entity_id_status", "entity_id", "status"),
        Index(
            "ix_close_runs_entity_id_period_start_period_end",
            "entity_id",
            "period_start",
            "period_end",
        ),
    )

    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("entities.id"),
        nullable=False,
    )
    period_start: Mapped[date] = mapped_column(nullable=False)
    period_end: Mapped[date] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reporting_currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    current_version_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    opened_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reopened_from_close_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("close_runs.id"),
        nullable=True,
    )


class CloseRunPhaseState(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist the gate status of one workflow phase for a single close run."""

    __tablename__ = "close_run_phase_states"
    __table_args__ = (
        build_text_choice_check(
            column_name="phase",
            values=WorkflowPhase.values(),
            constraint_name="phase_valid",
        ),
        build_text_choice_check(
            column_name="status",
            values=CloseRunPhaseStatus.values(),
            constraint_name="status_valid",
        ),
        CheckConstraint(
            "(status = 'blocked' AND blocking_reason IS NOT NULL) "
            "OR (status <> 'blocked' AND blocking_reason IS NULL)",
            name="blocking_reason_valid",
        ),
        UniqueConstraint(
            "close_run_id",
            "phase",
            name="uq_close_run_phase_states_close_run_phase",
        ),
    )

    close_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_runs.id"),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    blocking_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)


__all__ = ["CloseRun", "CloseRunPhaseState"]
