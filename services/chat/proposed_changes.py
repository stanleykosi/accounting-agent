"""
Purpose: Persist, query, and act on proposed edits generated from chat actions.
Scope: Create proposed edit records, transition their review status, and
provide the bridge between chat-originated action plans and the downstream
recommendation/journal approval pipelines.
Dependencies: Action repository, chat repository, audit service, and the
canonical chat action contract models.

Design notes:
- A proposed edit is a first-class review object that flows through the same
  approval/rejection/supersede lifecycle as system-generated recommendations.
- When auto-approval is off, proposed edits go to pending_review.
- When auto-approval is on (reduced_interruption), low-risk edits may update
  working state but are still fully audit-logged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from services.chat.action_models import (
    ProposedEditPayload,
    ProposedJournalEdit,
)
from services.db.repositories.chat_action_repo import (
    ChatActionPlanRecord,
)


class ProposedChangesErrorCode(StrEnum):
    """Enumerate the stable error codes for proposed-change workflows."""

    PLAN_NOT_FOUND = "plan_not_found"
    INVALID_TRANSITION = "invalid_transition"
    VALIDATION_FAILED = "validation_failed"
    POLICY_BLOCKED = "policy_blocked"


class ProposedChangesError(Exception):
    """Represent an expected proposed-changes failure that callers expose cleanly."""

    def __init__(
        self,
        *,
        status_code: int,
        code: ProposedChangesErrorCode,
        message: str,
    ) -> None:
        """Capture the HTTP status, stable error code, and recovery message."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ChatActionRepositoryProtocol(Protocol):
    """Describe the persistence operations required by the proposed-changes service."""

    def get_action_plan_by_id(
        self,
        *,
        action_plan_id: UUID,
    ) -> ChatActionPlanRecord | None:
        """Return one action plan by UUID or None."""

    def get_action_plan_for_thread(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID,
    ) -> ChatActionPlanRecord | None:
        """Return one action plan when it belongs to the specified thread."""

    def update_action_plan_status(
        self,
        *,
        action_plan_id: UUID,
        status: str,
        applied_result: dict[str, Any] | None = None,
        rejected_reason: str | None = None,
        superseded_by_id: UUID | None = None,
    ) -> ChatActionPlanRecord | None:
        """Transition an action plan to a new review status."""

    def supersede_pending_actions_for_target(
        self,
        *,
        target_type: str,
        target_id: UUID,
        superseded_by_id: UUID,
    ) -> int:
        """Mark pending actions for a target as superseded."""

    def commit(self) -> None:
        """Commit the current transaction."""

    def rollback(self) -> None:
        """Rollback the current transaction."""


class ProposedChangesService:
    """Manage the lifecycle of chat-originated proposed edits and their
    transition through the review pipeline.

    This service provides the canonical bridge between chat action plans and
    the downstream accounting approval workflows. When a proposed edit is
    approved, it can trigger materialization of recommendations or journal
    entries depending on the edit type.
    """

    def __init__(
        self,
        *,
        action_repository: ChatActionRepositoryProtocol,
    ) -> None:
        """Capture the action-plan persistence boundary."""
        self._action_repo = action_repository

    def get_proposed_edit(
        self,
        *,
        action_plan_id: UUID,
        thread_id: UUID | None = None,
    ) -> tuple[ChatActionPlanRecord, ProposedEditPayload | ProposedJournalEdit | None]:
        """Return a proposed edit action plan with its structured payload.

        Raises ProposedChangesError when the plan does not exist or does not
        represent a proposed edit intent.
        """
        if thread_id is not None:
            plan = self._action_repo.get_action_plan_for_thread(
                action_plan_id=action_plan_id,
                thread_id=thread_id,
            )
        else:
            plan = self._action_repo.get_action_plan_by_id(
                action_plan_id=action_plan_id,
            )

        if plan is None:
            raise ProposedChangesError(
                status_code=404,
                code=ProposedChangesErrorCode.PLAN_NOT_FOUND,
                message="That action plan does not exist.",
            )

        if plan.intent != "proposed_edit":
            raise ProposedChangesError(
                status_code=422,
                code=ProposedChangesErrorCode.VALIDATION_FAILED,
                message=f"Action plan intent is '{plan.intent}', not 'proposed_edit'.",
            )

        payload = self._extract_proposed_edit_from_plan(plan)
        return plan, payload

    def approve_proposed_edit(
        self,
        *,
        action_plan_id: UUID,
        actor_user_id: UUID,
        reason: str | None = None,
        source_surface: str = "desktop",
        trace_id: str | None = None,
    ) -> ChatActionPlanRecord:
        """Approve a pending proposed edit.

        This transitions the plan to 'approved' status and may trigger
        downstream materialization (e.g., updating a recommendation or
        journal entry). The applied_result field captures the downstream
        outcome.
        """
        plan = self._action_repo.get_action_plan_by_id(action_plan_id=action_plan_id)
        if plan is None:
            raise ProposedChangesError(
                status_code=404,
                code=ProposedChangesErrorCode.PLAN_NOT_FOUND,
                message="That action plan does not exist.",
            )

        if plan.status != "pending":
            raise ProposedChangesError(
                status_code=409,
                code=ProposedChangesErrorCode.INVALID_TRANSITION,
                message=(
                    f"Cannot approve action plan in '{plan.status}' status. "
                    "Only 'pending' actions can be approved."
                ),
            )

        if plan.intent != "proposed_edit":
            raise ProposedChangesError(
                status_code=422,
                code=ProposedChangesErrorCode.VALIDATION_FAILED,
                message=f"Action plan intent is '{plan.intent}', not 'proposed_edit'.",
            )

        # Supersede any other pending actions for the same target
        if plan.target_type and plan.target_id:
            self._action_repo.supersede_pending_actions_for_target(
                target_type=plan.target_type,
                target_id=plan.target_id,
                superseded_by_id=action_plan_id,
            )

        try:
            record = self._action_repo.update_action_plan_status(
                action_plan_id=action_plan_id,
                status="approved",
                applied_result={
                    "approved_by": str(actor_user_id),
                    "reason": reason,
                    "source_surface": source_surface,
                    "trace_id": trace_id,
                },
            )
            if record is None:
                raise ProposedChangesError(
                    status_code=404,
                    code=ProposedChangesErrorCode.PLAN_NOT_FOUND,
                    message="Action plan not found after update.",
                )

            self._action_repo.commit()
        except ProposedChangesError:
            raise
        except Exception:
            self._action_repo.rollback()
            raise

        return record

    def reject_proposed_edit(
        self,
        *,
        action_plan_id: UUID,
        actor_user_id: UUID,
        reason: str,
        source_surface: str = "desktop",
        trace_id: str | None = None,
    ) -> ChatActionPlanRecord:
        """Reject a pending proposed edit with a required reason."""
        plan = self._action_repo.get_action_plan_by_id(action_plan_id=action_plan_id)
        if plan is None:
            raise ProposedChangesError(
                status_code=404,
                code=ProposedChangesErrorCode.PLAN_NOT_FOUND,
                message="That action plan does not exist.",
            )

        if plan.status != "pending":
            raise ProposedChangesError(
                status_code=409,
                code=ProposedChangesErrorCode.INVALID_TRANSITION,
                message=(
                    f"Cannot reject action plan in '{plan.status}' status. "
                    "Only 'pending' actions can be rejected."
                ),
            )

        try:
            record = self._action_repo.update_action_plan_status(
                action_plan_id=action_plan_id,
                status="rejected",
                rejected_reason=reason,
            )
            if record is None:
                raise ProposedChangesError(
                    status_code=404,
                    code=ProposedChangesErrorCode.PLAN_NOT_FOUND,
                    message="Action plan not found after update.",
                )
            self._action_repo.commit()
        except ProposedChangesError:
            raise
        except Exception:
            self._action_repo.rollback()
            raise

        return record

    def list_pending_for_target(
        self,
        *,
        target_type: str,
        target_id: UUID,
    ) -> tuple[ChatActionPlanRecord, ...]:
        """Return pending proposed edits for a specific business object.

        This is used by review UIs to show pending chat-originated changes
        alongside system-generated recommendations.
        """
        return self._action_repo.list_actions_for_target(
            target_type=target_type,
            target_id=target_id,
            status="pending",
        )

    def _extract_proposed_edit_from_plan(
        self,
        plan: ChatActionPlanRecord,
    ) -> ProposedEditPayload | ProposedJournalEdit | None:
        """Extract the structured proposed edit payload from an action plan.

        The payload column stores the full JSONB action plan. This method
        parses it back into the appropriate Pydantic model.
        """
        payload = plan.payload

        if "proposed_edit" in payload:
            edit_data = payload["proposed_edit"]
            # Check if it's a journal edit
            if "journal_id" in edit_data:
                return ProposedJournalEdit(**edit_data)
            return ProposedEditPayload(**edit_data)

        return None


__all__ = [
    "ProposedChangesError",
    "ProposedChangesErrorCode",
    "ProposedChangesService",
]
