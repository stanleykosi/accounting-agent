"""
Purpose: Define canonical Step 6 supporting-schedule persistence models.
Scope: Standalone workpaper headers and typed row payloads for fixed assets,
       loan amortisation, accrual tracker, and budget-vs-actual schedules.
Dependencies: SQLAlchemy ORM primitives, PostgreSQL JSONB support, shared
       database helpers, and canonical supporting-schedule enums.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from services.common.enums import SupportingScheduleStatus, SupportingScheduleType
from services.common.types import JsonObject
from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class SupportingSchedule(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one Step 6 supporting-schedule header scoped to a close run."""

    __tablename__ = "supporting_schedules"
    __table_args__ = (
        build_text_choice_check(
            column_name="schedule_type",
            values=SupportingScheduleType.values(),
            constraint_name="supporting_schedule_type_valid",
        ),
        build_text_choice_check(
            column_name="status",
            values=SupportingScheduleStatus.values(),
            constraint_name="supporting_schedule_status_valid",
        ),
        UniqueConstraint(
            "close_run_id",
            "schedule_type",
            name="uq_supporting_schedules_close_run_type",
        ),
        Index("ix_supporting_schedules_close_run_id", "close_run_id"),
        Index(
            "ix_supporting_schedules_close_run_status",
            "close_run_id",
            "status",
        ),
    )

    close_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="Close run that owns this supporting schedule.",
    )
    schedule_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="Canonical Step 6 schedule type.",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SupportingScheduleStatus.DRAFT.value,
        server_default=text("'draft'"),
        comment="Lifecycle state of the supporting schedule.",
    )
    note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Operator note about the schedule review decision or applicability.",
    )
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        comment="User who last approved or marked this schedule not applicable.",
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="UTC timestamp when the schedule status was last finalized.",
    )


class SupportingScheduleRow(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one standalone workpaper row within a supporting schedule."""

    __tablename__ = "supporting_schedule_rows"
    __table_args__ = (
        UniqueConstraint(
            "supporting_schedule_id",
            "row_ref",
            name="uq_supporting_schedule_rows_schedule_row_ref",
        ),
        UniqueConstraint(
            "supporting_schedule_id",
            "line_no",
            name="uq_supporting_schedule_rows_schedule_line_no",
        ),
        Index("ix_supporting_schedule_rows_schedule_id", "supporting_schedule_id"),
    )

    supporting_schedule_id: Mapped[UUID] = mapped_column(
        ForeignKey("supporting_schedules.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent supporting schedule.",
    )
    row_ref: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Canonical unique workpaper row reference within the schedule.",
    )
    line_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Stable display order within the schedule.",
    )
    payload: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Typed row payload for the schedule-specific editor and matcher.",
    )


__all__ = ["SupportingSchedule", "SupportingScheduleRow"]
