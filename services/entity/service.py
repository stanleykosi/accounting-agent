"""
Purpose: Orchestrate entity workspace creation, updates, membership
management, and timeline reads.
Scope: Entity-domain business rules such as default-actor enforcement,
access checks, and activity-event emission built on top of the repository
layer.
Dependencies: Entity contracts, repository records, audit source
surfaces, and UUID serialization helpers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.common.enums import AutonomyMode
from services.common.types import JsonObject
from services.contracts.entity_models import (
    DEFAULT_WORKSPACE_LANGUAGE,
    EntityActivityEvent,
    EntityListResponse,
    EntityMembershipSummary,
    EntityOperatorSummary,
    EntitySummary,
    EntityWorkspace,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.entity_repo import (
    EntityAccessRecord,
    EntityActivityEventRecord,
    EntityMembershipRecord,
    EntityRecord,
    EntityUserRecord,
)


class EntityServiceErrorCode(StrEnum):
    """Enumerate the stable error codes surfaced by entity workspace workflows."""

    DEFAULT_ACTOR_REQUIRED = "default_actor_required"
    DUPLICATE_MEMBERSHIP = "duplicate_membership"
    ENTITY_NOT_FOUND = "entity_not_found"
    MEMBERSHIP_NOT_FOUND = "membership_not_found"
    USER_NOT_FOUND = "user_not_found"


class EntityServiceError(Exception):
    """Represent an expected entity-domain failure that API routes should expose cleanly."""

    def __init__(self, *, status_code: int, code: EntityServiceErrorCode, message: str) -> None:
        """Capture the HTTP status, stable error code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class EntityRepositoryProtocol(Protocol):
    """Describe the persistence operations required by the canonical entity service."""

    def list_entities_for_user(self, *, user_id: UUID) -> tuple[EntityAccessRecord, ...]:
        """Return the workspaces visible to the specified user."""

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> EntityAccessRecord | None:
        """Return one accessible entity plus the caller's membership when access exists."""

    def create_entity(
        self,
        *,
        name: str,
        legal_name: str | None,
        base_currency: str,
        country_code: str,
        timezone: str,
        accounting_standard: str | None,
        autonomy_mode: AutonomyMode,
    ) -> EntityRecord:
        """Persist a new entity workspace."""

    def update_entity(
        self,
        *,
        entity_id: UUID,
        fields_to_update: frozenset[str],
        name: str | None = None,
        legal_name: str | None = None,
        base_currency: str | None = None,
        country_code: str | None = None,
        timezone: str | None = None,
        accounting_standard: str | None = None,
        autonomy_mode: AutonomyMode | None = None,
    ) -> EntityRecord:
        """Persist a workspace update."""

    def get_user_by_email(self, *, email: str) -> EntityUserRecord | None:
        """Return one local user by canonical email when present."""

    def list_memberships_for_entity(self, *, entity_id: UUID) -> tuple[EntityMembershipRecord, ...]:
        """Return the memberships attached to one entity."""

    def get_membership(
        self,
        *,
        entity_id: UUID,
        membership_id: UUID,
    ) -> EntityMembershipRecord | None:
        """Return one entity membership by UUID when it belongs to the specified workspace."""

    def get_membership_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityMembershipRecord | None:
        """Return one membership by user UUID when it already exists."""

    def create_membership(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
        role: str,
        is_default_actor: bool,
    ) -> EntityMembershipRecord:
        """Persist a new entity membership."""

    def update_membership(
        self,
        *,
        membership_id: UUID,
        role: str | None = None,
        is_default_actor: bool | None = None,
    ) -> EntityMembershipRecord:
        """Persist membership changes."""

    def clear_default_actor_memberships(self, *, entity_id: UUID) -> None:
        """Unset the default-actor flag across one entity."""

    def count_memberships(self, *, entity_id: UUID) -> int:
        """Return the current number of memberships on an entity."""

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> EntityActivityEventRecord:
        """Persist an entity-scoped activity event."""

    def list_activity_for_entity(
        self,
        *,
        entity_id: UUID,
        limit: int,
    ) -> tuple[EntityActivityEventRecord, ...]:
        """Return recent entity-scoped activity events."""

    def get_latest_activity_for_entities(
        self,
        *,
        entity_ids: tuple[UUID, ...],
    ) -> dict[UUID, EntityActivityEventRecord]:
        """Return the newest activity event for each requested entity."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the provided exception originated from a DB integrity failure."""


class EntityService:
    """Provide the canonical entity workspace workflow used by the API and desktop UI."""

    def __init__(self, *, repository: EntityRepositoryProtocol) -> None:
        """Capture the persistence boundary used by entity-domain workflows."""

        self._repository = repository

    def list_entities_for_user(self, *, user_id: UUID) -> EntityListResponse:
        """Return accessible workspaces enriched with membership and activity context."""

        access_records = self._repository.list_entities_for_user(user_id=user_id)
        latest_activity = self._repository.get_latest_activity_for_entities(
            entity_ids=tuple(access_record.entity.id for access_record in access_records)
        )

        entities = tuple(
            self._build_entity_summary(
                access_record=access_record,
                memberships=self._repository.list_memberships_for_entity(
                    entity_id=access_record.entity.id
                ),
                last_activity=latest_activity.get(access_record.entity.id),
            )
            for access_record in access_records
        )
        return EntityListResponse(entities=entities)

    def create_entity(
        self,
        *,
        actor_user: EntityUserRecord,
        name: str,
        legal_name: str | None,
        base_currency: str,
        country_code: str,
        timezone: str,
        accounting_standard: str | None,
        autonomy_mode: AutonomyMode,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> EntityWorkspace:
        """Create a workspace, seed the owner membership, and emit the root activity event."""

        try:
            entity = self._repository.create_entity(
                name=name,
                legal_name=legal_name,
                base_currency=base_currency,
                country_code=country_code,
                timezone=timezone,
                accounting_standard=accounting_standard,
                autonomy_mode=autonomy_mode,
            )
            self._repository.create_membership(
                entity_id=entity.id,
                user_id=actor_user.id,
                role="owner",
                is_default_actor=True,
            )
            self._repository.create_activity_event(
                entity_id=entity.id,
                actor_user_id=actor_user.id,
                event_type="entity.created",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} created the workspace {entity.name} "
                        f"with {entity.base_currency} as the base currency."
                    ),
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return self.get_entity_workspace(user_id=actor_user.id, entity_id=entity.id)

    def get_entity_workspace(self, *, user_id: UUID, entity_id: UUID) -> EntityWorkspace:
        """Return one accessible entity workspace with memberships and activity history."""

        access_record = self._require_entity_access(entity_id=entity_id, user_id=user_id)
        memberships = self._repository.list_memberships_for_entity(entity_id=entity_id)
        activity_events = self._repository.list_activity_for_entity(entity_id=entity_id, limit=20)
        last_activity = activity_events[0] if activity_events else None

        summary = self._build_entity_summary(
            access_record=access_record,
            memberships=memberships,
            last_activity=last_activity,
        )
        return EntityWorkspace(
            **summary.model_dump(),
            memberships=tuple(
                self._build_membership_summary(membership) for membership in memberships
            ),
            activity_events=tuple(
                self._build_activity_event(activity_event) for activity_event in activity_events
            ),
        )

    def update_entity(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        fields_to_update: frozenset[str],
        name: str | None,
        legal_name: str | None,
        base_currency: str | None,
        country_code: str | None,
        timezone: str | None,
        accounting_standard: str | None,
        autonomy_mode: AutonomyMode | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> EntityWorkspace:
        """Update an accessible workspace and emit a timeline event for changed fields."""

        access_record = self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)
        changed_fields = _collect_changed_fields(
            entity=access_record.entity,
            fields_to_update=fields_to_update,
            name=name,
            legal_name=legal_name,
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            accounting_standard=accounting_standard,
            autonomy_mode=autonomy_mode,
        )

        if not changed_fields:
            return self.get_entity_workspace(user_id=actor_user.id, entity_id=entity_id)

        try:
            self._repository.update_entity(
                entity_id=entity_id,
                fields_to_update=fields_to_update,
                name=name,
                legal_name=legal_name,
                base_currency=base_currency,
                country_code=country_code,
                timezone=timezone,
                accounting_standard=accounting_standard,
                autonomy_mode=autonomy_mode,
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type="entity.updated",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} updated workspace settings: "
                        f"{', '.join(sorted(changed_fields))}."
                    ),
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return self.get_entity_workspace(user_id=actor_user.id, entity_id=entity_id)

    def add_membership(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        user_email: str,
        role: str,
        is_default_actor: bool,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> EntityWorkspace:
        """Add an existing local operator to an accessible entity workspace."""

        self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)
        target_user = self._repository.get_user_by_email(email=user_email)
        if target_user is None:
            raise EntityServiceError(
                status_code=404,
                code=EntityServiceErrorCode.USER_NOT_FOUND,
                message="No local operator exists with that email address.",
            )

        existing_membership = self._repository.get_membership_for_user(
            entity_id=entity_id,
            user_id=target_user.id,
        )
        if existing_membership is not None:
            raise EntityServiceError(
                status_code=409,
                code=EntityServiceErrorCode.DUPLICATE_MEMBERSHIP,
                message="That operator already belongs to this workspace.",
            )

        existing_memberships = self._repository.list_memberships_for_entity(entity_id=entity_id)
        should_be_default_actor = is_default_actor or not any(
            membership.is_default_actor for membership in existing_memberships
        )

        try:
            if should_be_default_actor:
                self._repository.clear_default_actor_memberships(entity_id=entity_id)

            membership = self._repository.create_membership(
                entity_id=entity_id,
                user_id=target_user.id,
                role=role,
                is_default_actor=should_be_default_actor,
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type="entity.membership_added",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} added {target_user.full_name} "
                        f"as {membership.role}."
                    ),
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise EntityServiceError(
                    status_code=409,
                    code=EntityServiceErrorCode.DUPLICATE_MEMBERSHIP,
                    message="That operator already belongs to this workspace.",
                ) from error
            raise

        return self.get_entity_workspace(user_id=actor_user.id, entity_id=entity_id)

    def update_membership(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        membership_id: UUID,
        role: str | None,
        is_default_actor: bool | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> EntityWorkspace:
        """Update a workspace membership while preserving exactly one default actor."""

        self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)
        membership = self._repository.get_membership(
            entity_id=entity_id,
            membership_id=membership_id,
        )
        if membership is None:
            raise EntityServiceError(
                status_code=404,
                code=EntityServiceErrorCode.MEMBERSHIP_NOT_FOUND,
                message="That workspace membership does not exist.",
            )

        existing_memberships = self._repository.list_memberships_for_entity(entity_id=entity_id)
        if is_default_actor is False and membership.is_default_actor:
            other_default_actor_exists = any(
                other_membership.id != membership.id and other_membership.is_default_actor
                for other_membership in existing_memberships
            )
            if not other_default_actor_exists:
                raise EntityServiceError(
                    status_code=409,
                    code=EntityServiceErrorCode.DEFAULT_ACTOR_REQUIRED,
                    message="Each workspace must keep one default actor.",
                )

        try:
            if is_default_actor is True:
                self._repository.clear_default_actor_memberships(entity_id=entity_id)

            updated_membership = self._repository.update_membership(
                membership_id=membership_id,
                role=role,
                is_default_actor=is_default_actor,
            )
            changed_fields = [
                field_name
                for field_name, current_value, updated_value in (
                    ("role", membership.role, updated_membership.role),
                    (
                        "default actor",
                        membership.is_default_actor,
                        updated_membership.is_default_actor,
                    ),
                )
                if updated_value != current_value
            ]
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type="entity.membership_updated",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} updated "
                        f"{updated_membership.user.full_name}'s membership: "
                        f"{', '.join(changed_fields) or 'no visible fields'}."
                    ),
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return self.get_entity_workspace(user_id=actor_user.id, entity_id=entity_id)

    def _require_entity_access(self, *, entity_id: UUID, user_id: UUID) -> EntityAccessRecord:
        """Load one accessible entity workspace or raise the canonical not-found error."""

        access_record = self._repository.get_entity_for_user(entity_id=entity_id, user_id=user_id)
        if access_record is None:
            raise EntityServiceError(
                status_code=404,
                code=EntityServiceErrorCode.ENTITY_NOT_FOUND,
                message="That workspace does not exist or is not accessible to the current user.",
            )

        return access_record

    def _build_entity_summary(
        self,
        *,
        access_record: EntityAccessRecord,
        memberships: tuple[EntityMembershipRecord, ...],
        last_activity: EntityActivityEventRecord | None,
    ) -> EntitySummary:
        """Convert repository records into the shared entity summary contract."""

        default_actor_membership = next(
            (membership for membership in memberships if membership.is_default_actor),
            None,
        )
        default_actor = (
            self._build_operator_summary(default_actor_membership.user)
            if default_actor_membership is not None
            else None
        )
        return EntitySummary(
            id=serialize_uuid(access_record.entity.id),
            name=access_record.entity.name,
            legal_name=access_record.entity.legal_name,
            base_currency=access_record.entity.base_currency,
            country_code=access_record.entity.country_code,
            timezone=access_record.entity.timezone,
            workspace_language=DEFAULT_WORKSPACE_LANGUAGE,
            accounting_standard=access_record.entity.accounting_standard,
            autonomy_mode=access_record.entity.autonomy_mode,
            status=access_record.entity.status.value,
            member_count=max(1, len(memberships)),
            current_user_membership=self._build_membership_summary(access_record.membership),
            default_actor=default_actor,
            last_activity=(
                self._build_activity_event(last_activity)
                if last_activity is not None
                else None
            ),
            default_confidence_thresholds=access_record.entity.default_confidence_thresholds,
            created_at=access_record.entity.created_at,
            updated_at=access_record.entity.updated_at,
        )

    def _build_operator_summary(self, user: EntityUserRecord) -> EntityOperatorSummary:
        """Convert one repository user record into the shared operator summary contract."""

        return EntityOperatorSummary(
            id=serialize_uuid(user.id),
            email=user.email,
            full_name=user.full_name,
        )

    def _build_membership_summary(
        self,
        membership: EntityMembershipRecord,
    ) -> EntityMembershipSummary:
        """Convert one repository membership record into the shared membership contract."""

        return EntityMembershipSummary(
            id=serialize_uuid(membership.id),
            role=membership.role,
            is_default_actor=membership.is_default_actor,
            user=self._build_operator_summary(membership.user),
        )

    def _build_activity_event(
        self,
        activity_event: EntityActivityEventRecord,
    ) -> EntityActivityEvent:
        """Convert one repository audit-event record into the shared timeline contract."""

        summary = activity_event.payload.get("summary")
        if not isinstance(summary, str) or not summary:
            summary = activity_event.event_type

        return EntityActivityEvent(
            id=serialize_uuid(activity_event.id),
            event_type=activity_event.event_type,
            summary=summary,
            source_surface=activity_event.source_surface.value,
            trace_id=activity_event.trace_id,
            created_at=activity_event.created_at,
            actor=(
                self._build_operator_summary(activity_event.actor)
                if activity_event.actor is not None
                else None
            ),
        )


def _collect_changed_fields(
    *,
    entity: EntityRecord,
    fields_to_update: frozenset[str],
    name: str | None,
    legal_name: str | None,
    base_currency: str | None,
    country_code: str | None,
    timezone: str | None,
    accounting_standard: str | None,
    autonomy_mode: AutonomyMode | None,
) -> set[str]:
    """Return provided entity fields whose incoming values differ from the current state."""

    changed_fields: set[str] = set()
    comparisons = (
        ("name", entity.name, name),
        ("legal_name", entity.legal_name, legal_name),
        ("base_currency", entity.base_currency, base_currency),
        ("country_code", entity.country_code, country_code),
        ("timezone", entity.timezone, timezone),
        ("accounting_standard", entity.accounting_standard, accounting_standard),
        ("autonomy_mode", entity.autonomy_mode, autonomy_mode),
    )
    for field_name, current_value, incoming_value in comparisons:
        if field_name not in fields_to_update:
            continue
        if incoming_value != current_value:
            changed_fields.add(field_name)

    return changed_fields


__all__ = ["EntityService", "EntityServiceError", "EntityServiceErrorCode"]
