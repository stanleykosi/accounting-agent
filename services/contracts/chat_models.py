"""
Purpose: Define strict API contracts for chat threads, messages, and
grounded finance copilot interactions.
Scope: Thread creation/list/read, message history, and send-message
request/response payloads for read-only analysis flows.
Dependencies: Pydantic contract defaults, shared numeric helpers, and
the canonical enum definitions for chat message types.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator
from services.contracts.api_models import ContractModel

CHAT_MESSAGE_ROLES = ("user", "assistant", "system")
CHAT_MESSAGE_TYPES = ("analysis", "workflow", "action", "warning")


class GroundingContext(ContractModel):
    """Describe the entity, close run, and period context that grounds a chat thread."""

    entity_id: str = Field(
        min_length=1,
        description="UUID of the entity workspace that owns this conversation.",
    )
    entity_name: str = Field(
        min_length=1,
        description="Display name of the entity workspace.",
    )
    close_run_id: str | None = Field(
        default=None,
        description="UUID of the close run scoping this thread, if period-specific.",
    )
    period_label: str | None = Field(
        default=None,
        description="Human-readable period label (e.g. 'Jan 2025') when a close run is present.",
    )
    autonomy_mode: str = Field(
        min_length=1,
        description="Current autonomy mode for the entity (human_review or reduced_interruption).",
    )
    base_currency: str = Field(
        min_length=3,
        max_length=3,
        description="Entity base currency used for formatting context.",
    )


class ChatThreadSummary(ContractModel):
    """Describe one chat thread for list and overview surfaces."""

    id: str = Field(description="Stable UUID for the chat thread.")
    entity_id: str = Field(description="Entity workspace that owns this thread.")
    close_run_id: str | None = Field(
        default=None,
        description="Close run scoping this thread, if period-specific.",
    )
    title: str | None = Field(
        default=None,
        description="Thread title shown in the conversation list.",
    )
    grounding: GroundingContext = Field(
        description="Entity and close run context that grounds this thread.",
    )
    message_count: int = Field(
        ge=0,
        description="Total number of messages in the thread.",
    )
    last_message_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the most recent message in the thread.",
    )
    created_at: datetime = Field(description="UTC timestamp when the thread was created.")
    updated_at: datetime = Field(description="UTC timestamp when the thread was last updated.")


class ChatMessageRecord(ContractModel):
    """Describe one message persisted inside a chat thread."""

    id: str = Field(description="Stable UUID for the chat message.")
    thread_id: str = Field(description="Parent chat thread that this message belongs to.")
    role: Literal["user", "assistant", "system"] = Field(
        description="Message originator: user, assistant, or system.",
    )
    content: str = Field(
        min_length=1,
        description="Message text content (Markdown for assistant messages).",
    )
    message_type: Literal["analysis", "workflow", "action", "warning"] = Field(
        default="analysis",
        description="Intent classification used for UI rendering.",
    )
    linked_action_id: str | None = Field(
        default=None,
        description="Optional reference to a recommendation discussed in this message.",
    )
    grounding_payload: dict[str, object] = Field(
        default_factory=dict,
        description="Evidence snapshot attached to assistant messages.",
    )
    model_metadata: dict[str, object] | None = Field(
        default=None,
        description="Model name, token usage, and latency for assistant messages.",
    )
    created_at: datetime = Field(description="UTC timestamp when the message was created.")


class CreateChatThreadRequest(ContractModel):
    """Capture the inputs required to create a new grounded chat thread."""

    entity_id: str = Field(
        min_length=1,
        description="Entity workspace UUID that will own this conversation.",
    )
    close_run_id: str | None = Field(
        default=None,
        description="Optional close run UUID to scope the thread to a specific period.",
    )
    title: str | None = Field(
        default=None,
        max_length=300,
        description="Optional user-provided or auto-derived thread title.",
    )

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        """Trim thread titles and collapse blank values to null."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_title_if_close_run_scoped(self) -> CreateChatThreadRequest:
        """Require a non-blank title when the thread is scoped to a close run."""

        if self.close_run_id is not None and (
            self.title is None or not self.title.strip()
        ):
            message = "Provide a title when creating a close-run-scoped thread."
            raise ValueError(message)

        return self


class SendChatMessageRequest(ContractModel):
    """Capture a user message to be sent to an existing chat thread."""

    content: str = Field(
        min_length=1,
        max_length=10_000,
        description="User question or instruction text.",
    )

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        """Reject whitespace-only user messages before they reach the service layer."""

        normalized = value.strip()
        if not normalized:
            message = "Message content cannot be blank."
            raise ValueError(message)

        return normalized


class ChatThreadWithMessages(ContractModel):
    """Return a full chat thread with its message history for detail views."""

    thread: ChatThreadSummary = Field(description="Thread summary with grounding context.")
    messages: tuple[ChatMessageRecord, ...] = Field(
        default=(),
        description="Messages ordered chronologically (oldest first).",
    )


class ChatMessageResponse(ContractModel):
    """Return the assistant response generated for a user message."""

    message: ChatMessageRecord = Field(
        description="The newly created assistant message with grounding payload.",
    )
    user_message: ChatMessageRecord | None = Field(
        default=None,
        description="Echo of the persisted user message when sent in the same response.",
    )


class ChatThreadListResponse(ContractModel):
    """Return the threads available for an entity or close run."""

    threads: tuple[ChatThreadSummary, ...] = Field(
        default=(),
        description="Threads ordered newest-first for the specified scope.",
    )


__all__ = [
    "CHAT_MESSAGE_ROLES",
    "CHAT_MESSAGE_TYPES",
    "ChatMessageRecord",
    "ChatMessageResponse",
    "ChatThreadListResponse",
    "ChatThreadSummary",
    "ChatThreadWithMessages",
    "CreateChatThreadRequest",
    "GroundingContext",
    "SendChatMessageRequest",
]
