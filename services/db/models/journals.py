"""
Purpose: Define journal-entry and journal-line persistence models for the accounting engine.
Scope: Versioned journal drafts generated from recommendations, with balanced debit/credit
lines, approval lifecycle tracking, and linkage back to source recommendations and close runs.
Dependencies: Canonical enums, SQLAlchemy ORM primitives, and the shared DB base helpers.

Design notes:
- JournalEntry represents one complete journal draft (header + metadata).
- JournalLine represents individual debit/credit line items belonging to a journal entry.
- The sum of all debit amounts must equal the sum of all credit amounts (enforced by
  application logic, not DB constraint, because Decimal rounding may require tolerance).
- Journal entries transition through ReviewStatus states: draft → pending_review → approved
  → applied (or rejected/superseded).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from services.common.enums import ReviewStatus
from services.common.types import JsonObject
from services.db.base import (
    Base,
    TimestampedModel,
    UUIDPrimaryKeyMixin,
    build_text_choice_check,
)
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class JournalEntry(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one journal entry header with its review lifecycle state.

    Journal entries are generated from approved recommendations (Step 28) and
    carry a collection of balanced debit/credit lines. They track the originating
    recommendation, entity, close run, accounting period, and review history.
    """

    __tablename__ = "journal_entries"
    __table_args__ = (
        build_text_choice_check(
            column_name="status",
            values=ReviewStatus.values(),
            constraint_name="journal_status_valid",
        ),
        CheckConstraint(
            "total_debits = total_credits",
            name="journal_debits_equal_credits",
        ),
        CheckConstraint("line_count >= 2", name="journal_minimum_lines"),
        Index("ix_journal_entries_close_run_status", "close_run_id", "status"),
        Index("ix_journal_entries_recommendation", "recommendation_id"),
        Index("ix_journal_entries_entity_period", "entity_id", "posting_date"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    close_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recommendation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("recommendations.id", ondelete="SET NULL"),
        nullable=True,
        comment="Source recommendation that produced this journal entry, if any.",
    )
    journal_number: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        unique=True,
        comment="Human-readable journal identifier (e.g., 'JE-2026-00001').",
    )
    posting_date: Mapped[date] = mapped_column(
        nullable=False,
        comment="Accounting date for the journal posting.",
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=ReviewStatus.DRAFT.value,
        comment="Review lifecycle state of the journal entry.",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Narrative description of the journal entry purpose.",
    )
    total_debits: Mapped[float] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        comment="Sum of all debit line amounts. Must equal total_credits.",
    )
    total_credits: Mapped[float] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        comment="Sum of all credit line amounts. Must equal total_debits.",
    )
    line_count: Mapped[int] = mapped_column(
        nullable=False,
        comment="Number of journal lines attached to this entry.",
    )
    source_surface: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="system",
        comment="Surface that created the journal (system, desktop, cli, chat).",
    )
    autonomy_mode: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        comment="Autonomy mode active when the journal was created.",
    )
    reasoning_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Explanation of why this journal was generated.",
    )
    metadata_payload: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Additional structured metadata (rule version, prompt version, etc.).",
    )
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="User who approved this journal entry.",
    )
    applied_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="User who applied this journal entry to working state.",
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID of the journal entry that superseded this one.",
    )


class JournalLine(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one debit or credit line belonging to a journal entry.

    Each line specifies a GL account, amount, direction (debit/credit), and
    optional dimension assignments (cost centre, department, project).
    """

    __tablename__ = "journal_lines"
    __table_args__ = (
        build_text_choice_check(
            column_name="line_type",
            values=("debit", "credit"),
            constraint_name="journal_line_type_valid",
        ),
        CheckConstraint("amount > 0", name="journal_line_amount_positive"),
        CheckConstraint("line_no >= 1", name="journal_line_no_positive"),
        Index("ix_journal_lines_journal_entry", "journal_entry_id"),
        Index("ix_journal_lines_account_code", "account_code"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    journal_entry_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "journal_entries.id",
            ondelete="CASCADE",
            name="fk_journal_lines_journal_entry_id",
        ),
        nullable=False,
    )
    line_no: Mapped[int] = mapped_column(
        nullable=False,
        comment="Sequential line number within the journal entry (1-based).",
    )
    account_code: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        comment="GL account code from the active chart of accounts.",
    )
    line_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Either 'debit' or 'credit'.",
    )
    amount: Mapped[float] = mapped_column(
        Numeric(20, 2),
        nullable=False,
        comment="Monetary amount for this line (always positive).",
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional memo or description for this specific line.",
    )
    dimensions: Mapped[JsonObject] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Assigned dimensions (cost_centre, department, project).",
    )
    reference: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="Optional external reference or transaction ID.",
    )


__all__ = [
    "JournalEntry",
    "JournalLine",
]
