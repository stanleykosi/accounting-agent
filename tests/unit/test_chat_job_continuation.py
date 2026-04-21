"""
Purpose: Regression coverage for worker-triggered chat continuation after async jobs finish.
Scope: Batch terminal gating, active-thread ownership checks, and continuation cleanup.
Dependencies: Chat job continuation service and lightweight in-memory doubles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from services.chat.continuation_state import (
    build_pending_async_turn_payload,
    embed_continuation_in_checkpoint,
    new_chat_operator_continuation,
)
from services.chat.job_continuation import ChatJobContinuationService
from services.common.enums import JobStatus
from services.jobs.service import JobRecord


def test_continue_for_job_waits_until_every_group_job_is_terminal() -> None:
    """The worker should not resume the chat thread while any owned job is still running."""

    continuation = new_chat_operator_continuation(
        thread_id=uuid4(),
        entity_id=uuid4(),
        actor_user_id=uuid4(),
        objective="Generate recommendations and keep going.",
        originating_tool="generate_recommendations",
        source_surface="desktop",
    )
    completed_job = _build_job_record(
        job_id=uuid4(),
        status=JobStatus.COMPLETED,
        checkpoint_payload=embed_continuation_in_checkpoint(
            checkpoint_payload=None,
            continuation=continuation,
        ),
    )
    running_job = _build_job_record(
        job_id=uuid4(),
        status=JobStatus.RUNNING,
        checkpoint_payload=embed_continuation_in_checkpoint(
            checkpoint_payload=None,
            continuation=continuation,
        ),
    )

    service = ChatJobContinuationService.__new__(ChatJobContinuationService)
    service._job_service = SimpleNamespace(get_job=lambda **kwargs: completed_job)
    service._load_jobs_for_group = lambda **kwargs: (completed_job, running_job)  # type: ignore[method-assign]

    result = service.continue_for_job(
        job_id=completed_job.id,
        dispatch_task_id="resume-task-1",
        trace_id="trace-continue",
    )

    assert result.status == "waiting"
    assert "non-terminal jobs" in result.detail


def test_continue_for_job_resumes_and_clears_the_active_async_turn() -> None:
    """Once every job is terminal, the worker should resume and clear the pending flag."""

    continuation = new_chat_operator_continuation(
        thread_id=uuid4(),
        entity_id=uuid4(),
        actor_user_id=uuid4(),
        objective="Run reconciliation and then keep moving the close forward.",
        originating_tool="run_reconciliation",
        source_surface="desktop",
    )
    terminal_job = _build_job_record(
        job_id=uuid4(),
        status=JobStatus.COMPLETED,
        checkpoint_payload=embed_continuation_in_checkpoint(
            checkpoint_payload=None,
            continuation=continuation,
        ),
    )
    thread_model = SimpleNamespace(
        context_payload=build_pending_async_turn_payload(
            existing_payload={},
            continuation=continuation,
            job_count=1,
            trace_id="trace-pending",
        )
    )
    db_session = _FakeDbSession()

    service = ChatJobContinuationService.__new__(ChatJobContinuationService)
    service._db_session = db_session
    service._job_service = SimpleNamespace(get_job=lambda **kwargs: terminal_job)
    service._load_jobs_for_group = lambda **kwargs: (terminal_job,)  # type: ignore[method-assign]
    service._load_thread_for_update = lambda **kwargs: thread_model  # type: ignore[method-assign]
    service._load_actor_user = lambda **kwargs: SimpleNamespace(id=continuation.actor_user_id)  # type: ignore[method-assign]
    service._executor = SimpleNamespace(
        resume_operator_turn=lambda **kwargs: SimpleNamespace(
            assistant_content="Reconciliation finished. The close can move into reporting.",
            assistant_message_id="msg-1",
        )
    )

    result = service.continue_for_job(
        job_id=terminal_job.id,
        dispatch_task_id="resume-task-2",
        trace_id="trace-continue",
    )

    assert result.status == "resumed"
    assert result.assistant_message_id == "msg-1"
    assert "agent_async_turn" not in thread_model.context_payload
    assert thread_model.context_payload["agent_last_async_turn"]["status"] == "completed"
    assert db_session.commit_calls == 2


def test_continue_for_job_restores_pending_state_when_resume_fails() -> None:
    """A failed resume attempt should put the async turn back into pending state."""

    continuation = new_chat_operator_continuation(
        thread_id=uuid4(),
        entity_id=uuid4(),
        actor_user_id=uuid4(),
        objective="Create the export package and record the final release.",
        originating_tool="generate_export",
        source_surface="desktop",
    )
    terminal_job = _build_job_record(
        job_id=uuid4(),
        status=JobStatus.COMPLETED,
        checkpoint_payload=embed_continuation_in_checkpoint(
            checkpoint_payload=None,
            continuation=continuation,
        ),
    )
    thread_model = SimpleNamespace(
        context_payload=build_pending_async_turn_payload(
            existing_payload={},
            continuation=continuation,
            job_count=1,
            trace_id="trace-pending",
        )
    )
    db_session = _FakeDbSession()

    service = ChatJobContinuationService.__new__(ChatJobContinuationService)
    service._db_session = db_session
    service._job_service = SimpleNamespace(get_job=lambda **kwargs: terminal_job)
    service._load_jobs_for_group = lambda **kwargs: (terminal_job,)  # type: ignore[method-assign]
    service._load_thread_for_update = lambda **kwargs: thread_model  # type: ignore[method-assign]
    service._load_actor_user = lambda **kwargs: SimpleNamespace(id=continuation.actor_user_id)  # type: ignore[method-assign]
    service._executor = SimpleNamespace(
        resume_operator_turn=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("resume failed"))
    )

    with pytest.raises(RuntimeError, match="resume failed"):
        service.continue_for_job(
            job_id=terminal_job.id,
            dispatch_task_id="resume-task-3",
            trace_id="trace-continue",
        )

    async_turn = thread_model.context_payload["agent_async_turn"]
    assert async_turn["status"] == "pending"
    assert async_turn["continuation_group_id"] == str(continuation.continuation_group_id)
    assert async_turn["resume_attempt_count"] == 1
    assert async_turn["last_resume_failure"] == "resume failed"
    assert db_session.commit_calls == 2


def test_continue_for_job_records_blocked_terminal_status_in_history() -> None:
    """Blocked job groups should preserve a blocked async outcome for later recovery guidance."""

    continuation = new_chat_operator_continuation(
        thread_id=uuid4(),
        entity_id=uuid4(),
        actor_user_id=uuid4(),
        objective="Generate recommendations and keep going.",
        originating_tool="generate_recommendations",
        source_surface="desktop",
    )
    blocked_job = _build_job_record(
        job_id=uuid4(),
        status=JobStatus.BLOCKED,
        checkpoint_payload=embed_continuation_in_checkpoint(
            checkpoint_payload=None,
            continuation=continuation,
        ),
    )
    thread_model = SimpleNamespace(
        context_payload=build_pending_async_turn_payload(
            existing_payload={},
            continuation=continuation,
            job_count=1,
            trace_id="trace-pending",
        )
    )
    db_session = _FakeDbSession()

    service = ChatJobContinuationService.__new__(ChatJobContinuationService)
    service._db_session = db_session
    service._job_service = SimpleNamespace(get_job=lambda **kwargs: blocked_job)
    service._load_jobs_for_group = lambda **kwargs: (blocked_job,)  # type: ignore[method-assign]
    service._load_thread_for_update = lambda **kwargs: thread_model  # type: ignore[method-assign]
    service._load_actor_user = lambda **kwargs: SimpleNamespace(id=continuation.actor_user_id)  # type: ignore[method-assign]
    service._executor = SimpleNamespace(
        resume_operator_turn=lambda **kwargs: SimpleNamespace(
            assistant_content=(
                "Recommendation generation is blocked until the source documents "
                "are approved."
            ),
            assistant_message_id="msg-blocked-1",
        )
    )

    result = service.continue_for_job(
        job_id=blocked_job.id,
        dispatch_task_id="resume-task-blocked",
        trace_id="trace-blocked",
    )

    assert result.status == "resumed"
    assert "agent_async_turn" not in thread_model.context_payload
    assert thread_model.context_payload["agent_last_async_turn"]["status"] == "blocked"


def _build_job_record(
    *,
    job_id: UUID,
    status: JobStatus,
    checkpoint_payload: dict[str, object],
) -> JobRecord:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    return JobRecord(
        id=job_id,
        entity_id=uuid4(),
        close_run_id=uuid4(),
        document_id=None,
        actor_user_id=uuid4(),
        canceled_by_user_id=None,
        resumed_from_job_id=None,
        task_name="accounting.recommend_close_run",
        queue_name="accounting",
        routing_key="accounting.recommend_close_run",
        status=status,
        payload={},
        checkpoint_payload=checkpoint_payload,
        result_payload={},
        failure_reason=None,
        failure_details=None,
        blocking_reason=None,
        trace_id=None,
        attempt_count=1,
        retry_count=0,
        max_retries=4,
        started_at=now,
        completed_at=now,
        cancellation_requested_at=None,
        canceled_at=None,
        dead_lettered_at=None,
        created_at=now,
        updated_at=now,
    )


class _FakeDbSession:
    def __init__(self) -> None:
        self.commit_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
