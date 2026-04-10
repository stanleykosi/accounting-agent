"""
Purpose: Define strict Pydantic models for the canonical workflow and lifecycle
language used throughout the product.
Scope: Serializable domain vocabulary catalogs plus close-run phase primitives
that later API routes, workers, and UIs can reuse directly.
Dependencies: Pydantic, shared contract defaults, low-level type aliases, and
the canonical enums in services/common/enums.py.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator
from services.common.enums import (
    CANONICAL_WORKFLOW_PHASES,
    ArtifactType,
    AutonomyMode,
    CanonicalDomainEnum,
    CloseRunPhaseStatus,
    CloseRunStatus,
    JobStatus,
    ReviewStatus,
    WorkflowPhase,
)
from services.common.types import PositiveInteger
from services.contracts.api_models import ContractModel


class DomainValueDefinition(ContractModel):
    """Describe one canonical enum value with render metadata for clients and docs."""

    value: str = Field(
        min_length=1,
        description="Serialized enum value used in APIs, persistence, and worker payloads.",
    )
    label: str = Field(
        min_length=1,
        description="Human-readable label used by UI and CLI surfaces.",
    )
    description: str = Field(
        min_length=1,
        description="Operator-facing explanation of what the value means in workflow terms.",
    )


class WorkflowPhaseDefinition(ContractModel):
    """Describe one canonical workflow phase in the required backbone order."""

    phase: WorkflowPhase = Field(
        description="Canonical workflow phase identifier used across the entire stack.",
    )
    label: str = Field(
        min_length=1,
        description="Human-readable workflow phase label for desktop and CLI rendering.",
    )
    description: str = Field(
        min_length=1,
        description="Explanation of the work that belongs to this phase.",
    )
    ordinal: PositiveInteger = Field(
        description="One-based position of the phase in the fixed five-phase workflow backbone.",
    )


class CloseRunPhaseState(ContractModel):
    """Capture the current state of one workflow phase for a specific close run."""

    phase: WorkflowPhase = Field(
        description="Workflow phase that this state row belongs to.",
    )
    status: CloseRunPhaseStatus = Field(
        description="Lifecycle state of the phase gate for the current close run.",
    )
    blocking_reason: str | None = Field(
        default=None,
        min_length=1,
        description="Recovery-oriented reason shown when the phase is explicitly blocked.",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="UTC timestamp marking when the phase became complete, if applicable.",
    )

    @model_validator(mode="after")
    def validate_blocking_reason(self) -> CloseRunPhaseState:
        """Require blocking details only when the phase gate is blocked."""

        if self.status is CloseRunPhaseStatus.BLOCKED and self.blocking_reason is None:
            message = "Blocked phase states require a blocking_reason."
            raise ValueError(message)

        if self.status is not CloseRunPhaseStatus.BLOCKED and self.blocking_reason is not None:
            message = "Only blocked phase states may include a blocking_reason."
            raise ValueError(message)

        return self


class CloseRunWorkflowState(ContractModel):
    """Capture a close run's lifecycle status together with all five phase states."""

    status: CloseRunStatus = Field(
        description="Lifecycle status of the enclosing close run.",
    )
    active_phase: WorkflowPhase | None = Field(
        default=None,
        description="Current working phase when the close run is actively progressing.",
    )
    phase_states: tuple[CloseRunPhaseState, ...] = Field(
        min_length=len(CANONICAL_WORKFLOW_PHASES),
        max_length=len(CANONICAL_WORKFLOW_PHASES),
        description="All five workflow phase states in canonical order.",
    )

    @model_validator(mode="after")
    def validate_phase_coverage(self) -> CloseRunWorkflowState:
        """Ensure the phase state list covers the full workflow backbone in order."""

        actual_order = tuple(phase_state.phase for phase_state in self.phase_states)
        if actual_order != CANONICAL_WORKFLOW_PHASES:
            message = "phase_states must include every canonical workflow phase in order."
            raise ValueError(message)

        if self.active_phase is not None and self.active_phase not in actual_order:
            message = "active_phase must refer to one of the canonical phase states."
            raise ValueError(message)

        return self


class DomainLanguageCatalog(ContractModel):
    """Collect the canonical enum vocabulary exposed to every runtime surface."""

    workflow_phases: tuple[WorkflowPhaseDefinition, ...] = Field(
        min_length=len(CANONICAL_WORKFLOW_PHASES),
        max_length=len(CANONICAL_WORKFLOW_PHASES),
        description="The fixed five-phase accounting workflow backbone in display order.",
    )
    close_run_statuses: tuple[DomainValueDefinition, ...] = Field(
        min_length=len(CloseRunStatus),
        description="Lifecycle vocabulary for close runs.",
    )
    close_run_phase_statuses: tuple[DomainValueDefinition, ...] = Field(
        min_length=len(CloseRunPhaseStatus),
        description="Per-phase gate statuses used inside close runs.",
    )
    job_statuses: tuple[DomainValueDefinition, ...] = Field(
        min_length=len(JobStatus),
        description="Asynchronous background job lifecycle states.",
    )
    autonomy_modes: tuple[DomainValueDefinition, ...] = Field(
        min_length=len(AutonomyMode),
        description="Human approval routing modes that govern AI-suggested changes.",
    )
    review_statuses: tuple[DomainValueDefinition, ...] = Field(
        min_length=len(ReviewStatus),
        description="Shared review lifecycle states for recommendations and similar objects.",
    )
    artifact_types: tuple[DomainValueDefinition, ...] = Field(
        min_length=len(ArtifactType),
        description="Released artifact categories linked to close run versions.",
    )


def build_domain_value_definitions(
    enum_type: type[CanonicalDomainEnum],
) -> tuple[DomainValueDefinition, ...]:
    """Serialize a canonical enum into immutable value definitions for contracts."""

    return tuple(
        DomainValueDefinition(
            value=member.value,
            label=member.label,
            description=member.description,
        )
        for member in enum_type
    )


def build_workflow_phase_definitions() -> tuple[WorkflowPhaseDefinition, ...]:
    """Serialize the five workflow phases in the exact order required by the product."""

    return tuple(
        WorkflowPhaseDefinition(
            phase=phase,
            label=phase.label,
            description=phase.description,
            ordinal=index,
        )
        for index, phase in enumerate(CANONICAL_WORKFLOW_PHASES, start=1)
    )


def build_domain_language_catalog() -> DomainLanguageCatalog:
    """Build the canonical domain-language catalog shared by APIs, UI, and CLI surfaces."""

    return DomainLanguageCatalog(
        workflow_phases=build_workflow_phase_definitions(),
        close_run_statuses=build_domain_value_definitions(CloseRunStatus),
        close_run_phase_statuses=build_domain_value_definitions(CloseRunPhaseStatus),
        job_statuses=build_domain_value_definitions(JobStatus),
        autonomy_modes=build_domain_value_definitions(AutonomyMode),
        review_statuses=build_domain_value_definitions(ReviewStatus),
        artifact_types=build_domain_value_definitions(ArtifactType),
    )


DEFAULT_DOMAIN_LANGUAGE_CATALOG = build_domain_language_catalog()

__all__ = [
    "DEFAULT_DOMAIN_LANGUAGE_CATALOG",
    "CloseRunPhaseState",
    "CloseRunWorkflowState",
    "DomainLanguageCatalog",
    "DomainValueDefinition",
    "WorkflowPhaseDefinition",
    "build_domain_language_catalog",
    "build_domain_value_definitions",
    "build_workflow_phase_definitions",
]
