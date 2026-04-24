"""
Purpose: Verify accounting workspace readiness and chat-side COA resolution behavior.
Scope: Focused unit coverage over readiness messaging and fallback COA activation hooks.
Dependencies: accounting context helpers, chat action executor, and canonical entity records.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from services.agents.accounting_context import (
    AccountingWorkspaceContextBuilder,
    _build_readiness_summary,
)
from services.chat.action_execution import ChatActionExecutor
from services.common.enums import (
    CloseRunOperatingMode,
    CloseRunStatus,
    DocumentStatus,
    DocumentType,
    WorkflowPhase,
)
from services.db.models.audit import AuditSourceSurface
from services.db.models.coa import CoaSetSource
from services.db.repositories.entity_repo import EntityUserRecord
from services.documents.imported_ledger_representation import (
    ImportedLedgerRepresentationResult,
)


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
        gl_coding_document_count=1,
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


def test_readiness_summary_prompts_reconciliation_when_no_gl_coding_documents_exist() -> None:
    """Processing should not demand recommendations when only support documents are present."""

    readiness = _build_readiness_summary(
        close_run={
            "active_phase": "processing",
            "phase_states": [],
        },
        coa_summary={
            "is_available": True,
            "requires_operator_upload": False,
        },
        document_summary={
            "approved": 1,
        },
        gl_coding_document_count=0,
        recommendation_summary={},
        journal_summary={},
        reconciliation_summary={},
        schedule_summary={},
        report_summary={},
        export_summary={},
        distribution_summary={},
        pending_action_count=0,
    )

    assert not any(
        "Generate accounting recommendations" in action
        for action in readiness["next_actions"]
    )
    assert any(
        "Advance the close run to Reconciliation" in action
        for action in readiness["next_actions"]
    )


def test_readiness_summary_allows_reporting_advance_when_reconciliation_has_no_blockers() -> None:
    """Reconciliation should prompt Reporting advance when no applicable work remains."""

    readiness = _build_readiness_summary(
        close_run={
            "active_phase": "reconciliation",
            "phase_states": [],
        },
        coa_summary={
            "is_available": True,
            "requires_operator_upload": False,
        },
        document_summary={
            "approved": 1,
        },
        gl_coding_document_count=0,
        recommendation_summary={},
        journal_summary={},
        reconciliation_summary={},
        schedule_summary={"draft": 4},
        report_summary={},
        export_summary={},
        distribution_summary={},
        pending_action_count=0,
    )

    assert readiness["blockers"] == []
    assert readiness["warnings"] == []
    assert any(
        "Advance the close run to Reporting" in action
        for action in readiness["next_actions"]
    )


def test_readiness_summary_surfaces_source_document_only_mode_guidance() -> None:
    """Readiness should explain that bank reconciliation is optional until ledger data exists."""

    readiness = _build_readiness_summary(
        close_run={
            "active_phase": "reconciliation",
            "phase_states": [],
            "operating_mode": {
                "mode": "source_documents_only",
                "description": "This close run currently has source documents only.",
            },
        },
        coa_summary={
            "is_available": True,
            "requires_operator_upload": False,
        },
        document_summary={
            "approved": 1,
        },
        gl_coding_document_count=0,
        recommendation_summary={},
        journal_summary={},
        reconciliation_summary={},
        schedule_summary={},
        report_summary={},
        export_summary={},
        distribution_summary={},
        pending_action_count=0,
    )

    assert any(
        "source documents only" in warning.lower() for warning in readiness["warnings"]
    )
    assert any(
        "Upload a GL/cashbook later only if you want detailed bank reconciliation" in action
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


def test_workspace_snapshot_hides_gl_coding_work_for_docs_already_in_imported_gl(
    monkeypatch,
) -> None:
    """Imported-GL runs should not keep prompting recommendation work for booked docs."""

    actor_user = EntityUserRecord(
        id=uuid4(),
        email="ops@example.com",
        full_name="Finance Ops",
    )
    entity_id = uuid4()
    close_run_id = uuid4()
    document_id = uuid4()

    monkeypatch.setattr(
        "services.agents.accounting_context.evaluate_documents_imported_gl_representation",
        lambda **kwargs: {
            document_id: ImportedLedgerRepresentationResult(
                document_id=document_id,
                represented_in_imported_gl=True,
                status="represented_in_imported_gl",
                reason="Imported GL baseline already contains this document.",
                matched_line_no=4,
                matched_reference="INV-1048",
                matched_description="Acme Office Interiors",
                matched_posting_date=None,
            )
        },
    )

    builder = AccountingWorkspaceContextBuilder(
        action_repository=SimpleNamespace(),
        close_run_service=SimpleNamespace(
            list_close_runs_for_entity=lambda **kwargs: SimpleNamespace(close_runs=()),
            get_close_run=lambda **kwargs: SimpleNamespace(
                id=close_run_id,
                status=CloseRunStatus.DRAFT,
                reporting_currency="USD",
                current_version_no=1,
                operating_mode=SimpleNamespace(
                    mode=CloseRunOperatingMode.IMPORTED_GENERAL_LEDGER,
                    description=CloseRunOperatingMode.IMPORTED_GENERAL_LEDGER.description,
                    has_general_ledger_baseline=True,
                    has_trial_balance_baseline=False,
                    has_working_ledger_entries=False,
                    bank_reconciliation_available=True,
                    trial_balance_review_available=False,
                    journal_posting_available=True,
                    general_ledger_export_available=True,
                ),
                workflow_state=SimpleNamespace(
                    active_phase=WorkflowPhase.PROCESSING,
                    phase_states=[],
                ),
            )
        ),
        coa_repository=SimpleNamespace(
            get_active_set=lambda **kwargs: SimpleNamespace(
                id=uuid4(),
                source=CoaSetSource.MANUAL_UPLOAD,
                version_no=1,
                activated_at=None,
            ),
            list_accounts_for_set=lambda **kwargs: [
                SimpleNamespace(
                    account_code="6100",
                    account_name="Office Expense",
                    account_type="expense",
                    is_active=True,
                    is_postable=True,
                )
            ],
        ),
        document_repository=SimpleNamespace(
            _db_session=object(),
            list_documents_for_close_run_with_latest_extraction=lambda **kwargs: [
                SimpleNamespace(
                    document=SimpleNamespace(
                        id=document_id,
                        original_filename="invoice.pdf",
                        status=DocumentStatus.APPROVED,
                        document_type=DocumentType.INVOICE,
                    ),
                    latest_extraction=None,
                    open_issues=[],
                )
            ],
        ),
        entity_repository=SimpleNamespace(
            get_entity_for_user=lambda **kwargs: SimpleNamespace(
                entity=SimpleNamespace(
                    id=entity_id,
                    name="Acme Workspace",
                    legal_name="Acme Workspace",
                    base_currency="USD",
                    country_code="US",
                    timezone="America/New_York",
                    accounting_standard="IFRS",
                    autonomy_mode=SimpleNamespace(value="human_review"),
                    status=SimpleNamespace(value="active"),
                )
            ),
            list_entities_for_user=lambda **kwargs: [],
        ),
        export_service=SimpleNamespace(
            list_export_summaries=lambda **kwargs: [],
            get_latest_evidence_pack=lambda **kwargs: None,
        ),
        job_service=SimpleNamespace(
            list_jobs_for_user=lambda **kwargs: [],
        ),
        reconciliation_repository=SimpleNamespace(
            list_reconciliations=lambda *args, **kwargs: [],
            list_items=lambda **kwargs: [],
            list_anomalies=lambda **kwargs: [],
        ),
        recommendation_repository=SimpleNamespace(
            list_recommendations_for_close_run=lambda **kwargs: [],
            list_journals_for_close_run=lambda **kwargs: [],
            list_postings_for_journal_ids=lambda **kwargs: {},
        ),
        report_repository=SimpleNamespace(
            list_report_runs_for_close_run=lambda **kwargs: [],
        ),
        supporting_schedule_service=SimpleNamespace(
            list_workspace=lambda **kwargs: [],
        ),
    )

    snapshot = builder.build_snapshot(
        actor=actor_user,
        entity_id=entity_id,
        close_run_id=close_run_id,
        thread_id=None,
    )

    assert not any(
        "Generate accounting recommendations" in action
        for action in snapshot["readiness"]["next_actions"]
    )
    assert any(
        "Advance the close run to Reconciliation" in action
        for action in snapshot["readiness"]["next_actions"]
    )


def test_workspace_snapshot_derives_entity_close_run_period_labels() -> None:
    """Workspace snapshots should derive period labels from close-run dates, not removed fields."""

    actor_user = EntityUserRecord(
        id=uuid4(),
        email="ops@example.com",
        full_name="Finance Ops",
    )
    entity_id = uuid4()
    close_run_id = uuid4()

    builder = AccountingWorkspaceContextBuilder(
        action_repository=SimpleNamespace(
            list_pending_actions_for_thread=lambda **kwargs: [],
        ),
        close_run_service=SimpleNamespace(
            list_close_runs_for_entity=lambda **kwargs: SimpleNamespace(
                close_runs=(
                    SimpleNamespace(
                        id=close_run_id,
                        status=CloseRunStatus.DRAFT,
                        period_start=date(2026, 3, 1),
                        period_end=date(2026, 3, 31),
                        reporting_currency="NGN",
                        current_version_no=1,
                        workflow_state=SimpleNamespace(active_phase=WorkflowPhase.COLLECTION),
                    ),
                ),
            )
        ),
        coa_repository=SimpleNamespace(
            get_active_set=lambda **kwargs: None,
            list_accounts_for_set=lambda **kwargs: [],
        ),
        document_repository=SimpleNamespace(
            _db_session=object(),
            list_documents_for_close_run_with_latest_extraction=lambda **kwargs: [],
        ),
        entity_repository=SimpleNamespace(
            get_entity_for_user=lambda **kwargs: SimpleNamespace(
                entity=SimpleNamespace(
                    id=entity_id,
                    name="Apex Meridian Nigeria Ltd",
                    legal_name="Apex Meridian Nigeria Ltd",
                    base_currency="NGN",
                    country_code="NG",
                    timezone="Africa/Lagos",
                    accounting_standard="IFRS",
                    autonomy_mode=SimpleNamespace(value="human_review"),
                    status=SimpleNamespace(value="active"),
                )
            ),
            list_entities_for_user=lambda **kwargs: [],
        ),
        export_service=SimpleNamespace(
            list_export_summaries=lambda **kwargs: [],
            get_latest_evidence_pack=lambda **kwargs: None,
        ),
        job_service=SimpleNamespace(
            list_jobs_for_user=lambda **kwargs: [],
        ),
        reconciliation_repository=SimpleNamespace(
            list_reconciliations=lambda *args, **kwargs: [],
            list_items=lambda **kwargs: [],
            list_anomalies=lambda **kwargs: [],
        ),
        recommendation_repository=SimpleNamespace(
            list_recommendations_for_close_run=lambda **kwargs: [],
            list_journals_for_close_run=lambda **kwargs: [],
            list_postings_for_journal_ids=lambda **kwargs: {},
        ),
        report_repository=SimpleNamespace(
            list_report_runs_for_close_run=lambda **kwargs: [],
        ),
        supporting_schedule_service=SimpleNamespace(
            list_workspace=lambda **kwargs: [],
        ),
    )

    snapshot = builder.build_snapshot(
        actor=actor_user,
        entity_id=entity_id,
        close_run_id=None,
        thread_id=None,
    )

    assert snapshot["entity_close_runs"] == [
        {
            "id": close_run_id,
            "status": "draft",
            "period_label": "Mar 2026",
            "reporting_currency": "NGN",
            "version_no": 1,
            "active_phase": "collection",
        }
    ]


def test_workspace_snapshot_includes_accessible_close_run_report_state() -> None:
    """Global assistant snapshots should include bounded report state for accessible runs."""

    actor_user = EntityUserRecord(
        id=uuid4(),
        email="ops@example.com",
        full_name="Finance Ops",
    )
    current_entity_id = uuid4()
    apex_entity_id = uuid4()
    close_run_id = uuid4()
    report_run_id = uuid4()

    def list_close_runs_for_entity(**kwargs):
        if kwargs["entity_id"] != apex_entity_id:
            return SimpleNamespace(close_runs=())
        return SimpleNamespace(
            close_runs=(
                SimpleNamespace(
                    id=close_run_id,
                    status=CloseRunStatus.APPROVED,
                    period_start=date(2026, 3, 1),
                    period_end=date(2026, 3, 31),
                    reporting_currency="NGN",
                    current_version_no=1,
                    workflow_state=SimpleNamespace(active_phase=None),
                ),
            )
        )

    builder = AccountingWorkspaceContextBuilder(
        action_repository=SimpleNamespace(
            list_pending_actions_for_thread=lambda **kwargs: [],
        ),
        close_run_service=SimpleNamespace(
            list_close_runs_for_entity=list_close_runs_for_entity,
        ),
        coa_repository=SimpleNamespace(
            get_active_set=lambda **kwargs: None,
            list_accounts_for_set=lambda **kwargs: [],
        ),
        document_repository=SimpleNamespace(
            _db_session=object(),
            list_documents_for_close_run_with_latest_extraction=lambda **kwargs: [],
        ),
        entity_repository=SimpleNamespace(
            get_entity_for_user=lambda **kwargs: SimpleNamespace(
                entity=SimpleNamespace(
                    id=current_entity_id,
                    name="Polymarket",
                    legal_name="Polymarket",
                    base_currency="USD",
                    country_code="US",
                    timezone="America/New_York",
                    accounting_standard="IFRS",
                    autonomy_mode=SimpleNamespace(value="human_review"),
                    status=SimpleNamespace(value="active"),
                )
            ),
            list_entities_for_user=lambda **kwargs: [
                SimpleNamespace(
                    entity=SimpleNamespace(
                        id=current_entity_id,
                        name="Polymarket",
                        legal_name="Polymarket",
                        base_currency="USD",
                        country_code="US",
                        timezone="America/New_York",
                        accounting_standard="IFRS",
                        autonomy_mode=SimpleNamespace(value="human_review"),
                        status=SimpleNamespace(value="active"),
                    )
                ),
                SimpleNamespace(
                    entity=SimpleNamespace(
                        id=apex_entity_id,
                        name="Apex Meridian Distribution Limited",
                        legal_name="Apex Meridian Distribution Limited",
                        base_currency="NGN",
                        country_code="NG",
                        timezone="Africa/Lagos",
                        accounting_standard="IFRS",
                        autonomy_mode=SimpleNamespace(value="human_review"),
                        status=SimpleNamespace(value="active"),
                    )
                ),
            ],
        ),
        export_service=SimpleNamespace(
            list_export_summaries=lambda **kwargs: (
                (
                    SimpleNamespace(
                        id="export-1",
                        version_no=1,
                        status="completed",
                        artifact_count=3,
                        distribution_count=0,
                        created_at=None,
                        completed_at=None,
                        latest_distribution_at=None,
                    ),
                )
                if kwargs["close_run_id"] == close_run_id
                else ()
            ),
            get_latest_evidence_pack=lambda **kwargs: None,
        ),
        job_service=SimpleNamespace(
            list_jobs_for_user=lambda **kwargs: [],
        ),
        reconciliation_repository=SimpleNamespace(
            list_reconciliations=lambda *args, **kwargs: [],
            list_items=lambda **kwargs: [],
            list_anomalies=lambda **kwargs: [],
        ),
        recommendation_repository=SimpleNamespace(
            list_recommendations_for_close_run=lambda **kwargs: [],
            list_journals_for_close_run=lambda **kwargs: [],
            list_postings_for_journal_ids=lambda **kwargs: {},
        ),
        report_repository=SimpleNamespace(
            list_report_runs_for_close_run=lambda **kwargs: (
                (
                    SimpleNamespace(
                        id=report_run_id,
                        status=SimpleNamespace(value="completed"),
                        version_no=1,
                        artifact_refs=[{"kind": "pdf"}],
                        completed_at=None,
                    ),
                )
                if kwargs["close_run_id"] == close_run_id
                else ()
            ),
            list_commentary_for_report_run=lambda **kwargs: (
                SimpleNamespace(
                    id=uuid4(),
                    report_run_id=report_run_id,
                    section_key="profit_and_loss",
                    status=SimpleNamespace(value="approved"),
                    body="Approved management commentary for March.",
                ),
            ),
        ),
        supporting_schedule_service=SimpleNamespace(
            list_workspace=lambda **kwargs: [],
        ),
    )

    snapshot = builder.build_snapshot(
        actor=actor_user,
        entity_id=current_entity_id,
        close_run_id=None,
        thread_id=None,
    )

    apex_row = next(
        row
        for row in snapshot["accessible_workspace_close_runs"]
        if row["workspace"]["id"] == str(apex_entity_id)
    )
    close_run = apex_row["close_runs"][0]
    assert close_run["report_runs"][0]["id"] == str(report_run_id)
    assert close_run["report_runs"][0]["status"] == "completed"
    assert close_run["commentary"][0]["section_key"] == "profit_and_loss"
    assert close_run["exports"][0]["id"] == "export-1"
