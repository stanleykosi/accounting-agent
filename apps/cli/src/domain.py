"""
Purpose: Mirror the canonical workflow and lifecycle language for the future CLI.
Scope: Rich/Textual-friendly label and style helpers that consume backend enums
without inventing a second vocabulary.
Dependencies: Shared domain enums from services/common/enums.py only.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.common.enums import (
    ArtifactType,
    AutonomyMode,
    CANONICAL_WORKFLOW_PHASES,
    CloseRunStatus,
    JobStatus,
    ReviewStatus,
    WorkflowPhase,
)

CliDomainValue = (
    WorkflowPhase | CloseRunStatus | JobStatus | ReviewStatus | AutonomyMode | ArtifactType
)


@dataclass(frozen=True, slots=True)
class CliBadge:
    """Describe a CLI-facing label, style token, and explanation for one domain value."""

    text: str
    style: str
    description: str


def iter_workflow_phases() -> tuple[WorkflowPhase, ...]:
    """Return workflow phases in the fixed order the CLI should render them."""

    return CANONICAL_WORKFLOW_PHASES


def get_cli_badge(value: CliDomainValue) -> CliBadge:
    """Resolve the text, style, and description used when rendering a domain badge."""

    return CliBadge(
        text=value.label,
        style=_resolve_cli_style(value),
        description=value.description,
    )


def _resolve_cli_style(value: CliDomainValue) -> str:
    """Map a canonical domain value to a stable Rich style token."""

    if value in {WorkflowPhase.COLLECTION, WorkflowPhase.PROCESSING, WorkflowPhase.RECONCILIATION}:
        return "bold cyan"

    if value in {WorkflowPhase.REPORTING, WorkflowPhase.REVIEW_SIGNOFF}:
        return "bold blue"

    if value in {
        CloseRunStatus.APPROVED,
        JobStatus.COMPLETED,
        ReviewStatus.APPROVED,
        ReviewStatus.APPLIED,
    }:
        return "bold green"

    if value in {
        CloseRunStatus.EXPORTED,
        CloseRunStatus.ARCHIVED,
        CloseRunStatus.REOPENED,
        ArtifactType.REPORT_EXCEL,
        ArtifactType.REPORT_PDF,
        ArtifactType.AUDIT_TRAIL,
        ArtifactType.EVIDENCE_PACK,
        ArtifactType.QUICKBOOKS_EXPORT,
    }:
        return "bold blue"

    if value in {
        CloseRunStatus.DRAFT,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        ReviewStatus.DRAFT,
        ReviewStatus.SUPERSEDED,
        AutonomyMode.REDUCED_INTERRUPTION,
    }:
        return "bold white"

    if value in {
        CloseRunStatus.IN_REVIEW,
        ReviewStatus.PENDING_REVIEW,
        JobStatus.CANCELED,
        AutonomyMode.HUMAN_REVIEW,
    }:
        return "bold yellow"

    return "bold red"


__all__ = ["CliBadge", "get_cli_badge", "iter_workflow_phases"]
