"""
Purpose: Resume chat-owned operator turns after long-running background jobs
finish in the worker.
Scope: Batch terminal-state gating, duplicate-resume prevention, thread-state
coordination, and automatic re-entry into the chat operator runtime.
Dependencies: Chat executor, chat/thread persistence, entity membership reads,
and durable job lifecycle records.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from services.accounting.recommendation_apply import RecommendationApplyService
from services.audit.service import AuditService
from services.chat.action_execution import ChatActionExecutor
from services.chat.continuation_state import (
    build_resuming_async_turn_payload,
    clear_async_turn_payload,
    get_active_async_turn,
    parse_checkpoint_continuation,
    restore_pending_async_turn_payload,
)
from services.chat.grounding import ChatGroundingService
from services.close_runs.delete_service import CloseRunDeleteService
from services.close_runs.service import CloseRunService
from services.db.models.chat import ChatThread
from services.db.models.jobs import Job
from services.db.repositories.chat_action_repo import ChatActionRepository
from services.db.repositories.chat_repo import ChatRepository
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.document_repo import DocumentRepository
from services.db.repositories.entity_repo import EntityRepository, EntityUserRecord
from services.db.repositories.integration_repo import IntegrationRepository
from services.db.repositories.recommendation_journal_repo import RecommendationJournalRepository
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.repositories.report_repo import ReportRepository
from services.db.session import get_session_factory
from services.documents.review_service import DocumentReviewService
from services.entity.delete_service import EntityDeleteService
from services.entity.service import EntityService
from services.exports.service import ExportService
from services.jobs.service import JobRecord, JobService
from services.model_gateway.client import ModelGateway
from services.reconciliation.service import ReconciliationService
from services.reporting.service import ReportService
from services.storage.repository import StorageRepository
from sqlalchemy import select
from sqlalchemy.orm import Session

_TERMINAL_JOB_STATUSES = frozenset({"completed", "blocked", "failed", "canceled"})


@dataclass(frozen=True, slots=True)
class ChatJobContinuationResult:
    """Describe the outcome of one worker-triggered chat continuation attempt."""

    status: str
    detail: str
    assistant_message_id: str | None = None


class ChatJobContinuationService:
    """Own the canonical worker-side continuation of chat-owned async job groups."""

    def __init__(self, *, db_session: Session, task_dispatcher: object) -> None:
        self._db_session = db_session
        self._task_dispatcher = task_dispatcher
        self._entity_repo = EntityRepository(db_session=db_session)
        self._chat_repo = ChatRepository(db_session=db_session)
        self._job_service = JobService(db_session=db_session)
        self._executor = _build_chat_action_executor(
            db_session=db_session,
            task_dispatcher=task_dispatcher,
        )

    def continue_for_job(
        self,
        *,
        job_id: UUID,
        dispatch_task_id: str,
        trace_id: str | None,
    ) -> ChatJobContinuationResult:
        """Resume one chat-owned async operator group when its full batch is terminal."""

        job_record = self._job_service.get_job(job_id=job_id)
        continuation = parse_checkpoint_continuation(
            checkpoint_payload=job_record.checkpoint_payload,
        )
        if continuation is None:
            return ChatJobContinuationResult(
                status="ignored",
                detail="Job is not owned by a chat continuation group.",
            )

        grouped_jobs = self._load_jobs_for_group(
            continuation_group_id=continuation.continuation_group_id,
        )
        if not grouped_jobs:
            raise RuntimeError(
                "Chat continuation group lookup returned no jobs for a job that carries "
                "continuation metadata."
            )
        if not self._all_jobs_terminal(grouped_jobs):
            return ChatJobContinuationResult(
                status="waiting",
                detail="The continuation group still has non-terminal jobs.",
            )

        locked_thread = self._load_thread_for_update(thread_id=continuation.thread_id)
        if locked_thread is None:
            return ChatJobContinuationResult(
                status="ignored",
                detail="Chat thread no longer exists.",
            )

        active_turn = get_active_async_turn(context_payload=dict(locked_thread.context_payload))
        if active_turn is None:
            return ChatJobContinuationResult(
                status="ignored",
                detail="Thread no longer has an active async operator turn.",
            )
        if str(active_turn.get("continuation_group_id") or "") != str(
            continuation.continuation_group_id
        ):
            return ChatJobContinuationResult(
                status="superseded",
                detail="A newer async operator group replaced this one.",
            )
        if str(active_turn.get("status") or "") == "resuming":
            return ChatJobContinuationResult(
                status="ignored",
                detail="This continuation group is already resuming.",
            )

        locked_thread.context_payload = build_resuming_async_turn_payload(
            existing_payload=dict(locked_thread.context_payload),
            continuation_group_id=continuation.continuation_group_id,
            dispatch_task_id=dispatch_task_id,
            trace_id=trace_id,
        )
        self._db_session.commit()

        actor_user = self._load_actor_user(
            entity_id=continuation.entity_id,
            actor_user_id=continuation.actor_user_id,
        )

        try:
            outcome = self._executor.resume_operator_turn(
                thread_id=continuation.thread_id,
                entity_id=continuation.entity_id,
                actor_user=actor_user,
                objective=continuation.objective,
                completed_jobs=grouped_jobs,
                source_surface=_coerce_source_surface(continuation.source_surface),
                trace_id=trace_id,
            )
        except Exception as error:
            locked_thread = self._load_thread_for_update(thread_id=continuation.thread_id)
            if locked_thread is not None:
                locked_thread.context_payload = restore_pending_async_turn_payload(
                    existing_payload=dict(locked_thread.context_payload),
                    continuation_group_id=continuation.continuation_group_id,
                    trace_id=trace_id,
                    note=str(error),
                )
                self._db_session.commit()
            raise

        locked_thread = self._load_thread_for_update(thread_id=continuation.thread_id)
        if locked_thread is not None:
            final_status = _derive_continuation_terminal_status(grouped_jobs)
            locked_thread.context_payload = clear_async_turn_payload(
                existing_payload=dict(locked_thread.context_payload),
                continuation_group_id=continuation.continuation_group_id,
                final_status=final_status,
                trace_id=trace_id,
                note=outcome.assistant_content,
            )
            self._db_session.commit()

        return ChatJobContinuationResult(
            status="resumed",
            detail="The chat operator turn resumed successfully after background completion.",
            assistant_message_id=outcome.assistant_message_id,
        )

    def _load_jobs_for_group(
        self,
        *,
        continuation_group_id: UUID,
    ) -> tuple[JobRecord, ...]:
        """Return all durable jobs owned by one continuation group."""

        statement = (
            select(Job)
            .where(
                Job.checkpoint_payload.contains(
                    {
                        "chat_operator_continuation": {
                            "continuation_group_id": str(continuation_group_id)
                        }
                    }
                )
            )
            .order_by(Job.created_at.asc(), Job.id.asc())
        )
        return tuple(
            self._job_service.get_job(job_id=job.id)
            for job in self._db_session.execute(statement).scalars().all()
        )

    def _all_jobs_terminal(self, jobs: tuple[JobRecord, ...]) -> bool:
        """Return whether every job in the continuation group is terminal."""

        return all(job.status.value in _TERMINAL_JOB_STATUSES for job in jobs)

    def _load_thread_for_update(self, *, thread_id: UUID) -> ChatThread | None:
        """Return one chat thread row with a write lock for continuation-state mutation."""

        statement = select(ChatThread).where(ChatThread.id == thread_id).with_for_update()
        return self._db_session.execute(statement).scalar_one_or_none()

    def _load_actor_user(self, *, entity_id: UUID, actor_user_id: UUID) -> EntityUserRecord:
        """Return the entity-scoped actor required to continue the operator turn."""

        access = self._entity_repo.get_entity_for_user(
            entity_id=entity_id,
            user_id=actor_user_id,
        )
        if access is None:
            raise RuntimeError(
                "Chat continuation could not load the originating actor in the workspace."
            )
        return access.membership.user


def _build_chat_action_executor(
    *,
    db_session: Session,
    task_dispatcher: object,
) -> ChatActionExecutor:
    """Construct the canonical chat action executor for worker-side continuation."""

    entity_repo = EntityRepository(db_session=db_session)
    close_run_repo = CloseRunRepository(db_session=db_session)
    grounding_service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )
    chat_repo = ChatRepository(db_session=db_session)
    action_repo = ChatActionRepository(db_session=db_session)
    document_repo = DocumentRepository(db_session=db_session)
    report_repo = ReportRepository(db_session=db_session)
    recommendation_repo = RecommendationJournalRepository(db_session=db_session)
    job_service = JobService(db_session=db_session)
    storage_repository = StorageRepository()
    audit_service = AuditService(db_session=db_session)
    export_service = ExportService(
        db_session=db_session,
        report_repository=report_repo,
    )
    close_run_service = CloseRunService(repository=close_run_repo)

    return ChatActionExecutor(
        db_session=db_session,
        chat_repository=chat_repo,
        action_repository=action_repo,
        grounding_service=grounding_service,
        entity_repository=entity_repo,
        close_run_service=close_run_service,
        close_run_delete_service=CloseRunDeleteService(
            repository=close_run_repo,
            storage_repository=storage_repository,
            job_service=job_service,
        ),
        close_run_repository=close_run_repo,
        document_review_service=DocumentReviewService(
            db_session=db_session,
            repository=document_repo,
        ),
        document_repository=document_repo,
        entity_service=EntityService(repository=entity_repo),
        entity_delete_service=EntityDeleteService(
            repository=entity_repo,
            storage_repository=storage_repository,
            job_service=job_service,
        ),
        recommendation_service=RecommendationApplyService(
            repository=recommendation_repo,
            audit_service=audit_service,
            db_session=db_session,
            integration_repository=IntegrationRepository(db_session=db_session),
            storage_repository=storage_repository,
        ),
        recommendation_repository=recommendation_repo,
        reconciliation_service=ReconciliationService(
            repository=ReconciliationRepository(session=db_session),
        ),
        reconciliation_repository=ReconciliationRepository(session=db_session),
        report_service=ReportService(
            repository=report_repo,
        ),
        report_repository=report_repo,
        export_service=export_service,
        model_gateway=ModelGateway(),
        job_service=job_service,
        task_dispatcher=task_dispatcher,
    )


def _derive_continuation_terminal_status(jobs: tuple[JobRecord, ...]) -> str:
    """Return the canonical terminal status for one continuation-owned job group."""

    statuses = {job.status.value for job in jobs}
    if "failed" in statuses:
        return "failed"
    if "blocked" in statuses:
        return "blocked"
    if "canceled" in statuses:
        return "canceled"
    return "completed"


def build_chat_job_continuation_service(
    *,
    db_session: Session | None = None,
    task_dispatcher: object,
) -> ChatJobContinuationService:
    """Construct the canonical chat job continuation service."""

    if db_session is None:
        return ChatJobContinuationService(
            db_session=get_session_factory()(),
            task_dispatcher=task_dispatcher,
        )
    return ChatJobContinuationService(
        db_session=db_session,
        task_dispatcher=task_dispatcher,
    )


def _coerce_source_surface(source_surface: str):
    """Import the audit enum lazily to avoid circular imports in module constants."""

    from services.db.models.audit import AuditSourceSurface

    return AuditSourceSurface(source_surface)


__all__ = [
    "ChatJobContinuationResult",
    "ChatJobContinuationService",
    "build_chat_job_continuation_service",
]
