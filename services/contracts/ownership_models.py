"""
Purpose: Define strict API contracts for ownership, last-touch, and in-progress locks.
Scope: Target identifiers, lock acquire/release/touch payloads, and response models
used by the desktop UI and later document/recommendation review surfaces.
Dependencies: Pydantic contract defaults and the canonical ownership target enum.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from services.common.enums import OwnershipTargetType
from services.contracts.api_models import ContractModel
from services.contracts.entity_models import EntityOperatorSummary


def _normalize_optional_note(value: str | None) -> str | None:
    """Trim optional lock notes and collapse blank strings to null."""

    if value is None:
        return None

    normalized = value.strip()
    return normalized or None


class OwnershipTargetReference(ContractModel):
    """Identify one business object that can carry owner and lock metadata."""

    target_type: OwnershipTargetType = Field(
        description="Canonical type of the ownership target.",
    )
    target_id: UUID = Field(description="Stable UUID of the target object.")
    close_run_id: UUID | None = Field(
        default=None,
        description="Owning close-run UUID for close-run-scoped targets.",
    )

    @model_validator(mode="after")
    def validate_target_scope(self) -> OwnershipTargetReference:
        """Ensure close-run scoped targets carry the close-run ID needed for access checks."""

        if (
            self.target_type
            in {
                OwnershipTargetType.DOCUMENT,
                OwnershipTargetType.RECOMMENDATION,
                OwnershipTargetType.REVIEW_TARGET,
            }
            and self.close_run_id is None
        ):
            raise ValueError("close_run_id is required for close-run-scoped ownership targets.")

        return self


class AcquireOwnershipLockRequest(OwnershipTargetReference):
    """Capture a request to assign ownership and hold an in-progress lock."""

    owner_user_id: UUID | None = Field(
        default=None,
        description="Optional entity member to assign as owner. Defaults to the caller.",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Optional operator note describing why the lock was acquired.",
    )

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        """Normalize optional lock notes before service execution."""

        return _normalize_optional_note(value)


class ReleaseOwnershipLockRequest(OwnershipTargetReference):
    """Capture a request to release the caller's in-progress lock."""


class TouchOwnershipTargetRequest(OwnershipTargetReference):
    """Capture a request to update last-touch metadata without taking a lock."""


class OwnershipState(ContractModel):
    """Describe current ownership, lock, and last-touch metadata for a target."""

    entity_id: str = Field(description="Stable UUID of the owning entity workspace.")
    close_run_id: str | None = Field(
        default=None,
        description="Owning close-run UUID when the target is close-run scoped.",
    )
    target_type: OwnershipTargetType = Field(description="Canonical target type.")
    target_id: str = Field(description="Stable UUID of the target object.")
    owner: EntityOperatorSummary | None = Field(
        default=None,
        description="Operator assigned to own this item, if any.",
    )
    locked_by: EntityOperatorSummary | None = Field(
        default=None,
        description="Operator currently holding the in-progress lock, if any.",
    )
    locked_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the in-progress lock was acquired.",
    )
    last_touched_by: EntityOperatorSummary | None = Field(
        default=None,
        description="Operator who most recently touched this target.",
    )
    last_touched_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the latest touch.",
    )
    lock_note: str | None = Field(
        default=None,
        description="Optional current lock note supplied by the locking operator.",
    )


__all__ = [
    "AcquireOwnershipLockRequest",
    "OwnershipState",
    "OwnershipTargetReference",
    "ReleaseOwnershipLockRequest",
    "TouchOwnershipTargetRequest",
]
