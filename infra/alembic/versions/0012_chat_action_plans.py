"""
Purpose: Create chat_action_plans table for chat-originated action execution
plans (proposed edits, approval requests, workflow actions).
Scope: Store proposed changes generated from chat messages with their review
lifecycle state, autonomy mode context, and downstream materialization results.
Dependencies: Alembic, SQLAlchemy, PostgreSQL JSONB and text types, chat_threads
and chat_messages tables (migration 0011).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision = "0012_chat_action_plans"
down_revision = "0011_chat_threads_and_messages"
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
    """Create chat_action_plans table for proposed changes and action routing."""

    op.create_table(
        "chat_action_plans",
        _uuid_pk(),
        sa.Column(
            "thread_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_threads.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment="Chat thread where this action originated.",
        ),
        sa.Column(
            "message_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
            comment="Message that triggered the action, if attributable.",
        ),
        sa.Column(
            "entity_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment="Entity workspace that owns this action plan.",
        ),
        sa.Column(
            "close_run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("close_runs.id", ondelete="CASCADE"),
            nullable=True,
            comment="Close run scope if the action is period-specific.",
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="User whose message triggered the action.",
        ),
        sa.Column(
            "intent",
            sa.String(60),
            nullable=False,
            comment="Classified action intent (e.g. 'proposed_edit', 'approval_request').",
        ),
        sa.Column(
            "target_type",
            sa.String(120),
            nullable=True,
            comment="Business object type being acted upon.",
        ),
        sa.Column(
            "target_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
            comment="UUID of the business object being acted upon.",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Full structured action plan JSONB including proposed changes.",
        ),
        sa.Column(
            "confidence",
            sa.Numeric(5, 4),
            nullable=False,
            comment="Classifier confidence for the detected intent.",
        ),
        sa.Column(
            "autonomy_mode",
            sa.String(30),
            nullable=False,
            comment="Autonomy mode in effect when the action was detected.",
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="'pending'",
            comment="Review lifecycle state (pending, approved, rejected, superseded, applied).",
        ),
        sa.Column(
            "requires_human_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="Whether explicit human approval is required before applying.",
        ),
        sa.Column(
            "reasoning",
            sa.String(3000),
            nullable=False,
            server_default="''",
            comment="Narrative explanation of the action plan.",
        ),
        sa.Column(
            "applied_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Result payload when the action was applied.",
        ),
        sa.Column(
            "rejected_reason",
            sa.String(500),
            nullable=True,
            comment="Reason provided when the action was rejected.",
        ),
        sa.Column(
            "superseded_by_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("chat_action_plans.id", ondelete="SET NULL"),
            nullable=True,
            comment="ID of the action plan that superseded this one.",
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="chat_action_plans_confidence_range",
        ),
    )
    op.create_index(
        "ix_chat_action_plans_thread_status",
        "chat_action_plans",
        ["thread_id", "status"],
    )
    op.create_index(
        "ix_chat_action_plans_target",
        "chat_action_plans",
        ["target_type", "target_id"],
    )
    op.create_index(
        "ix_chat_action_plans_entity",
        "chat_action_plans",
        ["entity_id", "status"],
    )


def downgrade() -> None:
    """Remove chat_action_plans table and its indexes."""

    op.drop_table("chat_action_plans")
