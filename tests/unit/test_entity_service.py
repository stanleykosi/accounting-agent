"""
Purpose: Verify the canonical entity service behavior for workspace
creation, updates, memberships, and activity timeline roots.
Scope: Pure unit coverage over entity-domain rules using an in-memory
repository double instead of a live database.
Dependencies: Entity service modules, repository record dataclasses,
and canonical audit and entity enums.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from services.common.enums import AutonomyMode
from services.common.types import JsonObject
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.entity_repo import (
    EntityAccessRecord,
    EntityActivityEventRecord,
    EntityMembershipRecord,
    EntityRecord,
    EntityUserRecord,
)
from services.entity.service import EntityService, EntityServiceError, EntityServiceErrorCode


def test_create_entity_seeds_owner_membership_and_activity_event() -> None:
    """Ensure workspace creation attaches the creator as owner and records the first event."""

    repository = InMemoryEntityRepository()
    actor = repository.add_user(email="finance@example.com", full_name="Finance Lead")
    service = EntityService(repository=repository)

    workspace = service.create_entity(
        actor_user=actor,
        name="Northwind Nigeria",
        legal_name="Northwind Nigeria Limited",
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard="IFRS for SMEs",
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-123",
    )

    assert workspace.name == "Northwind Nigeria"
    assert workspace.current_user_membership.role == "owner"
    assert workspace.current_user_membership.is_default_actor is True
    assert workspace.default_actor is not None
    assert workspace.default_actor.email == actor.email
    assert workspace.activity_events[0].event_type == "entity.created"


def test_create_entity_rejects_duplicate_accessible_workspace_name() -> None:
    """Workspace creation should fail fast when the caller already has that display name."""

    repository = InMemoryEntityRepository()
    actor = repository.add_user(email="finance@example.com", full_name="Finance Lead")
    service = EntityService(repository=repository)
    service.create_entity(
        actor_user=actor,
        name="Stanley",
        legal_name="Stanley Holdings Limited",
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard=None,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-123",
    )

    with pytest.raises(EntityServiceError) as error:
        service.create_entity(
            actor_user=actor,
            name="  stanley  ",
            legal_name="Stanley Trading Limited",
            base_currency="NGN",
            country_code="NG",
            timezone="Africa/Lagos",
            accounting_standard=None,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id="req-124",
        )

    assert error.value.status_code == 409
    assert error.value.code is EntityServiceErrorCode.DUPLICATE_ENTITY
    assert len(repository.entities) == 1


def test_create_entity_rejects_duplicate_accessible_legal_name() -> None:
    """Workspace creation should fail fast when legal identity already exists."""

    repository = InMemoryEntityRepository()
    actor = repository.add_user(email="finance@example.com", full_name="Finance Lead")
    service = EntityService(repository=repository)
    service.create_entity(
        actor_user=actor,
        name="Stanley",
        legal_name="Stanley Holdings Limited",
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard=None,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-123",
    )

    with pytest.raises(EntityServiceError) as error:
        service.create_entity(
            actor_user=actor,
            name="Stanley Trading",
            legal_name=" stanley holdings limited ",
            base_currency="NGN",
            country_code="NG",
            timezone="Africa/Lagos",
            accounting_standard=None,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id="req-124",
        )

    assert error.value.code is EntityServiceErrorCode.DUPLICATE_ENTITY
    assert len(repository.entities) == 1


def test_add_membership_can_switch_default_actor() -> None:
    """Ensure adding a member as the new default actor clears the previous default assignment."""

    repository = InMemoryEntityRepository()
    actor = repository.add_user(email="finance@example.com", full_name="Finance Lead")
    reviewer = repository.add_user(email="reviewer@example.com", full_name="Reviewer")
    service = EntityService(repository=repository)
    workspace = service.create_entity(
        actor_user=actor,
        name="Northwind Nigeria",
        legal_name=None,
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard=None,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-123",
    )

    refreshed_workspace = service.add_membership(
        actor_user=actor,
        entity_id=UUID(workspace.id),
        user_email=reviewer.email,
        role="reviewer",
        is_default_actor=True,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-124",
    )

    default_actors = [
        membership.user.email
        for membership in refreshed_workspace.memberships
        if membership.is_default_actor
    ]

    assert default_actors == [reviewer.email]
    assert refreshed_workspace.activity_events[0].event_type == "entity.membership_added"


def test_update_membership_rejects_removing_last_default_actor() -> None:
    """Ensure the service fails fast when a workspace would lose its last default actor."""

    repository = InMemoryEntityRepository()
    actor = repository.add_user(email="finance@example.com", full_name="Finance Lead")
    service = EntityService(repository=repository)
    workspace = service.create_entity(
        actor_user=actor,
        name="Northwind Nigeria",
        legal_name=None,
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard=None,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-123",
    )

    with pytest.raises(EntityServiceError) as error:
        service.update_membership(
            actor_user=actor,
            entity_id=UUID(workspace.id),
            membership_id=UUID(workspace.current_user_membership.id),
            role=None,
            is_default_actor=False,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id="req-125",
        )

    assert error.value.status_code == 409
    assert error.value.code is EntityServiceErrorCode.DEFAULT_ACTOR_REQUIRED


def test_update_entity_can_clear_nullable_fields_when_explicitly_provided() -> None:
    """Ensure PATCH updates can clear nullable workspace fields when explicitly provided."""

    repository = InMemoryEntityRepository()
    actor = repository.add_user(email="finance@example.com", full_name="Finance Lead")
    service = EntityService(repository=repository)
    workspace = service.create_entity(
        actor_user=actor,
        name="Northwind Nigeria",
        legal_name="Northwind Nigeria Limited",
        base_currency="NGN",
        country_code="NG",
        timezone="Africa/Lagos",
        accounting_standard="IFRS for SMEs",
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-123",
    )

    refreshed_workspace = service.update_entity(
        actor_user=actor,
        entity_id=UUID(workspace.id),
        fields_to_update=frozenset({"legal_name", "accounting_standard"}),
        name=None,
        legal_name=None,
        base_currency=None,
        country_code=None,
        timezone=None,
        accounting_standard=None,
        autonomy_mode=None,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-126",
    )

    assert refreshed_workspace.legal_name is None
    assert refreshed_workspace.accounting_standard is None
    assert refreshed_workspace.activity_events[0].event_type == "entity.updated"


class InMemoryEntityRepository:
    """Provide the minimal repository surface required by the entity service for unit tests."""

    def __init__(self) -> None:
        """Initialize in-memory stores for users, entities, memberships, and activity events."""

        self.users: dict[UUID, EntityUserRecord] = {}
        self.entities: dict[UUID, EntityRecord] = {}
        self.memberships: dict[UUID, EntityMembershipRecord] = {}
        self.activity_events: dict[UUID, EntityActivityEventRecord] = {}

    def add_user(self, *, email: str, full_name: str) -> EntityUserRecord:
        """Seed one in-memory local operator record for test setup."""

        user = EntityUserRecord(id=uuid4(), email=email, full_name=full_name)
        self.users[user.id] = user
        return user

    def list_entities_for_user(self, *, user_id: UUID) -> tuple[EntityAccessRecord, ...]:
        """Return the current user's accessible entity rows together with their memberships."""

        matching_memberships = [
            membership
            for membership in self.memberships.values()
            if membership.user_id == user_id
        ]
        ordered_memberships = sorted(
            matching_memberships,
            key=lambda membership: self.entities[membership.entity_id].updated_at,
            reverse=True,
        )
        return tuple(
            EntityAccessRecord(
                entity=self.entities[membership.entity_id],
                membership=membership,
            )
            for membership in ordered_memberships
        )

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> EntityAccessRecord | None:
        """Return one entity and membership when the given user can access it."""

        membership = next(
            (
                membership
                for membership in self.memberships.values()
                if membership.entity_id == entity_id and membership.user_id == user_id
            ),
            None,
        )
        entity = self.entities.get(entity_id)
        if membership is None or entity is None:
            return None

        return EntityAccessRecord(entity=entity, membership=membership)

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
        """Store one new in-memory entity record."""

        now = utc_now()
        entity = EntityRecord(
            id=uuid4(),
            name=name,
            legal_name=legal_name,
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            accounting_standard=accounting_standard,
            autonomy_mode=autonomy_mode,
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
        self.entities[entity.id] = entity
        return entity

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
        """Update one in-memory entity record and return the refreshed value."""

        entity = self.entities[entity_id]
        updated_entity = replace(
            entity,
            name=name if "name" in fields_to_update and name is not None else entity.name,
            legal_name=legal_name if "legal_name" in fields_to_update else entity.legal_name,
            base_currency=(
                base_currency
                if "base_currency" in fields_to_update and base_currency is not None
                else entity.base_currency
            ),
            country_code=(
                country_code
                if "country_code" in fields_to_update and country_code is not None
                else entity.country_code
            ),
            timezone=(
                timezone
                if "timezone" in fields_to_update and timezone is not None
                else entity.timezone
            ),
            accounting_standard=(
                accounting_standard
                if "accounting_standard" in fields_to_update
                else entity.accounting_standard
            ),
            autonomy_mode=(
                autonomy_mode
                if "autonomy_mode" in fields_to_update and autonomy_mode is not None
                else entity.autonomy_mode
            ),
            updated_at=utc_now(),
        )
        self.entities[entity_id] = updated_entity
        return updated_entity

    def get_user_by_email(self, *, email: str) -> EntityUserRecord | None:
        """Return one seeded user by email when present."""

        return next((user for user in self.users.values() if user.email == email), None)

    def list_memberships_for_entity(self, *, entity_id: UUID) -> tuple[EntityMembershipRecord, ...]:
        """Return one entity's memberships in default-actor then name order."""

        memberships = [
            membership
            for membership in self.memberships.values()
            if membership.entity_id == entity_id
        ]
        memberships.sort(
            key=lambda membership: (
                not membership.is_default_actor,
                membership.user.full_name,
                membership.user.email,
            )
        )
        return tuple(memberships)

    def get_membership(
        self,
        *,
        entity_id: UUID,
        membership_id: UUID,
    ) -> EntityMembershipRecord | None:
        """Return one membership by UUID when it belongs to the target entity."""

        membership = self.memberships.get(membership_id)
        if membership is None or membership.entity_id != entity_id:
            return None

        return membership

    def get_membership_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> EntityMembershipRecord | None:
        """Return one entity membership for the specified user when it exists."""

        return next(
            (
                membership
                for membership in self.memberships.values()
                if membership.entity_id == entity_id and membership.user_id == user_id
            ),
            None,
        )

    def create_membership(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
        role: str,
        is_default_actor: bool,
    ) -> EntityMembershipRecord:
        """Store one new in-memory entity membership record."""

        now = utc_now()
        membership = EntityMembershipRecord(
            id=uuid4(),
            entity_id=entity_id,
            user_id=user_id,
            role=role,
            is_default_actor=is_default_actor,
            created_at=now,
            updated_at=now,
            user=self.users[user_id],
        )
        self.memberships[membership.id] = membership
        return membership

    def update_membership(
        self,
        *,
        membership_id: UUID,
        role: str | None = None,
        is_default_actor: bool | None = None,
    ) -> EntityMembershipRecord:
        """Update one in-memory membership record and return the refreshed value."""

        membership = self.memberships[membership_id]
        updated_membership = replace(
            membership,
            role=role if role is not None else membership.role,
            is_default_actor=(
                is_default_actor if is_default_actor is not None else membership.is_default_actor
            ),
            updated_at=utc_now(),
        )
        self.memberships[membership_id] = updated_membership
        return updated_membership

    def clear_default_actor_memberships(self, *, entity_id: UUID) -> None:
        """Unset the default-actor flag across one entity's in-memory memberships."""

        for membership_id, membership in tuple(self.memberships.items()):
            if membership.entity_id != entity_id or not membership.is_default_actor:
                continue

            self.memberships[membership_id] = replace(
                membership,
                is_default_actor=False,
                updated_at=utc_now(),
            )

    def count_memberships(self, *, entity_id: UUID) -> int:
        """Return the number of in-memory memberships attached to one entity."""

        return sum(
            1 for membership in self.memberships.values() if membership.entity_id == entity_id
        )

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
        """Store one in-memory entity activity event."""

        event = EntityActivityEventRecord(
            id=uuid4(),
            entity_id=entity_id,
            event_type=event_type,
            source_surface=source_surface,
            payload=payload,
            trace_id=trace_id,
            created_at=utc_now(),
            actor=self.users.get(actor_user_id) if actor_user_id is not None else None,
        )
        self.activity_events[event.id] = event
        return event

    def list_activity_for_entity(
        self,
        *,
        entity_id: UUID,
        limit: int,
    ) -> tuple[EntityActivityEventRecord, ...]:
        """Return recent in-memory entity events in newest-first order."""

        events = [
            event
            for event in self.activity_events.values()
            if event.entity_id == entity_id
        ]
        events.sort(key=lambda event: event.created_at, reverse=True)
        return tuple(events[:limit])

    def get_latest_activity_for_entities(
        self,
        *,
        entity_ids: tuple[UUID, ...],
    ) -> dict[UUID, EntityActivityEventRecord]:
        """Return the newest in-memory activity event for each requested entity."""

        latest_events: dict[UUID, EntityActivityEventRecord] = {}
        for entity_id in entity_ids:
            entity_events = self.list_activity_for_entity(entity_id=entity_id, limit=1)
            if entity_events:
                latest_events[entity_id] = entity_events[0]

        return latest_events

    def commit(self) -> None:
        """Treat successful in-memory operations as immediately committed."""

    def rollback(self) -> None:
        """Treat rollback as a no-op in the in-memory test double."""

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return False because the in-memory repository does not emulate DB integrity errors."""

        return False


def utc_now() -> datetime:
    """Return a stable UTC-aware timestamp for the in-memory repository test double."""

    return datetime.now(tz=UTC) + timedelta(microseconds=1)
