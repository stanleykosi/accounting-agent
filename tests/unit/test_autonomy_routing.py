"""
Purpose: Verify autonomy-mode routing behavior for recommendations and journal entries.
Scope: Human review mode always routes to pending_review; reduced interruption mode
auto-approves low-risk/high-confidence items but still routes others to review. Also
tests approval/rejection/apply state transitions and audit-record emission.
Dependencies: pytest, canonical enums, recommendation apply service, and mock objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from services.accounting.recommendation_apply import (
    ActorContext,
    RecommendationApplyError,
    RecommendationApplyErrorCode,
    RecommendationApplyService,
)
from services.common.enums import AutonomyMode, ReviewStatus, RiskLevel
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.recommendation_journal_repo import (
    JournalEntryRecord,
    JournalLineRecord,
    JournalWithLinesResult,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MockRecommendation:
    id: UUID
    close_run_id: UUID
    document_id: UUID | None
    status: str
    payload: dict[str, Any]
    confidence: float
    reasoning_summary: str
    evidence_links: list[dict[str, Any]]
    prompt_version: str
    rule_version: str
    schema_version: str
    autonomy_mode: str | None = None
    recommendation_type: str = "gl_coding"
    created_by_system: bool = True
    superseded_by_id: UUID | None = None


@dataclass(frozen=True)
class _MockJournalEntry:
    id: UUID
    entity_id: UUID
    close_run_id: UUID
    recommendation_id: UUID | None
    journal_number: str
    posting_date: Any
    status: str
    description: str
    total_debits: float
    total_credits: float
    line_count: int
    source_surface: str
    autonomy_mode: str | None
    reasoning_summary: str | None
    metadata_payload: dict[str, Any]
    approved_by_user_id: UUID | None = None
    applied_by_user_id: UUID | None = None
    superseded_by_id: UUID | None = None


@dataclass
class _MockAuditRecord:
    entity_id: UUID
    close_run_id: UUID
    target_type: str
    target_id: UUID
    action: str
    actor_user_id: UUID
    autonomy_mode: str
    reason: str | None
    before_payload: dict | None
    after_payload: dict | None
    trace_id: str | None


class _MockRepository:
    """Minimal in-memory double for the recommendation/journal repository."""

    def __init__(self) -> None:
        self.recommendations: dict[UUID, _MockRecommendation] = {}
        self.journals: dict[UUID, _MockJournalEntry] = {}
        self.journal_sequence: int = 0
        self.audit_records: list[_MockAuditRecord] = []

    def get_recommendation(self, *, recommendation_id: UUID) -> _MockRecommendation | None:
        return self.recommendations.get(recommendation_id)

    def update_recommendation_status(
        self, *, recommendation_id: UUID, status: str, superseded_by_id: UUID | None = None
    ) -> Any:
        rec = self.recommendations[recommendation_id]
        self.recommendations[recommendation_id] = _MockRecommendation(
            **{**rec.__dict__, "status": status, "superseded_by_id": superseded_by_id}
        )
        return self.recommendations[recommendation_id]

    def get_journal_entry(
        self, *, journal_id: UUID
    ) -> JournalWithLinesResult | None:
        entry = self.journals.get(journal_id)
        if entry is None:
            return None
        record = JournalEntryRecord(
            id=entry.id,
            entity_id=entry.entity_id,
            close_run_id=entry.close_run_id,
            recommendation_id=entry.recommendation_id,
            journal_number=entry.journal_number,
            posting_date=entry.posting_date or uuid4(),
            status=entry.status,
            description=entry.description,
            total_debits=entry.total_debits,
            total_credits=entry.total_credits,
            line_count=entry.line_count,
            source_surface=entry.source_surface,
            autonomy_mode=entry.autonomy_mode,
            reasoning_summary=entry.reasoning_summary,
            metadata_payload=entry.metadata_payload,
            approved_by_user_id=entry.approved_by_user_id,
            applied_by_user_id=entry.applied_by_user_id,
            superseded_by_id=entry.superseded_by_id,
            created_at=None,
            updated_at=None,
        )
        return JournalWithLinesResult(entry=record, lines=())

    def create_journal_entry(self, **kwargs: Any) -> _MockJournalEntry:
        entry = _MockJournalEntry(
            id=uuid4(),
            entity_id=kwargs["entity_id"],
            close_run_id=kwargs["close_run_id"],
            recommendation_id=kwargs.get("recommendation_id"),
            journal_number=kwargs["journal_number"],
            posting_date=kwargs["posting_date"],
            status=kwargs["status"],
            description=kwargs["description"],
            total_debits=kwargs["total_debits"],
            total_credits=kwargs["total_credits"],
            line_count=kwargs["line_count"],
            source_surface=kwargs["source_surface"],
            autonomy_mode=kwargs.get("autonomy_mode"),
            reasoning_summary=kwargs.get("reasoning_summary"),
            metadata_payload=kwargs.get("metadata_payload", {}),
        )
        self.journals[entry.id] = entry
        return entry

    def create_journal_lines(self, *, journal_entry_id: UUID, lines: list[dict[str, Any]]) -> int:
        return len(lines)

    def update_journal_status(
        self,
        *,
        journal_id: UUID,
        status: str,
        approved_by_user_id: UUID | None = None,
        applied_by_user_id: UUID | None = None,
        superseded_by_id: UUID | None = None,
    ) -> Any:
        entry = self.journals[journal_id]
        self.journals[journal_id] = _MockJournalEntry(
            **{
                **entry.__dict__,
                "status": status,
                "approved_by_user_id": (
                    approved_by_user_id or entry.approved_by_user_id
                ),
                "applied_by_user_id": (
                    applied_by_user_id or entry.applied_by_user_id
                ),
                "superseded_by_id": superseded_by_id or entry.superseded_by_id,
            }
        )
        return self.journals[journal_id]

    def get_next_journal_sequence_no(self, *, entity_id: UUID, posting_date: Any) -> int:
        self.journal_sequence += 1
        return self.journal_sequence


class _MockAuditService:
    """Minimal in-memory double for the audit service."""

    def __init__(self) -> None:
        self.records: list[_MockAuditRecord] = []

    def record_review_action(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID,
        target_type: str,
        target_id: UUID,
        action: str,
        actor_user_id: UUID,
        autonomy_mode: Any,
        source_surface: AuditSourceSurface,
        reason: str | None,
        before_payload: dict | None,
        after_payload: dict | None,
        trace_id: str | None,
        audit_payload: dict | None = None,
    ) -> Any:
        record = _MockAuditRecord(
            entity_id=entity_id,
            close_run_id=close_run_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            actor_user_id=actor_user_id,
            autonomy_mode=(
                autonomy_mode.value
                if hasattr(autonomy_mode, "value")
                else str(autonomy_mode)
            ),
            reason=reason,
            before_payload=before_payload,
            after_payload=after_payload,
            trace_id=trace_id,
        )
        self.records.append(record)
        return record


def _make_actor() -> ActorContext:
    return ActorContext(
        user_id=uuid4(),
        full_name="Test Accountant",
        email="test@example.com",
    )


def _make_recommendation(
    *,
    status: str = ReviewStatus.DRAFT.value,
    payload: dict[str, Any] | None = None,
    autonomy_mode: str | None = None,
) -> _MockRecommendation:
    return _MockRecommendation(
        id=uuid4(),
        close_run_id=uuid4(),
        document_id=uuid4(),
        status=status,
        payload=payload or {"account_code": "5000", "amount": "100.00"},
        confidence=0.9,
        reasoning_summary="Test recommendation",
        evidence_links=[],
        prompt_version="1.0.0",
        rule_version="1.0.0",
        schema_version="1.0.0",
        autonomy_mode=autonomy_mode,
    )


class TestAutonomyRouting:
    """Verify the autonomy routing computation."""

    def _make_service(
        self,
    ) -> tuple[RecommendationApplyService, _MockRepository, _MockAuditService]:
        repo = _MockRepository()
        audit = _MockAuditService()
        service = RecommendationApplyService(
            repository=repo,
            audit_service=audit,
        )
        return service, repo, audit

    def test_human_review_always_routes_to_pending_review(self) -> None:
        """In human review mode, all recommendations route to PENDING_REVIEW."""
        service, repo, _audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.DRAFT.value)
        repo.recommendations[rec.id] = rec

        result = service.route_recommendation_to_review(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            confidence=0.95,
            risk_level=RiskLevel.LOW,
            actor=_make_actor(),
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.PENDING_REVIEW
        assert result.autonomy_routing.requires_human_approval is True
        assert result.autonomy_routing.can_apply_automatically is False

    def test_human_review_routes_low_risk_low_confidence_to_pending(self) -> None:
        """Even low-risk low-confidence items require human review."""
        service, repo, _audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.DRAFT.value)
        repo.recommendations[rec.id] = rec

        result = service.route_recommendation_to_review(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            confidence=0.5,
            risk_level=RiskLevel.LOW,
            actor=_make_actor(),
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.PENDING_REVIEW

    def test_reduced_interruption_auto_approves_high_confidence_low_risk(self) -> None:
        """Reduced interruption auto-approves high-confidence low-risk items."""
        service, repo, _audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.DRAFT.value)
        repo.recommendations[rec.id] = rec

        result = service.route_recommendation_to_review(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.90,
            risk_level=RiskLevel.LOW,
            actor=_make_actor(),
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.APPROVED
        assert result.autonomy_routing.requires_human_approval is False
        assert result.autonomy_routing.can_apply_automatically is True

    def test_reduced_interruption_routes_high_risk_to_pending(self) -> None:
        """Even high-confidence items route to review when risk is high."""
        service, repo, _audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.DRAFT.value)
        repo.recommendations[rec.id] = rec

        result = service.route_recommendation_to_review(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.95,
            risk_level=RiskLevel.HIGH,
            actor=_make_actor(),
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.PENDING_REVIEW
        assert result.autonomy_routing.requires_human_approval is True

    def test_reduced_interruption_routes_low_confidence_to_pending(self) -> None:
        """Low-confidence items route to review even in reduced interruption mode."""
        service, repo, _audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.DRAFT.value)
        repo.recommendations[rec.id] = rec

        result = service.route_recommendation_to_review(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.60,
            risk_level=RiskLevel.LOW,
            actor=_make_actor(),
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.PENDING_REVIEW

    def test_audit_record_is_emitted_on_routing(self) -> None:
        """Routing must emit an audit record."""
        service, repo, audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.DRAFT.value)
        repo.recommendations[rec.id] = rec

        service.route_recommendation_to_review(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            confidence=0.80,
            risk_level=RiskLevel.MEDIUM,
            actor=_make_actor(),
            trace_id="test-trace",
        )

        assert len(audit.records) >= 1
        route_record = audit.records[0]
        assert route_record.action == "route"
        assert route_record.autonomy_mode == AutonomyMode.HUMAN_REVIEW.value


class TestRecommendationApprovalAndRejection:
    """Verify manual approval and rejection of recommendations."""

    def _make_service(
        self,
    ) -> tuple[RecommendationApplyService, _MockRepository, _MockAuditService]:
        repo = _MockRepository()
        audit = _MockAuditService()
        service = RecommendationApplyService(
            repository=repo,
            audit_service=audit,
        )
        return service, repo, audit

    def test_approve_pending_recommendation(self) -> None:
        """A pending recommendation can be approved by a human."""
        service, repo, audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.PENDING_REVIEW.value)
        repo.recommendations[rec.id] = rec

        result = service.approve_recommendation(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            actor=_make_actor(),
            reason="Looks correct",
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.APPROVED
        assert result.initial_status == ReviewStatus.PENDING_REVIEW
        assert len(audit.records) >= 1
        assert audit.records[0].action == "approve"

    def test_approve_draft_recommendation(self) -> None:
        """A draft recommendation can also be approved."""
        service, repo, _audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.DRAFT.value)
        repo.recommendations[rec.id] = rec

        result = service.approve_recommendation(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            actor=_make_actor(),
            reason="Approved from draft",
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.APPROVED

    def test_approve_already_approved_raises_error(self) -> None:
        """An already-approved recommendation cannot be re-approved."""
        service, repo, _audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.APPROVED.value)
        repo.recommendations[rec.id] = rec

        with pytest.raises(RecommendationApplyError) as exc_info:
            service.approve_recommendation(
                recommendation_id=rec.id,
                entity_id=uuid4(),
                close_run_id=rec.close_run_id,
                actor=_make_actor(),
                reason=None,
                trace_id="test-trace",
            )

        assert exc_info.value.code == RecommendationApplyErrorCode.APPROVAL_NOT_ALLOWED

    def test_reject_pending_recommendation(self) -> None:
        """A pending recommendation can be rejected with a reason."""
        service, repo, audit = self._make_service()
        rec = _make_recommendation(status=ReviewStatus.PENDING_REVIEW.value)
        repo.recommendations[rec.id] = rec

        service.reject_recommendation(
            recommendation_id=rec.id,
            entity_id=uuid4(),
            close_run_id=rec.close_run_id,
            actor=_make_actor(),
            reason="Incorrect account code",
            trace_id="test-trace",
        )

        assert repo.recommendations[rec.id].status == ReviewStatus.REJECTED.value
        assert len(audit.records) >= 1
        assert audit.records[0].action == "reject"

    def test_recommendation_not_found_raises_error(self) -> None:
        """A non-existent recommendation raises a not-found error."""
        service, _repo, _audit = self._make_service()

        with pytest.raises(RecommendationApplyError) as exc_info:
            service.approve_recommendation(
                recommendation_id=uuid4(),
                entity_id=uuid4(),
                close_run_id=uuid4(),
                actor=_make_actor(),
                reason=None,
                trace_id="test-trace",
            )

        assert exc_info.value.code == RecommendationApplyErrorCode.RECOMMENDATION_NOT_FOUND


class TestJournalApprovalRejectionAndApply:
    """Verify journal entry approval, rejection, and application."""

    def _make_service(
        self,
    ) -> tuple[RecommendationApplyService, _MockRepository, _MockAuditService]:
        repo = _MockRepository()
        audit = _MockAuditService()
        service = RecommendationApplyService(
            repository=repo,
            audit_service=audit,
        )
        return service, repo, audit

    def _make_draft_journal(self) -> _MockJournalEntry:
        entry = _MockJournalEntry(
            id=uuid4(),
            entity_id=uuid4(),
            close_run_id=uuid4(),
            recommendation_id=uuid4(),
            journal_number="JE-2026-00001",
            posting_date=None,
            status=ReviewStatus.DRAFT.value,
            description="Draft journal",
            total_debits=1000.0,
            total_credits=1000.0,
            line_count=2,
            source_surface="system",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW.value,
            reasoning_summary="Test",
            metadata_payload={},
        )
        return entry

    def test_approve_draft_journal(self) -> None:
        """A draft journal can be approved."""
        service, repo, audit = self._make_service()
        entry = self._make_draft_journal()
        repo.journals[entry.id] = entry

        result = service.approve_journal(
            journal_id=entry.id,
            entity_id=entry.entity_id,
            close_run_id=entry.close_run_id,
            actor=_make_actor(),
            reason="Verified",
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.APPROVED
        assert result.action == "approve"
        assert len(audit.records) >= 1

    def test_apply_approved_journal(self) -> None:
        """An approved journal can be applied to working state."""
        service, repo, _audit = self._make_service()
        entry = self._make_draft_journal()
        entry = _MockJournalEntry(**{**entry.__dict__, "status": ReviewStatus.APPROVED.value})
        repo.journals[entry.id] = entry

        result = service.apply_journal(
            journal_id=entry.id,
            entity_id=entry.entity_id,
            close_run_id=entry.close_run_id,
            actor=_make_actor(),
            reason="Applied to working state",
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.APPLIED
        assert result.action == "apply"

    def test_apply_draft_journal_raises_error(self) -> None:
        """A draft journal cannot be applied directly."""
        service, repo, _audit = self._make_service()
        entry = self._make_draft_journal()
        repo.journals[entry.id] = entry

        with pytest.raises(RecommendationApplyError) as exc_info:
            service.apply_journal(
                journal_id=entry.id,
                entity_id=entry.entity_id,
                close_run_id=entry.close_run_id,
                actor=_make_actor(),
                reason=None,
                trace_id="test-trace",
            )

        assert exc_info.value.code == RecommendationApplyErrorCode.APPLY_NOT_ALLOWED

    def test_reject_draft_journal(self) -> None:
        """A draft journal can be rejected."""
        service, repo, _audit = self._make_service()
        entry = self._make_draft_journal()
        repo.journals[entry.id] = entry

        result = service.reject_journal(
            journal_id=entry.id,
            entity_id=entry.entity_id,
            close_run_id=entry.close_run_id,
            actor=_make_actor(),
            reason="Incorrect amounts",
            trace_id="test-trace",
        )

        assert result.final_status == ReviewStatus.REJECTED
        assert result.action == "reject"

    def test_reject_applied_journal_raises_error(self) -> None:
        """An already-applied journal cannot be rejected."""
        service, repo, _audit = self._make_service()
        entry = self._make_draft_journal()
        entry = _MockJournalEntry(**{**entry.__dict__, "status": ReviewStatus.APPLIED.value})
        repo.journals[entry.id] = entry

        with pytest.raises(RecommendationApplyError) as exc_info:
            service.reject_journal(
                journal_id=entry.id,
                entity_id=entry.entity_id,
                close_run_id=entry.close_run_id,
                actor=_make_actor(),
                reason="Should fail",
                trace_id="test-trace",
            )

        assert exc_info.value.code == RecommendationApplyErrorCode.REJECTION_NOT_ALLOWED

    def test_journal_not_found_raises_error(self) -> None:
        """A non-existent journal raises a not-found error."""
        service, _repo, _audit = self._make_service()

        with pytest.raises(RecommendationApplyError) as exc_info:
            service.approve_journal(
                journal_id=uuid4(),
                entity_id=uuid4(),
                close_run_id=uuid4(),
                actor=_make_actor(),
                reason=None,
                trace_id="test-trace",
            )

        assert exc_info.value.code == RecommendationApplyErrorCode.JOURNAL_NOT_FOUND


class TestComputeAutonomyRouting:
    """Directly test the _compute_autonomy_routing private method."""

    def _make_service(self) -> RecommendationApplyService:
        repo = _MockRepository()
        audit = _MockAuditService()
        return RecommendationApplyService(repository=repo, audit_service=audit)

    def test_human_review_all_params(self) -> None:
        """Human review mode always returns pending_review regardless of signals."""
        service = self._make_service()
        for confidence in [0.1, 0.5, 0.85, 0.99]:
            for risk in RiskLevel:
                result = service._compute_autonomy_routing(
                    autonomy_mode=AutonomyMode.HUMAN_REVIEW,
                    confidence=confidence,
                    risk_level=risk,
                )
                assert result.target_status == ReviewStatus.PENDING_REVIEW
                assert result.requires_human_approval is True
                assert result.can_apply_automatically is False

    def test_reduced_interruption_auto_approve_threshold(self) -> None:
        """Reduced interruption auto-approves at confidence >= 0.85 and LOW risk."""
        service = self._make_service()

        # At threshold
        result = service._compute_autonomy_routing(
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.85,
            risk_level=RiskLevel.LOW,
        )
        assert result.target_status == ReviewStatus.APPROVED

        # Above threshold
        result = service._compute_autonomy_routing(
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.99,
            risk_level=RiskLevel.LOW,
        )
        assert result.target_status == ReviewStatus.APPROVED

    def test_reduced_interruption_below_threshold(self) -> None:
        """Below 0.85 confidence routes to pending review."""
        service = self._make_service()
        result = service._compute_autonomy_routing(
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.84,
            risk_level=RiskLevel.LOW,
        )
        assert result.target_status == ReviewStatus.PENDING_REVIEW

    def test_reduced_interruption_medium_risk_routes_to_review(self) -> None:
        """Medium risk routes to review even with high confidence."""
        service = self._make_service()
        result = service._compute_autonomy_routing(
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.95,
            risk_level=RiskLevel.MEDIUM,
        )
        assert result.target_status == ReviewStatus.PENDING_REVIEW

    def test_reduced_interruption_high_risk_routes_to_review(self) -> None:
        """High risk always routes to review."""
        service = self._make_service()
        result = service._compute_autonomy_routing(
            autonomy_mode=AutonomyMode.REDUCED_INTERRUPTION,
            confidence=0.99,
            risk_level=RiskLevel.HIGH,
        )
        assert result.target_status == ReviewStatus.PENDING_REVIEW
