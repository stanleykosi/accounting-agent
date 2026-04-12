"""
Purpose: Implement approval routing and apply-state transitions for accounting
recommendations and their materialized journal entries.
Scope: Autonomy-mode routing, human approval/rejection, journal application,
edit-with-override flows, and immutable audit-record emission for every state change.
Dependencies: Canonical enums, audit service, journal draft generator, Pydantic contracts,
and UUID serialization.

Design notes:
- Autonomy mode changes routing, NOT safety boundaries or business rules.
- In HUMAN_REVIEW mode: all recommendations and journals route to pending_review.
- In REDUCED_INTERRUPTION mode: low-risk items may advance to approved/working state
  after policy checks, but everything is still logged and reversible before export.
- No state mutation happens without an audit record linking actor, surface, autonomy mode,
  and before/after payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from services.accounting.journal_drafts import (
    JournalDraftError,
    build_journal_draft_from_recommendation,
    build_journal_draft_input,
    generate_journal_number,
)
from services.common.enums import (
    AutonomyMode,
    ReviewStatus,
    RiskLevel,
)
from services.common.types import JsonObject, utc_now
from services.contracts.journal_models import (
    AutonomyRoutingResult,
    JournalDraftResult,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.recommendation_journal_repo import JournalWithLinesResult


class RecommendationApplyError(ValueError):
    """Represent an expected failure during recommendation or journal apply-state routing."""

    def __init__(self, *, code: str, message: str) -> None:
        """Capture a stable error code and operator-facing diagnostic message."""
        super().__init__(message)
        self.code = code
        self.message = message


class RecommendationApplyErrorCode(StrEnum):
    """Enumerate stable error codes for recommendation/journal apply routing."""

    INVALID_TRANSITION = "invalid_transition"
    RECOMMENDATION_NOT_FOUND = "recommendation_not_found"
    JOURNAL_NOT_FOUND = "journal_not_found"
    JOURNAL_NOT_BALANCED = "journal_not_balanced"
    APPROVAL_NOT_ALLOWED = "approval_not_allowed"
    REJECTION_NOT_ALLOWED = "rejection_not_allowed"
    APPLY_NOT_ALLOWED = "apply_not_allowed"
    EDIT_NOT_ALLOWED = "edit_not_allowed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Describe the authenticated actor performing an approval or apply action."""

    user_id: UUID
    full_name: str
    email: str


@dataclass(frozen=True, slots=True)
class RecommendationApplyResult:
    """Describe the outcome of applying autonomy routing to a recommendation."""

    recommendation_id: UUID
    initial_status: ReviewStatus
    final_status: ReviewStatus
    autonomy_routing: AutonomyRoutingResult
    journal_draft_result: JournalDraftResult | None = None


@dataclass(frozen=True, slots=True)
class JournalActionResult:
    """Describe the outcome of a journal approval, rejection, edit, or apply action."""

    journal_id: UUID
    action: str
    initial_status: ReviewStatus
    final_status: ReviewStatus
    autonomy_mode: AutonomyMode


class AuditServiceProtocol(Protocol):
    """Describe the minimal audit-emitter surface needed by apply-state logic."""

    def record_review_action(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        target_type: str,
        target_id: UUID,
        action: str,
        actor_user_id: UUID,
        autonomy_mode: AutonomyMode,
        source_surface: AuditSourceSurface,
        reason: str | None,
        before_payload: JsonObject | None,
        after_payload: JsonObject | None,
        trace_id: str | None,
        audit_payload: JsonObject | None = None,
    ) -> Any:
        """Persist a review action and linked audit event."""


class JournalRepositoryProtocol(Protocol):
    """Describe the persistence operations needed by the recommendation apply service."""

    def create_journal_entry(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        recommendation_id: UUID | None,
        journal_number: str,
        posting_date: Any,
        status: str,
        description: str,
        total_debits: float,
        total_credits: float,
        line_count: int,
        source_surface: str,
        autonomy_mode: str | None,
        reasoning_summary: str | None,
        metadata_payload: dict[str, Any],
    ) -> Any:
        """Persist a journal entry header and return its ORM instance."""

    def create_journal_lines(
        self,
        *,
        journal_entry_id: UUID,
        lines: list[dict[str, Any]],
    ) -> int:
        """Persist journal line items and return the count created."""

    def get_recommendation(
        self,
        *,
        recommendation_id: UUID,
    ) -> Any | None:
        """Return a recommendation by ID or None."""

    def get_journal_entry(
        self,
        *,
        journal_id: UUID,
    ) -> JournalWithLinesResult | None:
        """Return a journal entry with its lines by ID or None."""

    def update_recommendation_status(
        self,
        *,
        recommendation_id: UUID,
        status: str,
        superseded_by_id: UUID | None = None,
    ) -> Any:
        """Update a recommendation's review status."""

    def update_journal_status(
        self,
        *,
        journal_id: UUID,
        status: str,
        approved_by_user_id: UUID | None = None,
        applied_by_user_id: UUID | None = None,
        superseded_by_id: UUID | None = None,
    ) -> Any:
        """Update a journal entry's review status."""

    def get_next_journal_sequence_no(
        self,
        *,
        entity_id: UUID,
        posting_date: Any,
    ) -> int:
        """Return the next journal sequence number for an entity in a given year."""


class RecommendationApplyService:
    """Orchestrate approval routing, journal drafting, and apply-state transitions.

    This service is the canonical gateway between:
    1. A validated recommendation (from LangGraph workflow) and its persistence
    2. Autonomy-mode routing that determines draft vs. pending_review status
    3. Journal draft generation from approved recommendations
    4. Human approval/rejection/apply decisions on journals
    5. Immutable audit records for every state mutation

    All public methods emit audit events and enforce autonomy-mode routing rules.
    """

    def __init__(
        self,
        *,
        repository: JournalRepositoryProtocol,
        audit_service: AuditServiceProtocol,
    ) -> None:
        """Capture persistence and audit boundaries."""
        self._repository = repository
        self._audit_service = audit_service

    def route_recommendation_to_review(
        self,
        *,
        recommendation_id: UUID,
        entity_id: UUID,
        close_run_id: UUID,
        autonomy_mode: AutonomyMode,
        confidence: float,
        risk_level: RiskLevel,
        actor: ActorContext,
        trace_id: str | None,
        source_surface: AuditSourceSurface = AuditSourceSurface.SYSTEM,
    ) -> RecommendationApplyResult:
        """Route a newly created recommendation through autonomy-based review routing.

        In HUMAN_REVIEW mode, recommendations always land in PENDING_REVIEW.
        In REDUCED_INTERRUPTION mode, low-risk + high-confidence items may land
        in APPROVED status (working state), while others still route to review.

        Args:
            recommendation_id: The UUID of the recommendation to route.
            entity_id: Entity workspace owning the recommendation.
            close_run_id: Close run under processing.
            autonomy_mode: The actor's current autonomy mode.
            confidence: The recommendation's overall confidence score.
            risk_level: The deterministic risk level from policy evaluation.
            actor: Authenticated actor context.
            trace_id: Current trace ID for audit linkage.
            source_surface: Surface that triggered the routing.

        Returns:
            RecommendationApplyResult with initial and final statuses.
        """
        recommendation = self._repository.get_recommendation(
            recommendation_id=recommendation_id,
        )
        if recommendation is None:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.RECOMMENDATION_NOT_FOUND,
                message=f"Recommendation {recommendation_id} not found.",
            )

        initial_status_raw = recommendation.status
        initial_status = (
            ReviewStatus(initial_status_raw)
            if isinstance(initial_status_raw, str)
            else initial_status_raw
        )
        routing = self._compute_autonomy_routing(
            autonomy_mode=autonomy_mode,
            confidence=confidence,
            risk_level=risk_level,
        )

        before_payload = {"status": initial_status.value}
        after_payload = {
            "status": routing.target_status.value,
            "autonomy_mode": autonomy_mode.value,
        }

        self._audit_service.record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type="recommendation",
            target_id=recommendation_id,
            action="route",
            actor_user_id=actor.user_id,
            autonomy_mode=autonomy_mode,
            source_surface=source_surface,
            reason=None,
            before_payload=before_payload,
            after_payload=after_payload,
            trace_id=trace_id,
            audit_payload={
                "summary": (
                    f"Recommendation {recommendation_id} routed to "
                    f"{routing.target_status.value} under {autonomy_mode.value} mode."
                ),
                "confidence": confidence,
                "risk_level": risk_level.value,
            },
        )

        self._repository.update_recommendation_status(
            recommendation_id=recommendation_id,
            status=routing.target_status.value,
        )

        # If the recommendation was auto-approved, generate a journal draft
        journal_draft_result = None
        if routing.target_status == ReviewStatus.APPROVED:
            journal_draft_result = self._generate_journal_from_approved_recommendation(
                recommendation=recommendation,
                entity_id=entity_id,
                close_run_id=close_run_id,
                actor=actor,
                trace_id=trace_id,
                source_surface=source_surface,
            )

        return RecommendationApplyResult(
            recommendation_id=recommendation_id,
            initial_status=ReviewStatus(initial_status),
            final_status=routing.target_status,
            autonomy_routing=routing,
            journal_draft_result=journal_draft_result,
        )

    def approve_recommendation(
        self,
        *,
        recommendation_id: UUID,
        entity_id: UUID,
        close_run_id: UUID,
        actor: ActorContext,
        reason: str | None,
        trace_id: str | None,
        source_surface: AuditSourceSurface = AuditSourceSurface.DESKTOP,
    ) -> RecommendationApplyResult:
        """Manually approve a pending recommendation and generate its journal draft.

        Args:
            recommendation_id: The recommendation to approve.
            entity_id: Entity workspace.
            close_run_id: Close run under processing.
            actor: Authenticated actor context.
            reason: Optional reviewer note.
            trace_id: Current trace ID.
            source_surface: Surface that triggered the approval.

        Returns:
            RecommendationApplyResult with the approval and any generated journal.
        """
        recommendation = self._repository.get_recommendation(
            recommendation_id=recommendation_id,
        )
        if recommendation is None:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.RECOMMENDATION_NOT_FOUND,
                message=f"Recommendation {recommendation_id} not found.",
            )
        if recommendation.close_run_id != close_run_id:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.INVALID_TRANSITION,
                message=(
                    f"Recommendation {recommendation_id} belongs to close run "
                    f"{recommendation.close_run_id}, not the requested {close_run_id}."
                ),
            )

        if recommendation.status not in {
            ReviewStatus.DRAFT.value,
            ReviewStatus.PENDING_REVIEW.value,
        }:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.APPROVAL_NOT_ALLOWED,
                message=(
                    f"Recommendation status is '{recommendation.status}' and cannot be approved. "
                    f"Only draft or pending_review recommendations can be approved."
                ),
            )

        initial_status = ReviewStatus(recommendation.status)

        self._audit_service.record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type="recommendation",
            target_id=recommendation_id,
            action="approve",
            actor_user_id=actor.user_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            source_surface=source_surface,
            reason=reason,
            before_payload={"status": initial_status.value},
            after_payload={"status": ReviewStatus.APPROVED.value},
            trace_id=trace_id,
            audit_payload={
                "summary": f"{actor.full_name} approved recommendation {recommendation_id}.",
            },
        )

        self._repository.update_recommendation_status(
            recommendation_id=recommendation_id,
            status=ReviewStatus.APPROVED.value,
        )

        journal_draft_result = self._generate_journal_from_approved_recommendation(
            recommendation=recommendation,
            entity_id=entity_id,
            close_run_id=close_run_id,
            actor=actor,
            trace_id=trace_id,
            source_surface=source_surface,
        )

        return RecommendationApplyResult(
            recommendation_id=recommendation_id,
            initial_status=initial_status,
            final_status=ReviewStatus.APPROVED,
            autonomy_routing=AutonomyRoutingResult(
                target_status=ReviewStatus.APPROVED,
                requires_human_approval=True,
                can_apply_automatically=False,
                reason="Human approval granted by reviewer.",
            ),
            journal_draft_result=journal_draft_result,
        )

    def reject_recommendation(
        self,
        *,
        recommendation_id: UUID,
        entity_id: UUID,
        close_run_id: UUID,
        actor: ActorContext,
        reason: str,
        trace_id: str | None,
        source_surface: AuditSourceSurface = AuditSourceSurface.DESKTOP,
    ) -> None:
        """Reject a pending recommendation so it does not affect working state.

        Args:
            recommendation_id: The recommendation to reject.
            entity_id: Entity workspace.
            close_run_id: Close run under processing.
            actor: Authenticated actor context.
            reason: Required rejection reason.
            trace_id: Current trace ID.
            source_surface: Surface that triggered the rejection.
        """
        recommendation = self._repository.get_recommendation(
            recommendation_id=recommendation_id,
        )
        if recommendation is None:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.RECOMMENDATION_NOT_FOUND,
                message=f"Recommendation {recommendation_id} not found.",
            )
        if recommendation.close_run_id != close_run_id:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.INVALID_TRANSITION,
                message=(
                    f"Recommendation {recommendation_id} belongs to close run "
                    f"{recommendation.close_run_id}, not the requested {close_run_id}."
                ),
            )

        if recommendation.status not in {
            ReviewStatus.DRAFT.value,
            ReviewStatus.PENDING_REVIEW.value,
        }:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.REJECTION_NOT_ALLOWED,
                message=(
                    f"Recommendation status is '{recommendation.status}' and cannot be rejected. "
                    f"Only draft or pending_review recommendations can be rejected."
                ),
            )

        initial_status = ReviewStatus(recommendation.status)

        self._audit_service.record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type="recommendation",
            target_id=recommendation_id,
            action="reject",
            actor_user_id=actor.user_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            source_surface=source_surface,
            reason=reason,
            before_payload={"status": initial_status.value},
            after_payload={"status": ReviewStatus.REJECTED.value},
            trace_id=trace_id,
            audit_payload={
                "summary": f"{actor.full_name} rejected recommendation {recommendation_id}.",
            },
        )

        self._repository.update_recommendation_status(
            recommendation_id=recommendation_id,
            status=ReviewStatus.REJECTED.value,
        )

    def approve_journal(
        self,
        *,
        journal_id: UUID,
        entity_id: UUID,
        close_run_id: UUID,
        actor: ActorContext,
        reason: str | None,
        trace_id: str | None,
        source_surface: AuditSourceSurface = AuditSourceSurface.DESKTOP,
    ) -> JournalActionResult:
        """Approve a pending or draft journal entry.

        Args:
            journal_id: The journal entry to approve.
            entity_id: Entity workspace.
            close_run_id: Close run under processing.
            actor: Authenticated actor context.
            reason: Optional reviewer note.
            trace_id: Current trace ID.
            source_surface: Surface that triggered the approval.

        Returns:
            JournalActionResult with the action result.
        """
        result = self._repository.get_journal_entry(journal_id=journal_id)
        if result is None:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.JOURNAL_NOT_FOUND,
                message=f"Journal entry {journal_id} not found.",
            )
        journal = result.entry

        if journal.status not in {
            ReviewStatus.DRAFT.value,
            ReviewStatus.PENDING_REVIEW.value,
        }:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.APPROVAL_NOT_ALLOWED,
                message=(
                    f"Journal status is '{journal.status}' and cannot be approved. "
                    f"Only draft or pending_review journals can be approved."
                ),
            )

        initial_status = ReviewStatus(journal.status)
        autonomy_mode = self._resolve_autonomy_mode(journal.autonomy_mode)

        self._audit_service.record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type="journal",
            target_id=journal_id,
            action="approve",
            actor_user_id=actor.user_id,
            autonomy_mode=autonomy_mode,
            source_surface=source_surface,
            reason=reason,
            before_payload={"status": initial_status.value},
            after_payload={
                "status": ReviewStatus.APPROVED.value,
                "approved_by": str(actor.user_id),
            },
            trace_id=trace_id,
            audit_payload={
                "summary": f"{actor.full_name} approved journal {journal.journal_number}.",
            },
        )

        self._repository.update_journal_status(
            journal_id=journal_id,
            status=ReviewStatus.APPROVED.value,
            approved_by_user_id=actor.user_id,
        )

        return JournalActionResult(
            journal_id=journal_id,
            action="approve",
            initial_status=initial_status,
            final_status=ReviewStatus.APPROVED,
            autonomy_mode=autonomy_mode,
        )

    def apply_journal(
        self,
        *,
        journal_id: UUID,
        entity_id: UUID,
        close_run_id: UUID,
        actor: ActorContext,
        reason: str | None,
        trace_id: str | None,
        source_surface: AuditSourceSurface = AuditSourceSurface.DESKTOP,
    ) -> JournalActionResult:
        """Apply an approved journal entry to working accounting state.

        Args:
            journal_id: The approved journal to apply.
            entity_id: Entity workspace.
            close_run_id: Close run under processing.
            actor: Authenticated actor context.
            reason: Optional operator note.
            trace_id: Current trace ID.
            source_surface: Surface that triggered the apply.

        Returns:
            JournalActionResult with the apply result.
        """
        result = self._repository.get_journal_entry(journal_id=journal_id)
        if result is None:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.JOURNAL_NOT_FOUND,
                message=f"Journal entry {journal_id} not found.",
            )
        journal = result.entry

        if journal.status != ReviewStatus.APPROVED.value:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.APPLY_NOT_ALLOWED,
                message=(
                    f"Journal status is '{journal.status}' and cannot be applied. "
                    f"Only approved journals can be applied to working state."
                ),
            )

        initial_status = ReviewStatus(journal.status)
        autonomy_mode = self._resolve_autonomy_mode(journal.autonomy_mode)

        self._audit_service.record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type="journal",
            target_id=journal_id,
            action="apply",
            actor_user_id=actor.user_id,
            autonomy_mode=autonomy_mode,
            source_surface=source_surface,
            reason=reason,
            before_payload={"status": initial_status.value},
            after_payload={
                "status": ReviewStatus.APPLIED.value,
                "applied_by": str(actor.user_id),
                "applied_at": utc_now().isoformat(),
            },
            trace_id=trace_id,
            audit_payload={
                "summary": (
                    f"{actor.full_name} applied journal {journal.journal_number} "
                    f"to working state."
                ),
            },
        )

        self._repository.update_journal_status(
            journal_id=journal_id,
            status=ReviewStatus.APPLIED.value,
            applied_by_user_id=actor.user_id,
        )

        return JournalActionResult(
            journal_id=journal_id,
            action="apply",
            initial_status=initial_status,
            final_status=ReviewStatus.APPLIED,
            autonomy_mode=autonomy_mode,
        )

    def reject_journal(
        self,
        *,
        journal_id: UUID,
        entity_id: UUID,
        close_run_id: UUID,
        actor: ActorContext,
        reason: str,
        trace_id: str | None,
        source_surface: AuditSourceSurface = AuditSourceSurface.DESKTOP,
    ) -> JournalActionResult:
        """Reject a draft or pending journal entry.

        Args:
            journal_id: The journal to reject.
            entity_id: Entity workspace.
            close_run_id: Close run under processing.
            actor: Authenticated actor context.
            reason: Required rejection reason.
            trace_id: Current trace ID.
            source_surface: Surface that triggered the rejection.

        Returns:
            JournalActionResult with the rejection result.
        """
        result = self._repository.get_journal_entry(journal_id=journal_id)
        if result is None:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.JOURNAL_NOT_FOUND,
                message=f"Journal entry {journal_id} not found.",
            )
        journal = result.entry

        if journal.status not in {
            ReviewStatus.DRAFT.value,
            ReviewStatus.PENDING_REVIEW.value,
        }:
            raise RecommendationApplyError(
                code=RecommendationApplyErrorCode.REJECTION_NOT_ALLOWED,
                message=(
                    f"Journal status is '{journal.status}' and cannot be rejected. "
                    f"Only draft or pending_review journals can be rejected."
                ),
            )

        initial_status = ReviewStatus(journal.status)
        autonomy_mode = self._resolve_autonomy_mode(journal.autonomy_mode)

        self._audit_service.record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type="journal",
            target_id=journal_id,
            action="reject",
            actor_user_id=actor.user_id,
            autonomy_mode=autonomy_mode,
            source_surface=source_surface,
            reason=reason,
            before_payload={"status": initial_status.value},
            after_payload={"status": ReviewStatus.REJECTED.value},
            trace_id=trace_id,
            audit_payload={
                "summary": f"{actor.full_name} rejected journal {journal.journal_number}.",
            },
        )

        self._repository.update_journal_status(
            journal_id=journal_id,
            status=ReviewStatus.REJECTED.value,
        )

        return JournalActionResult(
            journal_id=journal_id,
            action="reject",
            initial_status=initial_status,
            final_status=ReviewStatus.REJECTED,
            autonomy_mode=autonomy_mode,
        )

    def _compute_autonomy_routing(
        self,
        *,
        autonomy_mode: AutonomyMode,
        confidence: float,
        risk_level: RiskLevel,
    ) -> AutonomyRoutingResult:
        """Compute where a recommendation should route based on autonomy mode and signals.

        Routing rules:
        - HUMAN_REVIEW: always route to PENDING_REVIEW
        - REDUCED_INTERRUPTION: if confidence >= 0.85 AND risk_level == LOW,
          route to APPROVED; otherwise route to PENDING_REVIEW
        """
        if autonomy_mode == AutonomyMode.HUMAN_REVIEW:
            return AutonomyRoutingResult(
                target_status=ReviewStatus.PENDING_REVIEW,
                requires_human_approval=True,
                can_apply_automatically=False,
                reason="Human review mode requires explicit approval for all recommendations.",
            )

        # REDUCED_INTERRUPTION mode
        high_confidence = confidence >= 0.85
        low_risk = risk_level == RiskLevel.LOW

        if high_confidence and low_risk:
            return AutonomyRoutingResult(
                target_status=ReviewStatus.APPROVED,
                requires_human_approval=False,
                can_apply_automatically=True,
                reason=(
                    f"Reduced interruption mode: confidence {confidence:.2f} >= 0.85 "
                    f"and risk level is {risk_level.value}. Auto-approved to working state."
                ),
            )

        return AutonomyRoutingResult(
            target_status=ReviewStatus.PENDING_REVIEW,
            requires_human_approval=True,
            can_apply_automatically=False,
            reason=(
                f"Reduced interruption mode: confidence {confidence:.2f} or risk level "
                f"{risk_level.value} requires human review before applying."
            ),
        )

    def _generate_journal_from_approved_recommendation(
        self,
        *,
        recommendation: Any,
        entity_id: UUID,
        close_run_id: UUID,
        actor: ActorContext,
        trace_id: str | None,
        source_surface: AuditSourceSurface,
    ) -> JournalDraftResult | None:
        """Generate a journal draft from an approved recommendation and persist it.

        Args:
            recommendation: The approved recommendation ORM record.
            entity_id: Entity workspace.
            close_run_id: Close run under processing.
            actor: Authenticated actor context.
            trace_id: Current trace ID.
            source_surface: Surface that triggered the generation.

        Returns:
            JournalDraftResult if a journal was generated, None if the recommendation
            payload does not support journal generation.
        """
        payload = recommendation.payload
        if not isinstance(payload, dict):
            return None

        recommendation_id = recommendation.id
        autonomy_mode = self._resolve_autonomy_mode(recommendation.autonomy_mode)

        try:
            draft_spec = build_journal_draft_from_recommendation(
                close_run_id=close_run_id,
                entity_id=entity_id,
                recommendation_id=recommendation_id,
                posting_date=utc_now().date(),
                payload=payload,
                reasoning_summary=recommendation.reasoning_summary,
                evidence_links=recommendation.evidence_links,
                rule_version=recommendation.rule_version,
                prompt_version=recommendation.prompt_version,
                schema_version=recommendation.schema_version,
            )
        except JournalDraftError:
            # Log the failure but do not block recommendation approval.
            # The journal can be created manually later.
            return None

        _ = build_journal_draft_input(spec=draft_spec)

        sequence_no = self._repository.get_next_journal_sequence_no(
            entity_id=entity_id,
            posting_date=draft_spec.posting_date,
        )
        journal_number = generate_journal_number(
            close_run_id=close_run_id,
            posting_date=draft_spec.posting_date,
            sequence_no=sequence_no,
        )

        # Determine journal status based on autonomy mode
        journal_status = ReviewStatus.PENDING_REVIEW
        if autonomy_mode == AutonomyMode.REDUCED_INTERRUPTION:
            journal_status = ReviewStatus.APPROVED

        journal_orm = self._repository.create_journal_entry(
            entity_id=entity_id,
            close_run_id=close_run_id,
            recommendation_id=recommendation_id,
            journal_number=journal_number,
            posting_date=draft_spec.posting_date,
            status=journal_status.value,
            description=draft_spec.description,
            total_debits=float(draft_spec.total_debits),
            total_credits=float(draft_spec.total_credits),
            line_count=len(draft_spec.lines),
            source_surface=source_surface,
            autonomy_mode=autonomy_mode.value,
            reasoning_summary=draft_spec.reasoning_summary,
            metadata_payload=draft_spec.metadata_payload or {},
        )

        lines_data = [
            {
                "line_no": line.line_no,
                "account_code": line.account_code,
                "line_type": line.line_type,
                "amount": float(line.amount),
                "description": line.description,
                "dimensions": line.dimensions or {},
                "reference": line.reference,
            }
            for line in draft_spec.lines
        ]
        self._repository.create_journal_lines(
            journal_entry_id=journal_orm.id,
            lines=lines_data,
        )

        # Emit audit event for journal creation
        self._audit_service.record_review_action(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type="journal",
            target_id=journal_orm.id,
            action="create",
            actor_user_id=actor.user_id,
            autonomy_mode=autonomy_mode,
            source_surface=source_surface,
            reason=None,
            before_payload=None,
            after_payload={
                "journal_number": journal_number,
                "status": journal_status.value,
                "total_debits": str(draft_spec.total_debits),
                "total_credits": str(draft_spec.total_credits),
                "line_count": len(draft_spec.lines),
            },
            trace_id=trace_id,
            audit_payload={
                "summary": (
                    f"Journal {journal_number} generated from recommendation "
                    f"{recommendation_id}."
                ),
            },
        )

        return JournalDraftResult(
            journal_id=journal_orm.id,
            journal_number=journal_number,
            status=journal_status,
            total_debits=str(draft_spec.total_debits),
            total_credits=str(draft_spec.total_credits),
            line_count=len(draft_spec.lines),
        )

    def _resolve_autonomy_mode(self, value: str | None) -> AutonomyMode:
        """Resolve a stored or null autonomy mode value safely."""
        if value is None:
            return AutonomyMode.HUMAN_REVIEW
        try:
            return AutonomyMode(value)
        except ValueError:
            return AutonomyMode.HUMAN_REVIEW


def build_before_payload(obj: Any) -> JsonObject:
    """Build a JSON-safe before/after payload from an ORM record or Pydantic model."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    payload: dict[str, Any] = {}
    for attr in ("id", "status", "journal_number", "description", "total_debits", "total_credits"):
        if hasattr(obj, attr):
            val = getattr(obj, attr)
            payload[attr] = str(val) if val is not None else None
    return payload


__all__ = [
    "ActorContext",
    "AutonomyRoutingResult",
    "JournalActionResult",
    "RecommendationApplyError",
    "RecommendationApplyErrorCode",
    "RecommendationApplyResult",
    "RecommendationApplyService",
    "build_before_payload",
]
