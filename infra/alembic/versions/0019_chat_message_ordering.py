"""
Purpose: Add canonical per-thread message ordering for chat transcripts.
Scope: Backfill deterministic message sequence numbers so user/assistant turns
cannot swap when database timestamps collide.
Dependencies: Alembic, PostgreSQL window functions, and the chat_messages table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision = "0019_chat_message_ordering"
down_revision = "0018_imported_gl_transaction_group_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add and backfill the canonical per-thread message order column."""

    op.add_column(
        "chat_messages",
        sa.Column(
            "message_order",
            sa.Integer(),
            nullable=True,
            comment=(
                "Canonical per-thread message sequence used for deterministic "
                "conversation ordering."
            ),
        ),
    )
    op.execute(
        """
        WITH ordered_messages AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY thread_id
                    ORDER BY created_at ASC, id ASC
                ) AS message_order
            FROM chat_messages
        )
        UPDATE chat_messages
        SET message_order = ordered_messages.message_order
        FROM ordered_messages
        WHERE chat_messages.id = ordered_messages.id
        """
    )
    op.alter_column("chat_messages", "message_order", nullable=False)
    op.create_unique_constraint(
        "uq_chat_messages_thread_message_order",
        "chat_messages",
        ["thread_id", "message_order"],
    )
    op.drop_index("ix_chat_messages_thread_order", table_name="chat_messages")
    op.create_index(
        "ix_chat_messages_thread_order",
        "chat_messages",
        ["thread_id", "message_order"],
    )


def downgrade() -> None:
    """Remove canonical per-thread message ordering from chat transcripts."""

    op.drop_index("ix_chat_messages_thread_order", table_name="chat_messages")
    op.create_index(
        "ix_chat_messages_thread_order",
        "chat_messages",
        ["thread_id", "created_at"],
    )
    op.drop_constraint(
        "uq_chat_messages_thread_message_order",
        "chat_messages",
        type_="unique",
    )
    op.drop_column("chat_messages", "message_order")
