"""
Purpose: Build the accounting workspace snapshot used by the agent planner.
Scope: Close-run progress state, documents, recommendations, journals,
reconciliations, reports, jobs, exports, evidence packs, and recent actions.
Dependencies: Accounting workflow services and repositories only.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from services.agents.context import WorkspaceContextBuilder
from services.close_runs.service import CloseRunService
from services.coa.service import CoaRepository
from services.db.models.coa import CoaSetSource
from services.db.repositories.chat_action_repo import ChatActionRepository
from services.db.repositories.document_repo import DocumentRepository
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.recommendation_journal_repo import RecommendationJournalRepository
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.repositories.report_repo import ReportRepository
from services.documents.recommendation_eligibility import (
    is_gl_coding_recommendation_eligible,
)
from services.documents.transaction_matching import (
    extract_auto_review_metadata,
    extract_auto_transaction_match_metadata,
)
from services.exports.service import ExportService
from services.jobs.service import JobService
from services.supporting_schedules.service import SupportingScheduleService


class AccountingWorkspaceContextBuilder(WorkspaceContextBuilder):
    """Build accounting-workspace snapshots for the generic agent kernel."""

    def __init__(
        self,
        *,
        action_repository: ChatActionRepository,
        close_run_service: CloseRunService,
        coa_repository: CoaRepository,
        document_repository: DocumentRepository,
        export_service: ExportService,
        job_service: JobService,
        reconciliation_repository: ReconciliationRepository,
        recommendation_repository: RecommendationJournalRepository,
        report_repository: ReportRepository,
        supporting_schedule_service: SupportingScheduleService,
    ) -> None:
        self._action_repo = action_repository
        self._close_run_service = close_run_service
        self._coa_repo = coa_repository
        self._document_repo = document_repository
        self._export_service = export_service
        self._job_service = job_service
        self._reconciliation_repo = reconciliation_repository
        self._recommendation_repo = recommendation_repository
        self._report_repo = report_repository
        self._supporting_schedule_service = supporting_schedule_service

    def build_snapshot(
        self,
        *,
        actor: Any,
        entity_id: UUID,
        close_run_id: UUID | None,
        thread_id: UUID | None,
    ) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the accounting workspace state."""

        actor_user = actor
        if not isinstance(actor_user, EntityUserRecord):
            raise TypeError("Accounting workspace snapshots require an EntityUserRecord actor.")

        snapshot: dict[str, Any] = {
            "entity_id": str(entity_id),
            "close_run_id": str(close_run_id) if close_run_id else None,
        }
        snapshot["coa"] = self._build_coa_snapshot(entity_id=entity_id)
        if close_run_id is None:
            snapshot["readiness"] = _build_readiness_summary(
                close_run=None,
                coa_summary=snapshot["coa"],
                document_summary={},
                gl_coding_document_count=0,
                recommendation_summary={},
                journal_summary={},
                reconciliation_summary={},
                schedule_summary={},
                report_summary={},
                export_summary={},
                pending_action_count=0,
            )
            snapshot["progress_summary"] = _build_progress_summary(
                close_run=None,
                coa_summary=snapshot["coa"],
                document_summary={},
                recommendation_summary={},
                journal_summary={},
                reconciliation_summary={},
                schedule_summary={},
                report_summary={},
                job_summary={},
                export_summary={},
                evidence_pack=None,
                pending_action_count=0,
            )
            return snapshot

        close_run = self._close_run_service.get_close_run(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        snapshot["close_run"] = {
            "id": close_run.id,
            "status": close_run.status.value,
            "reporting_currency": close_run.reporting_currency,
            "current_version_no": close_run.current_version_no,
            "active_phase": (
                close_run.workflow_state.active_phase.value
                if close_run.workflow_state.active_phase is not None
                else None
            ),
            "phase_states": [
                {
                    "phase": phase_state.phase.value,
                    "label": phase_state.phase.label,
                    "status": phase_state.status.value,
                    "blocking_reason": phase_state.blocking_reason,
                    "completed_at": phase_state.completed_at,
                }
                for phase_state in close_run.workflow_state.phase_states
            ],
        }

        documents = self._document_repo.list_documents_for_close_run_with_latest_extraction(
            close_run_id=close_run_id
        )
        document_summary = _count_by_key(row.document.status.value for row in documents)
        snapshot["documents"] = [
            {
                "id": str(row.document.id),
                "filename": row.document.original_filename,
                "status": row.document.status.value,
                "document_type": row.document.document_type.value,
                "auto_approved": _read_document_auto_approved(row.latest_extraction),
                "auto_transaction_match_status": _read_document_auto_transaction_match_status(
                    row.latest_extraction
                ),
                "fields": [
                    {
                        "id": str(field.id),
                        "field_name": field.field_name,
                        "value": field.field_value,
                    }
                    for field in (row.latest_extraction.fields if row.latest_extraction else ())
                ][:20],
            }
            for row in documents[:20]
        ]
        snapshot["document_summary"] = document_summary
        gl_coding_document_count = sum(
            1
            for row in documents
            if is_gl_coding_recommendation_eligible(row.document.document_type)
            and row.document.status.value in {"parsed", "needs_review", "approved", "rejected"}
        )

        recommendations = self._recommendation_repo.list_recommendations_for_close_run(
            close_run_id=close_run_id
        )
        snapshot["recommendations"] = [
            {
                "id": str(recommendation.id),
                "status": recommendation.status,
                "recommendation_type": recommendation.recommendation_type,
                "document_id": (
                    str(recommendation.document_id) if recommendation.document_id else None
                ),
                "reasoning_summary": recommendation.reasoning_summary,
            }
            for recommendation in recommendations[:25]
        ]
        snapshot["recommendation_summary"] = _count_by_key(
            recommendation.status for recommendation in recommendations
        )

        journals = self._recommendation_repo.list_journals_for_close_run(close_run_id=close_run_id)
        journal_postings = self._recommendation_repo.list_postings_for_journal_ids(
            journal_entry_ids=tuple(journal.id for journal in journals),
        )
        snapshot["journals"] = [
            {
                "id": str(journal.id),
                "status": journal.status,
                "journal_number": journal.journal_number,
                "description": journal.description,
                "latest_posting": _build_latest_posting_snapshot(
                    journal_postings.get(journal.id),
                ),
            }
            for journal in journals[:25]
        ]
        snapshot["journal_summary"] = _count_by_key(journal.status for journal in journals)
        snapshot["journal_posting_summary"] = _count_by_key(
            postings[0].posting_target
            for postings in (
                journal_postings.get(journal.id, ())
                for journal in journals
            )
            if postings
        )

        reconciliations = self._reconciliation_repo.list_reconciliations(close_run_id)
        snapshot["reconciliations"] = [
            {
                "id": str(reconciliation.id),
                "type": reconciliation.reconciliation_type.value,
                "status": reconciliation.status.value,
            }
            for reconciliation in reconciliations[:20]
        ]
        snapshot["reconciliation_summary"] = _count_by_key(
            reconciliation.status.value for reconciliation in reconciliations
        )

        supporting_schedules = self._supporting_schedule_service.list_workspace(
            close_run_id=close_run_id
        )
        snapshot["supporting_schedules"] = [
            {
                "id": str(schedule.schedule.id),
                "schedule_type": schedule.schedule.schedule_type.value,
                "status": schedule.schedule.status.value,
                "row_count": len(schedule.rows),
                "note": schedule.schedule.note,
                "reviewed_at": schedule.schedule.reviewed_at,
                "rows": [
                    {
                        "id": str(row.id),
                        "row_ref": row.row_ref,
                        "line_no": row.line_no,
                        "payload": dict(row.payload),
                    }
                    for row in schedule.rows[:25]
                ],
            }
            for schedule in supporting_schedules
        ]
        snapshot["supporting_schedule_summary"] = _count_by_key(
            schedule.schedule.status.value for schedule in supporting_schedules
        )
        snapshot["supporting_schedule_row_summary"] = {
            schedule.schedule.schedule_type.value: len(schedule.rows)
            for schedule in supporting_schedules
        }

        report_runs = self._report_repo.list_report_runs_for_close_run(close_run_id=close_run_id)
        snapshot["report_runs"] = [
            {
                "id": str(run.id),
                "status": run.status.value,
                "version_no": run.version_no,
            }
            for run in report_runs[:10]
        ]
        snapshot["report_summary"] = _count_by_key(run.status.value for run in report_runs)

        jobs = self._job_service.list_jobs_for_user(
            entity_id=entity_id,
            user_id=actor_user.id,
            close_run_id=close_run_id,
        )
        snapshot["jobs"] = [
            {
                "id": str(job.id),
                "task_name": job.task_name,
                "status": job.status.value,
                "blocking_reason": job.blocking_reason,
                "failure_reason": job.failure_reason,
                "created_at": job.created_at,
                "completed_at": job.completed_at,
            }
            for job in jobs[:20]
        ]
        snapshot["job_summary"] = _count_by_key(job.status.value for job in jobs)

        exports = self._export_service.list_export_summaries(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        snapshot["exports"] = [
            {
                "id": export.id,
                "status": export.status,
                "artifact_count": export.artifact_count,
                "distribution_count": export.distribution_count,
                "created_at": export.created_at,
                "completed_at": export.completed_at,
                "latest_distribution_at": export.latest_distribution_at,
            }
            for export in exports[:10]
        ]
        snapshot["export_summary"] = _count_by_key(export.status for export in exports)
        distribution_count = sum(export.distribution_count for export in exports)
        latest_distribution_at = max(
            (
                export.latest_distribution_at
                for export in exports
                if export.latest_distribution_at is not None
            ),
            default=None,
        )
        snapshot["distribution_summary"] = {
            "record_count": distribution_count,
            "latest_distribution_at": latest_distribution_at,
        }

        evidence_pack = self._export_service.get_latest_evidence_pack(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=close_run_id,
        )
        snapshot["evidence_pack"] = (
            {
                "version_no": evidence_pack.version_no,
                "generated_at": evidence_pack.generated_at,
                "storage_key": evidence_pack.storage_key,
                "size_bytes": evidence_pack.size_bytes,
                "idempotency_key": evidence_pack.idempotency_key,
            }
            if evidence_pack is not None
            else None
        )

        if thread_id is not None:
            recent_actions = self._action_repo.list_actions_for_thread(
                thread_id=thread_id,
                entity_id=entity_id,
                limit=50,
            )
            recent_actions = tuple(
                action
                for action in recent_actions
                if _action_matches_close_run_scope(action=action, close_run_id=close_run_id)
            )[:20]
        else:
            recent_actions = ()
        snapshot["recent_actions"] = [
            {
                "id": str(action.id),
                "status": action.status,
                "intent": action.intent,
                "target_type": action.target_type,
                "target_id": str(action.target_id) if action.target_id is not None else None,
                "requires_human_approval": action.requires_human_approval,
                "created_at": action.created_at,
            }
            for action in recent_actions
        ]
        snapshot["pending_action_count"] = sum(
            1 for action in recent_actions if action.status == "pending"
        )
        snapshot["workflow_blueprint"] = _build_workflow_blueprint(
            close_run=snapshot["close_run"],
        )
        snapshot["progress_summary"] = _build_progress_summary(
            close_run=snapshot["close_run"],
            coa_summary=snapshot["coa"],
            document_summary=document_summary,
            recommendation_summary=snapshot["recommendation_summary"],
            journal_summary=snapshot["journal_summary"],
            reconciliation_summary=snapshot["reconciliation_summary"],
            schedule_summary=snapshot["supporting_schedule_summary"],
            report_summary=snapshot["report_summary"],
            job_summary=snapshot["job_summary"],
            export_summary=snapshot["export_summary"],
            distribution_summary=snapshot["distribution_summary"],
            evidence_pack=snapshot["evidence_pack"],
            pending_action_count=snapshot["pending_action_count"],
        )
        snapshot["readiness"] = _build_readiness_summary(
            close_run=snapshot["close_run"],
            coa_summary=snapshot["coa"],
            document_summary=document_summary,
            gl_coding_document_count=gl_coding_document_count,
            recommendation_summary=snapshot["recommendation_summary"],
            journal_summary=snapshot["journal_summary"],
            reconciliation_summary=snapshot["reconciliation_summary"],
            schedule_summary=snapshot["supporting_schedule_summary"],
            report_summary=snapshot["report_summary"],
            export_summary=snapshot["export_summary"],
            distribution_summary=snapshot["distribution_summary"],
            pending_action_count=snapshot["pending_action_count"],
        )
        return snapshot

    def _build_coa_snapshot(self, *, entity_id: UUID) -> dict[str, Any]:
        """Return the active COA state exposed to the planner and workbench."""

        active_set = self._coa_repo.get_active_set(entity_id=entity_id)
        if active_set is None:
            return {
                "is_available": False,
                "status": "missing",
                "source": None,
                "version_no": None,
                "account_count": 0,
                "postable_account_count": 0,
                "requires_operator_upload": True,
                "activated_at": None,
                "summary": (
                    "No active chart of accounts is configured. Upload a production COA from the "
                    "workbench or Chart of Accounts page before relying on the agent for "
                    "high-precision coding and reporting."
                ),
                "accounts": [],
            }

        accounts = tuple(self._coa_repo.list_accounts_for_set(coa_set_id=active_set.id))
        active_accounts = tuple(account for account in accounts if account.is_active)
        postable_accounts = tuple(account for account in active_accounts if account.is_postable)
        is_fallback = active_set.source == CoaSetSource.FALLBACK_NIGERIAN_SME
        if is_fallback:
            status = "fallback"
            summary = (
                f"Fallback chart of accounts version {active_set.version_no} is active with "
                f"{len(active_accounts)} active accounts. You can continue collection and "
                "document review now; upload a production COA later if you want to replace "
                "the fallback before sign-off."
            )
        else:
            status = "active"
            summary = (
                f"{active_set.source.value.replace('_', ' ')} COA version {active_set.version_no} "
                f"is active with {len(active_accounts)} active accounts."
            )

        return {
            "is_available": True,
            "status": status,
            "source": active_set.source.value,
            "version_no": active_set.version_no,
            "account_count": len(active_accounts),
            "postable_account_count": len(postable_accounts),
            "requires_operator_upload": is_fallback,
            "activated_at": active_set.activated_at,
            "summary": summary,
            "accounts": [
                {
                    "account_code": account.account_code,
                    "account_name": account.account_name,
                    "account_type": account.account_type,
                    "is_active": account.is_active,
                    "is_postable": account.is_postable,
                }
                for account in active_accounts[:200]
            ],
        }


def _count_by_key(values: Any) -> dict[str, int]:
    """Count string-like values into a deterministic summary map."""

    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _build_latest_posting_snapshot(postings: Any) -> dict[str, Any] | None:
    """Return a compact latest-posting snapshot for the planner."""

    if not postings:
        return None
    latest_posting = postings[0]
    return {
        "posting_target": latest_posting.posting_target,
        "provider": latest_posting.provider,
        "status": latest_posting.status,
        "posted_at": latest_posting.posted_at,
    }


def _read_document_auto_approved(latest_extraction: Any) -> bool:
    """Return whether the latest extraction was auto-approved by policy."""

    if latest_extraction is None:
        return False
    metadata = extract_auto_review_metadata(latest_extraction.extracted_payload)
    return bool(metadata and metadata.get("auto_approved") is True)


def _read_document_auto_transaction_match_status(latest_extraction: Any) -> str | None:
    """Return the persisted transaction-linking status for one document."""

    if latest_extraction is None:
        return None
    metadata = extract_auto_transaction_match_metadata(latest_extraction.extracted_payload)
    status = metadata.get("status") if isinstance(metadata, dict) else None
    return str(status) if isinstance(status, str) else None


def _build_workflow_blueprint(*, close_run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the accountant workflow blueprint with current step-state projection."""

    phase_states = {
        str(phase_state.get("phase")): phase_state
        for phase_state in close_run.get("phase_states", [])
    }
    active_phase = close_run.get("active_phase")
    steps = [
        ("01", "Collect source documents", "collection"),
        ("02", "Review and verify documents", "collection"),
        ("03", "Code and classify transactions", "processing"),
        ("04", "Post transactions to the General Ledger", "processing"),
        ("05", "Reconcile key accounts", "reconciliation"),
        ("06", "Update supporting schedules", "reconciliation"),
        ("07", "Run and review trial balance", "reconciliation"),
        ("08", "Prepare management report", "reporting"),
        ("09", "Write commentary and analysis", "reporting"),
        ("10", "Review, sign-off, and distribute", "review_signoff"),
    ]
    blueprint: list[dict[str, Any]] = []
    for step_no, title, phase in steps:
        phase_state = phase_states.get(phase, {})
        status = (
            "completed"
            if phase_state.get("status") == "completed"
            else "active"
            if active_phase == phase
            else "upcoming"
        )
        if phase_state.get("status") == "blocked" and active_phase == phase:
            status = "blocked"
        blueprint.append(
            {
                "step_no": step_no,
                "title": title,
                "phase": phase,
                "status": status,
                "blocking_reason": phase_state.get("blocking_reason"),
            }
        )
    return blueprint


def _build_progress_summary(
    *,
    close_run: dict[str, Any] | None,
    coa_summary: dict[str, Any],
    document_summary: dict[str, int],
    recommendation_summary: dict[str, int],
    journal_summary: dict[str, int],
    reconciliation_summary: dict[str, int],
    schedule_summary: dict[str, int],
    report_summary: dict[str, int],
    job_summary: dict[str, int],
    export_summary: dict[str, int],
    distribution_summary: dict[str, Any],
    evidence_pack: dict[str, Any] | None,
    pending_action_count: int,
) -> str:
    """Render a compact progress narrative for the planner and chat history."""

    if close_run is None:
        return " ".join(
            [
                "Entity-scoped workspace with no close run selected.",
                f"COA={coa_summary.get('status')} source={coa_summary.get('source') or 'none'}.",
                coa_summary.get("summary") or "Chart-of-accounts state unavailable.",
            ]
        )

    blocked_phases = [
        f"{phase_state['label']}: {phase_state['blocking_reason']}"
        for phase_state in close_run.get("phase_states", [])
        if phase_state.get("status") == "blocked" and phase_state.get("blocking_reason")
    ]
    active_phase = close_run.get("active_phase") or "not_started"
    parts = [
        f"Close run status={close_run.get('status')} active_phase={active_phase}.",
        (
            f"COA={coa_summary.get('status')} source={coa_summary.get('source') or 'none'} "
            f"accounts={coa_summary.get('account_count', 0)}."
        ),
        f"Documents={json.dumps(document_summary, sort_keys=True)}.",
        f"Recommendations={json.dumps(recommendation_summary, sort_keys=True)}.",
        f"Journals={json.dumps(journal_summary, sort_keys=True)}.",
        f"Reconciliations={json.dumps(reconciliation_summary, sort_keys=True)}.",
        f"SupportingSchedules={json.dumps(schedule_summary, sort_keys=True)}.",
        f"Reports={json.dumps(report_summary, sort_keys=True)}.",
        f"Jobs={json.dumps(job_summary, sort_keys=True)}.",
        f"Exports={json.dumps(export_summary, sort_keys=True)}.",
        (f"Management distributions={distribution_summary.get('record_count', 0)}."),
        f"Pending chat approvals={pending_action_count}.",
        (
            f"Evidence pack ready at {evidence_pack.get('generated_at')}."
            if evidence_pack is not None
            else "Evidence pack not yet assembled."
        ),
    ]
    if blocked_phases:
        parts.append(f"Blocked phases: {' | '.join(blocked_phases)}.")
    return " ".join(parts)


def _action_matches_close_run_scope(*, action: Any, close_run_id: UUID | None) -> bool:
    """Return whether one thread action belongs to the active close-run scope."""

    if getattr(action, "close_run_id", None) == close_run_id:
        return True
    if close_run_id is None:
        return False
    applied_result = getattr(action, "applied_result", None)
    if not isinstance(applied_result, dict):
        return False
    return applied_result.get("reopened_close_run_id") == str(close_run_id) or applied_result.get(
        "created_close_run_id"
    ) == str(close_run_id)


def _build_readiness_summary(
    *,
    close_run: dict[str, Any] | None,
    coa_summary: dict[str, Any],
    document_summary: dict[str, int],
    gl_coding_document_count: int,
    recommendation_summary: dict[str, int],
    journal_summary: dict[str, int],
    reconciliation_summary: dict[str, int],
    schedule_summary: dict[str, int],
    report_summary: dict[str, int],
    export_summary: dict[str, int],
    distribution_summary: dict[str, Any],
    pending_action_count: int,
) -> dict[str, Any]:
    """Build a compact readiness model for the chat workbench and planner."""

    if close_run is None:
        return {
            "has_close_run": False,
            "status": "not_scoped",
            "blockers": [],
            "warnings": [],
            "next_actions": [
            "Create or open a close run to let the agent execute close-run workflows."
            ],
            "document_count": 0,
            "has_source_documents": False,
            "parsed_document_count": 0,
            "phase_states": [],
        }

    blockers: list[str] = []
    warnings: list[str] = []
    next_actions: list[str] = []

    if not coa_summary.get("is_available", False):
        blockers.append("No active chart of accounts is configured for this entity.")
        next_actions.append(
            "Upload a production chart of accounts from the workbench or Chart of Accounts page."
        )
    elif coa_summary.get("requires_operator_upload", False):
        warnings.append(
            "A fallback chart of accounts is active. You can continue intake work now, but "
            "upload a production COA before sign-off if you need entity-specific mapping."
        )
        next_actions.append(
            "Upload a production chart of accounts from the workbench or Chart of Accounts page "
            "if you want to replace the fallback before sign-off."
        )

    document_count = sum(document_summary.values())
    parsed_document_count = sum(
        document_summary.get(status, 0)
        for status in ("parsed", "needs_review", "approved", "rejected")
    )
    if document_count == 0:
        blockers.append("No source documents have been uploaded for this close run.")
        next_actions.append("Upload source documents so parsing and extraction can begin.")
    elif document_summary.get("processing", 0) > 0 or document_summary.get("uploaded", 0) > 0:
        next_actions.append(
            "Allow current parsing jobs to finish, then review extracted documents."
        )

    if recommendation_summary == {} and gl_coding_document_count > 0:
        next_actions.append("Generate accounting recommendations for the parsed document set.")
    if recommendation_summary.get("pending", 0) > 0 or journal_summary.get("pending", 0) > 0:
        next_actions.append("Review and approve pending recommendations or journal drafts.")
    if reconciliation_summary == {} and journal_summary.get("applied", 0) > 0:
        next_actions.append("Run reconciliations after journals are applied.")
    if (
        schedule_summary.get("approved", 0) < 4
        and close_run.get("active_phase") == "reconciliation"
    ):
        next_actions.append(
            "Update and review all supporting schedules before moving from "
            "Reconciliation to Reporting."
        )
    if report_summary == {} and reconciliation_summary.get("completed", 0) > 0:
        next_actions.append("Generate reports and commentary for the current close run.")
    if export_summary == {} and report_summary.get("completed", 0) > 0:
        next_actions.append("Generate the export package and assemble the evidence pack.")
    if export_summary.get("completed", 0) > 0 and distribution_summary.get("record_count", 0) == 0:
        next_actions.append(
            "Record management distribution for the finalized export package before sign-off."
        )
    if pending_action_count > 0:
        warnings.append("Pending chat approvals are waiting for operator review.")
    if schedule_summary.get("in_review", 0) > 0 or schedule_summary.get("draft", 0) > 0:
        warnings.append(
            "Supporting schedules are still being maintained or reviewed in the Step 6 workspace."
        )

    blocked_phase_reasons = [
        phase_state["blocking_reason"]
        for phase_state in close_run.get("phase_states", [])
        if phase_state.get("status") == "blocked" and phase_state.get("blocking_reason")
    ]
    blockers.extend(reason for reason in blocked_phase_reasons if reason not in blockers)
    if (
        close_run.get("active_phase") == "collection"
        and not blockers
        and document_count > 0
    ):
        next_actions.append(
            "Advance the close run to Processing when you are done collecting approved documents."
        )
    if (
        close_run.get("active_phase") == "processing"
        and not blockers
        and gl_coding_document_count == 0
    ):
        next_actions.append(
            "Advance the close run to Reconciliation when no GL-coding work is required for "
            "the approved documents."
        )
    if not next_actions:
        next_actions.append(
            "Ask the agent for the next best action or review the latest trace output."
        )

    status = "blocked" if blockers else "attention_required" if warnings else "ready"
    return {
        "has_close_run": True,
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": next_actions,
        "document_count": document_count,
        "has_source_documents": document_count > 0,
        "parsed_document_count": parsed_document_count,
        "phase_states": list(close_run.get("phase_states", [])),
    }
