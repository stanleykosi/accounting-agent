"""
Purpose: Define the database model for chat-originated proposed changes
and action execution plans.
Scope: Persist proposed edits, approval requests, document requests, and
other chat-initiated action plans with their review lifecycle state.
Dependencies: SQLAlchemy ORM, canonical enums, database base helpers.

Design notes:
- Every proposed change is attributable: actor, thread, message, autonomy mode.
- The payload column stores the structured action plan as JSONB for flexibility.
- The status column drives the review lifecycle (pending → approved/rejected/applied).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin
from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class ChatActionPlan(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one chat-originated action execution plan.

    When a user sends a message that contains an action intent (proposed edit,
    approval request, document request, etc.), this table stores the structured
    plan so it can flow through the review pipeline independently of the chat
    thread history.

    The payload column stores the full action plan JSONB including the classified
    intent, proposed changes, evidence references, and autonomy mode context.
    Top-level columns provide indexed surfaces for review queues and audit queries.
    """

    __tablename__ = "chat_action_plans"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="chat_action_plans_confidence_range",
        ),
        Index("ix_chat_action_plans_thread_status", "thread_id", "status"),
        Index("ix_chat_action_plans_target", "target_type", "target_id"),
        Index("ix_chat_action_plans_entity", "entity_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    thread_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Chat thread where this action originated.",
    )
    message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
        comment="Message that triggered the action, if attributable.",
    )
    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Entity workspace that owns this action plan.",
    )
    close_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("close_runs.id", ondelete="CASCADE"),
        nullable=True,
        comment="Close run scope if the action is period-specific.",
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="User whose message triggered the action.",
    )
    intent: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        comment="Classified action intent (e.g. 'proposed_edit', 'approval_request').",
    )
    target_type: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        comment="Business object type being acted upon.",
    )
    target_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="UUID of the business object being acted upon.",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Full structured action plan JSONB including proposed changes.",
    )
    confidence: Mapped[float] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        comment="Classifier confidence for the detected intent.",
    )
    autonomy_mode: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="Autonomy mode in effect when the action was detected.",
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="pending",
        comment="Review lifecycle state (pending, approved, rejected, superseded, applied).",
    )
    requires_human_approval: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        comment="Whether explicit human approval is required before applying.",
    )
    reasoning: Mapped[str] = mapped_column(
        String(3000),
        nullable=False,
        default="",
        comment="Narrative explanation of the action plan.",
    )
    applied_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Result payload when the action was applied (e.g. journal_id created).",
    )
    rejected_reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Reason provided when the action was rejected.",
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_action_plans.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID of the action plan that superseded this one.",
    )


__all__ = [
    "ChatActionPlan",
]
