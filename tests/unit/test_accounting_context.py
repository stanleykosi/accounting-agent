"""
Purpose: Verify accounting workspace readiness and chat-side COA resolution behavior.
Scope: Focused unit coverage over readiness messaging and fallback COA activation hooks.
Dependencies: accounting context helpers, chat action executor, and canonical entity records.
"""

from __future__ import annotations

from uuid import uuid4

from services.agents.accounting_context import _build_readiness_summary
from services.chat.action_execution import ChatActionExecutor
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.entity_repo import EntityUserRecord


def test_readiness_summary_treats_fallback_coa_as_warning_and_prompts_phase_advance() -> None:
    """Fallback COA should not block collection when approved documents are already ready."""

    readiness = _build_readiness_summary(
        close_run={
            "active_phase": "collection",
            "phase_states": [],
        },
        coa_summary={
            "is_available": True,
            "requires_operator_upload": True,
        },
        document_summary={
            "approved": 1,
        },
        recommendation_summary={},
        journal_summary={},
        reconciliation_summary={},
        schedule_summary={},
        report_summary={},
        export_summary={},
        distribution_summary={},
        pending_action_count=0,
    )

    assert readiness["status"] == "attention_required"
    assert readiness["blockers"] == []
    assert any("fallback chart of accounts" in warning.lower() for warning in readiness["warnings"])
    assert any(
        "Advance the close run to Processing" in action
        for action in readiness["next_actions"]
    )


def test_chat_executor_ensures_entity_coa_is_materialized_before_chat_reads() -> None:
    """Chat should ask the COA service for the canonical active-or-fallback workspace state."""

    actor_user = EntityUserRecord(
        id=uuid4(),
        email="ops@example.com",
        full_name="Finance Ops",
    )
    entity_id = uuid4()
    calls: list[dict[str, object]] = []

    class _CoaServiceDouble:
        def read_workspace(self, **kwargs):
            calls.append(kwargs)
            return None

    executor = ChatActionExecutor.__new__(ChatActionExecutor)
    executor._coa_service = _CoaServiceDouble()

    executor._ensure_entity_coa_available(actor_user=actor_user, entity_id=entity_id)

    assert calls == [
        {
            "actor_user": actor_user,
            "entity_id": entity_id,
            "source_surface": AuditSourceSurface.DESKTOP,
            "trace_id": None,
        }
    ]
