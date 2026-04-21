"""
Purpose: Resume chat-owned operator turns after long-running background job
groups reach a terminal state.
Scope: Worker-side continuation of async chat objectives through the canonical
chat executor runtime.
Dependencies: Celery worker app, chat continuation service, and canonical task routing.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from apps.api.app.dependencies.tasks import TaskDispatcher
from apps.worker.app.celery_app import ObservedTask, celery_app
from services.chat.job_continuation import ChatJobContinuationResult, ChatJobContinuationService
from services.db.session import get_session_factory
from services.jobs.task_names import TaskName, resolve_task_route
from services.observability.context import current_trace_metadata


def _run_resume_chat_operator(
    self: ObservedTask,
    *,
    job_id: str,
) -> dict[str, Any]:
    """Resume one chat-owned operator turn after its async job group settles."""

    with get_session_factory()() as db_session:
        service = ChatJobContinuationService(
            db_session=db_session,
            task_dispatcher=TaskDispatcher(celery_app=celery_app),
        )
        try:
            result = service.continue_for_job(
                job_id=UUID(job_id),
                dispatch_task_id=str(self.request.id),
                trace_id=current_trace_metadata().trace_id,
            )
        except Exception as error:
            raise RuntimeError("Chat operator continuation could not be completed.") from error
    return _serialize_result(result)


def _serialize_result(result: ChatJobContinuationResult) -> dict[str, Any]:
    """Render one continuation outcome into a JSON-safe worker task payload."""

    return {
        "status": result.status,
        "detail": result.detail,
        "assistant_message_id": result.assistant_message_id,
    }


resume_chat_operator = celery_app.task(
    bind=True,
    base=ObservedTask,
    name=TaskName.CHAT_RESUME_OPERATOR_TURN.value,
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=resolve_task_route(TaskName.CHAT_RESUME_OPERATOR_TURN).max_retries,
)(_run_resume_chat_operator)


__all__ = ["resume_chat_operator"]
