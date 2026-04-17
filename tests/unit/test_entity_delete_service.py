"""
Purpose: Verify the owner-only entity workspace delete workflow.
Scope: Unit coverage for delete authorization, job cancellation, DB cleanup,
and post-commit storage cleanup using in-memory doubles.
Dependencies: Entity delete service, repository record dataclasses, and job contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from services.common.enums import AutonomyMode, JobStatus
from services.db.models.entity import EntityStatus
from services.db.repositories.entity_repo import (
    EntityAccessRecord,
    EntityDeletionPlan,
    EntityMembershipRecord,
    EntityRecord,
    EntityUserRecord,
)
from services.entity.delete_service import (
    EntityDeleteService,
    EntityDeleteServiceError,
    EntityDeleteServiceErrorCode,
)
from services.jobs.service import JobRecord


def test_owner_can_delete_workspace_and_cleanup_jobs_and_storage() -> None:
    """Deleting a workspace should cancel active jobs, remove DB state, and clear storage."""

    repository = InMemoryEntityDeletionRepository(role="owner")
    storage = InMemoryEntityDeletionStorage()
    jobs = InMemoryEntityDeletionJobService()
    service = EntityDeleteService(
        repository=repository,
        storage_repository=storage,
        job_service=jobs,
    )

    response = service.delete_entity(
        actor_user=repository.actor,
        entity_id=repository.plan.entity.id,
    )

    assert response.deleted_entity_id == str(repository.plan.entity.id)
    assert response.deleted_entity_name == repository.plan.entity.name
    assert response.deleted_close_run_count == 2
    assert response.deleted_document_count == 4
    assert response.deleted_thread_count == 3
    assert response.canceled_job_count == 2
    assert repository.deleted_entity_id == repository.plan.entity.id
    assert repository.committed is True
    assert storage.deleted_source_keys == ["documents/source/a.pdf", "documents/source/b.pdf"]
    assert storage.deleted_derivative_keys == [
        "documents/ocr/a.txt",
        "documents/derivatives/a.json",
    ]
    assert storage.deleted_artifact_keys == ["artifacts/report.pdf"]
    assert jobs.canceled_job_ids == list(repository.plan.active_job_ids)


def test_non_owner_cannot_delete_workspace() -> None:
    """Workspace deletion should fail fast when the caller is not an owner."""

    repository = InMemoryEntityDeletionRepository(role="reviewer")
    service = EntityDeleteService(
        repository=repository,
        storage_repository=InMemoryEntityDeletionStorage(),
        job_service=InMemoryEntityDeletionJobService(),
    )

    with pytest.raises(EntityDeleteServiceError) as error:
        service.delete_entity(
            actor_user=repository.actor,
            entity_id=repository.plan.entity.id,
        )

    assert error.value.status_code == 403
    assert error.value.code is EntityDeleteServiceErrorCode.OWNER_REQUIRED
    assert repository.deleted_entity_id is None
    assert repository.committed is False


def test_delete_workspace_rolls_back_on_integrity_conflict() -> None:
    """Delete should translate DB integrity drift into a structured conflict."""

    repository = InMemoryEntityDeletionRepository(role="owner", raise_integrity_error=True)
    storage = InMemoryEntityDeletionStorage()
    service = EntityDeleteService(
        repository=repository,
        storage_repository=storage,
        job_service=InMemoryEntityDeletionJobService(),
    )

    with pytest.raises(EntityDeleteServiceError) as error:
        service.delete_entity(
            actor_user=repository.actor,
            entity_id=repository.plan.entity.id,
        )

    assert error.value.status_code == 409
    assert error.value.code is EntityDeleteServiceErrorCode.INTEGRITY_CONFLICT
    assert repository.rolled_back is True
    assert storage.deleted_source_keys == []
    assert storage.deleted_derivative_keys == []
    assert storage.deleted_artifact_keys == []


class FakeIntegrityError(Exception):
    """Represent one in-memory integrity failure raised by the repository double."""


class InMemoryEntityDeletionRepository:
    """Provide the minimal entity-deletion repository surface required by the service."""

    def __init__(self, *, role: str, raise_integrity_error: bool = False) -> None:
        self.actor = EntityUserRecord(
            id=uuid4(),
            email="owner@example.com",
            full_name="Owner User",
        )
        now = datetime.now(tz=UTC)
        self.entity = EntityRecord(
            id=uuid4(),
            name="Acme Workspace",
            legal_name="Acme Workspace LLC",
            base_currency="USD",
            country_code="US",
            timezone="America/New_York",
            accounting_standard="US GAAP",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            default_confidence_thresholds={
                "classification": 0.85,
                "coding": 0.85,
                "reconciliation": 0.9,
                "posting": 0.95,
            },
            status=EntityStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self.membership = EntityMembershipRecord(
            id=uuid4(),
            entity_id=self.entity.id,
            user_id=self.actor.id,
            role=role,
            is_default_actor=True,
            created_at=now,
            updated_at=now,
            user=self.actor,
        )
        self.access_record = EntityAccessRecord(
            entity=self.entity,
            membership=self.membership,
        )
        self.plan = EntityDeletionPlan(
            entity=self.entity,
            close_run_count=2,
            document_count=4,
            thread_count=3,
            active_job_ids=(uuid4(), uuid4()),
            source_storage_keys=("documents/source/a.pdf", "documents/source/b.pdf"),
            derivative_storage_keys=("documents/ocr/a.txt", "documents/derivatives/a.json"),
            artifact_storage_keys=("artifacts/report.pdf",),
        )
        self.raise_integrity_error = raise_integrity_error
        self.deleted_entity_id: UUID | None = None
        self.committed = False
        self.rolled_back = False

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> EntityAccessRecord | None:
        if entity_id != self.entity.id or user_id != self.actor.id:
            return None
        return self.access_record

    def get_entity_deletion_plan_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityDeletionPlan | None:
        if entity_id != self.entity.id or user_id != self.actor.id:
            return None
        return self.plan

    def delete_entity_workspace(self, *, entity_id: UUID) -> None:
        if self.raise_integrity_error:
            raise FakeIntegrityError("linked rows changed")
        self.deleted_entity_id = entity_id

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def is_integrity_error(self, error: Exception) -> bool:
        return isinstance(error, FakeIntegrityError)


class InMemoryEntityDeletionStorage:
    """Capture post-commit storage cleanup operations for assertions."""

    def __init__(self) -> None:
        self.deleted_source_keys: list[str] = []
        self.deleted_derivative_keys: list[str] = []
        self.deleted_artifact_keys: list[str] = []

    def delete_source_document(self, *, storage_key: str) -> None:
        self.deleted_source_keys.append(storage_key)

    def delete_derivative_object(self, *, object_key: str) -> None:
        self.deleted_derivative_keys.append(object_key)

    def delete_artifact_object(self, *, object_key: str) -> None:
        self.deleted_artifact_keys.append(object_key)


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    """Capture one job-cancellation request issued during workspace deletion."""

    actor_user_id: UUID
    entity_id: UUID
    job_id: UUID
    reason: str


class InMemoryEntityDeletionJobService:
    """Capture requested job cancellations while returning job-like records."""

    def __init__(self) -> None:
        self.requests: list[CancellationRequest] = []
        self.canceled_job_ids: list[UUID] = []

    def request_cancellation(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        self.requests.append(
            CancellationRequest(
                actor_user_id=actor_user_id,
                entity_id=entity_id,
                job_id=job_id,
                reason=reason,
            )
        )
        self.canceled_job_ids.append(job_id)
        now = datetime.now(tz=UTC)
        return JobRecord(
            id=job_id,
            entity_id=entity_id,
            close_run_id=None,
            document_id=None,
            actor_user_id=actor_user_id,
            canceled_by_user_id=actor_user_id,
            resumed_from_job_id=None,
            task_name="document.parse",
            queue_name="default",
            routing_key="default",
            status=JobStatus.CANCELED,
            payload={},
            checkpoint_payload={},
            result_payload=None,
            failure_reason=None,
            failure_details=None,
            blocking_reason=None,
            trace_id=None,
            attempt_count=1,
            retry_count=0,
            max_retries=0,
            started_at=None,
            completed_at=now,
            cancellation_requested_at=now,
            canceled_at=now,
            dead_lettered_at=None,
            created_at=now,
            updated_at=now,
        )
