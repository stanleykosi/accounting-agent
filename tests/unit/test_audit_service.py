"""
Purpose: Verify the canonical audit service used for immutable review and audit event writes.
Scope: Unit coverage with an in-memory DB-session double for payload validation,
row creation, source-surface capture, autonomy mode capture, and trace correlation.
Dependencies: services/audit/service.py plus audit persistence models and canonical enums.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from services.audit.service import AuditService, AuditServiceError, AuditServiceErrorCode
from services.common.enums import AutonomyMode
from services.db.models.audit import AuditEvent, AuditSourceSurface, ReviewAction


def test_record_review_action_writes_review_and_audit_rows() -> None:
    """Ensure review decisions create both immutable review records and audit events."""

    db_session = InMemoryAuditSession()
    service = AuditService(db_session=db_session)
    entity_id = uuid4()
    close_run_id = uuid4()
    actor_user_id = uuid4()
    target_id = uuid4()

    receipt = service.record_review_action(
        entity_id=entity_id,
        close_run_id=close_run_id,
        target_type="recommendation",
        target_id=target_id,
        action="approve",
        actor_user_id=actor_user_id,
        autonomy_mode=AutonomyMode.HUMAN_REVIEW,
        source_surface=AuditSourceSurface.DESKTOP,
        reason="Evidence matched invoice total",
        before_payload={"status": "pending_review"},
        after_payload={"status": "approved"},
        trace_id="trace-123",
        audit_payload={"summary": "Recommendation approved."},
    )

    review_action = db_session.rows_by_type(ReviewAction)[0]
    audit_event = db_session.rows_by_type(AuditEvent)[0]

    assert receipt.review_action_id == review_action.id
    assert receipt.audit_event_id == audit_event.id
    assert review_action.close_run_id == close_run_id
    assert review_action.target_type == "recommendation"
    assert review_action.target_id == target_id
    assert review_action.actor_user_id == actor_user_id
    assert review_action.autonomy_mode == AutonomyMode.HUMAN_REVIEW.value
    assert review_action.before_payload == {"status": "pending_review"}
    assert review_action.after_payload == {"status": "approved"}
    assert audit_event.entity_id == entity_id
    assert audit_event.close_run_id == close_run_id
    assert audit_event.event_type == "review_action.approve"
    assert audit_event.source_surface == AuditSourceSurface.DESKTOP.value
    assert audit_event.trace_id == "trace-123"
    assert audit_event.payload["autonomy_mode"] == AutonomyMode.HUMAN_REVIEW.value
    assert audit_event.payload["summary"] == "Recommendation approved."


def test_emit_audit_event_writes_standalone_timeline_event() -> None:
    """Ensure non-review events still capture surface and trace metadata."""

    db_session = InMemoryAuditSession()
    service = AuditService(db_session=db_session)
    entity_id = uuid4()

    receipt = service.emit_audit_event(
        entity_id=entity_id,
        event_type="close_run.created",
        actor_user_id=None,
        source_surface=AuditSourceSurface.WORKER,
        payload={"summary": "Worker reconciled checkpoint."},
        trace_id="trace-worker",
    )

    audit_event = db_session.rows_by_type(AuditEvent)[0]
    assert receipt.audit_event_id == audit_event.id
    assert audit_event.entity_id == entity_id
    assert audit_event.close_run_id is None
    assert audit_event.source_surface == AuditSourceSurface.WORKER.value
    assert audit_event.trace_id == "trace-worker"


def test_override_requires_reason_and_before_after_payloads() -> None:
    """Ensure override decisions remain explainable and diff-backed."""

    service = AuditService(db_session=InMemoryAuditSession())

    with pytest.raises(AuditServiceError) as reason_error:
        service.record_review_action(
            entity_id=uuid4(),
            close_run_id=uuid4(),
            target_type="recommendation",
            target_id=uuid4(),
            action="override",
            actor_user_id=uuid4(),
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            source_surface=AuditSourceSurface.DESKTOP,
            reason=" ",
            before_payload={"account": "6000"},
            after_payload={"account": "7000"},
            trace_id="trace-override",
        )

    with pytest.raises(AuditServiceError) as payload_error:
        service.record_review_action(
            entity_id=uuid4(),
            close_run_id=uuid4(),
            target_type="recommendation",
            target_id=uuid4(),
            action="override",
            actor_user_id=uuid4(),
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            source_surface=AuditSourceSurface.DESKTOP,
            reason="Vendor-specific mapping correction",
            before_payload=None,
            after_payload={"account": "7000"},
            trace_id="trace-override",
        )

    assert reason_error.value.code is AuditServiceErrorCode.OVERRIDE_REASON_REQUIRED
    assert payload_error.value.code is AuditServiceErrorCode.REVIEW_PAYLOAD_REQUIRED


class InMemoryAuditSession:
    """Capture ORM rows added by AuditService without requiring a live PostgreSQL database."""

    def __init__(self) -> None:
        """Initialize the append-only row store used by audit-service tests."""

        self.rows: list[object] = []
        self.flush_count = 0

    def add(self, row: object) -> None:
        """Record one staged ORM row."""

        self.rows.append(row)

    def flush(self) -> None:
        """Track flush calls; SQLAlchemy normally assigns IDs before returning."""

        for row in self.rows:
            if getattr(row, "id", None) is None:
                cast(Any, row).id = uuid4()
        self.flush_count += 1

    def rows_by_type[T](self, row_type: type[T]) -> list[T]:
        """Return staged rows matching a concrete ORM model type."""

        return [row for row in self.rows if isinstance(row, row_type)]
