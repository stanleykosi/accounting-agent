"""
Purpose: Orchestrate irreversible entity workspace deletion.
Scope: Access validation, owner-only deletion policy, active-job cancellation,
database graph cleanup, and best-effort storage cleanup for workspace-owned files.
Dependencies: Entity repository records, job service, storage repository, and
strict response contracts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.common.logging import get_logger
from services.contracts.entity_models import EntityDeleteResponse
from services.db.repositories.entity_repo import (
    EntityAccessRecord,
    EntityDeletionPlan,
    EntityUserRecord,
)
from services.jobs.service import JobRecord, JobServiceError, JobServiceErrorCode

logger = get_logger(__name__)


class EntityDeleteServiceErrorCode(StrEnum):
    """Enumerate the stable error codes surfaced by entity-deletion workflows."""

    ENTITY_NOT_FOUND = "entity_not_found"
    INTEGRITY_CONFLICT = "integrity_conflict"
    OWNER_REQUIRED = "owner_required"


class EntityDeleteServiceError(Exception):
    """Represent an expected workspace-delete failure for API translation."""

    def __init__(
        self,
        *,
        status_code: int,
        code: EntityDeleteServiceErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class EntityDeletionRepositoryProtocol(Protocol):
    """Describe the persistence operations required by workspace deletion."""

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> EntityAccessRecord | None:
        """Return one accessible entity when the caller has workspace membership."""

    def get_entity_deletion_plan_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityDeletionPlan | None:
        """Return the delete footprint for one accessible entity workspace."""

    def delete_entity_workspace(self, *, entity_id: UUID) -> None:
        """Delete one entity workspace and its owned database graph."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the provided exception originated from DB integrity drift."""


class EntityDeletionStorageProtocol(Protocol):
    """Describe the storage operations required for workspace deletion cleanup."""

    def delete_source_document(self, *, storage_key: str) -> None:
        """Delete one original source document."""

    def delete_derivative_object(self, *, object_key: str) -> None:
        """Delete one derivative object."""

    def delete_artifact_object(self, *, object_key: str) -> None:
        """Delete one released artifact object."""


class EntityDeletionJobServiceProtocol(Protocol):
    """Describe the durable job-cancellation operation required before deletion."""

    def request_cancellation(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        """Request cancellation for one queued, running, or blocked job."""


class EntityDeleteService:
    """Provide the owner-only destructive delete workflow for one workspace."""

    def __init__(
        self,
        *,
        repository: EntityDeletionRepositoryProtocol,
        storage_repository: EntityDeletionStorageProtocol,
        job_service: EntityDeletionJobServiceProtocol,
    ) -> None:
        self._repository = repository
        self._storage_repository = storage_repository
        self._job_service = job_service

    def delete_entity(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
    ) -> EntityDeleteResponse:
        """Delete one workspace when the caller is an owner of that entity."""

        access_record = self._repository.get_entity_for_user(
            entity_id=entity_id,
            user_id=actor_user.id,
        )
        if access_record is None:
            raise EntityDeleteServiceError(
                status_code=404,
                code=EntityDeleteServiceErrorCode.ENTITY_NOT_FOUND,
                message="That workspace does not exist or is not accessible to the current user.",
            )
        if access_record.membership.role != "owner":
            raise EntityDeleteServiceError(
                status_code=403,
                code=EntityDeleteServiceErrorCode.OWNER_REQUIRED,
                message="Only workspace owners can delete an entity workspace.",
            )

        deletion_plan = self._repository.get_entity_deletion_plan_for_user(
            entity_id=entity_id,
            user_id=actor_user.id,
        )
        if deletion_plan is None:
            raise EntityDeleteServiceError(
                status_code=404,
                code=EntityDeleteServiceErrorCode.ENTITY_NOT_FOUND,
                message="That workspace does not exist or is not accessible to the current user.",
            )

        canceled_job_count = self._cancel_active_jobs(
            actor_user=actor_user,
            deletion_plan=deletion_plan,
        )
        try:
            self._repository.delete_entity_workspace(entity_id=entity_id)
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise EntityDeleteServiceError(
                    status_code=409,
                    code=EntityDeleteServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The workspace could not be deleted because linked state changed.",
                ) from error
            raise

        self._delete_storage_objects(deletion_plan=deletion_plan)
        return EntityDeleteResponse(
            deleted_entity_id=serialize_uuid(deletion_plan.entity.id),
            deleted_entity_name=deletion_plan.entity.name,
            deleted_close_run_count=deletion_plan.close_run_count,
            deleted_document_count=deletion_plan.document_count,
            deleted_thread_count=deletion_plan.thread_count,
            canceled_job_count=canceled_job_count,
        )

    def _cancel_active_jobs(
        self,
        *,
        actor_user: EntityUserRecord,
        deletion_plan: EntityDeletionPlan,
    ) -> int:
        """Cancel active workspace jobs before the underlying entity is removed."""

        canceled_job_count = 0
        for job_id in deletion_plan.active_job_ids:
            try:
                self._job_service.request_cancellation(
                    entity_id=deletion_plan.entity.id,
                    job_id=job_id,
                    actor_user_id=actor_user.id,
                    reason=(
                        "Execution stopped because the linked workspace was deleted by an owner."
                    ),
                )
                canceled_job_count += 1
            except JobServiceError as error:
                if error.code in {
                    JobServiceErrorCode.CANCEL_NOT_ALLOWED,
                    JobServiceErrorCode.JOB_NOT_FOUND,
                }:
                    logger.warning(
                        (
                            "Workspace deletion skipped job cancellation because "
                            "the job state changed."
                        ),
                        entity_id=serialize_uuid(deletion_plan.entity.id),
                        job_id=serialize_uuid(job_id),
                        error_code=str(error.code),
                    )
                    continue
                raise

        return canceled_job_count

    def _delete_storage_objects(self, *, deletion_plan: EntityDeletionPlan) -> None:
        """Delete workspace-owned files after the DB transaction commits successfully."""

        for storage_key in deletion_plan.source_storage_keys:
            try:
                self._storage_repository.delete_source_document(storage_key=storage_key)
            except Exception:
                logger.exception(
                    "Workspace deletion left a source document in storage.",
                    entity_id=serialize_uuid(deletion_plan.entity.id),
                    storage_key=storage_key,
                )
        for object_key in deletion_plan.derivative_storage_keys:
            try:
                self._storage_repository.delete_derivative_object(object_key=object_key)
            except Exception:
                logger.exception(
                    "Workspace deletion left a derivative object in storage.",
                    entity_id=serialize_uuid(deletion_plan.entity.id),
                    object_key=object_key,
                )
        for object_key in deletion_plan.artifact_storage_keys:
            try:
                self._storage_repository.delete_artifact_object(object_key=object_key)
            except Exception:
                logger.exception(
                    "Workspace deletion left an artifact object in storage.",
                    entity_id=serialize_uuid(deletion_plan.entity.id),
                    object_key=object_key,
                )


__all__ = [
    "EntityDeleteService",
    "EntityDeleteServiceError",
    "EntityDeleteServiceErrorCode",
]
