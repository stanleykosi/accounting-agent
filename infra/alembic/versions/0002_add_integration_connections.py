"""
Purpose: Add the canonical persistence table for encrypted external integration credentials.
Scope: Entity-scoped integration connections, provider lifecycle state, and sync metadata.
Dependencies: Alembic, SQLAlchemy, and the Step 14 secret-boundary design.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0002_add_integration_connections"
down_revision = "0001_baseline_auth_and_close_runs"
branch_labels = None
depends_on = None

INTEGRATION_PROVIDERS = ("quickbooks_online",)
INTEGRATION_STATUSES = ("connected", "expired", "revoked", "error")


def upgrade() -> None:
    """Create the integration-connections table used for encrypted OAuth credential storage."""

    op.create_table(
        "integration_connections",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("entities.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'connected'")),
        sa.Column(
            "encrypted_credentials",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("external_realm_id", sa.Text(), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            _in_check("provider", INTEGRATION_PROVIDERS),
            name="ck_integration_connections_provider_valid",
        ),
        sa.CheckConstraint(
            _in_check("status", INTEGRATION_STATUSES),
            name="ck_integration_connections_status_valid",
        ),
        sa.UniqueConstraint(
            "entity_id",
            "provider",
            name="uq_integration_connections_entity_provider",
        ),
    )
    op.create_index(
        "ix_integration_connections_status",
        "integration_connections",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_integration_connections_last_sync_at",
        "integration_connections",
        ["last_sync_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the integration-connections table and its indexes."""

    op.drop_index("ix_integration_connections_last_sync_at", table_name="integration_connections")
    op.drop_index("ix_integration_connections_status", table_name="integration_connections")
    op.drop_table("integration_connections")


def _in_check(column_name: str, values: tuple[str, ...]) -> str:
    """Build a deterministic SQL expression for static text choice constraints."""

    quoted_values = ", ".join(f"'{value}'" for value in values)
    return f"{column_name} IN ({quoted_values})"
