"""
Purpose: Verify ownership lock, release, and last-touch workflow rules.
Scope: Pure unit coverage over ownership-domain behavior using an in-memory repository double.
Dependencies: Ownership service modules, repository record dataclasses, and canonical enums.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from services.common.enums import OwnershipTargetType
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.ownership_repo import (
    OwnershipCloseRunRecord,
    OwnershipEntityRecord,
    OwnershipTargetRecord,
    OwnershipUserRecord,
)
from services.ownership.service import (
    OwnershipService,
    OwnershipServiceError,
    OwnershipServiceErrorCode,
)


def test_acquire_lock_assigns_owner_and_last_touch() -> None:
    """Ensure acquiring a lock sets owner, lock holder, and last-touch metadata together."""

    repository = InMemoryOwnershipRepository()
    actor = repository.add_user(email="reviewer@example.com", full_name="Reviewer")
    entity = repository.add_entity(member_user_ids=(actor.id,))
    close_run = repository.add_close_run(entity_id=entity.id)
    document_id = uuid4()
    service = OwnershipService(repository=repository)

    state = service.acquire_lock(
        actor_user=_to_entity_user(actor),
        entity_id=entity.id,
        target_type=OwnershipTargetType.DOCUMENT,
        target_id=document_id,
        close_run_id=close_run.id,
        owner_user_id=None,
        note="Reviewing extraction confidence",
    )

    assert state.owner is not None
    assert state.owner.email == actor.email
    assert state.locked_by is not None
    assert state.locked_by.email == actor.email
    assert state.last_touched_by is not None
    assert state.last_touched_by.email == actor.email
    assert state.lock_note == "Reviewing extraction confidence"


def test_second_user_cannot_touch_or_lock_locked_target() -> None:
    """Ensure another operator cannot silently collide with an active in-progress lock."""

    repository = InMemoryOwnershipRepository()
    first_user = repository.add_user(email="first@example.com", full_name="First Reviewer")
    second_user = repository.add_user(email="second@example.com", full_name="Second Reviewer")
    entity = repository.add_entity(member_user_ids=(first_user.id, second_user.id))
    close_run = repository.add_close_run(entity_id=entity.id)
    recommendation_id = uuid4()
    service = OwnershipService(repository=repository)
    service.acquire_lock(
        actor_user=_to_entity_user(first_user),
        entity_id=entity.id,
        target_type=OwnershipTargetType.RECOMMENDATION,
        target_id=recommendation_id,
        close_run_id=close_run.id,
        owner_user_id=None,
        note=None,
    )

    with pytest.raises(OwnershipServiceError) as touch_error:
        service.touch_target(
            actor_user=_to_entity_user(second_user),
            entity_id=entity.id,
            target_type=OwnershipTargetType.RECOMMENDATION,
            target_id=recommendation_id,
            close_run_id=close_run.id,
        )
    with pytest.raises(OwnershipServiceError) as lock_error:
        service.acquire_lock(
            actor_user=_to_entity_user(second_user),
            entity_id=entity.id,
            target_type=OwnershipTargetType.RECOMMENDATION,
            target_id=recommendation_id,
            close_run_id=close_run.id,
            owner_user_id=None,
            note=None,
        )

    assert touch_error.value.code is OwnershipServiceErrorCode.LOCK_CONFLICT
    assert lock_error.value.code is OwnershipServiceErrorCode.LOCK_CONFLICT


def test_release_lock_requires_lock_holder() -> None:
    """Ensure only the operator who holds the lock can release it."""

    repository = InMemoryOwnershipRepository()
    first_user = repository.add_user(email="first@example.com", full_name="First Reviewer")
    second_user = repository.add_user(email="second@example.com", full_name="Second Reviewer")
    entity = repository.add_entity(member_user_ids=(first_user.id, second_user.id))
    close_run = repository.add_close_run(entity_id=entity.id)
    review_target_id = uuid4()
    service = OwnershipService(repository=repository)
    service.acquire_lock(
        actor_user=_to_entity_user(first_user),
        entity_id=entity.id,
        target_type=OwnershipTargetType.REVIEW_TARGET,
        target_id=review_target_id,
        close_run_id=close_run.id,
        owner_user_id=None,
        note=None,
    )

    with pytest.raises(OwnershipServiceError) as error:
        service.release_lock(
            actor_user=_to_entity_user(second_user),
            entity_id=entity.id,
            target_type=OwnershipTargetType.REVIEW_TARGET,
            target_id=review_target_id,
            close_run_id=close_run.id,
        )

    assert error.value.code is OwnershipServiceErrorCode.LOCK_NOT_HELD

    released = service.release_lock(
        actor_user=_to_entity_user(first_user),
        entity_id=entity.id,
        target_type=OwnershipTargetType.REVIEW_TARGET,
        target_id=review_target_id,
        close_run_id=close_run.id,
    )
    assert released.locked_by is None
    assert released.locked_at is None
    assert released.last_touched_by is not None
    assert released.last_touched_by.email == first_user.email


class InMemoryOwnershipRepository:
    """Provide the repository surface required by the ownership service for unit tests."""

    def __init__(self) -> None:
        """Initialize in-memory stores for users, entities, close runs, and ownership targets."""

        self.users: dict[UUID, OwnershipUserRecord] = {}
        self.entities: dict[UUID, OwnershipEntityRecord] = {}
        self.memberships: dict[UUID, set[UUID]] = {}
        self.close_runs: dict[UUID, OwnershipCloseRunRecord] = {}
        self.targets: dict[tuple[OwnershipTargetType, UUID], OwnershipTargetRecord] = {}

    def add_user(self, *, email: str, full_name: str) -> OwnershipUserRecord:
        """Seed one in-memory local operator."""

        user = OwnershipUserRecord(id=uuid4(), email=email, full_name=full_name)
        self.users[user.id] = user
        return user

    def add_entity(self, *, member_user_ids: tuple[UUID, ...]) -> OwnershipEntityRecord:
        """Seed one entity with the provided member users."""

        entity = OwnershipEntityRecord(id=uuid4(), name="Northwind Nigeria")
        self.entities[entity.id] = entity
        self.memberships[entity.id] = set(member_user_ids)
        return entity

    def add_close_run(self, *, entity_id: UUID) -> OwnershipCloseRunRecord:
        """Seed one close run for an existing entity."""

        close_run = OwnershipCloseRunRecord(id=uuid4(), entity_id=entity_id)
        self.close_runs[close_run.id] = close_run
        return close_run

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> OwnershipEntityRecord | None:
        """Return an entity when the user is a member."""

        if user_id not in self.memberships.get(entity_id, set()):
            return None
        return self.entities.get(entity_id)

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> OwnershipCloseRunRecord | None:
        """Return a close run when it belongs to an accessible entity."""

        close_run = self.close_runs.get(close_run_id)
        if close_run is None or close_run.entity_id != entity_id:
            return None
        if user_id not in self.memberships.get(entity_id, set()):
            return None
        return close_run

    def get_member_user(self, *, entity_id: UUID, user_id: UUID) -> OwnershipUserRecord | None:
        """Return a user when they are a member of the entity."""

        if user_id not in self.memberships.get(entity_id, set()):
            return None
        return self.users.get(user_id)

    def get_target(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
    ) -> OwnershipTargetRecord | None:
        """Return an in-memory ownership target by canonical identity."""

        return self.targets.get((target_type, target_id))

    def upsert_target(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        target_type: OwnershipTargetType,
        target_id: UUID,
        owner_user_id: UUID | None,
        locked_by_user_id: UUID | None,
        locked_at: datetime | None,
        last_touched_by_user_id: UUID,
        last_touched_at: datetime,
        lock_note: str | None,
    ) -> OwnershipTargetRecord:
        """Create or update one in-memory ownership target."""

        existing = self.targets.get((target_type, target_id))
        target = OwnershipTargetRecord(
            id=existing.id if existing is not None else uuid4(),
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type=target_type,
            target_id=target_id,
            owner=self.users.get(owner_user_id) if owner_user_id is not None else None,
            locked_by=self.users.get(locked_by_user_id) if locked_by_user_id is not None else None,
            locked_at=locked_at,
            last_touched_by=self.users[last_touched_by_user_id],
            last_touched_at=last_touched_at,
            lock_note=lock_note,
            created_at=existing.created_at if existing is not None else utc_now(),
            updated_at=utc_now(),
        )
        self.targets[(target_type, target_id)] = target
        return target

    def release_lock(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
        last_touched_by_user_id: UUID,
        last_touched_at: datetime,
    ) -> OwnershipTargetRecord:
        """Release one in-memory lock while preserving target ownership."""

        target = self.targets[(target_type, target_id)]
        released = replace(
            target,
            locked_by=None,
            locked_at=None,
            last_touched_by=self.users[last_touched_by_user_id],
            last_touched_at=last_touched_at,
            lock_note=None,
            updated_at=utc_now(),
        )
        self.targets[(target_type, target_id)] = released
        return released

    def commit(self) -> None:
        """Treat successful in-memory operations as immediately committed."""

    def rollback(self) -> None:
        """Treat rollback as a no-op in the in-memory test double."""

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return False because the in-memory repository does not emulate DB integrity errors."""

        return False


def _to_entity_user(user: OwnershipUserRecord) -> EntityUserRecord:
    """Project an ownership test user into the shared entity user record shape."""

    return EntityUserRecord(id=user.id, email=user.email, full_name=user.full_name)


def utc_now() -> datetime:
    """Return a monotonic UTC-aware timestamp for in-memory test records."""

    return datetime.now(tz=UTC) + timedelta(microseconds=1)
