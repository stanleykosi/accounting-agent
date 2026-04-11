"""
Purpose: Provide the canonical immutable audit and review event emitter.
Scope: Validate and persist review_actions plus audit_events with actor, surface,
autonomy mode, before/after payloads, and trace correlation.
Dependencies: SQLAlchemy ORM sessions, audit persistence models, canonical enums,
and JSON-safe shared types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.audit.events import ReviewActionType, review_action_event_type
from services.common.enums import AutonomyMode
from services.common.types import JsonObject, JsonValue
from services.db.models.audit import AuditEvent, AuditSourceSurface, ReviewAction


class AuditSession(Protocol):
    """Describe the minimal SQLAlchemy session surface required by the audit emitter."""

    def add(self, instance: object) -> None:
        """Stage one ORM row for persistence."""

    def flush(self) -> None:
        """Flush staged ORM rows so database-generated/default fields are available."""


class AuditServiceErrorCode(StrEnum):
    """Enumerate stable error codes for audit emitter validation failures."""

    EMPTY_EVENT_TYPE = "empty_event_type"
    EMPTY_REVIEW_ACTION = "empty_review_action"
    EMPTY_TARGET_TYPE = "empty_target_type"
    OVERRIDE_REASON_REQUIRED = "override_reason_required"
    REVIEW_PAYLOAD_REQUIRED = "review_payload_required"


class AuditServiceError(ValueError):
    """Represent an expected audit-service validation failure."""

    def __init__(self, *, code: AuditServiceErrorCode, message: str) -> None:
        """Capture a stable error code and operator-facing diagnostic message."""

        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AuditEventReceipt:
    """Describe one audit event row created by the canonical emitter."""

    audit_event_id: UUID
    event_type: str
    entity_id: UUID
    close_run_id: UUID | None
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class ReviewActionReceipt:
    """Describe review and audit rows created for one review decision."""

    review_action_id: UUID
    audit_event_id: UUID
    action: str
    target_type: str
    target_id: UUID
    trace_id: str | None


class AuditService:
    """Persist immutable review decisions and cross-surface audit timeline events."""

    def __init__(self, *, db_session: AuditSession) -> None:
        """Capture the request-scoped or worker-scoped SQLAlchemy session."""

        self._db_session = db_session

    def emit_audit_event(
        self,
        *,
        entity_id: UUID,
        event_type: str,
        source_surface: AuditSourceSurface,
        payload: JsonObject,
        close_run_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> AuditEventReceipt:
        """Persist one immutable audit event and return its durable identity."""

        normalized_event_type = _normalize_required_text(
            value=event_type,
            error_code=AuditServiceErrorCode.EMPTY_EVENT_TYPE,
            field_label="Audit event type",
        )
        event = AuditEvent(
            entity_id=entity_id,
            close_run_id=close_run_id,
            event_type=normalized_event_type,
            actor_user_id=actor_user_id,
            source_surface=source_surface.value,
            payload=_copy_json_object(payload),
            trace_id=trace_id,
        )
        self._db_session.add(event)
        self._db_session.flush()
        return AuditEventReceipt(
            audit_event_id=event.id,
            event_type=event.event_type,
            entity_id=event.entity_id,
            close_run_id=event.close_run_id,
            trace_id=event.trace_id,
        )

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
    ) -> ReviewActionReceipt:
        """Persist a review action and its linked audit event within the current transaction."""

        normalized_target_type = _normalize_required_text(
            value=target_type,
            error_code=AuditServiceErrorCode.EMPTY_TARGET_TYPE,
            field_label="Review target type",
        )
        normalized_action = _normalize_required_text(
            value=action,
            error_code=AuditServiceErrorCode.EMPTY_REVIEW_ACTION,
            field_label="Review action",
        )
        _validate_review_action_payload(
            action=normalized_action,
            reason=reason,
            before_payload=before_payload,
            after_payload=after_payload,
        )

        review_action = ReviewAction(
            close_run_id=close_run_id,
            target_type=normalized_target_type,
            target_id=target_id,
            action=normalized_action,
            actor_user_id=actor_user_id,
            autonomy_mode=autonomy_mode.value,
            reason=reason.strip() if reason is not None else None,
            before_payload=_copy_optional_json_object(before_payload),
            after_payload=_copy_optional_json_object(after_payload),
        )
        self._db_session.add(review_action)
        self._db_session.flush()

        event_payload = _build_review_event_payload(
            target_type=normalized_target_type,
            target_id=target_id,
            action=normalized_action,
            autonomy_mode=autonomy_mode,
            reason=reason,
            before_payload=before_payload,
            after_payload=after_payload,
            audit_payload=audit_payload,
        )
        audit_receipt = self.emit_audit_event(
            entity_id=entity_id,
            close_run_id=close_run_id,
            event_type=review_action_event_type(normalized_action),
            actor_user_id=actor_user_id,
            source_surface=source_surface,
            payload=event_payload,
            trace_id=trace_id,
        )
        return ReviewActionReceipt(
            review_action_id=review_action.id,
            audit_event_id=audit_receipt.audit_event_id,
            action=review_action.action,
            target_type=review_action.target_type,
            target_id=review_action.target_id,
            trace_id=audit_receipt.trace_id,
        )


def _normalize_required_text(
    *,
    value: str,
    error_code: AuditServiceErrorCode,
    field_label: str,
) -> str:
    """Return a stripped required string or raise a stable audit-service error."""

    normalized = value.strip()
    if normalized:
        return normalized

    raise AuditServiceError(
        code=error_code,
        message=f"{field_label} cannot be empty.",
    )


def _validate_review_action_payload(
    *,
    action: str,
    reason: str | None,
    before_payload: JsonObject | None,
    after_payload: JsonObject | None,
) -> None:
    """Enforce review payload rules that preserve explainable override history."""

    if action == ReviewActionType.OVERRIDE.value and not (reason and reason.strip()):
        raise AuditServiceError(
            code=AuditServiceErrorCode.OVERRIDE_REASON_REQUIRED,
            message="Override review actions require an explicit reviewer reason.",
        )

    if action in {ReviewActionType.EDIT.value, ReviewActionType.OVERRIDE.value} and (
        before_payload is None or after_payload is None
    ):
        raise AuditServiceError(
            code=AuditServiceErrorCode.REVIEW_PAYLOAD_REQUIRED,
            message="Edit and override review actions require before and after payloads.",
        )


def _build_review_event_payload(
    *,
    target_type: str,
    target_id: UUID,
    action: str,
    autonomy_mode: AutonomyMode,
    reason: str | None,
    before_payload: JsonObject | None,
    after_payload: JsonObject | None,
    audit_payload: JsonObject | None,
) -> JsonObject:
    """Build a compact JSON-safe audit payload for one review decision."""

    payload: JsonObject = _copy_json_object(audit_payload) if audit_payload is not None else {}
    # Canonical decision fields win over caller context so a workflow summary cannot
    # accidentally rewrite the persisted target/action lineage.
    payload.update(
        {
            "target_type": target_type,
            "target_id": str(target_id),
            "action": action,
            "autonomy_mode": autonomy_mode.value,
            "reason": reason.strip() if reason is not None else None,
            "before_payload": _copy_optional_json_object(before_payload),
            "after_payload": _copy_optional_json_object(after_payload),
        }
    )

    return payload


def _copy_optional_json_object(value: JsonObject | None) -> JsonObject | None:
    """Return a shallow JSON-object copy while preserving null optional payloads."""

    if value is None:
        return None
    return _copy_json_object(value)


def _copy_json_object(value: JsonObject) -> JsonObject:
    """Return a shallow JSON-object copy with a concrete JsonObject type."""

    copied: dict[str, JsonValue] = {}
    for key, item_value in value.items():
        copied[str(key)] = item_value
    return copied


__all__ = [
    "AuditEventReceipt",
    "AuditService",
    "AuditServiceError",
    "AuditServiceErrorCode",
    "ReviewActionReceipt",
]
