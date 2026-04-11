"""
Purpose: Persist and query generic ownership target metadata through SQLAlchemy.
Scope: Entity access checks, close-run scope checks, member validation, target
upserts, lock release, and immutable service-layer record projection.
Dependencies: SQLAlchemy ORM sessions plus auth, entity, close-run, and ownership models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from services.common.enums import OwnershipTargetType
from services.db.models.auth import User
from services.db.models.close_run import CloseRun
from services.db.models.entity import Entity, EntityMembership
from services.db.models.ownership import OwnershipTarget
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class OwnershipUserRecord:
    """Describe the subset of a local operator needed for ownership rendering."""

    id: UUID
    email: str
    full_name: str


@dataclass(frozen=True, slots=True)
class OwnershipEntityRecord:
    """Describe an entity visible to the acting user."""

    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class OwnershipCloseRunRecord:
    """Describe a close run visible to the acting user."""

    id: UUID
    entity_id: UUID


@dataclass(frozen=True, slots=True)
class OwnershipTargetRecord:
    """Describe one target's current owner, lock, and touch metadata."""

    id: UUID
    entity_id: UUID
    close_run_id: UUID | None
    target_type: OwnershipTargetType
    target_id: UUID
    owner: OwnershipUserRecord | None
    locked_by: OwnershipUserRecord | None
    locked_at: datetime | None
    last_touched_by: OwnershipUserRecord | None
    last_touched_at: datetime | None
    lock_note: str | None
    created_at: datetime
    updated_at: datetime


class OwnershipRepository:
    """Execute ownership persistence operations in one request-scoped DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the SQLAlchemy session used by ownership workflows."""

        self._db_session = db_session

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> OwnershipEntityRecord | None:
        """Return an entity when the user has workspace membership."""

        statement = (
            select(Entity)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(Entity.id == entity_id, EntityMembership.user_id == user_id)
        )
        entity = self._db_session.execute(statement).scalar_one_or_none()
        if entity is None:
            return None

        return OwnershipEntityRecord(id=entity.id, name=entity.name)

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> OwnershipCloseRunRecord | None:
        """Return a close run when it belongs to an accessible entity."""

        statement = (
            select(CloseRun)
            .join(EntityMembership, EntityMembership.entity_id == CloseRun.entity_id)
            .where(
                CloseRun.id == close_run_id,
                CloseRun.entity_id == entity_id,
                EntityMembership.user_id == user_id,
            )
        )
        close_run = self._db_session.execute(statement).scalar_one_or_none()
        if close_run is None:
            return None

        return OwnershipCloseRunRecord(id=close_run.id, entity_id=close_run.entity_id)

    def get_member_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> OwnershipUserRecord | None:
        """Return a user when they are a member of the specified entity."""

        statement = (
            select(User)
            .join(EntityMembership, EntityMembership.user_id == User.id)
            .where(EntityMembership.entity_id == entity_id, User.id == user_id)
        )
        user = self._db_session.execute(statement).scalar_one_or_none()
        if user is None:
            return None

        return _map_user(user)

    def get_target(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
    ) -> OwnershipTargetRecord | None:
        """Return one ownership target row by its canonical target identity."""

        target = self._load_target_or_none(target_type=target_type, target_id=target_id)
        return self._map_target(target) if target is not None else None

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
        """Create or update one ownership target with explicit owner, lock, and touch data."""

        target = self._load_target_or_none(target_type=target_type, target_id=target_id)
        if target is None:
            target = OwnershipTarget(
                entity_id=entity_id,
                close_run_id=close_run_id,
                target_type=target_type.value,
                target_id=target_id,
            )
            self._db_session.add(target)

        target.entity_id = entity_id
        target.close_run_id = close_run_id
        target.owner_user_id = owner_user_id
        target.locked_by_user_id = locked_by_user_id
        target.locked_at = locked_at
        target.last_touched_by_user_id = last_touched_by_user_id
        target.last_touched_at = last_touched_at
        target.lock_note = lock_note

        self._db_session.flush()
        return self._map_target(target)

    def release_lock(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
        last_touched_by_user_id: UUID,
        last_touched_at: datetime,
    ) -> OwnershipTargetRecord:
        """Release a target lock while preserving owner and last-touch metadata."""

        target = self._load_target(target_type=target_type, target_id=target_id)
        target.locked_by_user_id = None
        target.locked_at = None
        target.lock_note = None
        target.last_touched_by_user_id = last_touched_by_user_id
        target.last_touched_at = last_touched_at
        self._db_session.flush()
        return self._map_target(target)

    def commit(self) -> None:
        """Commit the current ownership unit of work."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current ownership unit of work."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the provided exception originated from the database."""

        return isinstance(error, IntegrityError)

    def _load_target_or_none(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
    ) -> OwnershipTarget | None:
        """Load a target row by type and ID when present."""

        statement = select(OwnershipTarget).where(
            OwnershipTarget.target_type == target_type.value,
            OwnershipTarget.target_id == target_id,
        )
        return self._db_session.execute(statement).scalar_one_or_none()

    def _load_target(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
    ) -> OwnershipTarget:
        """Load one target row or fail fast on inconsistent release logic."""

        target = self._load_target_or_none(target_type=target_type, target_id=target_id)
        if target is None:
            raise LookupError(f"Ownership target {target_type.value}/{target_id} does not exist.")

        return target

    def _map_target(self, target: OwnershipTarget) -> OwnershipTargetRecord:
        """Convert an ORM target row into an immutable service-layer record."""

        return OwnershipTargetRecord(
            id=target.id,
            entity_id=target.entity_id,
            close_run_id=target.close_run_id,
            target_type=_resolve_ownership_target_type(target.target_type),
            target_id=target.target_id,
            owner=self._load_user_or_none(user_id=target.owner_user_id),
            locked_by=self._load_user_or_none(user_id=target.locked_by_user_id),
            locked_at=target.locked_at,
            last_touched_by=self._load_user_or_none(user_id=target.last_touched_by_user_id),
            last_touched_at=target.last_touched_at,
            lock_note=target.lock_note,
            created_at=target.created_at,
            updated_at=target.updated_at,
        )

    def _load_user_or_none(self, *, user_id: UUID | None) -> OwnershipUserRecord | None:
        """Load a user record for optional target references."""

        if user_id is None:
            return None

        user = self._db_session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        return _map_user(user) if user is not None else None


def _map_user(user: User) -> OwnershipUserRecord:
    """Convert an ORM user row into an ownership operator record."""

    return OwnershipUserRecord(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
    )


def _resolve_ownership_target_type(value: str) -> OwnershipTargetType:
    """Resolve a stored ownership target type or fail fast on schema drift."""

    for target_type in OwnershipTargetType:
        if target_type.value == value:
            return target_type

    raise ValueError(f"Unsupported ownership target type value: {value}")


__all__ = [
    "OwnershipCloseRunRecord",
    "OwnershipEntityRecord",
    "OwnershipRepository",
    "OwnershipTargetRecord",
    "OwnershipUserRecord",
]
