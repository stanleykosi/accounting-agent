"""
Purpose: Verify chart-of-accounts source precedence and versioned revision behavior.
Scope: Unit coverage for fallback creation, precedence activation, and account-edit versioning
using an in-memory repository double.
Dependencies: COA service/contracts modules, entity status enum, and audit source surface values.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

from services.coa.importer import ImportedCoaAccountSeed
from services.coa.service import (
    CoaAccountRecord,
    CoaEntityRecord,
    CoaRepositoryProtocol,
    CoaService,
    CoaSetRecord,
)
from services.common.types import JsonObject, utc_now
from services.contracts.coa_models import CoaAccountCreateRequest
from services.db.models.audit import AuditSourceSurface
from services.db.models.coa import CoaSetSource
from services.db.models.entity import EntityStatus
from services.db.repositories.entity_repo import EntityUserRecord


def test_read_workspace_prefers_manual_set_over_quickbooks_when_no_active_set() -> None:
    """Ensure precedence activates manual-upload sets before quickbooks-sync sets."""

    repository = InMemoryCoaRepository()
    actor = repository.seed_user(email="lead@example.com", full_name="Finance Lead")
    entity = repository.seed_entity(name="Northwind")

    repository.seed_set(
        entity_id=entity.id,
        source=CoaSetSource.QUICKBOOKS_SYNC,
        version_no=2,
        is_active=False,
        account_codes=("1100",),
    )
    manual_set = repository.seed_set(
        entity_id=entity.id,
        source=CoaSetSource.MANUAL_UPLOAD,
        version_no=3,
        is_active=False,
        account_codes=("1000", "1010"),
    )

    workspace = CoaService(repository=repository).read_workspace(
        actor_user=actor,
        entity_id=entity.id,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-1",
    )

    assert workspace.active_set.id == str(manual_set.id)
    assert workspace.active_set.source == CoaSetSource.MANUAL_UPLOAD.value
    assert workspace.active_set.is_active is True


def test_read_workspace_creates_fallback_set_when_no_source_sets_exist() -> None:
    """Ensure the service creates and activates the Nigerian SME fallback when no sets exist."""

    repository = InMemoryCoaRepository()
    actor = repository.seed_user(email="lead@example.com", full_name="Finance Lead")
    entity = repository.seed_entity(name="Northwind")

    workspace = CoaService(repository=repository).read_workspace(
        actor_user=actor,
        entity_id=entity.id,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-2",
    )

    assert workspace.active_set.source == CoaSetSource.FALLBACK_NIGERIAN_SME.value
    assert workspace.active_set.is_active is True
    assert workspace.active_set.account_count > 5
    assert any(event["event_type"] == "coa.fallback_created" for event in repository.events)


def test_create_account_materializes_new_manual_revision_version() -> None:
    """Ensure account creation writes a new manual COA version instead of mutating in place."""

    repository = InMemoryCoaRepository()
    actor = repository.seed_user(email="lead@example.com", full_name="Finance Lead")
    entity = repository.seed_entity(name="Northwind")
    active_set = repository.seed_set(
        entity_id=entity.id,
        source=CoaSetSource.FALLBACK_NIGERIAN_SME,
        version_no=1,
        is_active=True,
        account_codes=("1000", "1010"),
    )

    workspace = CoaService(repository=repository).create_account(
        actor_user=actor,
        entity_id=entity.id,
        payload=CoaAccountCreateRequest(
            account_code="6100",
            account_name="Office Supplies",
            account_type="expense",
        ),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="req-3",
    )

    assert workspace.active_set.version_no == 2
    assert workspace.active_set.source == CoaSetSource.MANUAL_UPLOAD.value
    assert workspace.active_set.id != str(active_set.id)
    assert any(account.account_code == "6100" for account in workspace.accounts)

    previous_set = repository.sets[active_set.id]
    assert previous_set.is_active is False


class InMemoryCoaRepository(CoaRepositoryProtocol):
    """Provide an in-memory COA repository double for pure service-level unit tests."""

    def __init__(self) -> None:
        """Initialize in-memory stores for entities, sets, accounts, and event logs."""

        self.entities: dict[UUID, CoaEntityRecord] = {}
        self.users: dict[UUID, EntityUserRecord] = {}
        self.sets: dict[UUID, CoaSetRecord] = {}
        self.accounts: dict[UUID, tuple[CoaAccountRecord, ...]] = {}
        self.memberships: dict[UUID, set[UUID]] = {}
        self.events: list[JsonObject] = []

    def seed_user(self, *, email: str, full_name: str) -> EntityUserRecord:
        """Create one local user record with deterministic in-memory storage."""

        user = EntityUserRecord(id=uuid4(), email=email, full_name=full_name)
        self.users[user.id] = user
        return user

    def seed_entity(self, *, name: str) -> CoaEntityRecord:
        """Create one active entity and grant membership to all seeded users."""

        entity = CoaEntityRecord(id=uuid4(), name=name, status=EntityStatus.ACTIVE)
        self.entities[entity.id] = entity
        self.memberships[entity.id] = set(self.users.keys())
        return entity

    def seed_set(
        self,
        *,
        entity_id: UUID,
        source: CoaSetSource,
        version_no: int,
        is_active: bool,
        account_codes: tuple[str, ...],
    ) -> CoaSetRecord:
        """Create one COA set with simple account rows for test setup."""

        coa_set = CoaSetRecord(
            id=uuid4(),
            entity_id=entity_id,
            source=source,
            version_no=version_no,
            is_active=is_active,
            import_metadata={},
            activated_at=utc_now() if is_active else None,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.sets[coa_set.id] = coa_set

        seeded_accounts: list[CoaAccountRecord] = []
        for account_code in account_codes:
            seeded_accounts.append(
                CoaAccountRecord(
                    id=uuid4(),
                    coa_set_id=coa_set.id,
                    account_code=account_code,
                    account_name=f"Account {account_code}",
                    account_type="asset",
                    parent_account_id=None,
                    is_postable=True,
                    is_active=True,
                    external_ref=None,
                    dimension_defaults={},
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        self.accounts[coa_set.id] = tuple(seeded_accounts)

        if is_active:
            self.deactivate_all_sets(entity_id=entity_id)
            self.sets[coa_set.id] = coa_set

        return coa_set

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> CoaEntityRecord | None:
        """Return one entity when the given user belongs to the in-memory membership list."""

        if user_id not in self.memberships.get(entity_id, set()):
            return None

        return self.entities.get(entity_id)

    def get_active_set(self, *, entity_id: UUID) -> CoaSetRecord | None:
        """Return the currently active set for one entity, if any."""

        active_sets = [
            coa_set
            for coa_set in self.sets.values()
            if coa_set.entity_id == entity_id and coa_set.is_active
        ]
        if not active_sets:
            return None

        return sorted(active_sets, key=lambda coa_set: coa_set.version_no, reverse=True)[0]

    def get_latest_set_for_source(
        self,
        *,
        entity_id: UUID,
        source: CoaSetSource,
    ) -> CoaSetRecord | None:
        """Return the latest set for one source in descending version order."""

        matching_sets = [
            coa_set
            for coa_set in self.sets.values()
            if coa_set.entity_id == entity_id and coa_set.source is source
        ]
        if not matching_sets:
            return None

        return sorted(matching_sets, key=lambda coa_set: coa_set.version_no, reverse=True)[0]

    def get_set_for_entity(self, *, entity_id: UUID, coa_set_id: UUID) -> CoaSetRecord | None:
        """Return one set when it belongs to the specified entity."""

        coa_set = self.sets.get(coa_set_id)
        if coa_set is None or coa_set.entity_id != entity_id:
            return None

        return coa_set

    def list_coa_sets_for_entity(self, *, entity_id: UUID) -> tuple[CoaSetRecord, ...]:
        """Return all entity set versions in descending version order."""

        matching_sets = [
            coa_set for coa_set in self.sets.values() if coa_set.entity_id == entity_id
        ]
        matching_sets.sort(key=lambda coa_set: coa_set.version_no, reverse=True)
        return tuple(matching_sets)

    def list_accounts_for_set(self, *, coa_set_id: UUID) -> tuple[CoaAccountRecord, ...]:
        """Return set accounts in deterministic code order."""

        accounts = sorted(
            self.accounts.get(coa_set_id, ()), key=lambda account: account.account_code
        )
        return tuple(accounts)

    def next_version_no(self, *, entity_id: UUID) -> int:
        """Return the next COA version number for one entity."""

        current_versions = [
            coa_set.version_no for coa_set in self.sets.values() if coa_set.entity_id == entity_id
        ]
        return (max(current_versions) if current_versions else 0) + 1

    def create_set(
        self,
        *,
        entity_id: UUID,
        source: CoaSetSource,
        version_no: int,
        import_metadata: JsonObject,
        is_active: bool = False,
        activated_at: datetime | None = None,
    ) -> CoaSetRecord:
        """Create one in-memory COA set record."""

        coa_set = CoaSetRecord(
            id=uuid4(),
            entity_id=entity_id,
            source=source,
            version_no=version_no,
            is_active=is_active,
            import_metadata=dict(import_metadata),
            activated_at=activated_at,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.sets[coa_set.id] = coa_set
        self.accounts.setdefault(coa_set.id, ())
        return coa_set

    def deactivate_all_sets(self, *, entity_id: UUID) -> None:
        """Deactivate all sets for one entity in place."""

        for set_id, coa_set in list(self.sets.items()):
            if coa_set.entity_id != entity_id or not coa_set.is_active:
                continue
            self.sets[set_id] = replace(
                coa_set,
                is_active=False,
                updated_at=utc_now(),
            )

    def activate_set(self, *, coa_set_id: UUID, activated_at: datetime) -> CoaSetRecord:
        """Activate one set and return the refreshed in-memory record."""

        coa_set = self.sets[coa_set_id]
        activated = replace(
            coa_set, is_active=True, activated_at=activated_at, updated_at=utc_now()
        )
        self.sets[coa_set_id] = activated
        return activated

    def create_accounts_bulk(
        self,
        *,
        coa_set_id: UUID,
        accounts: tuple[ImportedCoaAccountSeed, ...],
    ) -> tuple[CoaAccountRecord, ...]:
        """Create account rows for one set and resolve parent IDs by account code."""

        account_rows: list[CoaAccountRecord] = []
        ids_by_code: dict[str, UUID] = {}

        for account in accounts:
            account_id = uuid4()
            ids_by_code[account.account_code] = account_id
            account_rows.append(
                CoaAccountRecord(
                    id=account_id,
                    coa_set_id=coa_set_id,
                    account_code=account.account_code,
                    account_name=account.account_name,
                    account_type=account.account_type,
                    parent_account_id=None,
                    is_postable=account.is_postable,
                    is_active=account.is_active,
                    external_ref=account.external_ref,
                    dimension_defaults=dict(account.dimension_defaults),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )

        account_rows = [
            replace(
                account_row,
                parent_account_id=(
                    ids_by_code[parent_code]
                    if (parent_code := accounts[index].parent_account_code) is not None
                    else None
                ),
            )
            for index, account_row in enumerate(account_rows)
        ]
        self.accounts[coa_set_id] = tuple(account_rows)
        return tuple(account_rows)

    def list_set_account_counts(self, *, entity_id: UUID) -> dict[UUID, int]:
        """Return account counts keyed by set ID for one entity."""

        return {
            coa_set.id: len(self.accounts.get(coa_set.id, ()))
            for coa_set in self.sets.values()
            if coa_set.entity_id == entity_id
        }

    def create_activity_event(
        self,
        *,
        entity_id: UUID,
        actor_user_id: UUID | None,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        trace_id: str | None,
    ) -> None:
        """Record one in-memory COA activity event for assertions."""

        self.events.append(
            {
                "actor_user_id": str(actor_user_id) if actor_user_id is not None else None,
                "entity_id": str(entity_id),
                "event_type": event_type,
                "payload": dict(payload),
                "source_surface": source_surface.value,
                "trace_id": trace_id,
            }
        )

    def commit(self) -> None:
        """Commit is a no-op for the in-memory repository double."""

    def rollback(self) -> None:
        """Rollback is a no-op for the in-memory repository double."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return false because in-memory test repository does not raise DB integrity errors."""

        _ = error
        return False
