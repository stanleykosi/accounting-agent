"""
Purpose: Create chat_threads and chat_messages tables for the grounded finance
copilot experience.
Scope: Thread container scoped to entity + optional close run, and message rows
with role, content type, grounding context, and optional action links.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB and text types.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0011_chat_threads_and_messages"
down_revision = "0010_report_templates_and_runs"
branch_labels = None
depends_on = None


def _uuid_pk() -> sa.Column:
    """Build a standard UUID primary key column definition."""

    return sa.Column(
        "id",
        sa.Uuid(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """Build canonical created_at / updated_at timestamp columns."""

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


def upgrade() -> None:
    """Create chat_threads and chat_messages tables with grounding context support."""

    op.create_table(
        "chat_threads",
        _uuid_pk(),
        sa.Column(
            "entity_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment="Entity workspace that owns this conversation thread.",
        ),
        sa.Column(
            "close_run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("close_runs.id", ondelete="SET NULL"),
            nullable=True,
            comment="Optional close run scoping the thread to a specific accounting period.",
        ),
        sa.Column(
            "title",
            sa.String(300),
            nullable=True,
            comment="Human-readable thread title, auto-generated or user-edited.",
        ),
        sa.Column(
            "context_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Grounding context snapshot (entity, close run, period, autonomy mode).",
        ),
        *_timestamps(),
    )
    op.create_index(
        "ix_chat_threads_entity_close_run",
        "chat_threads",
        ["entity_id", "close_run_id"],
    )

    op.create_table(
        "chat_messages",
        _uuid_pk(),
        sa.Column(
            "thread_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_threads.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment="Parent chat thread that this message belongs to.",
        ),
        sa.Column(
            "role",
            sa.String(20),
            nullable=False,
            comment="Message originator: user, assistant, or system.",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="Message text content (Markdown-supported for assistant messages).",
        ),
        sa.Column(
            "message_type",
            sa.String(20),
            nullable=False,
            server_default="'analysis'",
            comment="Intent classification: analysis, workflow, action, or warning.",
        ),
        sa.Column(
            "linked_action_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("recommendations.id", ondelete="SET NULL"),
            nullable=True,
            comment="Optional reference to a recommendation discussed in this message.",
        ),
        sa.Column(
            "grounding_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Evidence snapshot used to generate the assistant response.",
        ),
        sa.Column(
            "model_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Model name, token usage, latency, and provider metadata.",
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="ck_chat_messages_role",
        ),
        sa.CheckConstraint(
            "message_type IN ('analysis', 'workflow', 'action', 'warning')",
            name="ck_chat_messages_message_type",
        ),
    )
    op.create_index(
        "ix_chat_messages_thread_order",
        "chat_messages",
        ["thread_id", "created_at"],
    )


def downgrade() -> None:
    """Remove chat_messages and chat_threads tables."""

    op.drop_table("chat_messages")
    op.drop_table("chat_threads")
