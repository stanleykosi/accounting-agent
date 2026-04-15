"""
Purpose: Verify focused export-service regression handling.
Scope: Ensure idempotent export generation recovers cleanly from unique-constraint races.
Dependencies: ExportService and lightweight session doubles only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

from services.common.enums import AutonomyMode
from services.db.repositories.entity_repo import EntityUserRecord
from services.exports import service as export_service_module
from services.exports.service import ExportService
from sqlalchemy.exc import IntegrityError


class _QueryDouble:
    """Provide the tiny subset of SQLAlchemy's query API this test needs."""

    def __init__(self, session) -> None:
        self._session = session

    def filter(self, *args, **kwargs):
        del args, kwargs
        return self

    def first(self):
        if self._session.query_results:
            return self._session.query_results.pop(0)
        return None


class _SessionDouble:
    """Mimic the commit/rollback/query behavior used by ExportService."""

    def __init__(self, *, query_results) -> None:
        self.query_results = list(query_results)
        self.commit_attempts = 0
        self.rollback_markers: list[str] = []

    def query(self, model):
        del model
        return _QueryDouble(self)

    def add(self, obj) -> None:
        self.added_object = obj

    def commit(self) -> None:
        self.commit_attempts += 1
        raise IntegrityError("insert into export_runs", {}, Exception("duplicate key"))

    def rollback(self) -> None:
        self.rollback_markers.append("rollback")

    def refresh(self, obj) -> None:
        raise AssertionError(f"refresh should not run when recovering existing export: {obj!r}")


def test_trigger_export_recovers_existing_export_after_unique_constraint_race(
    monkeypatch,
) -> None:
    """Duplicate export clicks should return the already-created export instead of surfacing 500."""

    recovered_export = SimpleNamespace(id=uuid4())
    db_session = _SessionDouble(query_results=[None, recovered_export])
    actor_user = EntityUserRecord(
        id=uuid4(),
        email="reviewer@example.com",
        full_name="Casey Reviewer",
    )

    class _ManifestDouble:
        def __init__(self) -> None:
            self.artifacts = ()
            self.generated_at = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)

        def model_copy(self, update):
            del update
            return self

    monkeypatch.setattr(
        export_service_module,
        "_load_report_output_records",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        export_service_module,
        "build_export_manifest",
        lambda *_args, **_kwargs: SimpleNamespace(manifest=_ManifestDouble()),
    )
    monkeypatch.setattr(
        export_service_module,
        "_build_export_detail",
        lambda **kwargs: {"export_id": str(kwargs["export_run"].id)},
    )

    service = ExportService(
        db_session=db_session,
        report_repository=SimpleNamespace(),
    )
    service._verify_close_run_access = lambda **kwargs: None
    service._require_close_run_context = lambda **kwargs: (
        SimpleNamespace(
            current_version_no=1,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
        ),
        SimpleNamespace(name="Acme Entity", autonomy_mode=AutonomyMode.HUMAN_REVIEW),
    )

    result = service.trigger_export(
        actor_user=actor_user,
        entity_id=uuid4(),
        close_run_id=uuid4(),
        request=SimpleNamespace(
            include_evidence_pack=False,
            include_audit_trail=False,
            action_qualifier="full_export",
        ),
    )

    assert result == {"export_id": str(recovered_export.id)}
    assert db_session.commit_attempts == 1
    assert db_session.rollback_markers == ["rollback"]


def test_trigger_export_changes_dedup_key_when_export_options_change(
    monkeypatch,
) -> None:
    """Different export payloads should not collapse onto the same existing export record."""

    actor_user = EntityUserRecord(
        id=uuid4(),
        email="reviewer@example.com",
        full_name="Casey Reviewer",
    )
    entity_id = uuid4()
    close_run_id = uuid4()
    captured_keys: list[str] = []

    def capture_existing_export(**kwargs):
        captured_keys.append(kwargs["idempotency_key"])
        return SimpleNamespace(
            id=uuid4(),
            idempotency_key=kwargs["idempotency_key"],
            close_run_id=kwargs["close_run_id"],
            version_no=1,
            status="completed",
            artifact_manifest=[],
            failure_reason=None,
            created_at=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(
        export_service_module,
        "_load_export_run_by_idempotency",
        capture_existing_export,
    )
    monkeypatch.setattr(
        export_service_module,
        "_build_export_detail",
        lambda **kwargs: {"idempotency_key": kwargs["export_run"].idempotency_key},
    )

    service = ExportService(
        db_session=SimpleNamespace(),
        report_repository=SimpleNamespace(),
    )
    service._verify_close_run_access = lambda **kwargs: None
    service._require_close_run_context = lambda **kwargs: (
        SimpleNamespace(
            current_version_no=1,
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
        ),
        SimpleNamespace(name="Acme Entity", autonomy_mode=AutonomyMode.HUMAN_REVIEW),
    )

    without_evidence_pack = service.trigger_export(
        actor_user=actor_user,
        entity_id=entity_id,
        close_run_id=close_run_id,
        request=SimpleNamespace(
            include_evidence_pack=False,
            include_audit_trail=True,
            action_qualifier="full_export",
        ),
    )
    with_evidence_pack = service.trigger_export(
        actor_user=actor_user,
        entity_id=entity_id,
        close_run_id=close_run_id,
        request=SimpleNamespace(
            include_evidence_pack=True,
            include_audit_trail=True,
            action_qualifier="full_export",
        ),
    )

    assert len(captured_keys) == 2
    assert captured_keys[0] != captured_keys[1]
    assert without_evidence_pack["idempotency_key"] != with_evidence_pack["idempotency_key"]
