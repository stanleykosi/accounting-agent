"""
Purpose: Define strict API contracts for entity workspaces, memberships,
and activity timeline reads.
Scope: Entity create/update payloads plus response models for workspace
summaries, members, and the root event stream that drives the entity
activity timeline.
Dependencies: Pydantic contract defaults, canonical autonomy modes, and
shared numeric helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator
from services.common.enums import AutonomyMode
from services.common.types import PositiveInteger, Ratio
from services.contracts.api_models import ContractModel

DEFAULT_WORKSPACE_LANGUAGE: Literal["en"] = "en"


def _normalize_name(value: str, *, field_name: str) -> str:
    """Trim a human-readable name field and reject blank values after normalization."""

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be blank.")

    return normalized


def _normalize_currency_code(value: str) -> str:
    """Normalize a currency code into canonical uppercase ISO-like form."""

    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError("Currency codes must be three alphabetic characters.")

    return normalized


def _normalize_country_code(value: str) -> str:
    """Normalize a country code into canonical uppercase ISO-like form."""

    normalized = value.strip().upper()
    if len(normalized) != 2 or not normalized.isalpha():
        raise ValueError("Country codes must be two alphabetic characters.")

    return normalized


def _normalize_timezone(value: str) -> str:
    """Trim a timezone identifier and reject empty strings."""

    normalized = value.strip()
    if not normalized:
        raise ValueError("Timezone cannot be blank.")

    return normalized


def _normalize_role(value: str) -> str:
    """Normalize a membership role into a lower-case stable storage form."""

    normalized = value.strip().lower().replace(" ", "_")
    if not normalized:
        raise ValueError("Membership role cannot be blank.")

    return normalized


class EntityOperatorSummary(ContractModel):
    """Describe a local operator shown in workspace membership and activity responses."""

    id: str = Field(description="Stable UUID for the local operator.")
    email: str = Field(min_length=3, max_length=320, description="Canonical local user email.")
    full_name: str = Field(
        min_length=1,
        max_length=200,
        description="Audit-friendly operator display name.",
    )


class EntityMembershipSummary(ContractModel):
    """Describe one operator's membership inside an entity workspace."""

    id: str = Field(description="Stable UUID for the membership row.")
    role: str = Field(min_length=1, description="Normalized role label stored for the member.")
    is_default_actor: bool = Field(
        description="Indicates whether this member is the current default actor for the workspace."
    )
    user: EntityOperatorSummary = Field(description="Operator attached to the membership row.")


class EntityActivityEvent(ContractModel):
    """Describe one immutable entity-scoped activity event shown in the timeline."""

    id: str = Field(description="Stable UUID for the audit event row.")
    event_type: str = Field(
        min_length=1,
        description=(
            "Stable event type label such as `entity.created` or "
            "`entity.membership_added`."
        ),
    )
    summary: str = Field(
        min_length=1,
        description="Operator-facing summary shown in the entity activity timeline.",
    )
    source_surface: str = Field(
        min_length=1,
        description="Surface that emitted the event, such as desktop or worker.",
    )
    trace_id: str | None = Field(
        default=None,
        description="Request or trace identifier that links the event back to runtime logs.",
    )
    created_at: datetime = Field(description="UTC timestamp when the event was recorded.")
    actor: EntityOperatorSummary | None = Field(
        default=None,
        description="Operator who caused the event, when the event is actor-driven.",
    )


class EntitySummary(ContractModel):
    """Describe one entity workspace returned by list and detail reads."""

    id: str = Field(description="Stable UUID for the entity workspace.")
    name: str = Field(min_length=1, max_length=200, description="Primary workspace display name.")
    legal_name: str | None = Field(
        default=None,
        description="Optional legal entity name used in reporting and exports.",
    )
    base_currency: str = Field(
        min_length=3,
        max_length=3,
        description="Primary base currency for the workspace, defaulting to NGN.",
    )
    country_code: str = Field(
        min_length=2,
        max_length=2,
        description="Workspace country code used for defaults and formatting.",
    )
    timezone: str = Field(
        min_length=1,
        description="Canonical IANA timezone identifier for workspace-local scheduling.",
    )
    workspace_language: Literal["en"] = Field(
        default=DEFAULT_WORKSPACE_LANGUAGE,
        description=(
            "Current UI language scope for the workspace. English is the only "
            "supported value now."
        ),
    )
    accounting_standard: str | None = Field(
        default=None,
        description="Optional accounting-standard label captured for the workspace.",
    )
    autonomy_mode: AutonomyMode = Field(
        description="Approval-routing mode currently configured for the workspace.",
    )
    status: Literal["active", "archived"] = Field(
        description="Workspace lifecycle state."
    )
    member_count: PositiveInteger = Field(
        description="Number of active membership rows attached to the workspace."
    )
    current_user_membership: EntityMembershipSummary = Field(
        description="Membership row for the authenticated caller."
    )
    default_actor: EntityOperatorSummary | None = Field(
        default=None,
        description="Current default actor highlighted for the workspace.",
    )
    last_activity: EntityActivityEvent | None = Field(
        default=None,
        description="Most recent entity-scoped activity event, if one exists.",
    )
    default_confidence_thresholds: dict[str, Ratio] = Field(
        description="Entity-level confidence thresholds used by later workflow features."
    )
    created_at: datetime = Field(description="UTC timestamp when the workspace was created.")
    updated_at: datetime = Field(description="UTC timestamp when the workspace was last updated.")


class EntityWorkspace(EntitySummary):
    """Describe one full workspace detail response including members and activity history."""

    memberships: tuple[EntityMembershipSummary, ...] = Field(
        default=(),
        description="All current workspace memberships in deterministic display order.",
    )
    activity_events: tuple[EntityActivityEvent, ...] = Field(
        default=(),
        description="Recent entity-scoped activity events ordered newest first.",
    )


class EntityListResponse(ContractModel):
    """Return the authenticated caller's accessible entity workspaces."""

    entities: tuple[EntitySummary, ...] = Field(
        default=(),
        description="Entity workspaces the authenticated operator can access.",
    )


class CreateEntityRequest(ContractModel):
    """Capture the fields required to create a new entity workspace."""

    name: str = Field(
        min_length=1,
        max_length=200,
        description="Display name shown throughout the workspace shell.",
    )
    legal_name: str | None = Field(
        default=None,
        max_length=240,
        description="Optional legal entity name used in documents and reports.",
    )
    base_currency: str = Field(
        default="NGN",
        min_length=3,
        max_length=3,
        description="Primary base currency for the workspace. Defaults to NGN.",
    )
    country_code: str = Field(
        default="NG",
        min_length=2,
        max_length=2,
        description="Country code used for workspace defaults.",
    )
    timezone: str = Field(
        default="Africa/Lagos",
        min_length=1,
        description="IANA timezone identifier used for workspace-local timing.",
    )
    accounting_standard: str | None = Field(
        default=None,
        max_length=120,
        description="Optional accounting standard label for the workspace.",
    )
    autonomy_mode: AutonomyMode = Field(
        default=AutonomyMode.HUMAN_REVIEW,
        description="Approval-routing mode to seed on the new workspace.",
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Normalize the workspace display name before it reaches the service layer."""

        return _normalize_name(value, field_name="Workspace name")

    @field_validator("legal_name")
    @classmethod
    def normalize_legal_name(cls, value: str | None) -> str | None:
        """Trim the optional legal name and collapse blank values to null."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @field_validator("base_currency")
    @classmethod
    def normalize_base_currency(cls, value: str) -> str:
        """Normalize the workspace base currency into uppercase three-letter form."""

        return _normalize_currency_code(value)

    @field_validator("country_code")
    @classmethod
    def normalize_country_code(cls, value: str) -> str:
        """Normalize the workspace country code into uppercase two-letter form."""

        return _normalize_country_code(value)

    @field_validator("timezone")
    @classmethod
    def normalize_timezone(cls, value: str) -> str:
        """Normalize the workspace timezone before persistence."""

        return _normalize_timezone(value)

    @field_validator("accounting_standard")
    @classmethod
    def normalize_accounting_standard(cls, value: str | None) -> str | None:
        """Trim the optional accounting-standard label and collapse blanks to null."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None


class UpdateEntityRequest(ContractModel):
    """Capture the optional fields that can be updated on an existing entity workspace."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=240)
    base_currency: str | None = Field(default=None, min_length=3, max_length=3)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str | None = Field(default=None, min_length=1)
    accounting_standard: str | None = Field(default=None, max_length=120)
    autonomy_mode: AutonomyMode | None = Field(default=None)

    @field_validator("name")
    @classmethod
    def normalize_updated_name(cls, value: str | None) -> str | None:
        """Normalize updated workspace names when callers provide them."""

        if value is None:
            return None

        return _normalize_name(value, field_name="Workspace name")

    @field_validator("legal_name")
    @classmethod
    def normalize_updated_legal_name(cls, value: str | None) -> str | None:
        """Trim updated legal names and collapse blanks to null."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @field_validator("base_currency")
    @classmethod
    def normalize_updated_base_currency(cls, value: str | None) -> str | None:
        """Normalize updated base currencies into uppercase three-letter form."""

        if value is None:
            return None

        return _normalize_currency_code(value)

    @field_validator("country_code")
    @classmethod
    def normalize_updated_country_code(cls, value: str | None) -> str | None:
        """Normalize updated country codes into uppercase two-letter form."""

        if value is None:
            return None

        return _normalize_country_code(value)

    @field_validator("timezone")
    @classmethod
    def normalize_updated_timezone(cls, value: str | None) -> str | None:
        """Normalize updated timezone identifiers when supplied."""

        if value is None:
            return None

        return _normalize_timezone(value)

    @field_validator("accounting_standard")
    @classmethod
    def normalize_updated_accounting_standard(cls, value: str | None) -> str | None:
        """Trim updated accounting-standard labels and collapse blanks to null."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_non_empty_patch(self) -> UpdateEntityRequest:
        """Require callers to update at least one field."""

        if (
            self.name is None
            and self.legal_name is None
            and self.base_currency is None
            and self.country_code is None
            and self.timezone is None
            and self.accounting_standard is None
            and self.autonomy_mode is None
        ):
            raise ValueError("Provide at least one workspace field to update.")

        return self


class CreateEntityMembershipRequest(ContractModel):
    """Capture the inputs required to add an existing local operator to an entity workspace."""

    user_email: str = Field(
        min_length=3,
        max_length=320,
        description="Canonical email address for the already-provisioned local operator.",
    )
    role: str = Field(
        default="member",
        min_length=1,
        max_length=64,
        description="Role label stored on the membership row.",
    )
    is_default_actor: bool = Field(
        default=False,
        description="Whether the new membership should become the workspace's default actor.",
    )

    @field_validator("user_email")
    @classmethod
    def normalize_user_email(cls, value: str) -> str:
        """Normalize member emails into the same case-folded form used by local auth."""

        normalized = value.strip().casefold()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("Enter a valid email address.")

        return normalized

    @field_validator("role")
    @classmethod
    def normalize_role(cls, value: str) -> str:
        """Normalize membership roles into a lower-case stable storage form."""

        return _normalize_role(value)


class UpdateEntityMembershipRequest(ContractModel):
    """Capture the optional fields that can be updated on an existing workspace membership."""

    role: str | None = Field(default=None, min_length=1, max_length=64)
    is_default_actor: bool | None = Field(default=None)

    @field_validator("role")
    @classmethod
    def normalize_updated_role(cls, value: str | None) -> str | None:
        """Normalize updated membership roles when provided."""

        if value is None:
            return None

        return _normalize_role(value)

    @model_validator(mode="after")
    def validate_non_empty_membership_patch(self) -> UpdateEntityMembershipRequest:
        """Require callers to update at least one membership field."""

        if self.role is None and self.is_default_actor is None:
            raise ValueError("Provide at least one membership field to update.")

        return self


__all__ = [
    "DEFAULT_WORKSPACE_LANGUAGE",
    "CreateEntityMembershipRequest",
    "CreateEntityRequest",
    "EntityActivityEvent",
    "EntityListResponse",
    "EntityMembershipSummary",
    "EntityOperatorSummary",
    "EntitySummary",
    "EntityWorkspace",
    "UpdateEntityMembershipRequest",
    "UpdateEntityRequest",
]
