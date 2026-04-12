"""
Purpose: Persist encrypted integration connections and their sync metadata through SQLAlchemy.
Scope: Entity-scoped upserts, status changes, credential replacement, and thin
record mapping for future integration services.
Dependencies: SQLAlchemy ORM sessions plus the canonical integration model definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from services.common.types import JsonObject
from services.db.models.integration import (
    IntegrationConnection,
    IntegrationConnectionStatus,
    IntegrationProvider,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class IntegrationConnectionRecord:
    """Describe the subset of an integration row needed by service-layer workflows."""

    id: UUID
    entity_id: UUID
    provider: IntegrationProvider
    status: IntegrationConnectionStatus
    encrypted_credentials: JsonObject
    external_realm_id: str
    created_at: datetime
    updated_at: datetime
    last_sync_at: datetime | None


class IntegrationRepository:
    """Execute canonical integration-connection persistence operations in one DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request-scoped SQLAlchemy session used by integration workflows."""

        self._db_session = db_session

    def get_connection(
        self,
        *,
        entity_id: UUID,
        provider: IntegrationProvider,
    ) -> IntegrationConnectionRecord | None:
        """Return one provider connection for an entity or None when it has not been created."""

        statement = select(IntegrationConnection).where(
            IntegrationConnection.entity_id == entity_id,
            IntegrationConnection.provider == provider.value,
        )
        connection = self._db_session.execute(statement).scalar_one_or_none()
        if connection is None:
            return None

        return _map_connection(connection)

    def get_integration_credentials(
        self,
        *,
        entity_id: UUID,
        provider: IntegrationProvider,
    ) -> JsonObject | None:
        """Retrieve encrypted credentials for an entity/provider connection."""
        connection = self.get_connection(entity_id=entity_id, provider=provider)
        if connection is None:
            return None
        return connection.encrypted_credentials

    def save_integration_credentials(
        self,
        *,
        entity_id: UUID,
        provider: IntegrationProvider,
        credentials: JsonObject,
        external_realm_id: str,
    ) -> IntegrationConnectionRecord:
        """Save or update encrypted credentials for an entity/provider connection."""
        return self.upsert_connection(
            entity_id=entity_id,
            provider=provider,
            status=IntegrationConnectionStatus.CONNECTED,
            encrypted_credentials=credentials,
            external_realm_id=external_realm_id,
        )

    def update_connection_status(
        self,
        *,
        entity_id: UUID,
        provider: IntegrationProvider,
        status: IntegrationConnectionStatus,
    ) -> IntegrationConnectionRecord:
        """Update the connection status for an entity/provider connection."""
        connection = self._load_connection(entity_id=entity_id, provider=provider)
        if connection is None:
            raise LookupError(
                f"No connection found for entity {entity_id} and provider {provider.value}"
            )

        connection.status = status.value
        self._db_session.flush()
        return _map_connection(connection)

    def delete_integration_credentials(
        self,
        *,
        entity_id: UUID,
        provider: IntegrationProvider,
    ) -> bool:
        """Delete integration credentials for an entity/provider connection."""
        connection = self._load_connection(entity_id=entity_id, provider=provider)
        if connection is None:
            return False

        self._db_session.delete(connection)
        self._db_session.flush()
        return True

    def upsert_connection(
        self,
        *,
        entity_id: UUID,
        provider: IntegrationProvider,
        status: IntegrationConnectionStatus,
        encrypted_credentials: JsonObject,
        external_realm_id: str,
        last_sync_at: datetime | None = None,
    ) -> IntegrationConnectionRecord:
        """Create or update one entity/provider connection with encrypted credentials."""

        connection = self._load_connection(entity_id=entity_id, provider=provider)
        if connection is None:
            connection = IntegrationConnection(
                entity_id=entity_id,
                provider=provider.value,
                status=status.value,
                encrypted_credentials=dict(encrypted_credentials),
                external_realm_id=external_realm_id,
                last_sync_at=last_sync_at,
            )
            self._db_session.add(connection)
        else:
            connection.status = status.value
            connection.encrypted_credentials = dict(encrypted_credentials)
            connection.external_realm_id = external_realm_id
            connection.last_sync_at = last_sync_at

        self._db_session.flush()
        return _map_connection(connection)

    def update_status(
        self,
        *,
        connection_id: UUID,
        status: IntegrationConnectionStatus,
    ) -> IntegrationConnectionRecord:
        """Persist a lifecycle-state change for one integration connection."""

        connection = self._load_connection_by_id(connection_id=connection_id)
        connection.status = status.value
        self._db_session.flush()
        return _map_connection(connection)

    def replace_encrypted_credentials(
        self,
        *,
        connection_id: UUID,
        encrypted_credentials: JsonObject,
        external_realm_id: str,
    ) -> IntegrationConnectionRecord:
        """Replace the encrypted credential envelope after a refresh or reconnect flow."""

        connection = self._load_connection_by_id(connection_id=connection_id)
        connection.encrypted_credentials = dict(encrypted_credentials)
        connection.external_realm_id = external_realm_id
        self._db_session.flush()
        return _map_connection(connection)

    def mark_synced(
        self,
        *,
        connection_id: UUID,
        synced_at: datetime,
    ) -> IntegrationConnectionRecord:
        """Persist the latest successful sync timestamp for one connection."""

        connection = self._load_connection_by_id(connection_id=connection_id)
        connection.last_sync_at = synced_at
        self._db_session.flush()
        return _map_connection(connection)

    def commit(self) -> None:
        """Commit the current SQLAlchemy unit of work after a successful mutation."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current SQLAlchemy unit of work after a failed mutation."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the provided exception originated from a DB integrity failure."""

        return isinstance(error, IntegrityError)

    def _load_connection(
        self,
        *,
        entity_id: UUID,
        provider: IntegrationProvider,
    ) -> IntegrationConnection | None:
        """Load one connection ORM row by entity/provider for internal mutation helpers."""

        statement = select(IntegrationConnection).where(
            IntegrationConnection.entity_id == entity_id,
            IntegrationConnection.provider == provider.value,
        )
        return self._db_session.execute(statement).scalar_one_or_none()

    def _load_connection_by_id(self, *, connection_id: UUID) -> IntegrationConnection:
        """Load one connection ORM row by UUID and fail fast when it is missing."""

        statement = select(IntegrationConnection).where(IntegrationConnection.id == connection_id)
        connection = self._db_session.execute(statement).scalar_one_or_none()
        if connection is None:
            raise LookupError(f"Integration connection {connection_id} does not exist.")

        return connection


def _map_connection(connection: IntegrationConnection) -> IntegrationConnectionRecord:
    """Convert one ORM integration row into an immutable repository record."""

    return IntegrationConnectionRecord(
        id=connection.id,
        entity_id=connection.entity_id,
        provider=IntegrationProvider(connection.provider),
        status=IntegrationConnectionStatus(connection.status),
        encrypted_credentials=cast(JsonObject, dict(connection.encrypted_credentials)),
        external_realm_id=connection.external_realm_id,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        last_sync_at=connection.last_sync_at,
    )


__all__ = ["IntegrationConnectionRecord", "IntegrationRepository"]
