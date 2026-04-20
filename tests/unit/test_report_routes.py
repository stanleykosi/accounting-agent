"""
Purpose: Verify report-generation routing resolves the canonical default template correctly.
Scope: Focused helper-level tests for report template resolution and bootstrap behavior.
Dependencies: Report route helper plus lightweight repository doubles.
"""

from __future__ import annotations

from uuid import uuid4

from apps.api.app.routes import reports as report_routes


class _TemplateRecord:
    def __init__(self, template_id):
        self.id = template_id


class _FakeReportRepository:
    def __init__(self) -> None:
        self.entity_template = None
        self.global_template = None
        self.ensure_calls = 0

    def get_active_template_for_entity(self, *, entity_id):
        del entity_id
        return self.entity_template

    def ensure_active_global_template(self):
        self.ensure_calls += 1
        if self.global_template is None:
            self.global_template = _TemplateRecord(uuid4())
        return self.global_template


def test_resolve_template_prefers_entity_active_template() -> None:
    """Entity-level templates should win over the canonical global default."""

    repo = _FakeReportRepository()
    repo.entity_template = _TemplateRecord(uuid4())

    resolved_template_id = report_routes._resolve_template_id(
        repo=repo,
        entity_id=uuid4(),
        template_id=None,
    )

    assert resolved_template_id == repo.entity_template.id
    assert repo.ensure_calls == 0


def test_resolve_template_bootstraps_global_default_when_entity_has_none() -> None:
    """Report generation should auto-provision the canonical global template."""

    repo = _FakeReportRepository()

    resolved_template_id = report_routes._resolve_template_id(
        repo=repo,
        entity_id=uuid4(),
        template_id=None,
    )

    assert repo.ensure_calls == 1
    assert repo.global_template is not None
    assert resolved_template_id == repo.global_template.id
