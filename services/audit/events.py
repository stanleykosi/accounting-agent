"""
Purpose: Define the canonical audit-event vocabulary for privileged accounting workflows.
Scope: Stable event type strings for close-run lifecycle, review decisions, worker execution,
integration actions, and document/report/reconciliation surfaces.
Dependencies: Python's enum module only so this module stays safe for low-level imports.
"""

from __future__ import annotations

from enum import StrEnum


class AuditEventType(StrEnum):
    """Enumerate stable audit event names persisted in audit_events.event_type."""

    CLOSE_RUN_CREATED = "close_run.created"
    CLOSE_RUN_PHASE_TRANSITIONED = "close_run.phase_transitioned"
    CLOSE_RUN_APPROVED = "close_run.approved"
    CLOSE_RUN_ARCHIVED = "close_run.archived"
    CLOSE_RUN_REOPENED = "close_run.reopened"
    REVIEW_ACTION_RECORDED = "review_action.recorded"
    WORKER_JOB_EVENT = "worker.job_event"
    INTEGRATION_ACTION = "integration.action"


class ReviewActionType(StrEnum):
    """Enumerate stable review decision actions used by review_actions.action."""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    REQUEST_INFO = "request_info"
    OVERRIDE = "override"
    ARCHIVE = "archive"
    REOPEN = "reopen"


def review_action_event_type(action: str) -> str:
    """Return the canonical audit event type associated with a review decision action."""

    normalized_action = action.strip().lower()
    if not normalized_action:
        raise ValueError("Review action cannot be empty.")

    return f"review_action.{normalized_action}"


__all__ = ["AuditEventType", "ReviewActionType", "review_action_event_type"]
