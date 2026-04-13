"""
Purpose: Define chat thread and message persistence models for the grounded
finance copilot experience.
Scope: SQLAlchemy ORM models for chat threads scoped to entity + close run +
period, and messages with role, content type, grounding context, and optional
links to proposed action records.
Dependencies: SQLAlchemy ORM, canonical enums, database base helpers.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from services.db.base import Base, TimestampedModel, UUIDPrimaryKeyMixin
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class ChatThread(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one chat thread grounded to an entity and optional close run.

    Threads are the top-level conversation container. Every thread belongs to
    exactly one entity, and may optionally be scoped to a specific close run
    for period-aware conversation context. The context_payload column stores
    the resolved grounding snapshot (entity metadata, close run summary,
    active phase, autonomy mode) at thread creation time so that later model
    responses can reference a consistent accounting context.
    """

    __tablename__ = "chat_threads"
    __table_args__ = (
        Index("ix_chat_threads_entity_close_run", "entity_id", "close_run_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Entity workspace that owns this conversation thread.",
    )
    close_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("close_runs.id", ondelete="SET NULL"),
        nullable=True,
        comment="Optional close run that scopes this thread to a specific accounting period.",
    )
    title: Mapped[str | None] = mapped_column(
        String(300),
        nullable=True,
        comment="Human-readable thread title auto-generated or user-edited.",
    )
    context_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Grounding context snapshot (entity, close run, period, autonomy mode).",
    )


class ChatMessage(Base, UUIDPrimaryKeyMixin, TimestampedModel):
    """Persist one message inside a chat thread with role, content, and metadata.

    Messages capture user questions, assistant responses, and system markers.
    The message_type column classifies the intent (analysis, workflow, action,
    warning) so that UI surfaces can render different visual treatments. The
    grounding_payload stores the entity/close-run evidence snapshot used to
    generate an assistant response, and model_metadata captures the model name,
    token usage, and latency for observability.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="ck_chat_messages_role",
        ),
        CheckConstraint(
            "message_type IN ('analysis', 'workflow', 'action', 'warning')",
            name="ck_chat_messages_message_type",
        ),
        Index("ix_chat_messages_thread_order", "thread_id", "created_at"),
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
        comment="Parent chat thread that this message belongs to.",
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Message originator: user, assistant, or system.",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Message text content (Markdown-supported for assistant messages).",
    )
    message_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="analysis",
        comment="Intent classification: analysis, workflow, action, or warning.",
    )
    linked_action_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("recommendations.id", ondelete="SET NULL"),
        nullable=True,
        comment="Optional reference to a recommendation created or discussed in this message.",
    )
    grounding_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Evidence snapshot (documents, extractions, rules) used to generate the response.",
    )
    model_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Model name, token usage, latency, and provider metadata for assistant messages.",
    )


__all__ = [
    "ChatMessage",
    "ChatThread",
]
