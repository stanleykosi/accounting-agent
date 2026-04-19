"""
Purpose: Expose API routes for reconciliation review workflows.
Scope: List reconciliations, list items, trial balance, anomalies, reviewer disposition,
       bulk disposition, anomaly resolution, and reconciliation approval for a close run.
Dependencies: FastAPI, local-auth session helpers, reconciliation contracts and services,
       and the shared DB dependency.

Design notes:
- Every route authorizes the caller against the entity workspace before proceeding.
- All nested resource lookups (reconciliations, items, anomalies) are scoped to the
  path-level close_run_id to prevent cross-close-run resource access.
- All mutations call db_session.commit() explicitly before returning, matching the
  pattern used by recommendation mutation routes.
- Disposition actions are passed as enum objects to the service layer, not raw strings.
- Reviewer dispositions require explicit reasoning for audit traceability.
- Approval checks for pending dispositions and blocks if unresolved items remain.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.dependencies.tasks import TaskDispatcherDependency
from apps.api.app.routes.auth import (
    get_auth_service,
)
from apps.api.app.routes.close_runs import _to_entity_user
from apps.api.app.routes.recommendations import (
    _require_authenticated_browser_session,
)
from apps.api.app.routes.request_auth import RequestAuthDependency
from apps.api.app.routes.workflow_phase import require_active_close_run_phase
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from services.auth.service import (
    AuthService,
)
from services.common.enums import (
    DEFAULT_RECONCILIATION_EXECUTION_TYPES,
    MatchStatus,
    ReconciliationSourceType,
    ReconciliationStatus,
    ReconciliationType,
    SupportingScheduleStatus,
    WorkflowPhase,
)
from services.common.settings import AppSettings, get_settings
from services.contracts.reconciliation_models import (
    ApproveReconciliationRequest,
    ApproveReconciliationResult,
    BulkDispositionRequest,
    DispositionItemRequest,
    DispositionResult,
    ReconciliationAnomalyListResponse,
    ReconciliationAnomalySummary,
    ReconciliationItemListResponse,
    ReconciliationItemMatch,
    ReconciliationItemSummary,
    ReconciliationListResponse,
    ReconciliationRunResponse,
    ReconciliationSummary,
    ResolveAnomalyRequest,
    TrialBalanceAccountEntry,
    TrialBalanceDetailResponse,
    TrialBalanceSnapshotSummary,
)
from services.db.models.documents import Document, DocumentType
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.entity_repo import EntityUserRecord
from services.db.repositories.reconciliation_repo import (
    ReconciliationAnomalyRecord,
    ReconciliationItemRecord,
    ReconciliationRecord,
    ReconciliationRepository,
)
from services.db.repositories.supporting_schedule_repo import SupportingScheduleRepository
from services.jobs.service import JobService, JobServiceError
from services.jobs.task_names import TaskName
from services.ledger.effective_ledger import (
    load_close_run_ledger_binding,
    load_effective_ledger_transactions,
)
from services.reconciliation.applicability import (
    BANK_RECONCILIATION_LEDGER_GUIDANCE,
    NO_APPLICABLE_RECONCILIATION_WORK_MESSAGE,
    is_bank_reconciliation_applicable,
    is_trial_balance_applicable,
)
from services.reconciliation.service import ReconciliationService
from services.supporting_schedules.service import SupportingScheduleService
from sqlalchemy import func, select

RECONCILIATION_TAG = "reconciliation"
REC_PREFIX = "/entities/{entity_id}/close-runs/{close_run_id}"
router = APIRouter(prefix=REC_PREFIX, tags=[RECONCILIATION_TAG])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
DbSessionDep = DatabaseSessionDependency


def _get_reconciliation_service(
    db_session: DatabaseSessionDependency,
) -> ReconciliationService:
    """Construct the canonical reconciliation service from request-scoped persistence."""
    repository = ReconciliationRepository(session=db_session)
    return ReconciliationService(repository=repository)


ReconciliationServiceDependency = Annotated[
    ReconciliationService, Depends(_get_reconciliation_service)
]


class RunReconciliationRequest(BaseModel):
    """Capture an explicit request to execute reconciliation for a close run."""

    reconciliation_types: list[ReconciliationType] | None = Field(
        default=None,
        description="Optional reconciliation types. Defaults to all canonical types.",
    )


def _require_close_run_access(
    *,
    entity_id: UUID,
    close_run_id: UUID,
    user_id: UUID,
    db_session: DatabaseSessionDependency,
) -> tuple[EntityUserRecord, bool]:
    """Verify the user can access the entity and the close run belongs to it.

    Returns:
        A tuple of (entity_user_record, close_run_belongs_to_entity).
        The caller should check the boolean before returning data.
    """
    close_run_repo = CloseRunRepository(db_session=db_session)
    access = close_run_repo.get_close_run_for_user(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=user_id,
    )
    if access is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "access_denied",
                "message": "You do not have access to this close run.",
            },
        )
    return (
        EntityUserRecord(
            id=access.entity.id,
            email=access.entity.name,
            full_name=access.entity.name,
        ),
        True,
    )


def _resolve_requested_reconciliation_types(
    *,
    close_run_id: UUID,
    reconciliation_types: tuple[ReconciliationType, ...],
    db_session: DatabaseSessionDependency,
) -> tuple[tuple[ReconciliationType, ...], tuple[ReconciliationType, ...], str | None]:
    """Return applicable and skipped reconciliation types for the current close run."""

    approved_bank_statement_count = int(
        db_session.execute(
            select(func.count(Document.id)).where(
                Document.close_run_id == close_run_id,
                Document.document_type == DocumentType.BANK_STATEMENT.value,
                Document.status == "approved",
            )
        ).scalar_one()
    )
    effective_ledger_transaction_count = len(
        load_effective_ledger_transactions(db_session, close_run_id)
    )
    binding = load_close_run_ledger_binding(db_session, close_run_id)
    schedule_workspace = SupportingScheduleService(
        repository=SupportingScheduleRepository(session=db_session),
    ).list_workspace(close_run_id=close_run_id)
    started_schedule_types = {
        snapshot.schedule.schedule_type
        for snapshot in schedule_workspace
        if (
            len(snapshot.rows) > 0
            and snapshot.schedule.status is not SupportingScheduleStatus.NOT_APPLICABLE
        )
    }
    started_schedule_type_values = {schedule_type.value for schedule_type in started_schedule_types}

    applicable: list[ReconciliationType] = []
    skipped: list[ReconciliationType] = []
    messages: list[str] = []

    for reconciliation_type in reconciliation_types:
        if reconciliation_type is ReconciliationType.BANK_RECONCILIATION:
            if is_bank_reconciliation_applicable(
                approved_bank_statement_count=approved_bank_statement_count,
                effective_ledger_transaction_count=effective_ledger_transaction_count,
            ):
                applicable.append(reconciliation_type)
            else:
                skipped.append(reconciliation_type)
                if approved_bank_statement_count > 0:
                    messages.append(BANK_RECONCILIATION_LEDGER_GUIDANCE)
            continue

        if reconciliation_type in {
            ReconciliationType.FIXED_ASSETS,
            ReconciliationType.LOAN_AMORTISATION,
            ReconciliationType.ACCRUAL_TRACKER,
            ReconciliationType.BUDGET_VS_ACTUAL,
        }:
            if reconciliation_type.value in started_schedule_type_values:
                applicable.append(reconciliation_type)
            else:
                skipped.append(reconciliation_type)
            continue

        if reconciliation_type is ReconciliationType.TRIAL_BALANCE:
            if is_trial_balance_applicable(
                effective_ledger_transaction_count=effective_ledger_transaction_count,
                has_trial_balance_baseline=(
                    binding is not None and binding.trial_balance_import_batch_id is not None
                ),
            ):
                applicable.append(reconciliation_type)
            else:
                skipped.append(reconciliation_type)
            continue

        applicable.append(reconciliation_type)

    message: str | None = None
    if not applicable:
        unique_messages = tuple(dict.fromkeys(messages))
        message = (
            " ".join(unique_messages)
            if unique_messages
            else NO_APPLICABLE_RECONCILIATION_WORK_MESSAGE
        )
    elif messages:
        message = " ".join(tuple(dict.fromkeys(messages)))
    return tuple(applicable), tuple(skipped), message


# ---------------------------------------------------------------------------
# Reconciliation list and detail
# ---------------------------------------------------------------------------


@router.get(
    "/reconciliations",
    response_model=ReconciliationListResponse,
    summary="List reconciliation runs for one close run",
)
def list_reconciliations(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> ReconciliationListResponse:
    """Return reconciliation runs for an authenticated user's close run."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    records = reconciliation_service.list_reconciliations(close_run_id=close_run_id)
    return ReconciliationListResponse(
        reconciliations=tuple(_build_reconciliation_summary(rec) for rec in records)
    )


@router.post(
    "/reconciliations/run",
    response_model=ReconciliationRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue reconciliation execution for a close run",
)
def queue_reconciliation_run(
    entity_id: UUID,
    close_run_id: UUID,
    payload: RunReconciliationRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    db_session: DbSessionDep,
    task_dispatcher: TaskDispatcherDependency,
    auth_context: RequestAuthDependency,
) -> ReconciliationRunResponse:
    """Queue the canonical reconciliation execution workflow for this close run."""

    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.RECONCILIATION,
        action_label="Reconciliation execution",
        db_session=db_session,
    )

    reconciliation_types = payload.reconciliation_types or list(
        DEFAULT_RECONCILIATION_EXECUTION_TYPES
    )
    requested_types = tuple(reconciliation_types)
    applicable_types, skipped_types, message = _resolve_requested_reconciliation_types(
        close_run_id=close_run_id,
        reconciliation_types=requested_types,
        db_session=db_session,
    )
    if not applicable_types:
        return ReconciliationRunResponse(
            job_id=None,
            reconciliation_types=(),
            skipped_types=skipped_types,
            status="not_applicable",
            task_name="reconciliation.not_applicable",
            message=message or NO_APPLICABLE_RECONCILIATION_WORK_MESSAGE,
        )
    job_service = JobService(db_session=db_session)
    try:
        job = job_service.dispatch_job(
            dispatcher=task_dispatcher,
            task_name=TaskName.RECONCILIATION_EXECUTE_CLOSE_RUN,
            payload={
                "close_run_id": str(close_run_id),
                "reconciliation_types": [
                    reconciliation_type.value
                    for reconciliation_type in applicable_types
                ],
                "actor_user_id": str(session_result.user.id),
            },
            entity_id=entity_id,
            close_run_id=close_run_id,
            document_id=None,
            actor_user_id=session_result.user.id,
            trace_id=str(getattr(request.state, "request_id", "")),
        )
    except JobServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={
                "code": str(error.code),
                "message": error.message,
            },
        ) from error

    return ReconciliationRunResponse(
        job_id=str(job.id),
        task_name=job.task_name,
        status=job.status.value,
        reconciliation_types=applicable_types,
        skipped_types=skipped_types,
        message=message,
    )


@router.get(
    "/reconciliations/{reconciliation_id}/items",
    response_model=ReconciliationItemListResponse,
    summary="List reconciliation items with optional filters",
)
def list_reconciliation_items(
    entity_id: UUID,
    close_run_id: UUID,
    reconciliation_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
    match_status: MatchStatus | None = None,
    requires_disposition: bool | None = None,
) -> ReconciliationItemListResponse:
    """Return reconciliation items for a reconciliation run scoped to the close run."""
    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    # Verify the reconciliation belongs to this close run before listing its items
    rec = reconciliation_service._repo.get_reconciliation_for_close_run(
        reconciliation_id=reconciliation_id,
        close_run_id=close_run_id,
    )
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "reconciliation_not_found",
                "message": (
                    f"Reconciliation {reconciliation_id} not found for this close run."
                ),
            },
        )
    items = reconciliation_service.list_items(
        reconciliation_id=reconciliation_id,
        match_status=match_status,
        requires_disposition=requires_disposition,
    )
    return ReconciliationItemListResponse(
        items=tuple(_build_item_summary(item) for item in items)
    )


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------


@router.get(
    "/trial-balance",
    response_model=TrialBalanceDetailResponse,
    summary="Get the latest trial balance snapshot",
)
def get_trial_balance(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> TrialBalanceDetailResponse:
    """Return the most recent trial balance snapshot for the close run."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    snapshot = reconciliation_service.get_latest_trial_balance(close_run_id=close_run_id)
    if snapshot is None:
        return TrialBalanceDetailResponse(
            snapshot=TrialBalanceSnapshotSummary(
                id="00000000-0000-0000-0000-000000000000",
                close_run_id=str(close_run_id),
                snapshot_no=0,
                total_debits="0.00",
                total_credits="0.00",
                is_balanced=True,
                account_count=0,
                created_at="",
            ),
            accounts=[],
        )

    accounts = [
        TrialBalanceAccountEntry(
            account_code=entry["account_code"],
            account_name=entry["account_name"],
            account_type=entry["account_type"],
            debit_balance=str(entry.get("debit_balance", "0.00")),
            credit_balance=str(entry.get("credit_balance", "0.00")),
            net_balance=str(entry.get("net_balance", "0.00")),
            is_active=entry.get("is_active", True),
        )
        for entry in snapshot.account_balances
    ]

    return TrialBalanceDetailResponse(
        snapshot=TrialBalanceSnapshotSummary(
            id=str(snapshot.id),
            close_run_id=str(snapshot.close_run_id),
            snapshot_no=snapshot.snapshot_no,
            total_debits=str(snapshot.total_debits),
            total_credits=str(snapshot.total_credits),
            is_balanced=snapshot.is_balanced,
            account_count=len(accounts),
            generated_by_user_id=(
                str(snapshot.generated_by_user_id) if snapshot.generated_by_user_id else None
            ),
            created_at=snapshot.created_at.isoformat(),
        ),
        accounts=accounts,
    )


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


@router.get(
    "/anomalies",
    response_model=ReconciliationAnomalyListResponse,
    summary="List reconciliation anomalies",
)
def list_anomalies(
    entity_id: UUID,
    close_run_id: UUID,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
    severity: str | None = None,
    resolved: bool | None = None,
) -> ReconciliationAnomalyListResponse:
    """Return anomalies for the close run."""
    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    records = reconciliation_service.list_anomalies(
        close_run_id=close_run_id,
        severity=severity,
        resolved=resolved,
    )
    return ReconciliationAnomalyListResponse(
        anomalies=tuple(_build_anomaly_summary(rec) for rec in records)
    )


@router.post(
    "/anomalies/{anomaly_id}/resolve",
    response_model=ReconciliationAnomalySummary,
    summary="Resolve a reconciliation anomaly",
)
def resolve_anomaly(
    entity_id: UUID,
    close_run_id: UUID,
    anomaly_id: UUID,
    payload: ResolveAnomalyRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
) -> ReconciliationAnomalySummary:
    """Mark an anomaly as resolved with reviewer reasoning."""
    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.RECONCILIATION,
        action_label="Anomaly resolution",
        db_session=db_session,
    )
    result = reconciliation_service.resolve_anomaly(
        anomaly_id=anomaly_id,
        close_run_id=close_run_id,
        resolution_note=payload.resolution_note,
        user_id=session_result.user.id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "anomaly_not_found", "message": f"Anomaly {anomaly_id} not found."},
        )
    db_session.commit()
    return _build_anomaly_summary(result)


# ---------------------------------------------------------------------------
# Reviewer disposition
# ---------------------------------------------------------------------------


@router.post(
    "/items/{item_id}/disposition",
    response_model=DispositionResult,
    summary="Record reviewer disposition for one reconciliation item",
)
def disposition_item(
    entity_id: UUID,
    close_run_id: UUID,
    item_id: UUID,
    payload: DispositionItemRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
) -> DispositionResult:
    """Record a reviewer disposition for a reconciliation item."""
    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.RECONCILIATION,
        action_label="Reconciliation disposition",
        db_session=db_session,
    )
    result = reconciliation_service.disposition_item(
        item_id=item_id,
        close_run_id=close_run_id,
        disposition=payload.disposition,
        reason=payload.reason,
        user_id=session_result.user.id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "item_not_found",
                "message": f"Reconciliation item {item_id} not found for this close run.",
            },
        )
    db_session.commit()
    return DispositionResult(
        item_id=item_id,
        disposition=result.disposition if result.disposition else payload.disposition,
        requires_further_action=result.match_status
        in (MatchStatus.EXCEPTION, MatchStatus.UNMATCHED),
    )


@router.post(
    "/disposition/bulk",
    response_model=object,
    summary="Record bulk disposition for multiple reconciliation items",
)
def bulk_disposition_items(
    entity_id: UUID,
    close_run_id: UUID,
    payload: BulkDispositionRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
) -> dict[str, object]:
    """Record bulk dispositions for multiple reconciliation items."""
    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.RECONCILIATION,
        action_label="Bulk reconciliation disposition",
        db_session=db_session,
    )
    result = reconciliation_service.bulk_disposition_items(
        item_ids=payload.item_ids,
        close_run_id=close_run_id,
        disposition=payload.disposition,
        reason=payload.reason,
        user_id=session_result.user.id,
    )
    db_session.commit()
    return {
        "disposed_count": len(result.disposed_items),
        "failed_count": len(result.failed_item_ids),
        "failed_item_ids": [str(fid) for fid in result.failed_item_ids],
    }


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@router.post(
    "/reconciliations/{reconciliation_id}/approve",
    response_model=ApproveReconciliationResult,
    summary="Approve a reconciliation run",
)
def approve_reconciliation(
    entity_id: UUID,
    close_run_id: UUID,
    reconciliation_id: UUID,
    payload: ApproveReconciliationRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    reconciliation_service: ReconciliationServiceDependency,
    db_session: DbSessionDep,
) -> ApproveReconciliationResult:
    """Approve a reconciliation run after all required dispositions are recorded."""
    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        settings=settings,
        auth_service=auth_service,
    )
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    require_active_close_run_phase(
        actor_user=_to_entity_user(session_result),
        entity_id=entity_id,
        close_run_id=close_run_id,
        required_phase=WorkflowPhase.RECONCILIATION,
        action_label="Reconciliation approval",
        db_session=db_session,
    )
    result = reconciliation_service.approve_reconciliation(
        reconciliation_id=reconciliation_id,
        close_run_id=close_run_id,
        reason=payload.reason,
        user_id=session_result.user.id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "reconciliation_not_found",
                "message": f"Reconciliation {reconciliation_id} not found for this close run.",
            },
        )
    if result.status == ReconciliationStatus.BLOCKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "pending_dispositions",
                "message": result.blocking_reason or "Pending dispositions block approval.",
            },
        )
    db_session.commit()
    return ApproveReconciliationResult(
        reconciliation_id=reconciliation_id,
        status=result.status,
        approved_by_user_id=str(session_result.user.id),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_reconciliation_summary(record: ReconciliationRecord) -> ReconciliationSummary:
    """Build an API-ready reconciliation summary from a service record."""
    return ReconciliationSummary(
        id=str(record.id),
        close_run_id=str(record.close_run_id),
        reconciliation_type=record.reconciliation_type,
        status=record.status,
        summary=record.summary,
        blocking_reason=record.blocking_reason,
        approved_by_user_id=(
            str(record.approved_by_user_id) if record.approved_by_user_id else None
        ),
        created_by_user_id=(
            str(record.created_by_user_id) if record.created_by_user_id else None
        ),
        item_count=_summary_int(record.summary.get("total_items")),
        matched_count=_summary_int(record.summary.get("matched_count")),
        exception_count=_summary_int(record.summary.get("exception_count")),
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _build_item_summary(record: ReconciliationItemRecord) -> ReconciliationItemSummary:
    """Build an API-ready item summary from a service record."""
    matched_to = [
        ReconciliationItemMatch(
            source_type=_source_type(cp.get("source_type")),
            source_ref=str(cp.get("source_ref", "")),
            amount=str(cp["amount"]) if cp.get("amount") is not None else None,
            confidence=(
                float(cp["confidence"])
                if isinstance(cp.get("confidence"), (int, float, str))
                else None
            ),
        )
        for cp in (record.matched_to or [])
    ]
    return ReconciliationItemSummary(
        id=str(record.id),
        reconciliation_id=str(record.reconciliation_id),
        source_type=record.source_type,
        source_ref=record.source_ref,
        match_status=record.match_status,
        amount=str(record.amount),
        difference_amount=str(record.difference_amount),
        matched_to=matched_to,
        explanation=record.explanation,
        requires_disposition=record.requires_disposition,
        disposition=record.disposition,
        disposition_reason=record.disposition_reason,
        disposition_by_user_id=(
            str(record.disposition_by_user_id) if record.disposition_by_user_id else None
        ),
        dimensions=record.dimensions,
        period_date=record.period_date,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _build_anomaly_summary(record: ReconciliationAnomalyRecord) -> ReconciliationAnomalySummary:
    """Build an API-ready anomaly summary from a service record."""
    return ReconciliationAnomalySummary(
        id=str(record.id),
        close_run_id=str(record.close_run_id),
        anomaly_type=record.anomaly_type,
        severity=record.severity,
        account_code=record.account_code,
        description=record.description,
        details=record.details,
        resolved=record.resolved,
        resolved_by_user_id=(
            str(record.resolved_by_user_id) if record.resolved_by_user_id else None
        ),
        created_at=record.created_at.isoformat(),
    )


def _summary_int(value: object) -> int:
    """Coerce optional JSON summary counts into integers for API contracts."""

    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return 0


def _source_type(value: object) -> ReconciliationSourceType:
    """Resolve a reconciliation match source type from JSON metadata."""

    if isinstance(value, ReconciliationSourceType):
        return value
    if isinstance(value, str):
        try:
            return ReconciliationSourceType(value)
        except ValueError:
            return ReconciliationSourceType.LEDGER_TRANSACTION
    return ReconciliationSourceType.LEDGER_TRANSACTION
