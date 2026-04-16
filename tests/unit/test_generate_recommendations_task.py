"""
Purpose: Regression coverage for recommendation-task persistence phase guards.
Scope: Ensures stale recommendation writes are blocked in the same session that persists them.
Dependencies: Recommendation task helpers plus lightweight session doubles.
"""

from __future__ import annotations

from uuid import uuid4

from apps.worker.app.tasks import generate_recommendations as recommendation_task_module
from services.common.enums import WorkflowPhase
from services.contracts.recommendation_models import RecommendationContext


class _FakeQuery:
    def filter(self, *args, **kwargs):
        del args, kwargs
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commit_count = 0
        self.refreshed: list[object] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commit_count += 1

    def refresh(self, value: object) -> None:
        self.refreshed.append(value)

    def query(self, model):
        del model
        return _FakeQuery()


def test_persist_recommendation_checks_processing_phase_in_persistence_session(monkeypatch) -> None:
    """Recommendation persistence should validate phase with the same session that writes state."""

    fake_session = _FakeSession()
    guard_calls: list[tuple[object, object, object]] = []

    monkeypatch.setattr(
        recommendation_task_module,
        "get_session_factory",
        lambda: (lambda: fake_session),
    )
    monkeypatch.setattr(
        recommendation_task_module,
        "ensure_close_run_active_phase",
        lambda *, session, close_run_id, required_phase: guard_calls.append(
            (session, close_run_id, required_phase)
        ),
    )

    context = RecommendationContext(
        close_run_id=uuid4(),
        document_id=uuid4(),
        entity_id=uuid4(),
        period_start="2026-03-01",
        period_end="2026-03-31",
        document_type=None,
        extracted_fields={},
        line_items=[],
        coa_accounts=[],
        coa_source="uploaded",
        autonomy_mode="human_review",
        confidence_threshold=0.7,
    )

    receipt = recommendation_task_module._persist_recommendation(
        recommendation_data={
            "close_run_id": context.close_run_id,
            "document_id": context.document_id,
            "recommendation_type": "gl_coding",
            "payload": {"account_code": "6100"},
            "confidence": 0.93,
            "reasoning_summary": "Deterministic coding matched the expense account.",
            "evidence_links": [],
            "prompt_version": "test-prompt",
            "rule_version": "test-rules",
            "schema_version": "1.0.0",
        },
        routed_status="draft",
        context=context,
        actor_user_id=uuid4(),
        trace_id="trace-123",
    )

    assert receipt.status == "draft"
    assert guard_calls == [
        (fake_session, context.close_run_id, WorkflowPhase.PROCESSING)
    ]
    assert fake_session.commit_count == 1
