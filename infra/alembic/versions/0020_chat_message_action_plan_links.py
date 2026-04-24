"""
Purpose: Point chat message action links at chat action plans.
Scope: Replace the historical recommendation foreign key on chat_messages.linked_action_id
with the canonical chat_action_plans foreign key used by the operator runtime.
Dependencies: Alembic and the existing chat_messages/chat_action_plans tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision = "0020_chat_message_action_plan_links"
down_revision = "0019_chat_message_ordering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Replace the recommendation FK with the canonical chat action plan FK."""

    op.drop_constraint(
        "fk_chat_messages_linked_action_id_recommendations",
        "chat_messages",
        type_="foreignkey",
    )
    op.execute(
        """
        UPDATE chat_messages AS cm
        SET linked_action_id = NULL
        WHERE cm.linked_action_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM chat_action_plans AS cap
              WHERE cap.id = cm.linked_action_id
          )
        """
    )
    op.create_foreign_key(
        "fk_chat_messages_linked_action_id_chat_action_plans",
        "chat_messages",
        "chat_action_plans",
        ["linked_action_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(
        "chat_messages",
        "linked_action_id",
        existing_type=sa.Uuid(as_uuid=True),
        comment="Optional reference to the chat action plan created or discussed in this message.",
        existing_nullable=True,
    )


def downgrade() -> None:
    """Restore the historical recommendation FK."""

    op.drop_constraint(
        "fk_chat_messages_linked_action_id_chat_action_plans",
        "chat_messages",
        type_="foreignkey",
    )
    op.execute(
        """
        UPDATE chat_messages AS cm
        SET linked_action_id = NULL
        WHERE cm.linked_action_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM recommendations AS recommendation
              WHERE recommendation.id = cm.linked_action_id
          )
        """
    )
    op.create_foreign_key(
        "fk_chat_messages_linked_action_id_recommendations",
        "chat_messages",
        "recommendations",
        ["linked_action_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(
        "chat_messages",
        "linked_action_id",
        existing_type=sa.Uuid(as_uuid=True),
        comment="Optional reference to a recommendation discussed in this message.",
        existing_nullable=True,
    )
