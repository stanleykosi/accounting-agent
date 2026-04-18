"""
Purpose: Define entity-level imported ledger baseline models and close-run bindings.
Scope: General-ledger import batches, trial-balance import batches, imported line rows,
and the canonical binding that lets one close run consume an entity baseline.
Dependencies: Shared DB helpers, SQLAlchemy ORM primitives, and users/entities/close-runs.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin, build_text_choice_check
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

_LEDGER_IMPORT_BINDING_SOURCES = ("auto", "manual")


class GeneralLedgerImportBatch(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one uploaded general-ledger baseline for an entity period."""

    __tablename__ = "general_ledger_import_batches"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period_range_valid"),
        CheckConstraint("row_count >= 1", name="row_count_positive"),
        Index(
            "ix_gl_import_batches_entity_period",
            "entity_id",
            "period_start",
            "period_end",
        ),
    )

    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_start: Mapped[date] = mapped_column(nullable=False)
    period_end: Mapped[date] = mapped_column(nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), nullable=False)
    uploaded_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False)
    imported_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    import_metadata: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )


class GeneralLedgerImportLine(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one imported ledger transaction line tied to a batch."""

    __tablename__ = "general_ledger_import_lines"
    __table_args__ = (
        CheckConstraint(
            "debit_amount >= 0 AND credit_amount >= 0",
            name="amounts_non_negative",
        ),
        CheckConstraint(
            "(debit_amount = 0 AND credit_amount > 0) OR "
            "(credit_amount = 0 AND debit_amount > 0)",
            name="single_sided_amount",
        ),
        Index("ix_gl_import_lines_batch_date", "batch_id", "posting_date"),
        Index("ix_gl_import_lines_batch_account", "batch_id", "account_code"),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("general_ledger_import_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    line_no: Mapped[int] = mapped_column(nullable=False)
    posting_date: Mapped[date] = mapped_column(nullable=False)
    account_code: Mapped[str] = mapped_column(String(60), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    debit_amount: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    credit_amount: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    dimensions: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    external_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)


class TrialBalanceImportBatch(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one uploaded trial-balance baseline for an entity period."""

    __tablename__ = "trial_balance_import_batches"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period_range_valid"),
        CheckConstraint("row_count >= 1", name="row_count_positive"),
        Index(
            "ix_tb_import_batches_entity_period",
            "entity_id",
            "period_start",
            "period_end",
        ),
    )

    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_start: Mapped[date] = mapped_column(nullable=False)
    period_end: Mapped[date] = mapped_column(nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), nullable=False)
    uploaded_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False)
    imported_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )
    import_metadata: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )


class TrialBalanceImportLine(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one imported trial-balance account row tied to a batch."""

    __tablename__ = "trial_balance_import_lines"
    __table_args__ = (
        CheckConstraint(
            "debit_balance >= 0 AND credit_balance >= 0",
            name="balances_non_negative",
        ),
        CheckConstraint(
            "(debit_balance = 0 AND credit_balance >= 0) OR "
            "(credit_balance = 0 AND debit_balance >= 0)",
            name="single_sided_balance",
        ),
        Index("ix_tb_import_lines_batch_account", "batch_id", "account_code"),
    )

    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("trial_balance_import_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    line_no: Mapped[int] = mapped_column(nullable=False)
    account_code: Mapped[str] = mapped_column(String(60), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    debit_balance: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    credit_balance: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


class CloseRunLedgerBinding(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist the imported ledger baseline bound to one close run."""

    __tablename__ = "close_run_ledger_bindings"
    __table_args__ = (
        build_text_choice_check(
            column_name="binding_source",
            values=_LEDGER_IMPORT_BINDING_SOURCES,
            constraint_name="binding_source_valid",
        ),
        CheckConstraint(
            (
                "general_ledger_import_batch_id IS NOT NULL "
                "OR trial_balance_import_batch_id IS NOT NULL"
            ),
            name="at_least_one_import_required",
        ),
        UniqueConstraint("close_run_id", name="uq_close_run_ledger_bindings_close_run_id"),
        Index(
            "ix_close_run_ledger_bindings_gl_batch",
            "general_ledger_import_batch_id",
        ),
        Index(
            "ix_close_run_ledger_bindings_tb_batch",
            "trial_balance_import_batch_id",
        ),
    )

    close_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    general_ledger_import_batch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("general_ledger_import_batches.id", ondelete="CASCADE"),
        nullable=True,
    )
    trial_balance_import_batch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("trial_balance_import_batches.id", ondelete="CASCADE"),
        nullable=True,
    )
    binding_source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    bound_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
    )


__all__ = [
    "CloseRunLedgerBinding",
    "GeneralLedgerImportBatch",
    "GeneralLedgerImportLine",
    "TrialBalanceImportBatch",
    "TrialBalanceImportLine",
]
