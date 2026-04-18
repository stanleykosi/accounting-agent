"""
Purpose: Orchestrate irreversible deletion of one mutable close run.
Scope: Access validation, mutable-run enforcement, active-job cancellation,
database graph cleanup, and best-effort storage cleanup for run-owned files.
Dependencies: Close-run repository deletion plans, job service, storage repository,
and the shared close-run error contracts.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.close_runs.service import CloseRunServiceError, CloseRunServiceErrorCode
from services.common.enums import CloseRunStatus
from services.common.logging import get_logger
from services.contracts.close_run_models import CloseRunDeleteResponse
from services.db.repositories.close_run_repo import CloseRunAccessRecord, CloseRunDeletionPlan
from services.db.repositories.entity_repo import EntityUserRecord
from services.jobs.service import JobRecord, JobServiceError, JobServiceErrorCode

logger = get_logger(__name__)


class CloseRunDeletionRepositoryProtocol(Protocol):
    """Describe the persistence operations required by close-run deletion."""

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunAccessRecord | None:
        """Return one accessible close run and entity."""

    def get_close_run_deletion_plan_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunDeletionPlan | None:
        """Return the delete footprint for one accessible close run."""

    def delete_close_run(self, *, close_run_id: UUID) -> None:
        """Delete one mutable close run and its owned database graph."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the provided exception originated from DB integrity drift."""


class CloseRunDeletionStorageProtocol(Protocol):
    """Describe the storage operations required for close-run deletion cleanup."""

    def delete_source_document(self, *, storage_key: str) -> None:
        """Delete one original source document."""

    def delete_derivative_object(self, *, object_key: str) -> None:
        """Delete one derivative object."""

    def delete_artifact_object(self, *, object_key: str) -> None:
        """Delete one released artifact object."""


class CloseRunDeletionJobServiceProtocol(Protocol):
    """Describe the durable job-cancellation operation used after deletion commits."""

    def request_cancellation(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        """Request cancellation for one queued, running, or blocked job."""


class CloseRunDeleteService:
    """Provide the canonical destructive delete workflow for one mutable close run."""

    mutable_statuses = frozenset(
        {
            CloseRunStatus.DRAFT,
            CloseRunStatus.IN_REVIEW,
            CloseRunStatus.REOPENED,
        }
    )

    def __init__(
        self,
        *,
        repository: CloseRunDeletionRepositoryProtocol,
        storage_repository: CloseRunDeletionStorageProtocol,
        job_service: CloseRunDeletionJobServiceProtocol,
    ) -> None:
        self._repository = repository
        self._storage_repository = storage_repository
        self._job_service = job_service

    def delete_close_run(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID,
    ) -> CloseRunDeleteResponse:
        """Delete one mutable close run when the caller can access it."""

        access_record = self._repository.get_close_run_for_user(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        if access_record is None:
            raise CloseRunServiceError(
                status_code=404,
                code=CloseRunServiceErrorCode.CLOSE_RUN_NOT_FOUND,
                message="That close run does not exist or is not accessible to the current user.",
            )
        if access_record.close_run.status not in self.mutable_statuses:
            raise CloseRunServiceError(
                status_code=409,
                code=CloseRunServiceErrorCode.DELETE_NOT_ALLOWED,
                message="Only draft, in-review, or reopened close runs can be deleted.",
            )

        deletion_plan = self._repository.get_close_run_deletion_plan_for_user(
            entity_id=entity_id,
            close_run_id=close_run_id,
            user_id=actor_user.id,
        )
        if deletion_plan is None:
            raise CloseRunServiceError(
                status_code=404,
                code=CloseRunServiceErrorCode.CLOSE_RUN_NOT_FOUND,
                message="That close run does not exist or is not accessible to the current user.",
            )

        try:
            self._repository.delete_close_run(close_run_id=close_run_id)
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise CloseRunServiceError(
                    status_code=409,
                    code=CloseRunServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The close run could not be deleted because linked state changed.",
                ) from error
            raise

        canceled_job_count = self._cancel_active_jobs(
            actor_user=actor_user,
            deletion_plan=deletion_plan,
        )
        self._delete_storage_objects(deletion_plan=deletion_plan)
        return CloseRunDeleteResponse(
            deleted_close_run_id=serialize_uuid(deletion_plan.close_run.id),
            deleted_document_count=deletion_plan.document_count,
            deleted_recommendation_count=deletion_plan.recommendation_count,
            deleted_journal_count=deletion_plan.journal_count,
            deleted_report_run_count=deletion_plan.report_run_count,
            deleted_thread_count=deletion_plan.thread_count,
            canceled_job_count=canceled_job_count,
        )

    def _cancel_active_jobs(
        self,
        *,
        actor_user: EntityUserRecord,
        deletion_plan: CloseRunDeletionPlan,
    ) -> int:
        """Best-effort cancel active jobs after the close run has been removed."""

        canceled_job_count = 0
        for job_id in deletion_plan.active_job_ids:
            try:
                self._job_service.request_cancellation(
                    entity_id=deletion_plan.entity.id,
                    job_id=job_id,
                    actor_user_id=actor_user.id,
                    reason=(
                        "Execution stopped because the linked close run was deleted by an "
                        "operator."
                    ),
                )
                canceled_job_count += 1
            except JobServiceError as error:
                log_level = logger.warning
                log_message = (
                    "Close-run deletion skipped job cancellation because the job state changed."
                )
                if error.code not in {
                    JobServiceErrorCode.CANCEL_NOT_ALLOWED,
                    JobServiceErrorCode.JOB_NOT_FOUND,
                }:
                    log_level = logger.exception
                    log_message = (
                        "Close-run deletion could not record post-delete job cancellation."
                    )
                log_level(
                    log_message,
                    close_run_id=serialize_uuid(deletion_plan.close_run.id),
                    job_id=serialize_uuid(job_id),
                    error_code=str(error.code),
                )
            except Exception:
                logger.exception(
                    "Close-run deletion could not record post-delete job cancellation.",
                    close_run_id=serialize_uuid(deletion_plan.close_run.id),
                    job_id=serialize_uuid(job_id),
                )

        return canceled_job_count

    def _delete_storage_objects(self, *, deletion_plan: CloseRunDeletionPlan) -> None:
        """Delete close-run-owned files after the DB transaction commits successfully."""

        for storage_key in deletion_plan.source_storage_keys:
            try:
                self._storage_repository.delete_source_document(storage_key=storage_key)
            except Exception:
                logger.exception(
                    "Close-run deletion left a source document in storage.",
                    close_run_id=serialize_uuid(deletion_plan.close_run.id),
                    storage_key=storage_key,
                )
        for object_key in deletion_plan.derivative_storage_keys:
            try:
                self._storage_repository.delete_derivative_object(object_key=object_key)
            except Exception:
                logger.exception(
                    "Close-run deletion left a derivative object in storage.",
                    close_run_id=serialize_uuid(deletion_plan.close_run.id),
                    object_key=object_key,
                )
        for object_key in deletion_plan.artifact_storage_keys:
            try:
                self._storage_repository.delete_artifact_object(object_key=object_key)
            except Exception:
                logger.exception(
                    "Close-run deletion left an artifact object in storage.",
                    close_run_id=serialize_uuid(deletion_plan.close_run.id),
                    object_key=object_key,
                )


__all__ = [
    "CloseRunDeleteService",
]
