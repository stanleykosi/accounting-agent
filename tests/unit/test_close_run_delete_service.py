"""
Purpose: Verify the mutable close-run delete workflow.
Scope: Unit coverage for lifecycle guardrails, job cancellation, DB cleanup,
and post-commit storage cleanup using in-memory doubles.
Dependencies: Close-run delete service, close-run repository dataclasses, and job contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from services.close_runs.delete_service import CloseRunDeleteService
from services.close_runs.service import CloseRunServiceError, CloseRunServiceErrorCode
from services.common.enums import AutonomyMode, CloseRunStatus, JobStatus
from services.db.models.entity import EntityStatus
from services.db.repositories.close_run_repo import (
    CloseRunAccessRecord,
    CloseRunDeletionPlan,
    CloseRunEntityRecord,
    CloseRunRecord,
)
from services.db.repositories.entity_repo import EntityUserRecord
from services.jobs.service import JobRecord


def test_mutable_close_run_can_be_deleted_and_cleanup_jobs_and_storage() -> None:
    """Deleting a mutable close run should cancel jobs, remove DB state, and clear storage."""

    repository = InMemoryCloseRunDeletionRepository(status=CloseRunStatus.IN_REVIEW)
    storage = InMemoryCloseRunDeletionStorage()
    jobs = InMemoryCloseRunDeletionJobService(repository=repository)
    service = CloseRunDeleteService(
        repository=repository,
        storage_repository=storage,
        job_service=jobs,
    )

    response = service.delete_close_run(
        actor_user=repository.actor,
        entity_id=repository.plan.entity.id,
        close_run_id=repository.plan.close_run.id,
    )

    assert response.deleted_close_run_id == str(repository.plan.close_run.id)
    assert response.deleted_document_count == 2
    assert response.deleted_recommendation_count == 1
    assert response.deleted_journal_count == 1
    assert response.deleted_report_run_count == 1
    assert response.deleted_thread_count == 2
    assert response.canceled_job_count == 2
    assert repository.deleted_close_run_id == repository.plan.close_run.id
    assert repository.committed is True
    assert storage.deleted_source_keys == ["documents/source/a.pdf"]
    assert storage.deleted_derivative_keys == ["documents/ocr/a.txt"]
    assert storage.deleted_artifact_keys == ["artifacts/report.pdf"]
    assert jobs.canceled_job_ids == list(repository.plan.active_job_ids)
    assert jobs.repository_commit_states_at_request == [True, True]


def test_non_mutable_close_run_cannot_be_deleted() -> None:
    """Signed-off close runs should fail fast instead of deleting history."""

    repository = InMemoryCloseRunDeletionRepository(status=CloseRunStatus.APPROVED)
    service = CloseRunDeleteService(
        repository=repository,
        storage_repository=InMemoryCloseRunDeletionStorage(),
        job_service=InMemoryCloseRunDeletionJobService(),
    )

    with pytest.raises(CloseRunServiceError) as error:
        service.delete_close_run(
            actor_user=repository.actor,
            entity_id=repository.plan.entity.id,
            close_run_id=repository.plan.close_run.id,
        )

    assert error.value.status_code == 409
    assert error.value.code is CloseRunServiceErrorCode.DELETE_NOT_ALLOWED
    assert repository.deleted_close_run_id is None
    assert repository.committed is False


def test_delete_close_run_rolls_back_on_integrity_conflict() -> None:
    """Delete should translate DB integrity drift into a structured conflict."""

    repository = InMemoryCloseRunDeletionRepository(
        status=CloseRunStatus.REOPENED,
        raise_integrity_error=True,
    )
    storage = InMemoryCloseRunDeletionStorage()
    jobs = InMemoryCloseRunDeletionJobService(repository=repository)
    service = CloseRunDeleteService(
        repository=repository,
        storage_repository=storage,
        job_service=jobs,
    )

    with pytest.raises(CloseRunServiceError) as error:
        service.delete_close_run(
            actor_user=repository.actor,
            entity_id=repository.plan.entity.id,
            close_run_id=repository.plan.close_run.id,
        )

    assert error.value.status_code == 409
    assert error.value.code is CloseRunServiceErrorCode.INTEGRITY_CONFLICT
    assert repository.rolled_back is True
    assert jobs.canceled_job_ids == []
    assert storage.deleted_source_keys == []
    assert storage.deleted_derivative_keys == []
    assert storage.deleted_artifact_keys == []


class FakeIntegrityError(Exception):
    """Represent one in-memory integrity failure raised by the repository double."""


class InMemoryCloseRunDeletionRepository:
    """Provide the minimal close-run deletion repository surface required by the service."""

    def __init__(
        self,
        *,
        status: CloseRunStatus,
        raise_integrity_error: bool = False,
    ) -> None:
        now = datetime.now(tz=UTC)
        self.actor = EntityUserRecord(
            id=uuid4(),
            email="operator@example.com",
            full_name="Finance Operator",
        )
        self.entity = CloseRunEntityRecord(
            id=uuid4(),
            name="Transfa",
            base_currency="NGN",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            status=EntityStatus.ACTIVE,
        )
        self.close_run = CloseRunRecord(
            id=uuid4(),
            entity_id=self.entity.id,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
            status=status,
            reporting_currency="NGN",
            current_version_no=1,
            opened_by_user_id=self.actor.id,
            approved_by_user_id=None,
            approved_at=None,
            archived_at=None,
            reopened_from_close_run_id=None,
            created_at=now,
            updated_at=now,
        )
        self.access_record = CloseRunAccessRecord(close_run=self.close_run, entity=self.entity)
        self.plan = CloseRunDeletionPlan(
            close_run=self.close_run,
            entity=self.entity,
            document_count=2,
            recommendation_count=1,
            journal_count=1,
            report_run_count=1,
            thread_count=2,
            active_job_ids=(uuid4(), uuid4()),
            source_storage_keys=("documents/source/a.pdf",),
            derivative_storage_keys=("documents/ocr/a.txt",),
            artifact_storage_keys=("artifacts/report.pdf",),
        )
        self.raise_integrity_error = raise_integrity_error
        self.deleted_close_run_id: UUID | None = None
        self.committed = False
        self.rolled_back = False

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunAccessRecord | None:
        if (
            entity_id != self.entity.id
            or close_run_id != self.close_run.id
            or user_id != self.actor.id
        ):
            return None
        return self.access_record

    def get_close_run_deletion_plan_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> CloseRunDeletionPlan | None:
        if (
            entity_id != self.entity.id
            or close_run_id != self.close_run.id
            or user_id != self.actor.id
        ):
            return None
        return self.plan

    def delete_close_run(self, *, close_run_id: UUID) -> None:
        if self.raise_integrity_error:
            raise FakeIntegrityError("linked rows changed")
        self.deleted_close_run_id = close_run_id

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def is_integrity_error(self, error: Exception) -> bool:
        return isinstance(error, FakeIntegrityError)


class InMemoryCloseRunDeletionStorage:
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
    """Capture one job-cancellation request issued during close-run deletion."""

    actor_user_id: UUID
    entity_id: UUID
    job_id: UUID
    reason: str


class InMemoryCloseRunDeletionJobService:
    """Capture requested job cancellations while returning job-like records."""

    def __init__(
        self,
        repository: InMemoryCloseRunDeletionRepository | None = None,
    ) -> None:
        self._repository = repository
        self.requests: list[CancellationRequest] = []
        self.canceled_job_ids: list[UUID] = []
        self.repository_commit_states_at_request: list[bool | None] = []

    def request_cancellation(
        self,
        *,
        entity_id: UUID,
        job_id: UUID,
        actor_user_id: UUID,
        reason: str,
    ) -> JobRecord:
        self.repository_commit_states_at_request.append(
            self._repository.committed if self._repository is not None else None
        )
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
