"""
Purpose: Define strict API contracts for chart-of-accounts workflows.
Scope: COA workspace reads, set activation, manual upload responses, and
account create/update editor payloads.
Dependencies: Pydantic contracts and shared API contract base model defaults.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from services.contracts.api_models import ContractModel


def _normalize_required_text(value: str, *, field_name: str) -> str:
    """Trim required text values and reject blank strings."""

    normalized = value.strip()
    if normalized:
        return normalized

    raise ValueError(f"{field_name} cannot be blank.")


def _normalize_optional_text(value: str | None) -> str | None:
    """Trim optional strings and collapse blank values to null."""

    if value is None:
        return None

    normalized = value.strip()
    return normalized or None


def _normalize_account_type(value: str) -> str:
    """Normalize account-type labels into lower snake_case values."""

    normalized = _normalize_required_text(value, field_name="account_type")
    return normalized.lower().replace(" ", "_")


def _normalize_account_code(value: str) -> str:
    """Normalize account codes and reject whitespace-only values."""

    return _normalize_required_text(value, field_name="account_code")


class CoaAccountSummary(ContractModel):
    """Describe one account row attached to a COA set."""

    id: str = Field(description="Stable UUID for the account row.")
    coa_set_id: str = Field(description="Stable UUID of the owning COA set.")
    account_code: str = Field(min_length=1, description="Operator-facing account code.")
    account_name: str = Field(min_length=1, description="Operator-facing account name.")
    account_type: str = Field(min_length=1, description="Normalized account type label.")
    parent_account_id: str | None = Field(
        default=None,
        description="Optional parent account row in the same COA set.",
    )
    is_postable: bool = Field(description="Whether the account can be used for posting lines.")
    is_active: bool = Field(description="Whether the account is active for mapping and posting.")
    external_ref: str | None = Field(
        default=None,
        description="Optional external integration reference for this account.",
    )
    dimension_defaults: dict[str, str] = Field(
        default_factory=dict,
        description="Default cost-centre/department/project dimensions for this account.",
    )
    created_at: datetime = Field(description="UTC timestamp when the account row was created.")
    updated_at: datetime = Field(description="UTC timestamp when the account row was last updated.")


class CoaSetSummary(ContractModel):
    """Describe one versioned COA set for an entity workspace."""

    id: str = Field(description="Stable UUID for the COA set.")
    entity_id: str = Field(description="Stable UUID of the owning entity workspace.")
    source: str = Field(
        min_length=1,
        description="COA source: manual_upload, quickbooks_sync, or fallback_nigerian_sme.",
    )
    version_no: int = Field(ge=1, description="Monotonic version number for this entity's COA set.")
    is_active: bool = Field(description="Whether this COA set is currently active for the entity.")
    account_count: int = Field(ge=0, description="Number of account rows attached to the set.")
    import_metadata: dict[str, object] = Field(
        default_factory=dict,
        description="Source-specific import metadata captured when the set was created.",
    )
    activated_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the set became active, if ever.",
    )
    created_at: datetime = Field(description="UTC timestamp when the set was created.")
    updated_at: datetime = Field(description="UTC timestamp when the set was last updated.")


class CoaWorkspaceResponse(ContractModel):
    """Describe the resolved COA workspace used by the entity COA editor."""

    entity_id: str = Field(description="Stable UUID of the entity workspace.")
    active_set: CoaSetSummary = Field(description="Currently active COA set for the entity.")
    accounts: tuple[CoaAccountSummary, ...] = Field(
        default=(),
        description="Accounts attached to the active set in deterministic display order.",
    )
    coa_sets: tuple[CoaSetSummary, ...] = Field(
        default=(),
        description="All COA set versions for the entity, newest first.",
    )
    precedence_order: tuple[str, ...] = Field(
        default=("manual_upload", "quickbooks_sync", "fallback_nigerian_sme"),
        description="Current precedence order used when no set is active.",
    )


class CoaSetActivationRequest(ContractModel):
    """Capture an explicit operator reason when switching the active COA set."""

    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Optional reason persisted in the activity timeline for activation changes.",
    )

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        """Normalize optional activation reasons."""

        return _normalize_optional_text(value)


class CoaAccountCreateRequest(ContractModel):
    """Capture required fields for creating a new account in a COA revision."""

    account_code: str = Field(min_length=1, max_length=120, description="New account code.")
    account_name: str = Field(min_length=1, max_length=240, description="New account name.")
    account_type: str = Field(min_length=1, max_length=120, description="New account type.")
    parent_account_id: UUID | None = Field(
        default=None,
        description="Optional parent account UUID in the active set.",
    )
    is_postable: bool = Field(default=True, description="Posting eligibility for the new account.")
    is_active: bool = Field(default=True, description="Activation flag for the new account.")
    external_ref: str | None = Field(
        default=None,
        max_length=200,
        description="Optional external system reference.",
    )
    dimension_defaults: dict[str, str] = Field(
        default_factory=dict,
        description="Optional default dimensions attached to the account.",
    )

    @field_validator("account_code")
    @classmethod
    def normalize_account_code(cls, value: str) -> str:
        """Normalize required account-code values."""

        return _normalize_account_code(value)

    @field_validator("account_name")
    @classmethod
    def normalize_account_name(cls, value: str) -> str:
        """Normalize required account-name values."""

        return _normalize_required_text(value, field_name="account_name")

    @field_validator("account_type")
    @classmethod
    def normalize_account_type(cls, value: str) -> str:
        """Normalize account-type labels."""

        return _normalize_account_type(value)

    @field_validator("external_ref")
    @classmethod
    def normalize_external_ref(cls, value: str | None) -> str | None:
        """Normalize optional external references."""

        return _normalize_optional_text(value)


class CoaAccountUpdateRequest(ContractModel):
    """Capture editable fields for updating one account in a versioned COA revision."""

    account_code: str | None = Field(default=None, min_length=1, max_length=120)
    account_name: str | None = Field(default=None, min_length=1, max_length=240)
    account_type: str | None = Field(default=None, min_length=1, max_length=120)
    parent_account_id: UUID | None = Field(default=None)
    is_postable: bool | None = Field(default=None)
    is_active: bool | None = Field(default=None)
    external_ref: str | None = Field(default=None, max_length=200)
    dimension_defaults: dict[str, str] | None = Field(default=None)

    @field_validator("account_code")
    @classmethod
    def normalize_account_code(cls, value: str | None) -> str | None:
        """Normalize optional account-code updates."""

        if value is None:
            return None
        return _normalize_account_code(value)

    @field_validator("account_name")
    @classmethod
    def normalize_account_name(cls, value: str | None) -> str | None:
        """Normalize optional account-name updates."""

        if value is None:
            return None
        return _normalize_required_text(value, field_name="account_name")

    @field_validator("account_type")
    @classmethod
    def normalize_account_type(cls, value: str | None) -> str | None:
        """Normalize optional account-type updates."""

        if value is None:
            return None
        return _normalize_account_type(value)

    @field_validator("external_ref")
    @classmethod
    def normalize_external_ref(cls, value: str | None) -> str | None:
        """Normalize optional external-reference updates."""

        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_has_fields(self) -> CoaAccountUpdateRequest:
        """Reject empty updates so API callers provide at least one mutable field."""

        if not self.model_fields_set:
            raise ValueError("Provide at least one account field to update.")

        return self


__all__ = [
    "CoaAccountCreateRequest",
    "CoaAccountSummary",
    "CoaAccountUpdateRequest",
    "CoaSetActivationRequest",
    "CoaSetSummary",
    "CoaWorkspaceResponse",
]
