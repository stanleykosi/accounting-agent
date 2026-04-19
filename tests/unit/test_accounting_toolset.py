"""
Purpose: Regression coverage for accounting tool runtime approval and reopen mapping.
Scope: Dynamic approval checks and deterministic document remapping across reopened runs.
Dependencies: AccountingToolset, agent execution context, and lightweight service doubles.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import services.agents.accounting_toolset as accounting_toolset_module
from services.agents.accounting_toolset import AccountingToolset
from services.agents.models import AgentExecutionContext
from services.common.enums import CloseRunStatus, WorkflowPhase
from services.db.models.documents import Document
from services.db.models.recommendations import Recommendation
from services.db.repositories.entity_repo import EntityUserRecord


class _FakeCloseRunService:
    def __init__(self, *, close_run: object, create_result: object | None = None) -> None:
        self.close_run = close_run
        self.create_result = create_result
        self.create_call: dict[str, object] | None = None

    def get_close_run(self, **kwargs):
        del kwargs
        return self.close_run

    def create_close_run(self, **kwargs):
        self.create_call = kwargs
        if self.create_result is None:
            raise AssertionError("create_close_run was not expected in this test.")
        return self.create_result


class _FakeDocumentRepository:
    def __init__(
        self,
        *,
        source_document: object,
        documents_by_close_run_id: dict[UUID, tuple[object, ...]],
    ) -> None:
        self.source_document = source_document
        self.documents_by_close_run_id = documents_by_close_run_id

    def get_document_for_user(self, **kwargs):
        del kwargs
        return SimpleNamespace(document=self.source_document)

    def list_documents_for_close_run(self, *, close_run_id: UUID):
        return self.documents_by_close_run_id[close_run_id]


def test_requires_human_approval_when_auto_tool_would_reopen_released_run() -> None:
    """Auto tools should stage for approval before reopening a released close run."""

    actor = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    close_run_id = uuid4()
    toolset = _make_toolset(
        close_run_service=_FakeCloseRunService(
            close_run=SimpleNamespace(
                status=CloseRunStatus.APPROVED,
                workflow_state=SimpleNamespace(active_phase=WorkflowPhase.REPORTING),
            )
        )
    )

    requires_approval = toolset.requires_human_approval_for_invocation(
        tool_name="generate_reports",
        tool_arguments={},
        context=_build_execution_context(actor=actor, close_run_id=close_run_id),
    )

    assert requires_approval is True


def test_requires_human_approval_when_auto_tool_would_rewind_phase() -> None:
    """Auto tools should stage for approval before rewinding to an earlier phase."""

    actor = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    close_run_id = uuid4()
    toolset = _make_toolset(
        close_run_service=_FakeCloseRunService(
            close_run=SimpleNamespace(
                status=CloseRunStatus.REOPENED,
                workflow_state=SimpleNamespace(active_phase=WorkflowPhase.REPORTING),
            )
        )
    )

    requires_approval = toolset.requires_human_approval_for_invocation(
        tool_name="run_reconciliation",
        tool_arguments={},
        context=_build_execution_context(actor=actor, close_run_id=close_run_id),
    )

    assert requires_approval is True


def test_auto_tool_remains_unblocked_when_scope_is_already_mutable() -> None:
    """Auto tools should continue executing immediately when no scope adjustment is needed."""

    actor = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    close_run_id = uuid4()
    toolset = _make_toolset(
        close_run_service=_FakeCloseRunService(
            close_run=SimpleNamespace(
                status=CloseRunStatus.REOPENED,
                workflow_state=SimpleNamespace(active_phase=WorkflowPhase.REPORTING),
            )
        )
    )

    requires_approval = toolset.requires_human_approval_for_invocation(
        tool_name="generate_reports",
        tool_arguments={},
        context=_build_execution_context(actor=actor, close_run_id=close_run_id),
    )

    assert requires_approval is False


def test_resolve_document_id_for_scope_preserves_duplicate_upload_order() -> None:
    """Reopened duplicate uploads should remap by peer index instead of failing as ambiguous."""

    actor = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    source_close_run_id = uuid4()
    target_close_run_id = uuid4()
    fingerprint = {
        "original_filename": "statement.pdf",
        "storage_key": "documents/statement.pdf",
        "sha256_hash": "abc123",
        "file_size_bytes": 4096,
    }
    source_documents = (
        _document_record(
            document_id=uuid4(),
            created_at=datetime(2026, 4, 1, tzinfo=UTC),
            **fingerprint,
        ),
        _document_record(
            document_id=uuid4(),
            created_at=datetime(2026, 4, 2, tzinfo=UTC),
            **fingerprint,
        ),
    )
    target_documents = (
        _document_record(
            document_id=uuid4(),
            created_at=datetime(2026, 4, 3, tzinfo=UTC),
            **fingerprint,
        ),
        _document_record(
            document_id=uuid4(),
            created_at=datetime(2026, 4, 4, tzinfo=UTC),
            **fingerprint,
        ),
    )
    toolset = _make_toolset(
        document_repository=_FakeDocumentRepository(
            source_document=source_documents[1],
            documents_by_close_run_id={
                source_close_run_id: source_documents,
                target_close_run_id: target_documents,
            },
        )
    )

    resolved_id = toolset._resolve_document_id_for_scope(
        actor_user=actor,
        entity_id=uuid4(),
        source_close_run_id=source_close_run_id,
        target_close_run_id=target_close_run_id,
        document_id=source_documents[1].id,
    )

    assert resolved_id == target_documents[1].id


def test_create_close_run_tool_uses_canonical_contract_validation() -> None:
    """Create-close-run tool calls should normalize and validate the route contract."""

    actor = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    created_close_run_id = uuid4()
    close_run_service = _FakeCloseRunService(
        close_run=SimpleNamespace(),
        create_result=SimpleNamespace(
            id=str(created_close_run_id),
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            reporting_currency="NGN",
            current_version_no=1,
            status=CloseRunStatus.DRAFT,
            workflow_state=SimpleNamespace(active_phase=WorkflowPhase.COLLECTION),
        ),
    )
    toolset = _make_toolset(close_run_service=close_run_service)

    result = toolset._create_close_run(
        {
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "reporting_currency": "ngn",
        },
        _build_execution_context(actor=actor, close_run_id=uuid4()),
    )

    assert close_run_service.create_call is not None
    assert close_run_service.create_call["period_start"] == date(2026, 4, 1)
    assert close_run_service.create_call["period_end"] == date(2026, 4, 30)
    assert close_run_service.create_call["reporting_currency"] == "NGN"
    assert close_run_service.create_call["allow_duplicate_period"] is False
    assert close_run_service.create_call["duplicate_period_reason"] is None
    assert result["created_close_run_id"] == str(created_close_run_id)
    assert result["active_phase"] == WorkflowPhase.COLLECTION.value


def test_queue_recommendation_jobs_skips_bank_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only bookable approved documents should enter GL-coding recommendation generation."""

    actor = EntityUserRecord(id=uuid4(), email="ops@example.com", full_name="Finance Ops")
    entity_id = uuid4()
    close_run_id = uuid4()
    invoice_id = uuid4()
    bank_statement_id = uuid4()
    dispatched_document_ids: list[UUID] = []
    dispatched_payloads: list[dict[str, object]] = []

    class _FakeQuery:
        def __init__(self, records: list[object]) -> None:
            self._records = records

        def join(self, *args, **kwargs):
            del args, kwargs
            return self

        def filter(self, *args, **kwargs):
            del kwargs
            filtered = list(self._records)
            for expression in args:
                rendered = str(expression)
                if "documents.status" in rendered:
                    filtered = [
                        record
                        for record in filtered
                        if getattr(record, "status", None) == "approved"
                    ]
                elif "documents.document_type" in rendered and " IN " in rendered:
                    filtered = [
                        record
                        for record in filtered
                        if getattr(record, "document_type", None)
                        in {"invoice", "receipt", "payslip"}
                    ]
            self._records = filtered
            return self

        def order_by(self, *args, **kwargs):
            del args, kwargs
            return self

        def all(self):
            return list(self._records)

    class _FakeDbSession:
        def __init__(self) -> None:
            self.documents = [
                SimpleNamespace(
                    id=invoice_id,
                    close_run_id=close_run_id,
                    document_type="invoice",
                    status="approved",
                    created_at=datetime(2026, 4, 1, tzinfo=UTC),
                ),
                SimpleNamespace(
                    id=bank_statement_id,
                    close_run_id=close_run_id,
                    document_type="bank_statement",
                    status="approved",
                    created_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
            ]

        def query(self, model):
            if model is Document:
                return _FakeQuery(list(self.documents))
            if model is Recommendation:
                return _FakeQuery([])
            raise AssertionError(f"Unexpected query model: {model!r}")

    class _FakeJobService:
        def dispatch_job(self, **kwargs):
            dispatched_document_ids.append(kwargs["document_id"])
            dispatched_payloads.append(dict(kwargs["payload"]))
            return SimpleNamespace(
                id=uuid4(),
                task_name="accounting.recommend_close_run",
                status=SimpleNamespace(value="queued"),
            )

    toolset = _make_toolset()
    toolset._db_session = _FakeDbSession()
    toolset._job_service = _FakeJobService()
    monkeypatch.setattr(
        accounting_toolset_module,
        "evaluate_documents_imported_gl_representation",
        lambda **kwargs: {},
    )

    queued_jobs = toolset._queue_recommendation_jobs(
        entity_id=entity_id,
        close_run_id=close_run_id,
        actor_user=actor,
        document_ids=None,
        force=False,
        trace_id=None,
    )

    assert [job["document_id"] for job in queued_jobs] == [str(invoice_id)]
    assert dispatched_document_ids == [invoice_id]
    assert dispatched_payloads == [
        {
            "entity_id": str(entity_id),
            "close_run_id": str(close_run_id),
            "document_id": str(invoice_id),
            "actor_user_id": str(actor.id),
            "force": False,
        }
    ]


def _make_toolset(
    *,
    close_run_service: object | None = None,
    document_repository: object | None = None,
) -> AccountingToolset:
    """Build an AccountingToolset with only the collaborators this test suite needs."""

    return AccountingToolset(
        db_session=SimpleNamespace(),
        close_run_service=close_run_service or _FakeCloseRunService(close_run=SimpleNamespace()),
        document_review_service=SimpleNamespace(),
        document_repository=document_repository or SimpleNamespace(),
        export_service=SimpleNamespace(),
        job_service=SimpleNamespace(),
        recommendation_service=SimpleNamespace(),
        recommendation_repository=SimpleNamespace(),
        reconciliation_service=SimpleNamespace(),
        reconciliation_repository=SimpleNamespace(),
        report_service=SimpleNamespace(),
        report_repository=SimpleNamespace(),
        supporting_schedule_service=SimpleNamespace(),
        task_dispatcher=SimpleNamespace(),
    )


def _build_execution_context(
    *,
    actor: EntityUserRecord,
    close_run_id: UUID,
) -> AgentExecutionContext:
    """Return the minimum execution context needed for approval checks."""

    return AgentExecutionContext(
        actor=actor,
        entity_id=uuid4(),
        close_run_id=close_run_id,
        source_close_run_id=None,
        thread_id=None,
        trace_id=None,
        source_surface=None,
    )


def _document_record(
    *,
    document_id: UUID,
    created_at: datetime,
    original_filename: str,
    storage_key: str,
    sha256_hash: str,
    file_size_bytes: int,
):
    """Return one lightweight document record with the fields used by remapping."""

    return SimpleNamespace(
        id=document_id,
        created_at=created_at,
        original_filename=original_filename,
        storage_key=storage_key,
        sha256_hash=sha256_hash,
        file_size_bytes=file_size_bytes,
    )
