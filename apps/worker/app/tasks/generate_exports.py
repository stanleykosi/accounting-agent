"""
Purpose: Execute long-running export and evidence-pack release workflows in the worker.
Scope: Canonical async export-package generation and evidence-pack assembly used by
chat-owned operator turns and future release automation.
Dependencies: Celery worker app, export service, close-run phase guard, DB session
factory, and tracked-job lifecycle wrappers.
"""

from __future__ import annotations

from uuid import UUID

from apps.worker.app.celery_app import celery_app
from apps.worker.app.tasks.base import JobRuntimeContext, TrackedJobTask
from apps.worker.app.tasks.close_run_phase_guard import ensure_close_run_active_phase
from services.common.enums import WorkflowPhase
from services.contracts.export_models import CreateExportRequest
from services.db.repositories.entity_repo import EntityRepository, EntityUserRecord
from services.db.repositories.report_repo import ReportRepository
from services.db.session import get_session_factory
from services.exports.service import ExportService
from services.jobs.retry_policy import JobCancellationRequestedError
from services.jobs.task_names import TaskName, resolve_task_route


def _load_actor_user_for_entity(
    *,
    entity_repo: EntityRepository,
    entity_id: UUID,
    actor_user_id: UUID,
) -> EntityUserRecord:
    """Return the entity-scoped actor required for export ownership checks."""

    access = entity_repo.get_entity_for_user(entity_id=entity_id, user_id=actor_user_id)
    if access is None:
        raise RuntimeError(
            "The export worker could not load the originating actor in this workspace."
        )
    return access.membership.user


def _run_generate_export_task(
    *,
    entity_id: str,
    close_run_id: str,
    actor_user_id: str,
    include_evidence_pack: bool,
    include_audit_trail: bool,
    action_qualifier: str | None,
    job_context: JobRuntimeContext,
) -> dict[str, object]:
    """Generate the canonical export package for one close run version."""

    parsed_entity_id = UUID(entity_id)
    parsed_close_run_id = UUID(close_run_id)
    parsed_actor_user_id = UUID(actor_user_id)

    with get_session_factory()() as db_session:
        entity_repo = EntityRepository(db_session=db_session)
        export_service = ExportService(
            db_session=db_session,
            report_repository=ReportRepository(db_session=db_session),
        )

        def ensure_signoff_phase() -> None:
            try:
                job_context.ensure_not_canceled()
                ensure_close_run_active_phase(
                    session=db_session,
                    close_run_id=parsed_close_run_id,
                    required_phase=WorkflowPhase.REVIEW_SIGNOFF,
                )
            except JobCancellationRequestedError:
                raise

        actor_user = _load_actor_user_for_entity(
            entity_repo=entity_repo,
            entity_id=parsed_entity_id,
            actor_user_id=parsed_actor_user_id,
        )
        job_context.checkpoint(
            step="load_export_scope",
            state={
                "entity_id": entity_id,
                "close_run_id": close_run_id,
                "include_evidence_pack": include_evidence_pack,
                "include_audit_trail": include_audit_trail,
                "action_qualifier": action_qualifier or "",
            },
        )
        ensure_signoff_phase()

        export_detail = export_service.trigger_export(
            actor_user=actor_user,
            entity_id=parsed_entity_id,
            close_run_id=parsed_close_run_id,
            request=CreateExportRequest(
                include_evidence_pack=include_evidence_pack,
                include_audit_trail=include_audit_trail,
                action_qualifier=action_qualifier,
            ),
        )
        job_context.checkpoint(
            step="generate_export_package",
            state={
                "export_id": export_detail.id,
                "status": export_detail.status,
                "artifact_count": export_detail.artifact_count,
            },
        )
        return {
            "export_id": export_detail.id,
            "status": export_detail.status,
            "artifact_count": export_detail.artifact_count,
            "has_evidence_pack": export_detail.evidence_pack is not None,
            "distribution_count": export_detail.distribution_count,
        }


def _run_assemble_evidence_pack_task(
    *,
    entity_id: str,
    close_run_id: str,
    actor_user_id: str,
    job_context: JobRuntimeContext,
) -> dict[str, object]:
    """Assemble or reuse the canonical evidence pack for one close run version."""

    parsed_entity_id = UUID(entity_id)
    parsed_close_run_id = UUID(close_run_id)
    parsed_actor_user_id = UUID(actor_user_id)

    with get_session_factory()() as db_session:
        entity_repo = EntityRepository(db_session=db_session)
        export_service = ExportService(
            db_session=db_session,
            report_repository=ReportRepository(db_session=db_session),
        )

        actor_user = _load_actor_user_for_entity(
            entity_repo=entity_repo,
            entity_id=parsed_entity_id,
            actor_user_id=parsed_actor_user_id,
        )
        job_context.checkpoint(
            step="load_evidence_pack_scope",
            state={
                "entity_id": entity_id,
                "close_run_id": close_run_id,
            },
        )
        job_context.ensure_not_canceled()
        ensure_close_run_active_phase(
            session=db_session,
            close_run_id=parsed_close_run_id,
            required_phase=WorkflowPhase.REVIEW_SIGNOFF,
        )

        evidence_pack = export_service.assemble_evidence_pack(
            actor_user=actor_user,
            entity_id=parsed_entity_id,
            close_run_id=parsed_close_run_id,
        )
        job_context.checkpoint(
            step="assemble_evidence_pack",
            state={
                "version_no": evidence_pack.version_no,
                "storage_key": evidence_pack.storage_key or "",
                "size_bytes": evidence_pack.size_bytes or 0,
            },
        )
        return {
            "version_no": evidence_pack.version_no,
            "generated_at": evidence_pack.generated_at,
            "storage_key": evidence_pack.storage_key,
            "size_bytes": evidence_pack.size_bytes,
            "idempotency_key": evidence_pack.idempotency_key,
        }


@celery_app.task(
    bind=True,
    base=TrackedJobTask,
    name=TaskName.EXPORTS_GENERATE_CLOSE_RUN_PACKAGE.value,
    autoretry_for=(),
    retry_backoff=False,
    retry_jitter=False,
    max_retries=resolve_task_route(TaskName.EXPORTS_GENERATE_CLOSE_RUN_PACKAGE).max_retries,
)
def generate_export_package(
    self: TrackedJobTask,
    *,
    entity_id: str,
    close_run_id: str,
    actor_user_id: str,
    include_evidence_pack: bool = True,
    include_audit_trail: bool = True,
    action_qualifier: str | None = None,
) -> dict[str, object]:
    """Generate the export package for one close run under tracked-job control."""

    return self.run_tracked_job(
        runner=lambda job_context: _run_generate_export_task(
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor_user_id=actor_user_id,
            include_evidence_pack=include_evidence_pack,
            include_audit_trail=include_audit_trail,
            action_qualifier=action_qualifier,
            job_context=job_context,
        )
    )


@celery_app.task(
    bind=True,
    base=TrackedJobTask,
    name=TaskName.EXPORTS_ASSEMBLE_EVIDENCE_PACK.value,
    autoretry_for=(),
    retry_backoff=False,
    retry_jitter=False,
    max_retries=resolve_task_route(TaskName.EXPORTS_ASSEMBLE_EVIDENCE_PACK).max_retries,
)
def assemble_evidence_pack(
    self: TrackedJobTask,
    *,
    entity_id: str,
    close_run_id: str,
    actor_user_id: str,
) -> dict[str, object]:
    """Assemble the evidence pack for one close run under tracked-job control."""

    return self.run_tracked_job(
        runner=lambda job_context: _run_assemble_evidence_pack_task(
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor_user_id=actor_user_id,
            job_context=job_context,
        )
    )


__all__ = [
    "_run_assemble_evidence_pack_task",
    "_run_generate_export_task",
    "assemble_evidence_pack",
    "generate_export_package",
]
