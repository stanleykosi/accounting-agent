"""
Purpose: Define validated Pydantic contracts for accounting recommendation payloads.
Scope: Request/response models for recommendation creation, LLM reasoning outputs,
evidence links, confidence metrics, and autonomy-mode routing decisions. These
models are the source of truth for LangGraph node outputs and API route contracts.
Dependencies: Pydantic, canonical enums, API contract base model.

Design notes:
- Every model uses extra='forbid' so that stray LLM keys are rejected, not silently ignored.
- Confidence values are strictly bounded between 0.0 and 1.0.
- Evidence links are structured references to document extractions, parser outputs,
  and deterministic rule evaluations.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from services.common.enums import (
    AccountType,
    AutonomyMode,
    DocumentType,
    ReviewStatus,
)
from services.contracts.api_models import ContractModel

# ---------------------------------------------------------------------------
# LLM bounded reasoning outputs (validated before any state mutation)
# ---------------------------------------------------------------------------


class DocumentTypeScore(ContractModel):
    """Represent one candidate type with a confidence score."""

    document_type: DocumentType = Field(description="Candidate document type.")
    score: float = Field(ge=0.0, le=1.0, description="Confidence score for this candidate.")


class DocumentClassificationOutput(ContractModel):
    """Structured output from the document classification reasoning prompt."""

    predicted_type: DocumentType = Field(
        description="The predicted canonical document type.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score between 0.0 and 1.0.",
    )
    reasoning: str = Field(
        min_length=1,
        max_length=2000,
        description="Brief explanation of the classification decision.",
    )
    secondary_candidates: list[DocumentTypeScore] = Field(
        default_factory=list,
        description="Alternative document types with their scores.",
    )


class GLCodingExplanationOutput(ContractModel):
    """Structured output from the GL coding explanation prompt."""

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall confidence in the GL coding recommendation.",
    )
    reasoning_summary: str = Field(
        min_length=1,
        max_length=2000,
        description="Concise explanation for why this account was selected.",
    )
    risk_factors: list[str] = Field(
        default_factory=list,
        description="Risk signals that may warrant human review.",
    )
    alternative_accounts: list[str] = Field(
        default_factory=list,
        description="Plausible alternative account codes.",
    )


class AccountRanking(ContractModel):
    """Represent one candidate account in an ambiguous ranking."""

    account_code: str = Field(min_length=1, description="Candidate account code.")
    score: float = Field(ge=0.0, le=1.0, description="Relevance score for this account.")
    reason: str = Field(min_length=1, description="Brief reason for this ranking.")


class AmbiguousMappingRankingOutput(ContractModel):
    """Structured output from the ambiguous mapping ranking prompt."""

    rankings: list[AccountRanking] = Field(
        min_length=1,
        description="Candidate accounts ranked by relevance score.",
    )
    top_recommendation: str = Field(
        min_length=1,
        description="The account code with the highest ranking score.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall confidence in the top recommendation.",
    )

    @field_validator("rankings")
    @classmethod
    def validate_rankings_not_empty(cls, value: list[AccountRanking]) -> list[AccountRanking]:
        """Ensure at least one ranking is present."""
        if not value:
            raise ValueError("At least one account ranking is required.")
        return value


class JournalNarrativeOutput(ContractModel):
    """Structured output from the journal narrative prompt."""

    description: str = Field(
        min_length=1,
        max_length=500,
        description="One-to-two-sentence journal entry description.",
    )
    memo_lines: list[str] = Field(
        default_factory=list,
        description="Brief memo notes for individual line items.",
    )


# ---------------------------------------------------------------------------
# Evidence and confidence structures
# ---------------------------------------------------------------------------


class EvidenceLink(ContractModel):
    """Reference one piece of supporting evidence for a recommendation."""

    model_config = ConfigDict(frozen=True)

    source_type: str = Field(
        min_length=1,
        description="Type of evidence source (e.g., 'extraction', 'parser', 'rule').",
    )
    source_id: str = Field(
        min_length=1,
        description="UUID or key identifying the evidence source.",
    )
    description: str = Field(
        min_length=1,
        description="Brief description of what this evidence supports.",
    )


class ConfidenceMetrics(ContractModel):
    """Aggregate confidence signals for a recommendation."""

    overall_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Aggregate confidence score for the recommendation.",
    )
    deterministic_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence from deterministic rule evaluation, if applicable.",
    )
    model_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence from the LLM reasoning step, if applicable.",
    )
    low_confidence_fields: list[str] = Field(
        default_factory=list,
        description="Field names with confidence below the review threshold.",
    )


# ---------------------------------------------------------------------------
# Recommendation creation contracts
# ---------------------------------------------------------------------------


class CreateRecommendationInput(ContractModel):
    """Input for creating a validated accounting recommendation."""

    model_config = ConfigDict(frozen=False)

    close_run_id: UUID = Field(description="Close run this recommendation belongs to.")
    document_id: UUID | None = Field(
        default=None,
        description="Source document UUID, if applicable.",
    )
    recommendation_type: str = Field(
        min_length=1,
        description="Canonical recommendation type (e.g., 'gl_coding', 'journal_draft').",
    )
    payload: dict[str, Any] = Field(
        description="Validated recommendation payload (structured data).",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall recommendation confidence between 0.0 and 1.0.",
    )
    reasoning_summary: str = Field(
        min_length=1,
        max_length=5000,
        description="Human-readable reasoning narrative for the recommendation.",
    )
    evidence_links: list[EvidenceLink] = Field(
        default_factory=list,
        description="Structured references to supporting evidence.",
    )
    prompt_version: str = Field(
        min_length=1,
        description="Version of the prompt template that produced this recommendation.",
    )
    rule_version: str = Field(
        min_length=1,
        description="Version of the deterministic rules used in this recommendation.",
    )
    schema_version: str = Field(
        min_length=1,
        description="Version of the output schema this recommendation conforms to.",
    )


class CreateRecommendationResult(ContractModel):
    """Result returned after persisting a new recommendation."""

    recommendation_id: UUID = Field(description="The UUID of the newly created recommendation.")
    status: ReviewStatus = Field(
        description="Initial review status (draft or pending_review based on autonomy mode).",
    )


# ---------------------------------------------------------------------------
# Recommendation context for LangGraph workflow
# ---------------------------------------------------------------------------


class RecommendationContext(ContractModel):
    """Aggregate context passed into the LangGraph recommendation workflow."""

    model_config = ConfigDict(frozen=False)

    close_run_id: UUID = Field(description="Close run under processing.")
    document_id: UUID | None = Field(default=None, description="Source document, if any.")
    entity_id: UUID = Field(description="Entity workspace owning the close run.")
    period_start: date = Field(description="Accounting period start.")
    period_end: date = Field(description="Accounting period end.")
    document_type: DocumentType | None = Field(
        default=None,
        description="Classified document type.",
    )
    extracted_fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured extraction payload.",
    )
    line_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Extracted line items with amounts and descriptions.",
    )
    coa_accounts: list[CoaAccountRef] = Field(
        default_factory=list,
        description="Active chart of accounts available for mapping.",
    )
    coa_source: str = Field(
        default="fallback_nigerian_sme",
        description="Source of the active COA (manual_upload, quickbooks_sync, fallback).",
    )
    autonomy_mode: AutonomyMode = Field(
        default=AutonomyMode.HUMAN_REVIEW,
        description="Entity autonomy mode for routing the final recommendation.",
    )
    confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum confidence required for auto-approval routing.",
    )


class CoaAccountRef(ContractModel):
    """Minimal COA account reference used inside recommendation context."""

    model_config = ConfigDict(frozen=True)

    account_code: str = Field(min_length=1, description="GL account code.")
    account_name: str = Field(min_length=1, description="GL account display name.")
    account_type: AccountType = Field(description="Canonical account family.")
    is_active: bool = Field(default=True, description="Whether the account is postable.")


# ---------------------------------------------------------------------------
# Graph state and node outputs
# ---------------------------------------------------------------------------


class GraphState(BaseModel):
    """Mutable state passed through the LangGraph recommendation workflow.

    Note: This model uses frozen=False because LangGraph nodes mutate state dicts.
    The model is used for validation of the initial context and final outputs,
    not for the intermediate mutable state during graph execution.
    """

    context: RecommendationContext = Field(
        description="Immutable recommendation context.",
    )
    deterministic_result: dict[str, Any] | None = Field(
        default=None,
        description="Result from deterministic rule evaluation, if any.",
    )
    model_reasoning: dict[str, Any] | None = Field(
        default=None,
        description="Validated LLM reasoning output, if the model was called.",
    )
    final_recommendation: CreateRecommendationInput | None = Field(
        default=None,
        description="Assembled and validated recommendation ready for persistence.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Accumulated error messages from graph nodes.",
    )


__all__ = [
    "AccountRanking",
    "AmbiguousMappingRankingOutput",
    "CoaAccountRef",
    "ConfidenceMetrics",
    "CreateRecommendationInput",
    "CreateRecommendationResult",
    "DocumentClassificationOutput",
    "DocumentTypeScore",
    "EvidenceLink",
    "GLCodingExplanationOutput",
    "GraphState",
    "JournalNarrativeOutput",
    "RecommendationContext",
]
