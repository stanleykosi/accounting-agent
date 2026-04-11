"""
Purpose: Add generic ownership and in-progress lock metadata for reviewable targets.
Scope: One canonical ownership table used by documents, recommendations, review targets,
close runs, and entity workspaces without compatibility fallbacks.
Dependencies: Alembic, SQLAlchemy, and the baseline auth/entity/close-run tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision = "0003_ownership_targets"
down_revision = "0002_add_integration_connections"
branch_labels = None
depends_on = None

OWNERSHIP_TARGET_TYPES = (
    "entity",
    "close_run",
    "document",
    "recommendation",
    "review_target",
)


def upgrade() -> None:
    """Create the canonical ownership target table for Step 17."""

    op.create_table(
        "ownership_targets",
        *_uuid_primary_key_column(),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("close_run_id", sa.Uuid(), sa.ForeignKey("close_runs.id"), nullable=True),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("locked_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_touched_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("last_touched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_note", sa.Text(), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            _in_check("target_type", OWNERSHIP_TARGET_TYPES),
            name="ck_ownership_targets_target_type_valid",
        ),
        sa.CheckConstraint(
            "(locked_by_user_id IS NULL AND locked_at IS NULL) "
            "OR (locked_by_user_id IS NOT NULL AND locked_at IS NOT NULL)",
            name="ck_ownership_targets_lock_metadata_valid",
        ),
        sa.CheckConstraint(
            "(last_touched_by_user_id IS NULL AND last_touched_at IS NULL) "
            "OR (last_touched_by_user_id IS NOT NULL AND last_touched_at IS NOT NULL)",
            name="ck_ownership_targets_last_touch_metadata_valid",
        ),
        sa.UniqueConstraint(
            "target_type",
            "target_id",
            name="uq_ownership_targets_type_target",
        ),
    )
    op.create_index("ix_ownership_targets_entity_id", "ownership_targets", ["entity_id"])
    op.create_index("ix_ownership_targets_close_run_id", "ownership_targets", ["close_run_id"])
    op.create_index(
        "ix_ownership_targets_locked_by_user_id",
        "ownership_targets",
        ["locked_by_user_id"],
    )


def downgrade() -> None:
    """Drop the ownership table in reverse creation order."""

    op.drop_index("ix_ownership_targets_locked_by_user_id", table_name="ownership_targets")
    op.drop_index("ix_ownership_targets_close_run_id", table_name="ownership_targets")
    op.drop_index("ix_ownership_targets_entity_id", table_name="ownership_targets")
    op.drop_table("ownership_targets")


def _uuid_primary_key_column() -> tuple[sa.Column[Any], ...]:
    """Return the shared UUID primary key column used by migration-created tables."""

    return (
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )


def _timestamp_columns() -> tuple[sa.Column[Any], sa.Column[Any]]:
    """Return standard creation and update timestamp columns."""

    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def _in_check(column_name: str, values: Iterable[str]) -> str:
    """Build static SQL for a text-column membership check constraint."""

    joined_values = ", ".join(_quote_sql_literal(value) for value in values)
    return f"{column_name} IN ({joined_values})"


def _quote_sql_literal(value: str) -> str:
    """Quote a trusted string literal for static migration SQL."""

    escaped_value = value.replace("'", "''")
    return f"'{escaped_value}'"
