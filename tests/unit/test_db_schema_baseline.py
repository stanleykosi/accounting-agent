"""
Purpose: Verify the baseline ORM metadata exposes the expected Step 8 tables and constraints.
Scope: Lightweight schema assertions that catch accidental drift before migrations are applied.
Dependencies: services/db/base.py and the registered ORM model modules.
"""

from __future__ import annotations

import services.db.models  # noqa: F401  # Register ORM models for metadata inspection.
from services.db.base import Base
from sqlalchemy import CheckConstraint, DefaultClause, UniqueConstraint


def test_baseline_metadata_registers_expected_tables() -> None:
    """Ensure the baseline metadata includes every Step 8 foundation table."""

    expected_tables = {
        "api_tokens",
        "artifacts",
        "audit_events",
        "close_run_phase_states",
        "close_runs",
        "document_issues",
        "document_extractions",
        "document_line_items",
        "document_versions",
        "documents",
        "entities",
        "entity_memberships",
        "export_distributions",
        "export_runs",
        "extracted_fields",
        "general_ledger_import_batches",
        "general_ledger_import_lines",
        "integration_connections",
        "journal_postings",
        "jobs",
        "close_run_ledger_bindings",
        "ownership_targets",
        "review_actions",
        "sessions",
        "supporting_schedule_rows",
        "supporting_schedules",
        "trial_balance_import_batches",
        "trial_balance_import_lines",
        "users",
    }

    assert expected_tables.issubset(Base.metadata.tables)


def test_close_run_phase_states_enforce_blocking_reason_integrity() -> None:
    """Ensure blocked phase rows require a blocking reason and others must omit it."""

    table = Base.metadata.tables["close_run_phase_states"]
    constraint_sql = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert (
        "(status = 'blocked' AND blocking_reason IS NOT NULL) "
        "OR (status <> 'blocked' AND blocking_reason IS NULL)"
    ) in constraint_sql


def test_entities_have_canonical_confidence_threshold_default() -> None:
    """Ensure entity workspaces carry the seeded confidence threshold categories."""

    table = Base.metadata.tables["entities"]
    server_default = table.c.default_confidence_thresholds.server_default

    assert isinstance(server_default, DefaultClause)
    default_expression = str(server_default.arg)

    assert "classification" in default_expression
    assert "coding" in default_expression
    assert "reconciliation" in default_expression
    assert "posting" in default_expression


def test_integration_connections_enforce_unique_provider_per_entity() -> None:
    """Ensure one entity cannot persist duplicate connections for the same provider."""

    table = Base.metadata.tables["integration_connections"]
    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("entity_id", "provider") in unique_constraints


def test_ownership_targets_enforce_unique_target_and_lock_integrity() -> None:
    """Ensure ownership metadata has one row per target and consistent lock timestamps."""

    table = Base.metadata.tables["ownership_targets"]
    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    constraint_sql = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert ("target_type", "target_id") in unique_constraints
    assert (
        "(locked_by_user_id IS NULL AND locked_at IS NULL) "
        "OR (locked_by_user_id IS NOT NULL AND locked_at IS NOT NULL)"
    ) in constraint_sql


def test_jobs_metadata_exposes_checkpoint_and_dead_letter_integrity() -> None:
    """Ensure job rows retain checkpoints and only dead-letter failed executions."""

    table = Base.metadata.tables["jobs"]
    constraint_sql = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "dead_lettered_at IS NULL OR status = 'failed'" in constraint_sql
    assert (
        "(status = 'blocked' AND blocking_reason IS NOT NULL) "
        "OR (status <> 'blocked' AND blocking_reason IS NULL)"
    ) in constraint_sql


def test_chat_messages_expose_canonical_per_thread_message_order() -> None:
    """Ensure chat transcripts use a stable per-thread message sequence."""

    table = Base.metadata.tables["chat_messages"]
    unique_constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert "message_order" in table.c
    assert table.c.message_order.nullable is False
    assert ("thread_id", "message_order") in unique_constraints


def test_general_ledger_import_lines_expose_transaction_group_key() -> None:
    """Ensure imported GL rows carry the canonical persisted transaction grouping key."""

    table = Base.metadata.tables["general_ledger_import_lines"]

    assert "transaction_group_key" in table.c
    assert table.c.transaction_group_key.nullable is False
