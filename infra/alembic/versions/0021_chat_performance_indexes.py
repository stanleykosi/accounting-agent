"""
Purpose: Add production indexes for chat, close-run list, and membership hot paths.
Scope: Speed up workspace chat thread lists, global assistant thread lists, and
membership joins used by chat/API access checks.
Dependencies: Alembic and the existing chat_threads/close_runs/entity_memberships tables.
"""

from __future__ import annotations

from alembic import op

# Revision identifiers, used by Alembic.
revision = "0021_chat_performance_indexes"
down_revision = "0020_chat_message_action_plan_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the canonical chat performance indexes."""

    op.create_index(
        "ix_chat_threads_entity_close_run_created",
        "chat_threads",
        ["entity_id", "close_run_id", "created_at", "id"],
    )
    op.create_index(
        "ix_chat_threads_entity_updated",
        "chat_threads",
        ["entity_id", "updated_at", "id"],
    )
    op.create_index(
        "ix_entity_memberships_user_entity",
        "entity_memberships",
        ["user_id", "entity_id"],
    )
    op.create_index(
        "ix_close_runs_entity_period_version",
        "close_runs",
        ["entity_id", "period_start", "current_version_no", "id"],
    )


def downgrade() -> None:
    """Drop the chat performance indexes."""

    op.drop_index("ix_close_runs_entity_period_version", table_name="close_runs")
    op.drop_index("ix_entity_memberships_user_entity", table_name="entity_memberships")
    op.drop_index("ix_chat_threads_entity_updated", table_name="chat_threads")
    op.drop_index("ix_chat_threads_entity_close_run_created", table_name="chat_threads")
