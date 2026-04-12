"""
Purpose: Orchestrate chart-of-accounts precedence, activation, upload, and editor workflows.
Scope: Entity access checks, fallback creation, source precedence resolution,
versioned account-set revisions, and immutable activity-event emission.
Dependencies: COA importer/fallback modules, SQLAlchemy persistence models,
COA API contracts, and shared audit helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from services.audit.service import AuditService
from services.auth.service import serialize_uuid
from services.coa.fallback_nigerian_sme import (
    FALLBACK_TEMPLATE_VERSION,
    build_nigerian_sme_fallback_accounts,
)
from services.coa.importer import (
    CoaImportError,
    CoaImportErrorCode,
    ImportedCoaAccountSeed,
    import_coa_file,
)
from services.common.types import JsonObject, JsonValue, utc_now
from services.contracts.coa_models import (
    CoaAccountCreateRequest,
    CoaAccountSummary,
    CoaAccountUpdateRequest,
    CoaSetSummary,
    CoaWorkspaceResponse,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.coa import CoaAccount, CoaSet, CoaSetSource
from services.db.models.entity import Entity, EntityMembership, EntityStatus
from services.db.repositories.entity_repo import EntityUserRecord
from sqlalchemy import Select, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class CoaEntityRecord:
    """Describe the subset of entity fields required by COA workflows."""

    id: UUID
    name: str
    status: EntityStatus


@dataclass(frozen=True, slots=True)
class CoaSetRecord:
    """Describe one persisted chart-of-accounts set as an immutable record."""

    id: UUID
    entity_id: UUID
    source: CoaSetSource
    version_no: int
    is_active: bool
    import_metadata: JsonObject
    activated_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CoaAccountRecord:
    """Describe one persisted COA account row as an immutable record."""

    id: UUID
    coa_set_id: UUID
    account_code: str
    account_name: str
    account_type: str
    parent_account_id: UUID | None
    is_postable: bool
    is_active: bool
    external_ref: str | None
    dimension_defaults: JsonObject
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RevisionAccountSeed:
    """Describe one account payload used to materialize a new immutable COA revision."""

    source_account_id: UUID | None
    account_code: str
    account_name: str
    account_type: str
    parent_account_code: str | None
    is_postable: bool
    is_active: bool
    external_ref: str | None
    dimension_defaults: JsonObject


class CoaServiceErrorCode(StrEnum):
    """Enumerate stable error codes surfaced by COA workflows."""

    COA_ACCOUNT_NOT_FOUND = "coa_account_not_found"
    COA_SET_NOT_FOUND = "coa_set_not_found"
    DUPLICATE_ACCOUNT_CODE = "duplicate_account_code"
    ENTITY_ARCHIVED = "entity_archived"
    ENTITY_NOT_FOUND = "entity_not_found"
    INTEGRITY_CONFLICT = "integrity_conflict"
    INVALID_COA_FILE = "invalid_coa_file"
    INVALID_PARENT_ACCOUNT = "invalid_parent_account"
    STALE_ACCOUNT = "stale_account"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"


class CoaServiceError(Exception):
    """Represent an expected COA-domain failure for API translation."""

    def __init__(self, *, status_code: int, code: CoaServiceErrorCode, message: str) -> None:
        """Capture HTTP status, stable error code, and recovery-focused message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class CoaRepositoryProtocol(Protocol):
    """Describe persistence operations required by COA service workflows."""

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> CoaEntityRecord | None:
        """Return one entity when the user has workspace access."""

    def get_active_set(self, *, entity_id: UUID) -> CoaSetRecord | None:
        """Return the currently active COA set for an entity, if one exists."""

    def get_latest_set_for_source(
        self,
        *,
        entity_id: UUID,
        source: CoaSetSource,
    ) -> CoaSetRecord | None:
        """Return the latest COA set for a specific source, if one exists."""

    def get_set_for_entity(self, *, entity_id: UUID, coa_set_id: UUID) -> CoaSetRecord | None:
        """Return one COA set when it belongs to the supplied entity."""

    def list_coa_sets_for_entity(self, *, entity_id: UUID) -> tuple[CoaSetRecord, ...]:
        """Return all COA sets for an entity in deterministic newest-first order."""

    def list_accounts_for_set(self, *, coa_set_id: UUID) -> tuple[CoaAccountRecord, ...]:
        """Return accounts for one COA set in deterministic account-code order."""

    def next_version_no(self, *, entity_id: UUID) -> int:
        """Return the next COA set version number for the entity."""

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
        """Persist one new COA set row."""

    def deactivate_all_sets(self, *, entity_id: UUID) -> None:
        """Deactivate all currently active COA sets for one entity."""

    def activate_set(self, *, coa_set_id: UUID, activated_at: datetime) -> CoaSetRecord:
        """Activate one COA set and set its activation timestamp."""

    def create_accounts_bulk(
        self,
        *,
        coa_set_id: UUID,
        accounts: tuple[ImportedCoaAccountSeed, ...],
    ) -> tuple[CoaAccountRecord, ...]:
        """Persist account rows for one COA set and resolve parent links by account code."""

    def list_set_account_counts(self, *, entity_id: UUID) -> dict[UUID, int]:
        """Return account counts grouped by COA set for one entity."""

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
        """Persist one immutable COA activity event for the entity timeline."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def is_integrity_error(self, error: Exception) -> bool:
        """Return whether the provided exception originated from database integrity checks."""


class CoaRepository:
    """Execute canonical chart-of-accounts persistence operations in one DB session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request-scoped SQLAlchemy session used by COA workflows."""

        self._db_session = db_session

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> CoaEntityRecord | None:
        """Return one entity when the user has workspace access."""

        statement = (
            select(Entity)
            .join(EntityMembership, EntityMembership.entity_id == Entity.id)
            .where(Entity.id == entity_id, EntityMembership.user_id == user_id)
        )
        entity = self._db_session.execute(statement).scalar_one_or_none()
        if entity is None:
            return None

        return _map_entity(entity)

    def get_active_set(self, *, entity_id: UUID) -> CoaSetRecord | None:
        """Return one active COA set for an entity when present."""

        statement = (
            select(CoaSet)
            .where(CoaSet.entity_id == entity_id, CoaSet.is_active.is_(True))
            .order_by(desc(CoaSet.version_no))
            .limit(1)
        )
        coa_set = self._db_session.execute(statement).scalar_one_or_none()
        return _map_set(coa_set) if coa_set is not None else None

    def get_latest_set_for_source(
        self,
        *,
        entity_id: UUID,
        source: CoaSetSource,
    ) -> CoaSetRecord | None:
        """Return the latest COA set for one source when it exists."""

        statement = (
            select(CoaSet)
            .where(CoaSet.entity_id == entity_id, CoaSet.source == source.value)
            .order_by(desc(CoaSet.version_no))
            .limit(1)
        )
        coa_set = self._db_session.execute(statement).scalar_one_or_none()
        return _map_set(coa_set) if coa_set is not None else None

    def get_set_for_entity(self, *, entity_id: UUID, coa_set_id: UUID) -> CoaSetRecord | None:
        """Return one COA set when it belongs to the target entity."""

        statement = select(CoaSet).where(CoaSet.entity_id == entity_id, CoaSet.id == coa_set_id)
        coa_set = self._db_session.execute(statement).scalar_one_or_none()
        return _map_set(coa_set) if coa_set is not None else None

    def list_coa_sets_for_entity(self, *, entity_id: UUID) -> tuple[CoaSetRecord, ...]:
        """Return all COA set versions for one entity in newest-first order."""

        statement = (
            select(CoaSet)
            .where(CoaSet.entity_id == entity_id)
            .order_by(desc(CoaSet.version_no), desc(CoaSet.created_at))
        )
        return tuple(_map_set(coa_set) for coa_set in self._db_session.scalars(statement))

    def list_accounts_for_set(self, *, coa_set_id: UUID) -> tuple[CoaAccountRecord, ...]:
        """Return account rows for one set in deterministic account-code order."""

        statement = (
            select(CoaAccount)
            .where(CoaAccount.coa_set_id == coa_set_id)
            .order_by(CoaAccount.account_code.asc(), CoaAccount.id.asc())
        )
        return tuple(_map_account(account) for account in self._db_session.scalars(statement))

    def next_version_no(self, *, entity_id: UUID) -> int:
        """Return the next COA version number for one entity workspace."""

        statement = select(func.max(CoaSet.version_no)).where(CoaSet.entity_id == entity_id)
        current_max = self._db_session.execute(statement).scalar_one_or_none()
        return int(current_max or 0) + 1

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
        """Stage one COA set row and return its immutable record mapping."""

        coa_set = CoaSet(
            entity_id=entity_id,
            source=source.value,
            version_no=version_no,
            is_active=is_active,
            import_metadata=dict(import_metadata),
            activated_at=activated_at,
        )
        self._db_session.add(coa_set)
        self._db_session.flush()
        return _map_set(coa_set)

    def deactivate_all_sets(self, *, entity_id: UUID) -> None:
        """Deactivate every active COA set row for one entity."""

        statement: Select[tuple[CoaSet]] = select(CoaSet).where(
            CoaSet.entity_id == entity_id,
            CoaSet.is_active.is_(True),
        )
        for coa_set in self._db_session.scalars(statement):
            coa_set.is_active = False

        self._db_session.flush()

    def activate_set(self, *, coa_set_id: UUID, activated_at: datetime) -> CoaSetRecord:
        """Activate one COA set and stamp the activation timestamp."""

        coa_set = self._load_set(coa_set_id=coa_set_id)
        coa_set.is_active = True
        coa_set.activated_at = activated_at
        self._db_session.flush()
        return _map_set(coa_set)

    def create_accounts_bulk(
        self,
        *,
        coa_set_id: UUID,
        accounts: tuple[ImportedCoaAccountSeed, ...],
    ) -> tuple[CoaAccountRecord, ...]:
        """Persist accounts for one set and resolve parent links by account code."""

        rows: list[CoaAccount] = []
        accounts_by_code: dict[str, CoaAccount] = {}

        for account in accounts:
            row = CoaAccount(
                coa_set_id=coa_set_id,
                account_code=account.account_code,
                account_name=account.account_name,
                account_type=account.account_type,
                parent_account_id=None,
                is_postable=account.is_postable,
                is_active=account.is_active,
                external_ref=account.external_ref,
                dimension_defaults=dict(account.dimension_defaults),
            )
            rows.append(row)
            if account.account_code in accounts_by_code:
                raise ValueError(f"Duplicate account code found: {account.account_code}")
            accounts_by_code[account.account_code] = row

        self._db_session.add_all(rows)
        self._db_session.flush()

        for index, account in enumerate(accounts):
            parent_code = account.parent_account_code
            if parent_code is None:
                continue
            parent_row = accounts_by_code.get(parent_code)
            if parent_row is None:
                raise LookupError(f"Unknown parent account code {parent_code}.")
            rows[index].parent_account_id = parent_row.id

        self._db_session.flush()
        return tuple(_map_account(row) for row in rows)

    def list_set_account_counts(self, *, entity_id: UUID) -> dict[UUID, int]:
        """Return account counts keyed by COA set UUID for one entity."""

        statement = (
            select(CoaAccount.coa_set_id, func.count(CoaAccount.id))
            .join(CoaSet, CoaSet.id == CoaAccount.coa_set_id)
            .where(CoaSet.entity_id == entity_id)
            .group_by(CoaAccount.coa_set_id)
        )
        return {
            cast(UUID, coa_set_id): int(account_count)
            for coa_set_id, account_count in self._db_session.execute(statement).all()
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
        """Persist one entity-scoped COA activity event."""

        AuditService(db_session=self._db_session).emit_audit_event(
            entity_id=entity_id,
            event_type=event_type,
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload=dict(payload),
            trace_id=trace_id,
        )

    def commit(self) -> None:
        """Commit the current SQLAlchemy unit of work."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current SQLAlchemy unit of work."""

        self._db_session.rollback()

    @staticmethod
    def is_integrity_error(error: Exception) -> bool:
        """Return whether the given exception originated from DB integrity validation."""

        return isinstance(error, IntegrityError)

    def _load_set(self, *, coa_set_id: UUID) -> CoaSet:
        """Load one COA set ORM row or fail fast when service assumptions are violated."""

        statement = select(CoaSet).where(CoaSet.id == coa_set_id)
        coa_set = self._db_session.execute(statement).scalar_one_or_none()
        if coa_set is None:
            raise LookupError(f"COA set {coa_set_id} does not exist.")

        return coa_set


class CoaService:
    """Provide the canonical COA workflow used by API routes and desktop editor surfaces."""

    precedence_order: tuple[CoaSetSource, ...] = (
        CoaSetSource.MANUAL_UPLOAD,
        CoaSetSource.QUICKBOOKS_SYNC,
        CoaSetSource.FALLBACK_NIGERIAN_SME,
    )

    def __init__(self, *, repository: CoaRepositoryProtocol) -> None:
        """Capture the persistence boundary used by COA workflows."""

        self._repository = repository

    def read_workspace(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CoaWorkspaceResponse:
        """Return the COA workspace, creating or activating fallback sets when needed."""

        self._require_entity_access(entity_id=entity_id, user_id=actor_user.id)
        active_set = self._repository.get_active_set(entity_id=entity_id)
        if active_set is None:
            active_set = self._ensure_active_set(
                actor_user=actor_user,
                entity_id=entity_id,
                source_surface=source_surface,
                trace_id=trace_id,
            )

        return self._build_workspace(entity_id=entity_id, active_set_id=active_set.id)

    def upload_manual_coa(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        filename: str,
        payload: bytes,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CoaWorkspaceResponse:
        """Validate and import a manual COA file as a new active versioned set."""

        self._require_mutable_entity(entity_id=entity_id, user_id=actor_user.id)

        try:
            imported = import_coa_file(filename=filename, payload=payload)
        except CoaImportError as error:
            if error.code is CoaImportErrorCode.UNSUPPORTED_FILE_TYPE:
                raise CoaServiceError(
                    status_code=415,
                    code=CoaServiceErrorCode.UNSUPPORTED_FILE_TYPE,
                    message=error.message,
                ) from error
            raise CoaServiceError(
                status_code=400,
                code=CoaServiceErrorCode.INVALID_COA_FILE,
                message=error.message,
            ) from error

        now = utc_now()
        import_metadata: JsonObject = dict(imported.import_metadata)
        import_metadata.update(
            {
                "import_mode": "manual_upload",
                "uploaded_by_user_id": serialize_uuid(actor_user.id),
                "uploaded_by_user_name": actor_user.full_name,
                "uploaded_at": now.isoformat(),
            }
        )

        try:
            version_no = self._repository.next_version_no(entity_id=entity_id)
            coa_set = self._repository.create_set(
                entity_id=entity_id,
                source=CoaSetSource.MANUAL_UPLOAD,
                version_no=version_no,
                import_metadata=import_metadata,
            )
            self._repository.create_accounts_bulk(
                coa_set_id=coa_set.id,
                accounts=imported.accounts,
            )
            self._activate_set(entity_id=entity_id, coa_set_id=coa_set.id, activated_at=now)
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type="coa.manual_upload_imported",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} uploaded COA version {version_no} from {filename}."
                    ),
                    "coa_set_id": serialize_uuid(coa_set.id),
                    "source": CoaSetSource.MANUAL_UPLOAD.value,
                    "version_no": version_no,
                    "row_count": len(imported.accounts),
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise CoaServiceError(
                    status_code=409,
                    code=CoaServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The uploaded chart of accounts conflicts with existing set state.",
                ) from error
            raise

        return self._build_workspace(entity_id=entity_id, active_set_id=coa_set.id)

    def activate_coa_set(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        coa_set_id: UUID,
        reason: str | None,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CoaWorkspaceResponse:
        """Activate an existing COA set version for the entity."""

        self._require_mutable_entity(entity_id=entity_id, user_id=actor_user.id)
        target_set = self._repository.get_set_for_entity(entity_id=entity_id, coa_set_id=coa_set_id)
        if target_set is None:
            raise CoaServiceError(
                status_code=404,
                code=CoaServiceErrorCode.COA_SET_NOT_FOUND,
                message="The requested chart-of-accounts set does not exist for this entity.",
            )

        if target_set.is_active:
            return self._build_workspace(entity_id=entity_id, active_set_id=target_set.id)

        try:
            self._activate_set(
                entity_id=entity_id, coa_set_id=target_set.id, activated_at=utc_now()
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type="coa.set_activated",
                source_surface=source_surface,
                payload={
                    "summary": (
                        f"{actor_user.full_name} activated COA version {target_set.version_no}."
                    ),
                    "coa_set_id": serialize_uuid(target_set.id),
                    "reason": reason,
                    "source": target_set.source.value,
                    "version_no": target_set.version_no,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise CoaServiceError(
                    status_code=409,
                    code=CoaServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The selected chart-of-accounts set could not be activated.",
                ) from error
            raise

        return self._build_workspace(entity_id=entity_id, active_set_id=target_set.id)

    def create_account(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        payload: CoaAccountCreateRequest,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CoaWorkspaceResponse:
        """Create one account by materializing a new immutable manual COA revision."""

        self._require_mutable_entity(entity_id=entity_id, user_id=actor_user.id)
        active_set = self._ensure_active_set(
            actor_user=actor_user,
            entity_id=entity_id,
            source_surface=source_surface,
            trace_id=trace_id,
        )
        revision_accounts = list(self._build_revision_seeds(active_set_id=active_set.id))

        if any(account.account_code == payload.account_code for account in revision_accounts):
            raise CoaServiceError(
                status_code=409,
                code=CoaServiceErrorCode.DUPLICATE_ACCOUNT_CODE,
                message=(
                    f"Account code {payload.account_code} already exists in the active chart "
                    "of accounts."
                ),
            )

        parent_account_code = None
        if payload.parent_account_id is not None:
            parent_account_code = self._resolve_parent_account_code(
                revision_accounts=tuple(revision_accounts),
                parent_account_id=payload.parent_account_id,
            )

        revision_accounts.append(
            RevisionAccountSeed(
                source_account_id=None,
                account_code=payload.account_code,
                account_name=payload.account_name,
                account_type=payload.account_type,
                parent_account_code=parent_account_code,
                is_postable=payload.is_postable,
                is_active=payload.is_active,
                external_ref=payload.external_ref,
                dimension_defaults=dict(payload.dimension_defaults),
            )
        )

        created_set = self._materialize_revision(
            actor_user=actor_user,
            entity_id=entity_id,
            base_set=active_set,
            revision_accounts=tuple(revision_accounts),
            revision_reason="account_created",
            source_surface=source_surface,
            trace_id=trace_id,
            summary=f"{actor_user.full_name} created account {payload.account_code}.",
            event_type="coa.account_created",
            event_payload={
                "account_code": payload.account_code,
                "account_name": payload.account_name,
            },
        )
        return self._build_workspace(entity_id=entity_id, active_set_id=created_set.id)

    def update_account(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        account_id: UUID,
        payload: CoaAccountUpdateRequest,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CoaWorkspaceResponse:
        """Update one account by materializing a new immutable manual COA revision."""

        self._require_mutable_entity(entity_id=entity_id, user_id=actor_user.id)
        active_set = self._ensure_active_set(
            actor_user=actor_user,
            entity_id=entity_id,
            source_surface=source_surface,
            trace_id=trace_id,
        )
        revision_accounts = list(self._build_revision_seeds(active_set_id=active_set.id))

        target_index = next(
            (
                index
                for index, account in enumerate(revision_accounts)
                if account.source_account_id == account_id
            ),
            None,
        )
        if target_index is None:
            raise CoaServiceError(
                status_code=409,
                code=CoaServiceErrorCode.STALE_ACCOUNT,
                message=(
                    "The selected account is no longer part of the active COA set. "
                    "Refresh the page and retry your edit."
                ),
            )

        target_account = revision_accounts[target_index]
        next_parent_account_code = target_account.parent_account_code
        if "parent_account_id" in payload.model_fields_set:
            if payload.parent_account_id is None:
                next_parent_account_code = None
            else:
                parent_account_id = payload.parent_account_id
                if parent_account_id == account_id:
                    raise CoaServiceError(
                        status_code=400,
                        code=CoaServiceErrorCode.INVALID_PARENT_ACCOUNT,
                        message="An account cannot be its own parent.",
                    )
                next_parent_account_code = self._resolve_parent_account_code(
                    revision_accounts=tuple(revision_accounts),
                    parent_account_id=parent_account_id,
                )

        updated_account = replace(
            target_account,
            account_code=(
                payload.account_code
                if "account_code" in payload.model_fields_set and payload.account_code is not None
                else target_account.account_code
            ),
            account_name=(
                payload.account_name
                if "account_name" in payload.model_fields_set and payload.account_name is not None
                else target_account.account_name
            ),
            account_type=(
                payload.account_type
                if "account_type" in payload.model_fields_set and payload.account_type is not None
                else target_account.account_type
            ),
            parent_account_code=next_parent_account_code,
            is_postable=(
                payload.is_postable
                if "is_postable" in payload.model_fields_set and payload.is_postable is not None
                else target_account.is_postable
            ),
            is_active=(
                payload.is_active
                if "is_active" in payload.model_fields_set and payload.is_active is not None
                else target_account.is_active
            ),
            external_ref=(
                payload.external_ref
                if "external_ref" in payload.model_fields_set
                else target_account.external_ref
            ),
            dimension_defaults=(
                dict(payload.dimension_defaults)
                if "dimension_defaults" in payload.model_fields_set
                and payload.dimension_defaults is not None
                else target_account.dimension_defaults
            ),
        )
        revision_accounts[target_index] = updated_account

        self._validate_revision_codes(revision_accounts=tuple(revision_accounts))

        created_set = self._materialize_revision(
            actor_user=actor_user,
            entity_id=entity_id,
            base_set=active_set,
            revision_accounts=tuple(revision_accounts),
            revision_reason="account_updated",
            source_surface=source_surface,
            trace_id=trace_id,
            summary=f"{actor_user.full_name} updated account {target_account.account_code}.",
            event_type="coa.account_updated",
            event_payload={
                "account_id": serialize_uuid(account_id),
                "previous_account_code": target_account.account_code,
                "updated_account_code": updated_account.account_code,
                "fields_changed": [
                    cast(JsonValue, field) for field in sorted(payload.model_fields_set)
                ],
            },
        )
        return self._build_workspace(entity_id=entity_id, active_set_id=created_set.id)

    def _materialize_revision(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        base_set: CoaSetRecord,
        revision_accounts: tuple[RevisionAccountSeed, ...],
        revision_reason: str,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
        summary: str,
        event_type: str,
        event_payload: JsonObject,
    ) -> CoaSetRecord:
        """Persist one immutable manual COA revision and activate it."""

        import_metadata: JsonObject = {
            "created_by_user_id": serialize_uuid(actor_user.id),
            "created_by_user_name": actor_user.full_name,
            "revision_of_set_id": serialize_uuid(base_set.id),
            "revision_reason": revision_reason,
            "row_count": len(revision_accounts),
        }

        now = utc_now()
        try:
            next_version_no = self._repository.next_version_no(entity_id=entity_id)
            created_set = self._repository.create_set(
                entity_id=entity_id,
                source=CoaSetSource.MANUAL_UPLOAD,
                version_no=next_version_no,
                import_metadata=import_metadata,
            )
            imported_accounts = tuple(
                ImportedCoaAccountSeed(
                    account_code=account.account_code,
                    account_name=account.account_name,
                    account_type=account.account_type,
                    parent_account_code=account.parent_account_code,
                    is_postable=account.is_postable,
                    is_active=account.is_active,
                    external_ref=account.external_ref,
                    dimension_defaults=dict(account.dimension_defaults),
                )
                for account in revision_accounts
            )
            self._repository.create_accounts_bulk(
                coa_set_id=created_set.id,
                accounts=imported_accounts,
            )
            self._activate_set(entity_id=entity_id, coa_set_id=created_set.id, activated_at=now)
            payload: JsonObject = {
                "summary": summary,
                "base_set_id": serialize_uuid(base_set.id),
                "coa_set_id": serialize_uuid(created_set.id),
                "version_no": next_version_no,
            }
            payload.update(event_payload)
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type=event_type,
                source_surface=source_surface,
                payload=payload,
                trace_id=trace_id,
            )
            self._repository.commit()
            return created_set
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise CoaServiceError(
                    status_code=409,
                    code=CoaServiceErrorCode.INTEGRITY_CONFLICT,
                    message="A concurrent COA update was detected. Retry the request.",
                ) from error
            raise

    def _build_workspace(self, *, entity_id: UUID, active_set_id: UUID) -> CoaWorkspaceResponse:
        """Build a canonical COA workspace response from current repository state."""

        coa_sets = self._repository.list_coa_sets_for_entity(entity_id=entity_id)
        active_set = next((coa_set for coa_set in coa_sets if coa_set.id == active_set_id), None)
        if active_set is None:
            raise LookupError(
                f"Active COA set {active_set_id} is not available for entity {entity_id}."
            )

        accounts = self._repository.list_accounts_for_set(coa_set_id=active_set.id)
        account_counts = self._repository.list_set_account_counts(entity_id=entity_id)

        return CoaWorkspaceResponse(
            entity_id=serialize_uuid(entity_id),
            active_set=self._build_set_summary(
                coa_set=active_set,
                account_count=account_counts.get(active_set.id, len(accounts)),
            ),
            accounts=tuple(self._build_account_summary(account) for account in accounts),
            coa_sets=tuple(
                self._build_set_summary(
                    coa_set=coa_set,
                    account_count=account_counts.get(coa_set.id, 0),
                )
                for coa_set in coa_sets
            ),
            precedence_order=tuple(source.value for source in self.precedence_order),
        )

    def _build_set_summary(self, *, coa_set: CoaSetRecord, account_count: int) -> CoaSetSummary:
        """Convert one immutable set record into the API response shape."""

        return CoaSetSummary(
            id=serialize_uuid(coa_set.id),
            entity_id=serialize_uuid(coa_set.entity_id),
            source=coa_set.source.value,
            version_no=coa_set.version_no,
            is_active=coa_set.is_active,
            account_count=account_count,
            import_metadata=dict(coa_set.import_metadata),
            activated_at=coa_set.activated_at,
            created_at=coa_set.created_at,
            updated_at=coa_set.updated_at,
        )

    def _build_account_summary(self, account: CoaAccountRecord) -> CoaAccountSummary:
        """Convert one immutable account record into the API response shape."""

        return CoaAccountSummary(
            id=serialize_uuid(account.id),
            coa_set_id=serialize_uuid(account.coa_set_id),
            account_code=account.account_code,
            account_name=account.account_name,
            account_type=account.account_type,
            parent_account_id=(
                serialize_uuid(account.parent_account_id)
                if account.parent_account_id is not None
                else None
            ),
            is_postable=account.is_postable,
            is_active=account.is_active,
            external_ref=account.external_ref,
            dimension_defaults=cast(dict[str, str], dict(account.dimension_defaults)),
            created_at=account.created_at,
            updated_at=account.updated_at,
        )

    def _ensure_active_set(
        self,
        *,
        actor_user: EntityUserRecord,
        entity_id: UUID,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> CoaSetRecord:
        """Ensure one active set exists, resolving precedence and fallback creation when needed."""

        existing_active = self._repository.get_active_set(entity_id=entity_id)
        if existing_active is not None:
            return existing_active

        try:
            selected_set = self._select_precedence_candidate(entity_id=entity_id)
            event_type = "coa.precedence_activated"
            event_summary = ""

            if selected_set is None:
                selected_set = self._create_fallback_set(entity_id=entity_id)
                event_type = "coa.fallback_created"
                event_summary = (
                    f"{actor_user.full_name} triggered fallback COA creation for the entity."
                )
            else:
                event_summary = (
                    f"{actor_user.full_name} activated {selected_set.source.value} COA "
                    "according to precedence rules."
                )

            self._activate_set(
                entity_id=entity_id, coa_set_id=selected_set.id, activated_at=utc_now()
            )
            self._repository.create_activity_event(
                entity_id=entity_id,
                actor_user_id=actor_user.id,
                event_type=event_type,
                source_surface=source_surface,
                payload={
                    "summary": event_summary,
                    "coa_set_id": serialize_uuid(selected_set.id),
                    "source": selected_set.source.value,
                    "version_no": selected_set.version_no,
                },
                trace_id=trace_id,
            )
            self._repository.commit()
        except Exception as error:
            self._repository.rollback()
            if self._repository.is_integrity_error(error):
                raise CoaServiceError(
                    status_code=409,
                    code=CoaServiceErrorCode.INTEGRITY_CONFLICT,
                    message="The active chart-of-accounts set could not be resolved.",
                ) from error
            raise

        activated_set = self._repository.get_active_set(entity_id=entity_id)
        if activated_set is None:
            raise LookupError("An active COA set was expected after precedence activation.")

        return activated_set

    def _select_precedence_candidate(self, *, entity_id: UUID) -> CoaSetRecord | None:
        """Pick the highest-priority available COA set according to source precedence."""

        for source in self.precedence_order:
            candidate = self._repository.get_latest_set_for_source(
                entity_id=entity_id, source=source
            )
            if candidate is not None:
                return candidate

        return None

    def _create_fallback_set(self, *, entity_id: UUID) -> CoaSetRecord:
        """Create a fallback Nigerian SME COA set when no manual or synced set exists."""

        next_version = self._repository.next_version_no(entity_id=entity_id)
        fallback_set = self._repository.create_set(
            entity_id=entity_id,
            source=CoaSetSource.FALLBACK_NIGERIAN_SME,
            version_no=next_version,
            import_metadata={
                "generated_by": "system",
                "template": FALLBACK_TEMPLATE_VERSION,
                "row_count": len(build_nigerian_sme_fallback_accounts()),
            },
        )
        self._repository.create_accounts_bulk(
            coa_set_id=fallback_set.id,
            accounts=tuple(
                ImportedCoaAccountSeed(
                    account_code=account.account_code,
                    account_name=account.account_name,
                    account_type=account.account_type,
                    parent_account_code=account.parent_account_code,
                    is_postable=account.is_postable,
                    is_active=account.is_active,
                    external_ref=None,
                    dimension_defaults={},
                )
                for account in build_nigerian_sme_fallback_accounts()
            ),
        )
        return fallback_set

    def _activate_set(
        self, *, entity_id: UUID, coa_set_id: UUID, activated_at: datetime
    ) -> CoaSetRecord:
        """Deactivate current active sets and activate the target set for the entity."""

        self._repository.deactivate_all_sets(entity_id=entity_id)
        return self._repository.activate_set(coa_set_id=coa_set_id, activated_at=activated_at)

    def _build_revision_seeds(self, *, active_set_id: UUID) -> tuple[RevisionAccountSeed, ...]:
        """Project active-set accounts into mutable revision seeds."""

        accounts = self._repository.list_accounts_for_set(coa_set_id=active_set_id)
        accounts_by_id = {account.id: account for account in accounts}

        return tuple(
            RevisionAccountSeed(
                source_account_id=account.id,
                account_code=account.account_code,
                account_name=account.account_name,
                account_type=account.account_type,
                parent_account_code=(
                    accounts_by_id[account.parent_account_id].account_code
                    if account.parent_account_id is not None
                    else None
                ),
                is_postable=account.is_postable,
                is_active=account.is_active,
                external_ref=account.external_ref,
                dimension_defaults=dict(account.dimension_defaults),
            )
            for account in accounts
        )

    def _resolve_parent_account_code(
        self,
        *,
        revision_accounts: tuple[RevisionAccountSeed, ...],
        parent_account_id: UUID,
    ) -> str:
        """Resolve one parent-account UUID to the revision account code."""

        parent = next(
            (
                account
                for account in revision_accounts
                if account.source_account_id == parent_account_id
            ),
            None,
        )
        if parent is None:
            raise CoaServiceError(
                status_code=400,
                code=CoaServiceErrorCode.INVALID_PARENT_ACCOUNT,
                message="The selected parent account does not exist in the active COA set.",
            )

        return parent.account_code

    def _validate_revision_codes(
        self, *, revision_accounts: tuple[RevisionAccountSeed, ...]
    ) -> None:
        """Ensure a revision does not contain duplicate account codes."""

        seen_codes: set[str] = set()
        duplicates: set[str] = set()
        for account in revision_accounts:
            if account.account_code in seen_codes:
                duplicates.add(account.account_code)
            seen_codes.add(account.account_code)

        if duplicates:
            duplicate_codes = ", ".join(sorted(duplicates))
            raise CoaServiceError(
                status_code=409,
                code=CoaServiceErrorCode.DUPLICATE_ACCOUNT_CODE,
                message=f"Duplicate account codes are not allowed: {duplicate_codes}.",
            )

    def _require_entity_access(self, *, entity_id: UUID, user_id: UUID) -> CoaEntityRecord:
        """Require entity membership access for the user."""

        entity = self._repository.get_entity_for_user(entity_id=entity_id, user_id=user_id)
        if entity is None:
            raise CoaServiceError(
                status_code=404,
                code=CoaServiceErrorCode.ENTITY_NOT_FOUND,
                message="The requested entity workspace is not accessible.",
            )

        return entity

    def _require_mutable_entity(self, *, entity_id: UUID, user_id: UUID) -> CoaEntityRecord:
        """Require mutable entity state for COA write operations."""

        entity = self._require_entity_access(entity_id=entity_id, user_id=user_id)
        if entity.status is EntityStatus.ARCHIVED:
            raise CoaServiceError(
                status_code=409,
                code=CoaServiceErrorCode.ENTITY_ARCHIVED,
                message="Archived entity workspaces cannot mutate chart-of-accounts state.",
            )

        return entity


def _map_entity(entity: Entity) -> CoaEntityRecord:
    """Convert one ORM entity row into an immutable COA entity record."""

    return CoaEntityRecord(
        id=entity.id,
        name=entity.name,
        status=EntityStatus(entity.status),
    )


def _map_set(coa_set: CoaSet) -> CoaSetRecord:
    """Convert one ORM COA set row into an immutable set record."""

    return CoaSetRecord(
        id=coa_set.id,
        entity_id=coa_set.entity_id,
        source=CoaSetSource(coa_set.source),
        version_no=coa_set.version_no,
        is_active=coa_set.is_active,
        import_metadata=cast(JsonObject, dict(coa_set.import_metadata)),
        activated_at=coa_set.activated_at,
        created_at=coa_set.created_at,
        updated_at=coa_set.updated_at,
    )


def _map_account(account: CoaAccount) -> CoaAccountRecord:
    """Convert one ORM COA account row into an immutable account record."""

    return CoaAccountRecord(
        id=account.id,
        coa_set_id=account.coa_set_id,
        account_code=account.account_code,
        account_name=account.account_name,
        account_type=account.account_type,
        parent_account_id=account.parent_account_id,
        is_postable=account.is_postable,
        is_active=account.is_active,
        external_ref=account.external_ref,
        dimension_defaults=cast(JsonObject, dict(account.dimension_defaults)),
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


__all__ = [
    "CoaRepository",
    "CoaService",
    "CoaServiceError",
    "CoaServiceErrorCode",
]
