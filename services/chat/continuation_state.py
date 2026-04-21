"""
Purpose: Provide the canonical metadata and thread-state helpers for
chat-owned asynchronous operator continuations.
Scope: Job checkpoint payload metadata, active async-turn thread context,
and fail-fast parsing for worker-triggered chat resume flows.
Dependencies: Shared JSON types plus Python dataclasses and UUID handling only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from services.common.types import JsonObject, utc_now

_JOB_CHECKPOINT_KEY = "chat_operator_continuation"
_THREAD_STATE_KEY = "agent_async_turn"
_LAST_THREAD_STATE_KEY = "agent_last_async_turn"


@dataclass(frozen=True, slots=True)
class ChatOperatorContinuation:
    """Describe one chat-owned async continuation group attached to background jobs."""

    continuation_group_id: UUID
    thread_id: UUID
    entity_id: UUID
    actor_user_id: UUID
    objective: str
    originating_tool: str
    source_surface: str

    def to_payload(self) -> JsonObject:
        """Render the continuation metadata into a JSON-safe checkpoint payload."""

        return {
            "continuation_group_id": str(self.continuation_group_id),
            "thread_id": str(self.thread_id),
            "entity_id": str(self.entity_id),
            "actor_user_id": str(self.actor_user_id),
            "objective": self.objective,
            "originating_tool": self.originating_tool,
            "source_surface": self.source_surface,
        }


def new_chat_operator_continuation(
    *,
    thread_id: UUID,
    entity_id: UUID,
    actor_user_id: UUID,
    objective: str,
    originating_tool: str,
    source_surface: str,
) -> ChatOperatorContinuation:
    """Create one canonical async continuation group for a chat-owned job wave."""

    return ChatOperatorContinuation(
        continuation_group_id=uuid4(),
        thread_id=thread_id,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        objective=objective.strip(),
        originating_tool=originating_tool,
        source_surface=source_surface,
    )


def embed_continuation_in_checkpoint(
    *,
    checkpoint_payload: JsonObject | None,
    continuation: ChatOperatorContinuation,
) -> JsonObject:
    """Attach continuation metadata to one job checkpoint payload."""

    return {
        **dict(checkpoint_payload or {}),
        _JOB_CHECKPOINT_KEY: continuation.to_payload(),
    }


def parse_checkpoint_continuation(
    *,
    checkpoint_payload: dict[str, Any],
) -> ChatOperatorContinuation | None:
    """Parse one job checkpoint continuation payload or return None when absent."""

    raw_payload = checkpoint_payload.get(_JOB_CHECKPOINT_KEY)
    if not isinstance(raw_payload, dict):
        return None

    try:
        return ChatOperatorContinuation(
            continuation_group_id=UUID(str(raw_payload["continuation_group_id"])),
            thread_id=UUID(str(raw_payload["thread_id"])),
            entity_id=UUID(str(raw_payload["entity_id"])),
            actor_user_id=UUID(str(raw_payload["actor_user_id"])),
            objective=str(raw_payload["objective"]).strip(),
            originating_tool=str(raw_payload["originating_tool"]).strip(),
            source_surface=str(raw_payload["source_surface"]).strip(),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            "Job continuation metadata is malformed. Re-dispatch the job through the "
            "canonical chat operator path."
        ) from error


def build_pending_async_turn_payload(
    *,
    existing_payload: dict[str, Any],
    continuation: ChatOperatorContinuation,
    job_count: int,
    trace_id: str | None,
) -> dict[str, Any]:
    """Return thread context payload with one active pending async continuation."""

    updated_payload = dict(existing_payload)
    prior_turn = updated_payload.get(_THREAD_STATE_KEY)
    if isinstance(prior_turn, dict):
        updated_payload[_LAST_THREAD_STATE_KEY] = {
            **prior_turn,
            "status": "superseded",
            "superseded_at": utc_now().isoformat(),
        }

    updated_payload[_THREAD_STATE_KEY] = {
        **continuation.to_payload(),
        "job_count": job_count,
        "status": "pending",
        "resume_attempt_count": 0,
        "last_resume_failure": None,
        "last_resume_failure_at": None,
        "trace_id": trace_id,
        "activated_at": utc_now().isoformat(),
    }
    return updated_payload


def build_resuming_async_turn_payload(
    *,
    existing_payload: dict[str, Any],
    continuation_group_id: UUID,
    dispatch_task_id: str,
    trace_id: str | None,
) -> dict[str, Any]:
    """Mark one pending async continuation as actively resuming in the thread context."""

    updated_payload = dict(existing_payload)
    raw_turn = updated_payload.get(_THREAD_STATE_KEY)
    if not isinstance(raw_turn, dict):
        raise RuntimeError(
            "Thread is missing the active async continuation state required for chat resume."
        )
    if str(raw_turn.get("continuation_group_id") or "") != str(continuation_group_id):
        raise RuntimeError(
            "Thread async continuation state does not match the completed job group."
        )

    updated_payload[_THREAD_STATE_KEY] = {
        **raw_turn,
        "status": "resuming",
        "resume_dispatch_task_id": dispatch_task_id,
        "resume_trace_id": trace_id,
        "resuming_at": utc_now().isoformat(),
    }
    return updated_payload


def clear_async_turn_payload(
    *,
    existing_payload: dict[str, Any],
    continuation_group_id: UUID,
    final_status: str,
    trace_id: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Move the active async continuation into compact history and clear it from the thread."""

    updated_payload = dict(existing_payload)
    raw_turn = updated_payload.get(_THREAD_STATE_KEY)
    if not isinstance(raw_turn, dict):
        return updated_payload
    if str(raw_turn.get("continuation_group_id") or "") != str(continuation_group_id):
        return updated_payload

    updated_payload[_LAST_THREAD_STATE_KEY] = {
        **raw_turn,
        "status": final_status,
        "final_trace_id": trace_id,
        "final_note": note,
        "completed_at": utc_now().isoformat(),
    }
    updated_payload.pop(_THREAD_STATE_KEY, None)
    return updated_payload


def restore_pending_async_turn_payload(
    *,
    existing_payload: dict[str, Any],
    continuation_group_id: UUID,
    trace_id: str | None,
    note: str | None = None,
) -> dict[str, Any]:
    """Return thread context with one resuming async turn moved back to pending."""

    updated_payload = dict(existing_payload)
    raw_turn = updated_payload.get(_THREAD_STATE_KEY)
    if not isinstance(raw_turn, dict):
        return updated_payload
    if str(raw_turn.get("continuation_group_id") or "") != str(continuation_group_id):
        return updated_payload

    updated_payload[_THREAD_STATE_KEY] = {
        **raw_turn,
        "status": "pending",
        "resume_attempt_count": int(raw_turn.get("resume_attempt_count") or 0) + 1,
        "last_resume_failure": note,
        "last_resume_failure_at": utc_now().isoformat() if note is not None else None,
        "resume_trace_id": trace_id,
        "resume_dispatch_task_id": None,
        "resuming_at": None,
    }
    return updated_payload


def get_active_async_turn(
    *,
    context_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the active async-turn payload when one exists."""

    raw_turn = context_payload.get(_THREAD_STATE_KEY)
    return dict(raw_turn) if isinstance(raw_turn, dict) else None


__all__ = [
    "ChatOperatorContinuation",
    "build_pending_async_turn_payload",
    "build_resuming_async_turn_payload",
    "clear_async_turn_payload",
    "embed_continuation_in_checkpoint",
    "get_active_async_turn",
    "new_chat_operator_continuation",
    "parse_checkpoint_continuation",
    "restore_pending_async_turn_payload",
]
