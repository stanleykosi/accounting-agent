"""
Purpose: Define strict API contracts for QuickBooks Online integration management.
Scope: Connection status, connect redirects, sync responses, and disconnect responses used by
desktop/web clients and generated SDK consumers.
Dependencies: Pydantic contract base model defaults and datetime serialization.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator
from services.contracts.api_models import ContractModel


class QuickBooksConnectionStatusResponse(ContractModel):
    """Describe one entity's QuickBooks Online connection state."""

    status: str = Field(
        description="Connection status: disconnected, connected, expired, revoked, or error."
    )
    external_realm_id: str | None = Field(
        default=None,
        description="QuickBooks company realm ID for connected or previously connected accounts.",
    )
    last_sync_at: datetime | None = Field(
        default=None,
        description="UTC timestamp for the latest successful chart-of-accounts sync.",
    )
    recovery_action: str | None = Field(
        default=None,
        description="Operator-facing recovery action when the connection is not usable.",
    )


class QuickBooksConnectResponse(ContractModel):
    """Describe the generated authorization redirect for a QuickBooks connect flow."""

    authorization_url: str = Field(
        description="QuickBooks authorization URL for browser navigation."
    )


class QuickBooksDisconnectResponse(ContractModel):
    """Describe the result of disconnecting a QuickBooks connection."""

    status: str = Field(description="The resulting connection status.")
    message: str = Field(description="Operator-facing disconnect result message.")


class QuickBooksCoaSyncResponse(ContractModel):
    """Describe the durable result of syncing QuickBooks chart-of-accounts accounts."""

    account_count: int = Field(ge=0, description="Number of QuickBooks accounts imported.")
    activated: bool = Field(description="Whether the synced QuickBooks set became the active COA.")
    coa_set_id: str = Field(description="UUID of the created QuickBooks COA set.")
    message: str = Field(description="Operator-facing sync result message.")
    synced_at: datetime = Field(description="UTC timestamp of the successful sync.")
    version_no: int = Field(ge=1, description="Entity-scoped COA set version number.")


class QuickBooksCallbackErrorResponse(ContractModel):
    """Describe callback failures when a browser is redirected back from Intuit."""

    code: str = Field(min_length=1, description="Stable error code for UI handling.")
    message: str = Field(min_length=1, description="Operator-facing recovery message.")

    @field_validator("code", "message")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        """Trim required callback error text and reject blank values."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("QuickBooks callback error fields cannot be blank.")
        return normalized


__all__ = [
    "QuickBooksCallbackErrorResponse",
    "QuickBooksCoaSyncResponse",
    "QuickBooksConnectResponse",
    "QuickBooksConnectionStatusResponse",
    "QuickBooksDisconnectResponse",
]
