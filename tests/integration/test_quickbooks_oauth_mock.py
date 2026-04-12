"""
Purpose: Cover the mocked QuickBooks OAuth/token and chart-of-accounts sync flow.
Scope: Signed OAuth state validation, token exchange parsing, and QuickBooks account normalization
into canonical COA set creation without hitting Intuit or a live database.
Dependencies: httpx mock transports, typed settings, QuickBooks OAuth/sync services, and in-memory
repository doubles.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
from services.coa.importer import ImportedCoaAccountSeed
from services.coa.service import CoaAccountRecord, CoaSetRecord
from services.common.settings import AppSettings
from services.common.types import JsonObject, utc_now
from services.db.models.audit import AuditSourceSurface
from services.db.models.coa import CoaSetSource
from services.db.repositories.entity_repo import EntityUserRecord
from services.integrations.quickbooks.oauth import QuickBooksOAuth
from services.integrations.quickbooks.sync_accounts import (
    normalize_quickbooks_accounts,
    sync_chart_of_accounts,
)


def test_quickbooks_oauth_state_and_token_exchange_are_mockable() -> None:
    """Ensure OAuth state is signed locally and token exchange parses mocked Intuit responses."""

    settings = _quickbooks_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a deterministic token payload for the mocked Intuit token endpoint."""

        assert str(request.url) == "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
        return httpx.Response(
            200,
            json={
                "access_token": "access-token-1",
                "expires_in": 3600,
                "refresh_token": "refresh-token-1",
                "token_type": "bearer",
                "x_refresh_token_expires_in": 8726400,
            },
        )

    oauth = QuickBooksOAuth(
        settings=settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    entity_id = uuid4()
    actor_user_id = uuid4()

    authorization = oauth.build_authorization_url(
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        return_url="http://127.0.0.1:3000/entities/demo/integrations",
        now_epoch=1_700_000_000,
    )
    restored_state = oauth.validate_state(
        state=authorization.state,
        now_epoch=1_700_000_100,
    )
    token_set = oauth.exchange_code_for_tokens(code="auth-code", realm_id="1234567890")

    assert restored_state.entity_id == entity_id
    assert restored_state.actor_user_id == actor_user_id
    assert "client_id=quickbooks-client-id" in authorization.authorization_url
    assert token_set.access_token == "access-token-1"
    assert token_set.refresh_token == "refresh-token-1"
    assert token_set.realm_id == "1234567890"


def test_quickbooks_account_sync_creates_inactive_set_when_manual_coa_exists() -> None:
    """Ensure QuickBooks sync respects manual-upload precedence and still persists a QBO set."""

    repository = InMemoryQuickBooksCoaRepository()
    integration_repository = InMemoryIntegrationRepository()
    entity_id = uuid4()
    manual_set = repository.seed_set(
        entity_id=entity_id,
        source=CoaSetSource.MANUAL_UPLOAD,
        version_no=1,
        is_active=True,
    )
    actor_user = EntityUserRecord(
        id=uuid4(),
        email="controller@example.com",
        full_name="Demo Controller",
    )

    result = sync_chart_of_accounts(
        entity_id=entity_id,
        actor_user=actor_user,
        quickbooks_client=FakeQuickBooksClient(),
        coa_repository=repository,
        integration_repository=cast(Any, integration_repository),
        connection_id=uuid4(),
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-qbo-1",
    )

    assert result.account_count == 2
    assert result.activated is False
    assert repository.sets[manual_set.id].is_active is True
    assert repository.sets[result.coa_set.id].source is CoaSetSource.QUICKBOOKS_SYNC
    assert repository.sets[result.coa_set.id].is_active is False
    assert integration_repository.synced_at is not None


def test_normalize_quickbooks_accounts_resolves_parent_codes() -> None:
    """Ensure QuickBooks parent refs become COA parent account codes."""

    seeds = normalize_quickbooks_accounts(
        (
            {
                "AcctNum": "1000",
                "Active": True,
                "Id": "1",
                "Name": "Cash",
                "AccountType": "Bank",
            },
            {
                "AcctNum": "1010",
                "Active": True,
                "Id": "2",
                "Name": "Operating Account",
                "AccountType": "Bank",
                "ParentRef": {"value": "1"},
            },
        )
    )

    child = next(seed for seed in seeds if seed.account_code == "1010")
    assert child.parent_account_code == "1000"
    assert child.account_type == "asset"


class FakeQuickBooksClient:
    """Provide deterministic QuickBooks account records for sync tests."""

    def query_accounts(self) -> tuple[JsonObject, ...]:
        """Return a small parent/child account tree."""

        return (
            {
                "AcctNum": "4000",
                "Active": True,
                "FullyQualifiedName": "Revenue",
                "Id": "qb-4000",
                "Name": "Revenue",
                "AccountType": "Income",
            },
            {
                "AcctNum": "4010",
                "Active": True,
                "FullyQualifiedName": "Revenue:Product Sales",
                "Id": "qb-4010",
                "Name": "Product Sales",
                "AccountType": "Income",
                "ParentRef": {"value": "qb-4000"},
            },
        )


class InMemoryIntegrationRepository:
    """Record sync timestamps without using a live database."""

    def __init__(self) -> None:
        """Initialize sync metadata."""

        self.synced_at: datetime | None = None

    def mark_synced(self, *, connection_id: UUID, synced_at: datetime) -> object:
        """Persist the latest successful sync timestamp in memory."""

        _ = connection_id
        self.synced_at = synced_at
        return object()


class InMemoryQuickBooksCoaRepository:
    """Provide the subset of COA repository behavior required by QuickBooks sync."""

    def __init__(self) -> None:
        """Initialize in-memory COA set and account stores."""

        self.sets: dict[UUID, CoaSetRecord] = {}
        self.accounts: dict[UUID, tuple[CoaAccountRecord, ...]] = {}
        self.events: list[JsonObject] = []
        self.committed = False

    def seed_set(
        self,
        *,
        entity_id: UUID,
        source: CoaSetSource,
        version_no: int,
        is_active: bool,
    ) -> CoaSetRecord:
        """Create one in-memory COA set for test setup."""

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
        self.accounts[coa_set.id] = ()
        return coa_set

    def get_latest_set_for_source(
        self,
        *,
        entity_id: UUID,
        source: CoaSetSource,
    ) -> CoaSetRecord | None:
        """Return the latest set for one source."""

        matching = [
            coa_set
            for coa_set in self.sets.values()
            if coa_set.entity_id == entity_id and coa_set.source is source
        ]
        return max(matching, key=lambda coa_set: coa_set.version_no) if matching else None

    def next_version_no(self, *, entity_id: UUID) -> int:
        """Return the next entity-scoped COA version number."""

        versions = [
            coa_set.version_no
            for coa_set in self.sets.values()
            if coa_set.entity_id == entity_id
        ]
        return (max(versions) if versions else 0) + 1

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
        """Create one COA set in memory."""

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
        self.accounts[coa_set.id] = ()
        return coa_set

    def create_accounts_bulk(
        self,
        *,
        coa_set_id: UUID,
        accounts: tuple[ImportedCoaAccountSeed, ...],
    ) -> tuple[CoaAccountRecord, ...]:
        """Persist account seeds as in-memory COA account records."""

        rows = tuple(
            CoaAccountRecord(
                id=uuid4(),
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
            for account in accounts
        )
        self.accounts[coa_set_id] = rows
        return rows

    def deactivate_all_sets(self, *, entity_id: UUID) -> None:
        """Deactivate all sets for an entity."""

        for set_id, coa_set in tuple(self.sets.items()):
            if coa_set.entity_id == entity_id:
                self.sets[set_id] = replace(coa_set, is_active=False, updated_at=utc_now())

    def activate_set(self, *, coa_set_id: UUID, activated_at: datetime) -> CoaSetRecord:
        """Activate one set in memory."""

        activated = replace(
            self.sets[coa_set_id],
            activated_at=activated_at,
            is_active=True,
            updated_at=utc_now(),
        )
        self.sets[coa_set_id] = activated
        return activated

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
        """Record one activity event in memory."""

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
        """Mark the in-memory transaction committed."""

        self.committed = True

    def rollback(self) -> None:
        """Mark the in-memory transaction rolled back."""

        self.committed = False


def _quickbooks_settings() -> AppSettings:
    """Build deterministic settings for mocked QuickBooks tests."""

    return AppSettings(
        quickbooks={
            "client_id": "quickbooks-client-id",
            "client_secret": SecretStr("quickbooks-client-secret"),
            "redirect_uri": "http://127.0.0.1:8000/api/integrations/quickbooks/callback",
            "use_sandbox": True,
        },
        security={
            "credential_encryption_key": SecretStr(
                base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
            ),
            "session_secret": SecretStr("session-secret"),
            "token_signing_secret": SecretStr("token-signing-secret"),
        },
    )
