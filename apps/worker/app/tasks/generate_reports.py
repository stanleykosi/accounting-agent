"""
Purpose: Celery task that generates Excel and PDF report packs for a close run.
Scope: Orchestrates data loading, commentary generation, Excel/PDF building,
artifact storage, and report-run persistence. Handles versioning, status tracking,
and failure reporting.
Dependencies: Celery worker app, Excel/PDF builders, commentary generator,
DB session factory, storage repository, report repository, audit helpers,
and structured logging.

Design notes:
- This is the canonical entry point for all report generation.
- The task loads close run data, generates commentary, builds Excel and PDF artifacts,
  uploads them to MinIO, and updates the report run record.
- If Excel generation fails, PDF generation is NOT attempted — the task fails fast
  with explicit error reporting.
- Report runs are versioned per close run so regeneration does not overwrite prior artifacts.
- The task records checkpoints at each major phase for resumability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from uuid import UUID

from apps.worker.app.celery_runtime import celery_app
from apps.worker.app.tasks.base import JobRuntimeContext, TrackedJobTask
from apps.worker.app.tasks.close_run_phase_guard import ensure_close_run_active_phase
from services.common.enums import (
    ArtifactType,
    ReportSectionKey,
    WorkflowPhase,
)
from services.common.logging import get_logger
from services.common.types import utc_now
from services.contracts.storage_models import CloseRunStorageScope
from services.db.models.close_run import CloseRun
from services.db.models.entity import Entity
from services.db.models.reporting import (
    CommentaryStatus,
    ReportRunStatus,
    ReportTemplate,
)
from services.db.repositories.report_repo import ReportRepository
from services.db.session import get_session_factory
from services.jobs.retry_policy import JobCancellationRequestedError
from services.jobs.task_names import TaskName, resolve_task_route
from services.storage.repository import StorageRepository

if TYPE_CHECKING:
    from services.reporting.commentary import CommentaryGenerationResult
    from services.reporting.excel_builder import ExcelReportResult
    from services.reporting.pdf_builder import PdfReportResult

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReportGenerationReceipt:
    """Describe the outcome of one report generation execution.

    Attributes:
        report_run_id: UUID of the created/updated report run.
        close_run_id: UUID of the close run reports were generated for.
        version_no: Report run version number.
        excel_generated: Whether the Excel report pack was built successfully.
        pdf_generated: Whether the PDF report pack was built successfully.
        commentary_generated: Whether management commentary was generated.
        artifact_refs: List of storage references for generated artifacts.
        errors: Explicit error messages encountered during execution.
    """

    report_run_id: str
    close_run_id: str
    version_no: int
    excel_generated: bool
    pdf_generated: bool
    commentary_generated: bool
    artifact_refs: list[dict[str, Any]]
    errors: list[str]


def _run_report_generation_task(
    *,
    close_run_id: str,
    report_run_id: str | None = None,
    actor_user_id: str | None = None,
    sections: list[str] | None = None,
    generate_commentary_flag: bool = True,
    use_llm_commentary: bool = False,
    job_context: JobRuntimeContext,
) -> dict[str, Any]:
    """Execute the full report generation workflow for a close run.

    Args:
        close_run_id: UUID of the close run to generate reports for.
        report_run_id: Optional UUID of the pre-created report run. When the
            API creates the report run before dispatch, this ID is provided.
            When the task runs standalone (e.g., retry), a new run is created.
        actor_user_id: Optional UUID of the user who triggered generation.
        sections: Optional list of section keys to include. If None, all.
        generate_commentary_flag: Whether to generate commentary drafts.
        use_llm_commentary: Whether to attempt LLM-enhanced commentary.

    Returns:
        Dictionary with report generation receipt data.
    """

    parsed_close_run_id = UUID(close_run_id)
    parsed_report_run_id = UUID(report_run_id) if report_run_id else None
    parsed_actor_user_id = UUID(actor_user_id) if actor_user_id else None
    errors: list[str] = []

    logger.info(
        "Starting report generation for close run %s",
        close_run_id,
    )

    with get_session_factory()() as db:
        repo = ReportRepository(db_session=db)
        run_record: Any | None = None

        def fail_known_report_run(failure_reason: str) -> None:
            """Mark a pre-created report run failed when cancellation happens before resolution."""

            if run_record is not None or parsed_report_run_id is None:
                return
            try:
                repo.rollback()
                repo.update_report_run_status(
                    report_run_id=parsed_report_run_id,
                    status=ReportRunStatus.FAILED,
                    failure_reason=_report_failure_reason_for_cancellation(failure_reason),
                    completed_at=utc_now(),
                )
                repo.commit()
            except Exception:
                repo.rollback()

        def ensure_reporting_phase() -> None:
            try:
                job_context.ensure_not_canceled()
                ensure_close_run_active_phase(
                    session=db,
                    close_run_id=parsed_close_run_id,
                    required_phase=WorkflowPhase.REPORTING,
                )
            except JobCancellationRequestedError as error:
                fail_known_report_run(error.message)
                raise

        # Phase 1: Load close run and entity context
        context = _load_report_context(
            db=db,
            close_run_id=parsed_close_run_id,
        )
        job_context.checkpoint(
            step="load_report_context",
            state={"close_run_id": close_run_id, "report_run_id": report_run_id or ""},
        )
        ensure_reporting_phase()
        if context is None:
            error_msg = f"Close run {close_run_id} or associated data not found."
            logger.error("report_context_load_failed", close_run_id=close_run_id)
            errors.append(error_msg)
            receipt = ReportGenerationReceipt(
                report_run_id="",
                close_run_id=close_run_id,
                version_no=0,
                excel_generated=False,
                pdf_generated=False,
                commentary_generated=False,
                artifact_refs=[],
                errors=errors,
            )
            return _report_generation_receipt_to_payload(receipt)

        # Phase 2: Resolve or create report run record.
        # When the API creates the run before dispatch, use that existing record.
        # Otherwise, create a new one (e.g., manual retry or direct invocation).
        if job_context.step_completed("resolve_report_run"):
            run_record, version_no = _restore_report_run_resolution(
                repo=repo,
                close_run_id=parsed_close_run_id,
                job_context=job_context,
            )
        else:
            if parsed_report_run_id is not None:
                run_record = repo.get_report_run(
                    report_run_id=parsed_report_run_id,
                    close_run_id=parsed_close_run_id,
                )
                if run_record is None:
                    error_msg = (
                        f"Pre-created report run {report_run_id} not found. "
                        "Falling back to new report run creation."
                    )
                    logger.warning("report_run_not_found_using_fallback", error=error_msg)
                    errors.append(error_msg)
                    run_record = None

            if run_record is None:
                version_no = repo.next_version_no_for_close_run(
                    close_run_id=parsed_close_run_id,
                )
                run_record = repo.create_report_run(
                    close_run_id=parsed_close_run_id,
                    template_id=context.template_id,
                    version_no=version_no,
                    status=ReportRunStatus.GENERATING,
                    generation_config={
                        "sections": sections or [],
                        "generate_commentary": generate_commentary_flag,
                        "use_llm_commentary": use_llm_commentary,
                    },
                    generated_by_user_id=parsed_actor_user_id,
                )
            else:
                version_no = run_record.version_no
                # Transition existing record to generating state.
                repo.update_report_run_status(
                    report_run_id=run_record.id,
                    status=ReportRunStatus.GENERATING,
                )
            job_context.checkpoint(
                step="resolve_report_run",
                state={
                    "report_run_id": str(run_record.id),
                    "version_no": version_no,
                },
            )

        artifact_refs: list[dict[str, Any]] = []
        excel_generated = False
        pdf_generated = False
        commentary_generated = False

        try:
            # Phase 3: Gather section data from database
            section_data = _gather_section_data(
                db=db,
                close_run_id=parsed_close_run_id,
                sections=sections,
            )
            job_context.checkpoint(
                step="gather_report_sections",
                state={"section_count": len(section_data)},
            )
            ensure_reporting_phase()

            # Phase 4: Generate commentary if requested
            commentary: dict[str, str] = {}
            if generate_commentary_flag:
                if job_context.step_completed("generate_commentary"):
                    commentary = _restore_commentary_checkpoint(job_context=job_context)
                    commentary_generated = len(commentary) > 0
                else:
                    commentary_result = _generate_commentary_phase(
                        context=context,
                        section_data=section_data,
                        use_llm=use_llm_commentary,
                    )
                    commentary = commentary_result.commentary
                    commentary_generated = commentary_result.sections_generated > 0
                    errors.extend(commentary_result.errors)

                    # Persist commentary drafts to the database
                    ensure_reporting_phase()
                    _persist_commentary_drafts(
                        repo=repo,
                        report_run_id=run_record.id,
                        commentary=commentary,
                        actor_user_id=parsed_actor_user_id,
                    )
                    job_context.checkpoint(
                        step="generate_commentary",
                        state={"commentary": commentary},
                    )
                    ensure_reporting_phase()

            # Phase 5: Build Excel report pack
            scope = CloseRunStorageScope(
                entity_id=context.entity_id,
                close_run_id=parsed_close_run_id,
                period_start=context.period_start,
                period_end=context.period_end,
                close_run_version_no=version_no,
            )
            storage_repo = StorageRepository()
            if job_context.step_completed("build_excel_pack"):
                artifact_refs.append(
                    _restore_artifact_checkpoint(
                        job_context=job_context,
                        step="build_excel_pack",
                    )
                )
                excel_generated = True
            else:
                excel_result = _build_excel_report(
                    context=context,
                    section_data=section_data,
                    commentary=commentary,
                )
                excel_generated = True

                # Phase 6: Upload Excel artifact to MinIO
                ensure_reporting_phase()
                excel_artifact = storage_repo.store_artifact(
                    scope=scope,
                    artifact_type=ArtifactType.REPORT_EXCEL,
                    idempotency_key=f"{close_run_id}:excel:v{version_no}",
                    filename=excel_result.filename,
                    payload=excel_result.payload,
                    content_type=excel_result.content_type,
                )
                excel_artifact_ref = {
                    "type": "report_excel",
                    "filename": excel_result.filename,
                    "storage_key": excel_artifact.reference.object_key,
                    "bucket_kind": excel_artifact.reference.bucket_kind.value,
                    "sha256": excel_artifact.sha256_checksum,
                    "size_bytes": excel_artifact.size_bytes,
                }
                artifact_refs.append(excel_artifact_ref)
                job_context.checkpoint(
                    step="build_excel_pack",
                    state={"artifact_ref": excel_artifact_ref},
                )
                ensure_reporting_phase()

            # Phase 7: Build PDF report pack
            if job_context.step_completed("build_pdf_pack"):
                artifact_refs.append(
                    _restore_artifact_checkpoint(
                        job_context=job_context,
                        step="build_pdf_pack",
                    )
                )
                pdf_generated = True
            else:
                pdf_result = _build_pdf_report(
                    context=context,
                    section_data=section_data,
                    commentary=commentary,
                )
                pdf_generated = True

                # Phase 8: Upload PDF artifact to MinIO
                ensure_reporting_phase()
                pdf_artifact = storage_repo.store_artifact(
                    scope=scope,
                    artifact_type=ArtifactType.REPORT_PDF,
                    idempotency_key=f"{close_run_id}:pdf:v{version_no}",
                    filename=pdf_result.filename,
                    payload=pdf_result.payload,
                    content_type=pdf_result.content_type,
                )
                pdf_artifact_ref = {
                    "type": "report_pdf",
                    "filename": pdf_result.filename,
                    "storage_key": pdf_artifact.reference.object_key,
                    "bucket_kind": pdf_artifact.reference.bucket_kind.value,
                    "sha256": pdf_artifact.sha256_checksum,
                    "size_bytes": pdf_artifact.size_bytes,
                }
                artifact_refs.append(pdf_artifact_ref)
                job_context.checkpoint(
                    step="build_pdf_pack",
                    state={"artifact_ref": pdf_artifact_ref},
                )
                ensure_reporting_phase()

            # Phase 9: Update report run status to completed.
            # artifact_refs is a JSON array — persist it as-is via JSONB.
            if not job_context.step_completed("finalize_report_run"):
                ensure_reporting_phase()
                repo.update_report_run_status(
                    report_run_id=run_record.id,
                    status=ReportRunStatus.COMPLETED,
                    artifact_refs=artifact_refs,
                    completed_at=utc_now(),
                )
                repo.commit()
                job_context.checkpoint(
                    step="finalize_report_run",
                    state={"artifact_count": len(artifact_refs)},
                )

        except JobCancellationRequestedError as error:
            repo.rollback()
            try:
                repo.update_report_run_status(
                    report_run_id=run_record.id,
                    status=ReportRunStatus.FAILED,
                    failure_reason=_report_failure_reason_for_cancellation(error.message),
                    artifact_refs=artifact_refs if artifact_refs else None,
                    completed_at=utc_now(),
                )
                repo.commit()
            except Exception:
                repo.rollback()
            raise
        except Exception as exc:
            error_msg = f"Report generation failed: {exc}"
            logger.exception("report_generation_failed", close_run_id=close_run_id)
            errors.append(error_msg)

            # Update report run status to failed
            try:
                repo.update_report_run_status(
                    report_run_id=run_record.id,
                    status=ReportRunStatus.FAILED,
                    failure_reason=error_msg,
                    artifact_refs=artifact_refs if artifact_refs else None,
                    completed_at=utc_now(),
                )
                repo.commit()
            except Exception:
                repo.rollback()

    receipt = ReportGenerationReceipt(
        report_run_id=str(run_record.id),
        close_run_id=close_run_id,
        version_no=version_no,
        excel_generated=excel_generated,
        pdf_generated=pdf_generated,
        commentary_generated=commentary_generated,
        artifact_refs=artifact_refs,
        errors=errors,
    )
    return _report_generation_receipt_to_payload(receipt)


def _restore_report_run_resolution(
    *,
    repo: ReportRepository,
    close_run_id: UUID,
    job_context: JobRuntimeContext,
) -> tuple[Any, int]:
    """Restore the already-created report run from checkpoint state during resume."""

    checkpoint_state = job_context.step_state("resolve_report_run")
    checkpoint_report_run_id = checkpoint_state.get("report_run_id")
    if not isinstance(checkpoint_report_run_id, str):
        raise RuntimeError("Report resume requires a persisted report_run_id checkpoint value.")

    run_record = repo.get_report_run(
        report_run_id=UUID(checkpoint_report_run_id),
        close_run_id=close_run_id,
    )
    if run_record is None:
        raise RuntimeError(
            "Report resume could not load the previously created report run from checkpoint state."
        )

    return run_record, int(checkpoint_state["version_no"])


def _restore_commentary_checkpoint(*, job_context: JobRuntimeContext) -> dict[str, str]:
    """Restore commentary drafts from checkpoint state during resume."""

    checkpoint_state = job_context.step_state("generate_commentary")
    raw_commentary = checkpoint_state.get("commentary", {})
    if not isinstance(raw_commentary, dict):
        return {}

    return {str(key): str(value) for key, value in raw_commentary.items()}


def _restore_artifact_checkpoint(
    *,
    job_context: JobRuntimeContext,
    step: str,
) -> dict[str, Any]:
    """Restore one uploaded artifact reference from checkpoint state during resume."""

    checkpoint_state = job_context.step_state(step)
    raw_artifact_ref = checkpoint_state.get("artifact_ref")
    if not isinstance(raw_artifact_ref, dict):
        raise RuntimeError(
            f"Report resume requires artifact_ref state for completed step '{step}'."
        )

    return dict(raw_artifact_ref)


def _report_generation_receipt_to_payload(receipt: ReportGenerationReceipt) -> dict[str, Any]:
    """Convert the slotted receipt dataclass into the JSON-safe task payload shape."""

    return {
        "report_run_id": receipt.report_run_id,
        "close_run_id": receipt.close_run_id,
        "version_no": receipt.version_no,
        "excel_generated": receipt.excel_generated,
        "pdf_generated": receipt.pdf_generated,
        "commentary_generated": receipt.commentary_generated,
        "artifact_refs": receipt.artifact_refs,
        "errors": receipt.errors,
    }


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReportContext:
    """Carry the loaded close run and entity context for report generation."""

    close_run_id: UUID
    entity_id: UUID
    entity_name: str
    period_start: date
    period_end: date
    currency_code: str
    template_id: UUID
    template_name: str


def _load_report_context(
    db: Any,
    close_run_id: UUID,
    template_id: UUID | None = None,
) -> ReportContext | None:
    """Load close run, entity, and template data from the database.

    Args:
        db: Active SQLAlchemy session.
        close_run_id: UUID of the close run.
        template_id: Optional explicit template UUID.

    Returns:
        ReportContext if all required data is found, else None.
    """

    close_run = db.query(CloseRun).filter(CloseRun.id == close_run_id).first()
    if close_run is None:
        return None

    entity = db.query(Entity).filter(Entity.id == close_run.entity_id).first()
    if entity is None:
        return None

    # Determine template to use
    if template_id is not None:
        template = (
            db.query(ReportTemplate)
            .filter(ReportTemplate.id == template_id)
            .first()
        )
    else:
        # Use the active template for the entity, or the global default
        template = (
            db.query(ReportTemplate)
            .filter(
                ReportTemplate.entity_id == close_run.entity_id,
                ReportTemplate.is_active.is_(True),
            )
            .first()
        )
        if template is None:
            template = (
                db.query(ReportTemplate)
                .filter(
                    ReportTemplate.entity_id.is_(None),
                    ReportTemplate.is_active.is_(True),
                )
                .first()
            )

    if template is None:
        return None

    # Determine currency
    currency = "NGN"  # Default to Nigerian Naira
    if hasattr(entity, "base_currency") and entity.base_currency:
        currency = entity.base_currency

    period_start = close_run.period_start
    if isinstance(period_start, str):
        period_start = date.fromisoformat(period_start)
    period_end = close_run.period_end
    if isinstance(period_end, str):
        period_end = date.fromisoformat(period_end)

    return ReportContext(
        close_run_id=close_run.id,
        entity_id=entity.id,
        entity_name=entity.name,
        period_start=period_start,
        period_end=period_end,
        currency_code=currency,
        template_id=template.id,
        template_name=template.name,
    )


# ---------------------------------------------------------------------------
# Section data gathering
# ---------------------------------------------------------------------------

def _gather_section_data(
    db: Any,
    close_run_id: UUID,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """Gather numerical data for all requested report sections.

    Args:
        db: Active SQLAlchemy session.
        close_run_id: UUID of the close run.
        sections: Optional list of section keys to include.

    Returns:
        Dictionary with data keyed by section identifier.
    """

    from services.reporting.section_data import gather_report_section_data

    section_data = gather_report_section_data(
        session=db,
        close_run_id=close_run_id,
        sections=sections,
    )
    return {
        'p_and_l': section_data.get(ReportSectionKey.PROFIT_AND_LOSS.value, {}),
        'balance_sheet': section_data.get(ReportSectionKey.BALANCE_SHEET.value, {}),
        'cash_flow': section_data.get(ReportSectionKey.CASH_FLOW.value, {}),
        'budget_variance': section_data.get(ReportSectionKey.BUDGET_VARIANCE.value, {}),
        'kpi_dashboard': section_data.get(ReportSectionKey.KPI_DASHBOARD.value, {}),
    }


# ---------------------------------------------------------------------------
# Commentary generation
# ---------------------------------------------------------------------------

def _generate_commentary_phase(
    *,
    context: ReportContext,
    section_data: dict[str, Any],
    use_llm: bool = False,
) -> CommentaryGenerationResult:
    """Execute the commentary generation phase.

    Args:
        context: Loaded close run context.
        section_data: Section numerical data for commentary generation.
        use_llm: Whether to attempt LLM-enhanced commentary.

    Returns:
        CommentaryGenerationResult with generated commentary.
    """

    from services.reporting.commentary import (
        CommentaryGenerationInput,
        generate_commentary,
    )

    input_data = CommentaryGenerationInput(
        close_run_id=context.close_run_id,
        entity_name=context.entity_name,
        period_start=context.period_start,
        period_end=context.period_end,
        currency_code=context.currency_code,
        p_and_l=section_data.get('p_and_l', {}),
        balance_sheet=section_data.get('balance_sheet', {}),
        cash_flow=section_data.get('cash_flow', {}),
        budget_variance=section_data.get('budget_variance', {}),
        kpi_dashboard=section_data.get('kpi_dashboard', {}),
        use_llm=use_llm,
    )

    return generate_commentary(input_data)


def _persist_commentary_drafts(
    *,
    repo: ReportRepository,
    report_run_id: UUID,
    commentary: dict[str, str],
    actor_user_id: UUID | None,
) -> None:
    """Persist generated commentary as draft rows in the database.

    Args:
        repo: Report repository for persistence.
        report_run_id: UUID of the report run.
        commentary: Generated commentary text by section key.
        actor_user_id: Optional UUID of the user who triggered generation.
    """

    for section_key, body in commentary.items():
        if not body.strip():
            continue

        repo.create_commentary(
            report_run_id=report_run_id,
            section_key=section_key,
            status=CommentaryStatus.DRAFT,
            body=body,
            authored_by_user_id=actor_user_id,
        )


# ---------------------------------------------------------------------------
# Excel and PDF building
# ---------------------------------------------------------------------------

def _build_excel_report(
    *,
    context: ReportContext,
    section_data: dict[str, Any],
    commentary: dict[str, str],
) -> ExcelReportResult:
    """Build the Excel report pack.

    Args:
        context: Loaded close run context.
        section_data: Section numerical data.
        commentary: Generated commentary text.

    Returns:
        ExcelReportResult with generated Excel bytes.
    """

    from services.reporting.excel_builder import (
        ExcelReportInput,
        build_excel_report_pack,
    )

    input_data = ExcelReportInput(
        close_run_id=context.close_run_id,
        entity_name=context.entity_name,
        period_start=context.period_start,
        period_end=context.period_end,
        currency_code=context.currency_code,
        p_and_l=section_data.get('p_and_l', {}),
        balance_sheet=section_data.get('balance_sheet', {}),
        cash_flow=section_data.get('cash_flow', {}),
        budget_variance=section_data.get('budget_variance', {}),
        kpi_dashboard=section_data.get('kpi_dashboard', {}),
        commentary=commentary,
        generated_at=utc_now(),
    )

    return build_excel_report_pack(input_data)


def _build_pdf_report(
    *,
    context: ReportContext,
    section_data: dict[str, Any],
    commentary: dict[str, str],
) -> PdfReportResult:
    """Build the PDF report pack.

    Args:
        context: Loaded close run context.
        section_data: Section numerical data.
        commentary: Generated commentary text.

    Returns:
        PdfReportResult with generated PDF bytes.
    """

    from services.reporting.pdf_builder import (
        PdfReportInput,
        build_pdf_report_pack,
    )

    input_data = PdfReportInput(
        close_run_id=context.close_run_id,
        entity_name=context.entity_name,
        period_start=context.period_start,
        period_end=context.period_end,
        currency_code=context.currency_code,
        p_and_l=section_data.get('p_and_l', {}),
        balance_sheet=section_data.get('balance_sheet', {}),
        cash_flow=section_data.get('cash_flow', {}),
        budget_variance=section_data.get('budget_variance', {}),
        kpi_dashboard=section_data.get('kpi_dashboard', {}),
        commentary=commentary,
        generated_at=utc_now(),
    )

    return build_pdf_report_pack(input_data)


def _report_failure_reason_for_cancellation(message: str) -> str:
    """Return the report-run failure reason shown for operator cancellations."""

    if "operator requested cancellation" in message:
        return "Report generation was canceled by an operator."
    return message


# ---------------------------------------------------------------------------
# Celery task registration
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    base=TrackedJobTask,
    name=TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK.value,
    autoretry_for=(),
    retry_backoff=False,
    retry_jitter=False,
    max_retries=resolve_task_route(TaskName.REPORTING_GENERATE_CLOSE_RUN_PACK).max_retries,
)
def generate_reports(
    self: TrackedJobTask,
    *,
    close_run_id: str,
    report_run_id: str | None = None,
    actor_user_id: str | None = None,
    sections: list[str] | None = None,
    generate_commentary_flag: bool = True,
    use_llm_commentary: bool = False,
) -> dict[str, Any]:
    """Execute report generation under the canonical checkpointed job wrapper."""

    return self.run_tracked_job(
        runner=lambda job_context: _run_report_generation_task(
            close_run_id=close_run_id,
            report_run_id=report_run_id,
            actor_user_id=actor_user_id,
            sections=sections,
            generate_commentary_flag=generate_commentary_flag,
            use_llm_commentary=use_llm_commentary,
            job_context=job_context,
        )
    )


__all__ = [
    "ReportGenerationReceipt",
    "_build_excel_report",
    "_build_pdf_report",
    "_gather_section_data",
    "_generate_commentary_phase",
    "_load_report_context",
    "_persist_commentary_drafts",
    "_run_report_generation_task",
    "generate_reports",
]
