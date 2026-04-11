"""
Purpose: Provide worker-side helpers for emitting audit events from Celery tasks.
Scope: Open a canonical DB session, resolve worker trace metadata, emit immutable audit rows,
and commit or rollback the unit of work explicitly.
Dependencies: services/audit/service.py, shared DB session helpers, and observability context.
"""

from __future__ import annotations

from uuid import UUID

from services.audit.service import AuditEventReceipt, AuditService, ReviewActionReceipt
from services.common.enums import AutonomyMode
from services.common.types import JsonObject
from services.db.models.audit import AuditSourceSurface
from services.db.session import get_session_factory
from services.observability.context import current_trace_metadata


class WorkerAuditHooks:
    """Emit canonical audit rows from worker tasks with explicit transaction handling."""

    def emit_event(
        self,
        *,
        entity_id: UUID,
        event_type: str,
        payload: JsonObject,
        close_run_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> AuditEventReceipt:
        """Persist one worker audit event and commit it immediately."""

        resolved_trace_id = trace_id or current_trace_metadata().trace_id
        with get_session_factory()() as db_session:
            audit_service = AuditService(db_session=db_session)
            try:
                receipt = audit_service.emit_audit_event(
                    entity_id=entity_id,
                    close_run_id=close_run_id,
                    event_type=event_type,
                    actor_user_id=actor_user_id,
                    source_surface=AuditSourceSurface.WORKER,
                    payload=payload,
                    trace_id=resolved_trace_id,
                )
                db_session.commit()
                return receipt
            except Exception:
                db_session.rollback()
                raise

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
        reason: str | None,
        before_payload: JsonObject | None,
        after_payload: JsonObject | None,
        audit_payload: JsonObject | None = None,
        trace_id: str | None = None,
    ) -> ReviewActionReceipt:
        """Persist one worker-originated review decision and linked audit event."""

        resolved_trace_id = trace_id or current_trace_metadata().trace_id
        with get_session_factory()() as db_session:
            audit_service = AuditService(db_session=db_session)
            try:
                receipt = audit_service.record_review_action(
                    entity_id=entity_id,
                    close_run_id=close_run_id,
                    target_type=target_type,
                    target_id=target_id,
                    action=action,
                    actor_user_id=actor_user_id,
                    autonomy_mode=autonomy_mode,
                    source_surface=AuditSourceSurface.WORKER,
                    reason=reason,
                    before_payload=before_payload,
                    after_payload=after_payload,
                    trace_id=resolved_trace_id,
                    audit_payload=audit_payload,
                )
                db_session.commit()
                return receipt
            except Exception:
                db_session.rollback()
                raise


worker_audit_hooks = WorkerAuditHooks()

__all__ = ["WorkerAuditHooks", "worker_audit_hooks"]

