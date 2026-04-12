"""
Purpose: Normalize QuickBooks Online account records into canonical chart-of-accounts versions.
Scope: Account type mapping, stable account code derivation, parent-link normalization, COA set
materialization, activation precedence, sync timestamps, and audit activity emission.
Dependencies: COA repository/service records, QuickBooks REST client, integration repository, and
shared audit/source-surface enums.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.coa.importer import ImportedCoaAccountSeed
from services.coa.service import CoaRepositoryProtocol, CoaSetRecord
from services.common.types import JsonObject, utc_now
from services.db.models.audit import AuditSourceSurface
from services.db.models.coa import CoaSetSource
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.integration_repo import IntegrationRepository
from services.integrations.quickbooks.client import QuickBooksClientError


class QuickBooksSyncErrorCode(StrEnum):
    """Enumerate stable QuickBooks sync error codes for API/UI consumers."""

    EMPTY_ACCOUNT_LIST = "empty_account_list"
    INVALID_ACCOUNT_RECORD = "invalid_account_record"
    SYNC_FAILED = "sync_failed"


class QuickBooksSyncError(Exception):
    """Represent an expected QuickBooks COA sync failure."""

    def __init__(self, *, code: QuickBooksSyncErrorCode, message: str) -> None:
        """Capture a stable code and operator-facing message."""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class QuickBooksSyncResult:
    """Describe the durable result of one QuickBooks chart-of-accounts sync."""

    coa_set: CoaSetRecord
    account_count: int
    activated: bool
    synced_at: datetime


class QuickBooksAccountClient(Protocol):
    """Describe the QuickBooks account query used by sync workflows."""

    def query_accounts(self) -> tuple[JsonObject, ...]:
        """Return account records from QuickBooks Online."""


_ACCOUNT_TYPE_MAP: dict[str, str] = {
    "accounts payable": "liability",
    "accounts receivable": "asset",
    "asset": "asset",
    "bank": "asset",
    "cost of goods sold": "cost_of_sales",
    "credit card": "liability",
    "equity": "equity",
    "expense": "expense",
    "fixed asset": "asset",
    "income": "revenue",
    "long term liability": "liability",
    "other asset": "asset",
    "other current asset": "asset",
    "other current liability": "liability",
    "other expense": "other_expense",
    "other income": "other_income",
}


def sync_chart_of_accounts(
    *,
    entity_id: UUID,
    actor_user: EntityUserRecord,
    quickbooks_client: QuickBooksAccountClient,
    coa_repository: CoaRepositoryProtocol,
    integration_repository: IntegrationRepository,
    connection_id: UUID,
    source_surface: AuditSourceSurface,
    trace_id: str | None,
) -> QuickBooksSyncResult:
    """Fetch QuickBooks accounts and persist them as a versioned COA set."""

    try:
        quickbooks_accounts = quickbooks_client.query_accounts()
    except QuickBooksClientError as error:
        raise QuickBooksSyncError(
            code=QuickBooksSyncErrorCode.SYNC_FAILED,
            message=str(error),
        ) from error

    account_seeds = normalize_quickbooks_accounts(quickbooks_accounts)
    if not account_seeds:
        raise QuickBooksSyncError(
            code=QuickBooksSyncErrorCode.EMPTY_ACCOUNT_LIST,
            message="QuickBooks returned no active or inactive accounts to synchronize.",
        )

    now = utc_now()
    should_activate = coa_repository.get_latest_set_for_source(
        entity_id=entity_id,
        source=CoaSetSource.MANUAL_UPLOAD,
    ) is None
    try:
        coa_set = coa_repository.create_set(
            entity_id=entity_id,
            source=CoaSetSource.QUICKBOOKS_SYNC,
            version_no=coa_repository.next_version_no(entity_id=entity_id),
            import_metadata={
                "account_count": len(account_seeds),
                "provider": "quickbooks_online",
                "synced_at": now.isoformat(),
                "synced_by_user_id": serialize_uuid(actor_user.id),
                "synced_by_user_name": actor_user.full_name,
            },
        )
        coa_repository.create_accounts_bulk(coa_set_id=coa_set.id, accounts=account_seeds)

        activated_set = coa_set
        if should_activate:
            coa_repository.deactivate_all_sets(entity_id=entity_id)
            activated_set = coa_repository.activate_set(coa_set_id=coa_set.id, activated_at=now)

        integration_repository.mark_synced(connection_id=connection_id, synced_at=now)
        coa_repository.create_activity_event(
            entity_id=entity_id,
            actor_user_id=actor_user.id,
            event_type="quickbooks.coa_synced",
            source_surface=source_surface,
            payload={
                "account_count": len(account_seeds),
                "activated": should_activate,
                "coa_set_id": serialize_uuid(coa_set.id),
                "provider": "quickbooks_online",
                "source": CoaSetSource.QUICKBOOKS_SYNC.value,
                "summary": (
                    f"{actor_user.full_name} synchronized {len(account_seeds)} "
                    "QuickBooks chart-of-accounts accounts."
                ),
                "version_no": coa_set.version_no,
            },
            trace_id=trace_id,
        )
        coa_repository.commit()
    except Exception:
        coa_repository.rollback()
        raise

    return QuickBooksSyncResult(
        coa_set=activated_set,
        account_count=len(account_seeds),
        activated=should_activate,
        synced_at=now,
    )


def normalize_quickbooks_accounts(
    quickbooks_accounts: tuple[JsonObject, ...],
) -> tuple[ImportedCoaAccountSeed, ...]:
    """Convert QuickBooks account JSON into validated COA account seed rows."""

    account_codes_by_external_id: dict[str, str] = {}
    used_codes: set[str] = set()
    for account in quickbooks_accounts:
        external_id = _required_account_text(account, "Id")
        account_codes_by_external_id[external_id] = _derive_unique_account_code(
            account=account,
            used_codes=used_codes,
        )

    seeds: list[ImportedCoaAccountSeed] = []
    for account in quickbooks_accounts:
        external_id = _required_account_text(account, "Id")
        parent_ref = account.get("ParentRef")
        parent_account_code = None
        if isinstance(parent_ref, dict):
            parent_value = parent_ref.get("value")
            if isinstance(parent_value, str):
                parent_account_code = account_codes_by_external_id.get(parent_value)

        seeds.append(
            ImportedCoaAccountSeed(
                account_code=account_codes_by_external_id[external_id],
                account_name=_required_account_text(account, "Name"),
                account_type=map_quickbooks_account_type(str(account.get("AccountType") or "")),
                parent_account_code=parent_account_code,
                is_postable=not bool(account.get("SubAccount", False)),
                is_active=bool(account.get("Active", True)),
                external_ref=external_id,
                dimension_defaults={
                    "quickbooks_classification": str(account.get("Classification") or ""),
                    "quickbooks_fully_qualified_name": str(
                        account.get("FullyQualifiedName") or account.get("Name") or ""
                    ),
                },
            )
        )

    return tuple(sorted(seeds, key=lambda seed: seed.account_code))


def map_quickbooks_account_type(quickbooks_account_type: str) -> str:
    """Map QuickBooks account type labels to the canonical COA account-type vocabulary."""

    normalized = quickbooks_account_type.strip().lower()
    if not normalized:
        return "expense"
    return _ACCOUNT_TYPE_MAP.get(normalized, normalized.replace(" ", "_"))


def _derive_unique_account_code(*, account: JsonObject, used_codes: set[str]) -> str:
    """Derive a stable unique account code from QuickBooks AcctNum, Id, or name fields."""

    base_code = _optional_account_text(account, "AcctNum")
    if base_code is None:
        base_code = f"QB-{_required_account_text(account, 'Id')}"
    normalized = base_code.strip().replace(" ", "-")
    candidate = normalized
    suffix = 2
    while candidate in used_codes:
        candidate = f"{normalized}-{suffix}"
        suffix += 1
    used_codes.add(candidate)
    return candidate


def _required_account_text(account: JsonObject, key: str) -> str:
    """Return a required QuickBooks account text field or raise a sync validation error."""

    value = account.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QuickBooksSyncError(
            code=QuickBooksSyncErrorCode.INVALID_ACCOUNT_RECORD,
            message=f"QuickBooks account record is missing required field {key}.",
        )
    return value.strip()


def _optional_account_text(account: JsonObject, key: str) -> str | None:
    """Return a normalized optional QuickBooks account text field."""

    value = account.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "QuickBooksSyncError",
    "QuickBooksSyncErrorCode",
    "QuickBooksSyncResult",
    "map_quickbooks_account_type",
    "normalize_quickbooks_accounts",
    "sync_chart_of_accounts",
]
