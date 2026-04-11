"""
Purpose: Orchestrate ownership assignment, in-progress locks, and last-touch updates.
Scope: Generic target validation for entities, close runs, documents, recommendations,
and review targets, plus collision prevention for concurrent review work.
Dependencies: Ownership contracts, repository records, canonical enums, and UUID serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.common.enums import OwnershipTargetType
from services.common.types import utc_now
from services.contracts.entity_models import EntityOperatorSummary
from services.contracts.ownership_models import OwnershipState
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.ownership_repo import (
    OwnershipCloseRunRecord,
    OwnershipEntityRecord,
    OwnershipTargetRecord,
    OwnershipUserRecord,
)


class OwnershipServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by ownership workflows."""

    CLOSE_RUN_NOT_FOUND = "close_run_not_found"
    ENTITY_NOT_FOUND = "entity_not_found"
    INTEGRITY_CONFLICT = "integrity_conflict"
    LOCK_CONFLICT = "lock_conflict"
    LOCK_NOT_HELD = "lock_not_held"
    OWNER_NOT_FOUND = "owner_not_found"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_SCOPE_INVALID = "target_scope_invalid"


class OwnershipServiceError(Exception):
    """Represent an expected ownership-domain failure for API translation."""

    def __init__(self, *, status_code: int, code: OwnershipServiceErrorCode, message: str) -> None:
        """Capture HTTP status, stable code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class OwnershipRepositoryProtocol(Protocol):
    """Describe persistence operations required by the ownership service."""

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> OwnershipEntityRecord | None:
        """Return an entity when the user has access."""

    def get_close_run_for_user(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        user_id: UUID,
    ) -> OwnershipCloseRunRecord | None:
        """Return a close run when the user has workspace access."""

    def get_member_user(self, *, entity_id: UUID, user_id: UUID) -> OwnershipUserRecord | None:
        """Return an entity member by user UUID."""

    def get_target(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
    ) -> OwnershipTargetRecord | None:
        """Return one ownership target when metadata already exists."""

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
        """Create or update one target row."""

    def release_lock(
        self,
        *,
        target_type: OwnershipTargetType,
        target_id: UUID,
        last_touched_by_user_id: UUID,
        last_touched_at: datetime,
    ) -> OwnershipTargetRecord:
        """Release a held target lock."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the error originated from database integrity constraints."""


class OwnershipService:
    """Provide the canonical ownership workflow for API, UI, and later review surfaces."""

    def __init__(self, *, repository: OwnershipRepositoryProtocol) -> None:
        """Capture the persistence boundary used by ownership workflows."""

        self._repository = repository

    def get_ownership_state(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        target_type: OwnershipTargetType,
        target_id: UUID,
        close_run_id: UUID | None,
    ) -> OwnershipState:
        """Return existing ownership metadata for one target or an empty unlocked state."""

        resolved_scope = self._resolve_scope(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type=target_type,
            target_id=target_id,
        )
        target = self._repository.get_target(target_type=target_type, target_id=target_id)
        if target is None:
            resolved_close_run_id = _scope_close_run_id(resolved_scope)
            return OwnershipState(
                entity_id=serialize_uuid(entity_id),
                close_run_id=(
                    serialize_uuid(resolved_close_run_id)
                    if resolved_close_run_id is not None
                    else None
                ),
                target_type=target_type,
                target_id=serialize_uuid(target_id),
                owner=None,
                locked_by=None,
                locked_at=None,
                last_touched_by=None,
                last_touched_at=None,
                lock_note=None,
            )

        self._require_target_matches_scope(target=target, entity_id=entity_id)
        return _build_ownership_state(target)

    def acquire_lock(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        target_type: OwnershipTargetType,
        target_id: UUID,
        close_run_id: UUID | None,
        owner_user_id: UUID | None,
        note: str | None,
    ) -> OwnershipState:
        """Assign an owner and acquire an in-progress lock unless another user holds it."""

        resolved_scope = self._resolve_scope(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type=target_type,
            target_id=target_id,
        )
        existing_target = self._repository.get_target(target_type=target_type, target_id=target_id)
        if existing_target is not None:
            self._require_target_matches_scope(target=existing_target, entity_id=entity_id)
            if (
                existing_target.locked_by is not None
                and existing_target.locked_by.id != actor_user.id
            ):
                raise OwnershipServiceError(
                    status_code=409,
                    code=OwnershipServiceErrorCode.LOCK_CONFLICT,
                    message=(
                        f"{existing_target.locked_by.full_name} is already working on this item. "
                        "Ask them to release it before making review changes."
                    ),
                )

        resolved_owner_user_id = owner_user_id or actor_user.id
        if (
            self._repository.get_member_user(entity_id=entity_id, user_id=resolved_owner_user_id)
            is None
        ):
            raise OwnershipServiceError(
                status_code=404,
                code=OwnershipServiceErrorCode.OWNER_NOT_FOUND,
                message="The requested owner is not a member of this workspace.",
            )

        observed_at = utc_now()
        try:
            target = self._repository.upsert_target(
                entity_id=entity_id,
                close_run_id=_scope_close_run_id(resolved_scope),
                target_type=target_type,
                target_id=target_id,
                owner_user_id=resolved_owner_user_id,
                locked_by_user_id=actor_user.id,
                locked_at=observed_at,
                last_touched_by_user_id=actor_user.id,
                last_touched_at=observed_at,
                lock_note=note,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise OwnershipServiceError(
                    status_code=409,
                    code=OwnershipServiceErrorCode.INTEGRITY_CONFLICT,
                    message="Ownership metadata changed while the lock was being acquired.",
                ) from error
            raise

        return _build_ownership_state(target)

    def release_lock(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        target_type: OwnershipTargetType,
        target_id: UUID,
        close_run_id: UUID | None,
    ) -> OwnershipState:
        """Release the caller's lock and fail fast when another user owns the lock."""

        self._resolve_scope(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type=target_type,
            target_id=target_id,
        )
        target = self._repository.get_target(target_type=target_type, target_id=target_id)
        if target is None:
            raise OwnershipServiceError(
                status_code=404,
                code=OwnershipServiceErrorCode.TARGET_NOT_FOUND,
                message="This item does not have ownership metadata to release.",
            )

        self._require_target_matches_scope(target=target, entity_id=entity_id)
        if target.locked_by is None or target.locked_by.id != actor_user.id:
            raise OwnershipServiceError(
                status_code=409,
                code=OwnershipServiceErrorCode.LOCK_NOT_HELD,
                message="Only the operator holding the lock can release it.",
            )

        try:
            released_target = self._repository.release_lock(
                target_type=target_type,
                target_id=target_id,
                last_touched_by_user_id=actor_user.id,
                last_touched_at=utc_now(),
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return _build_ownership_state(released_target)

    def touch_target(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        target_type: OwnershipTargetType,
        target_id: UUID,
        close_run_id: UUID | None,
    ) -> OwnershipState:
        """Record the current user as last touch without taking a lock."""

        resolved_scope = self._resolve_scope(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type=target_type,
            target_id=target_id,
        )
        existing_target = self._repository.get_target(target_type=target_type, target_id=target_id)
        if existing_target is not None:
            self._require_target_matches_scope(target=existing_target, entity_id=entity_id)
            if (
                existing_target.locked_by is not None
                and existing_target.locked_by.id != actor_user.id
            ):
                raise OwnershipServiceError(
                    status_code=409,
                    code=OwnershipServiceErrorCode.LOCK_CONFLICT,
                    message=(
                        f"{existing_target.locked_by.full_name} is already working on this item. "
                        "Refresh before continuing so review changes do not collide."
                    ),
                )

        observed_at = utc_now()
        try:
            target = self._repository.upsert_target(
                entity_id=entity_id,
                close_run_id=_scope_close_run_id(resolved_scope),
                target_type=target_type,
                target_id=target_id,
                owner_user_id=existing_target.owner.id
                if existing_target is not None and existing_target.owner is not None
                else None,
                locked_by_user_id=existing_target.locked_by.id
                if existing_target is not None and existing_target.locked_by is not None
                else None,
                locked_at=existing_target.locked_at if existing_target is not None else None,
                last_touched_by_user_id=actor_user.id,
                last_touched_at=observed_at,
                lock_note=existing_target.lock_note if existing_target is not None else None,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return _build_ownership_state(target)

    def _resolve_scope(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        close_run_id: UUID | None,
        target_type: OwnershipTargetType,
        target_id: UUID,
    ) -> OwnershipCloseRunRecord | OwnershipEntityScope:
        """Validate target scope and return the persisted close-run scope when required."""

        if self._repository.get_entity_for_user(entity_id=entity_id, user_id=actor_user.id) is None:
            raise OwnershipServiceError(
                status_code=404,
                code=OwnershipServiceErrorCode.ENTITY_NOT_FOUND,
                message="That workspace does not exist or is not accessible to the current user.",
            )

        if target_type is OwnershipTargetType.ENTITY:
            if target_id != entity_id or close_run_id is not None:
                raise OwnershipServiceError(
                    status_code=422,
                    code=OwnershipServiceErrorCode.TARGET_SCOPE_INVALID,
                    message="Entity ownership targets must use the entity ID and no close_run_id.",
                )
            return OwnershipEntityScope(close_run_id=None)

        resolved_close_run_id = close_run_id
        if target_type is OwnershipTargetType.CLOSE_RUN:
            resolved_close_run_id = target_id

        if resolved_close_run_id is None:
            raise OwnershipServiceError(
                status_code=422,
                code=OwnershipServiceErrorCode.TARGET_SCOPE_INVALID,
                message="A close_run_id is required for this ownership target.",
            )

        close_run = self._repository.get_close_run_for_user(
            entity_id=entity_id,
            close_run_id=resolved_close_run_id,
            user_id=actor_user.id,
        )
        if close_run is None:
            raise OwnershipServiceError(
                status_code=404,
                code=OwnershipServiceErrorCode.CLOSE_RUN_NOT_FOUND,
                message="That close run does not exist or is not accessible to the current user.",
            )

        return close_run

    def _require_target_matches_scope(
        self,
        *,
        target: OwnershipTargetRecord,
        entity_id: UUID,
    ) -> None:
        """Reject attempts to reuse target UUIDs across entity scopes."""

        if target.entity_id != entity_id:
            raise OwnershipServiceError(
                status_code=409,
                code=OwnershipServiceErrorCode.TARGET_SCOPE_INVALID,
                message="Ownership metadata for this target belongs to a different workspace.",
            )


@dataclass(frozen=True, slots=True)
class OwnershipEntityScope:
    """Represent a validated entity-scoped target that has no close-run parent."""

    close_run_id: None


def _build_ownership_state(target: OwnershipTargetRecord) -> OwnershipState:
    """Convert one repository target record into the public ownership contract."""

    return OwnershipState(
        entity_id=serialize_uuid(target.entity_id),
        close_run_id=(
            serialize_uuid(target.close_run_id) if target.close_run_id is not None else None
        ),
        target_type=target.target_type,
        target_id=serialize_uuid(target.target_id),
        owner=_build_operator_summary(target.owner),
        locked_by=_build_operator_summary(target.locked_by),
        locked_at=target.locked_at,
        last_touched_by=_build_operator_summary(target.last_touched_by),
        last_touched_at=target.last_touched_at,
        lock_note=target.lock_note,
    )


def _scope_close_run_id(scope: OwnershipCloseRunRecord | OwnershipEntityScope) -> UUID | None:
    """Return the close-run UUID for close-run-scoped targets and null for entity targets."""

    if isinstance(scope, OwnershipEntityScope):
        return None

    return scope.id


def _build_operator_summary(user: OwnershipUserRecord | None) -> EntityOperatorSummary | None:
    """Convert an optional ownership user record into the shared operator summary."""

    if user is None:
        return None

    return EntityOperatorSummary(
        id=serialize_uuid(user.id),
        email=user.email,
        full_name=user.full_name,
    )


__all__ = [
    "OwnershipRepositoryProtocol",
    "OwnershipService",
    "OwnershipServiceError",
    "OwnershipServiceErrorCode",
]
