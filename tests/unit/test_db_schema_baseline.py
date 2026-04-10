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
        "audit_events",
        "close_run_phase_states",
        "close_runs",
        "entities",
        "entity_memberships",
        "integration_connections",
        "review_actions",
        "sessions",
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
