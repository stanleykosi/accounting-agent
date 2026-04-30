"""
Purpose: Execute first-class chat operator turns from the control worker queue.
Scope: Durable background execution for user-submitted chat turns acknowledged by
the API before planner/model/tool work completes.
Dependencies: Celery worker app, canonical chat executor factory, and entity access.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from apps.worker.app.celery_runtime import celery_app
from apps.worker.app.tasks.base import JobRuntimeContext, TrackedJobTask
from services.chat.job_continuation import _build_chat_action_executor
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.entity_repo import EntityRepository
from services.db.session import get_session_factory
from services.jobs.dispatcher import TaskDispatcher
from services.jobs.retry_policy import BlockedJobError
from services.jobs.task_names import TaskName, resolve_task_route
from services.observability.context import current_trace_metadata


def _run_execute_chat_operator(
    *,
    thread_id: str,
    entity_id: str,
    actor_user_id: str,
    content: str,
    job_context: JobRuntimeContext,
    client_turn_id: str | None = None,
    message_grounding_payload: dict[str, Any] | None = None,
    operator_message_for_memory: str | None = None,
    user_message_content: str | None = None,
    persist_user_message: bool = True,
    process_existing_user_turn: bool = False,
) -> dict[str, Any]:
    """Execute one accepted chat operator turn and persist its assistant reply."""

    job_context.ensure_not_canceled()
    with get_session_factory()() as db_session:
        entity_repo = EntityRepository(db_session=db_session)
        access = entity_repo.get_entity_for_user(
            entity_id=UUID(entity_id),
            user_id=UUID(actor_user_id),
        )
        if access is None:
            raise BlockedJobError(
                "Chat operator turn could not load the originating actor membership.",
                details={
                    "entity_id": entity_id,
                    "actor_user_id": actor_user_id,
                    "code": "access_denied",
                },
            )

        executor = _build_chat_action_executor(
            db_session=db_session,
            task_dispatcher=TaskDispatcher(celery_app=celery_app, source_surface="worker"),
        )
        outcome = executor.send_action_message(
            thread_id=UUID(thread_id),
            entity_id=UUID(entity_id),
            actor_user=access.membership.user,
            content=content,
            client_turn_id=client_turn_id,
            message_grounding_payload=message_grounding_payload,
            operator_message_for_memory=operator_message_for_memory,
            user_message_content=user_message_content,
            persist_user_message=persist_user_message,
            process_existing_user_turn=process_existing_user_turn,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=current_trace_metadata().trace_id,
        )
    return {
        "assistant_message_id": outcome.assistant_message_id,
        "thread_entity_id": outcome.thread_entity_id,
        "thread_close_run_id": outcome.thread_close_run_id,
        "job_id": str(job_context.job_record.id),
        "status": "completed",
    }


@celery_app.task(
    bind=True,
    base=TrackedJobTask,
    name=TaskName.CHAT_EXECUTE_OPERATOR_TURN.value,
    autoretry_for=(),
    retry_backoff=False,
    retry_jitter=False,
    max_retries=resolve_task_route(TaskName.CHAT_EXECUTE_OPERATOR_TURN).max_retries,
)
def execute_chat_operator(
    self: TrackedJobTask,
    *,
    thread_id: str,
    entity_id: str,
    actor_user_id: str,
    content: str,
    client_turn_id: str | None = None,
    message_grounding_payload: dict[str, Any] | None = None,
    operator_message_for_memory: str | None = None,
    user_message_content: str | None = None,
    persist_user_message: bool = True,
    process_existing_user_turn: bool = False,
) -> dict[str, Any]:
    """Execute one accepted chat operator turn under durable job tracking."""

    return self.run_tracked_job(
        runner=lambda job_context: _run_execute_chat_operator(
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user_id=actor_user_id,
            content=content,
            client_turn_id=client_turn_id,
            message_grounding_payload=message_grounding_payload,
            operator_message_for_memory=operator_message_for_memory,
            user_message_content=user_message_content,
            persist_user_message=persist_user_message,
            process_existing_user_turn=process_existing_user_turn,
            job_context=job_context,
        )
    )


__all__ = ["execute_chat_operator"]
