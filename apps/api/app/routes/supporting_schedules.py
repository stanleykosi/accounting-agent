"""
Purpose: Expose standalone Step 6 supporting-schedule workpaper routes.
Scope: Read the schedule workspace, create/update/delete schedule rows, and
       transition schedule review status within a close run.
Dependencies: FastAPI, request-scoped auth helpers, close-run access checks,
       supporting-schedule contracts and service, and workflow phase guards.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.close_runs import _to_entity_user
from apps.api.app.routes.request_auth import RequestAuthDependency
from apps.api.app.routes.workflow_phase import require_active_close_run_phase
from fastapi import APIRouter, Depends, HTTPException, status
from services.auth.service import serialize_uuid
from services.common.enums import SupportingScheduleStatus, SupportingScheduleType, WorkflowPhase
from services.contracts.supporting_schedule_models import (
    SupportingScheduleDetail,
    SupportingScheduleRowMutationResult,
    SupportingScheduleRowSummary,
    SupportingScheduleSummary,
    SupportingScheduleWorkspaceResponse,
    UpdateSupportingScheduleStatusRequest,
    UpsertSupportingScheduleRowRequest,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.supporting_schedule_repo import (
    SupportingScheduleRepository,
    SupportingScheduleRowRecord,
)
from services.supporting_schedules.service import (
    SupportingScheduleService,
    SupportingScheduleServiceError,
    SupportingScheduleSnapshot,
)

SCHEDULES_PREFIX = "/entities/{entity_id}/close-runs/{close_run_id}"
SCHEDULES_TAG = "supporting_schedules"
router = APIRouter(prefix=SCHEDULES_PREFIX, tags=[SCHEDULES_TAG])

DbSessionDep = Annotated[DatabaseSessionDependency, Depends()]


def _get_schedule_service(
    db_session: DatabaseSessionDependency,
) -> SupportingScheduleService:
    return SupportingScheduleService(
        repository=SupportingScheduleRepository(session=db_session),
    )


SupportingScheduleServiceDependency = Annotated[
    SupportingScheduleService, Depends(_get_schedule_service)
]


def _require_close_run_access(
    *,
    entity_id: UUID,
    close_run_id: UUID,
    user_id: UUID,
    db_session: DatabaseSessionDependency,
) -> None:
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


@router.get(
    "/supporting-schedules",
    response_model=SupportingScheduleWorkspaceResponse,
    summary="Read the Step 6 supporting-schedule workspace for one close run",
)
def read_supporting_schedule_workspace(
    entity_id: UUID,
    close_run_id: UUID,
    schedule_service: SupportingScheduleServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> SupportingScheduleWorkspaceResponse:
    """Return all standalone Step 6 schedules and their current row state."""

    session_result = auth_context
    _require_close_run_access(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        db_session=db_session,
    )
    workspace = schedule_service.list_workspace(close_run_id=close_run_id)
    return SupportingScheduleWorkspaceResponse(
        schedules=tuple(_build_schedule_detail(snapshot) for snapshot in workspace),
    )


@router.post(
    "/supporting-schedules/{schedule_type}/rows",
    response_model=SupportingScheduleRowMutationResult,
    summary="Create or update one Step 6 supporting-schedule row",
)
def save_supporting_schedule_row(
    entity_id: UUID,
    close_run_id: UUID,
    schedule_type: SupportingScheduleType,
    payload: UpsertSupportingScheduleRowRequest,
    schedule_service: SupportingScheduleServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> SupportingScheduleRowMutationResult:
    """Create or update one row in a standalone Step 6 supporting schedule."""

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
        action_label="Supporting schedule maintenance",
        db_session=db_session,
    )
    try:
        snapshot = schedule_service.save_row(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
            row_id=UUID(payload.row_id) if payload.row_id is not None else None,
            payload=payload.payload.model_dump(exclude_none=True),
        )
    except SupportingScheduleServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "supporting_schedule_invalid",
                "message": str(error),
            },
        ) from error
    _emit_schedule_event(
        db_session=db_session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        actor_user_id=session_result.user.id,
        event_type="supporting_schedule.row_saved",
        schedule_type=schedule_type,
        payload={
            "schedule_type": schedule_type.value,
            "row_id": payload.row_id,
            "row_count": len(snapshot.rows),
        },
    )
    db_session.commit()
    return SupportingScheduleRowMutationResult(schedule=_build_schedule_detail(snapshot))


@router.delete(
    "/supporting-schedules/{schedule_type}/rows/{row_id}",
    response_model=SupportingScheduleRowMutationResult,
    summary="Delete one supporting-schedule row",
)
def delete_supporting_schedule_row(
    entity_id: UUID,
    close_run_id: UUID,
    schedule_type: SupportingScheduleType,
    row_id: UUID,
    schedule_service: SupportingScheduleServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> SupportingScheduleRowMutationResult:
    """Delete one persisted Step 6 workpaper row and refresh the schedule state."""

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
        action_label="Supporting schedule maintenance",
        db_session=db_session,
    )
    try:
        snapshot = schedule_service.delete_row(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
            row_id=row_id,
        )
    except SupportingScheduleServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "supporting_schedule_row_not_found",
                "message": str(error),
            },
        ) from error
    _emit_schedule_event(
        db_session=db_session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        actor_user_id=session_result.user.id,
        event_type="supporting_schedule.row_deleted",
        schedule_type=schedule_type,
        payload={
            "schedule_type": schedule_type.value,
            "row_id": str(row_id),
            "row_count": len(snapshot.rows),
        },
    )
    db_session.commit()
    return SupportingScheduleRowMutationResult(schedule=_build_schedule_detail(snapshot))


@router.post(
    "/supporting-schedules/{schedule_type}/status",
    response_model=SupportingScheduleRowMutationResult,
    summary="Update one supporting-schedule review status",
)
def update_supporting_schedule_status(
    entity_id: UUID,
    close_run_id: UUID,
    schedule_type: SupportingScheduleType,
    payload: UpdateSupportingScheduleStatusRequest,
    schedule_service: SupportingScheduleServiceDependency,
    db_session: DbSessionDep,
    auth_context: RequestAuthDependency,
) -> SupportingScheduleRowMutationResult:
    """Finalize or reopen one Step 6 supporting schedule."""

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
        action_label="Supporting schedule review",
        db_session=db_session,
    )
    try:
        snapshot = schedule_service.update_status(
            close_run_id=close_run_id,
            schedule_type=schedule_type,
            status=SupportingScheduleStatus(payload.status),
            note=payload.note,
            actor_user_id=session_result.user.id,
        )
    except SupportingScheduleServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "supporting_schedule_status_invalid",
                "message": str(error),
            },
        ) from error
    _emit_schedule_event(
        db_session=db_session,
        entity_id=entity_id,
        close_run_id=close_run_id,
        actor_user_id=session_result.user.id,
        event_type="supporting_schedule.status_updated",
        schedule_type=schedule_type,
        payload={
            "schedule_type": schedule_type.value,
            "status": snapshot.schedule.status.value,
            "note": snapshot.schedule.note,
        },
    )
    db_session.commit()
    return SupportingScheduleRowMutationResult(schedule=_build_schedule_detail(snapshot))


def _emit_schedule_event(
    *,
    db_session: DatabaseSessionDependency,
    entity_id: UUID,
    close_run_id: UUID,
    actor_user_id: UUID,
    event_type: str,
    schedule_type: SupportingScheduleType,
    payload: dict[str, object],
) -> None:
    CloseRunRepository(db_session=db_session).create_activity_event(
        entity_id=entity_id,
        close_run_id=close_run_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        source_surface=AuditSourceSurface.DESKTOP,
        payload={
            "schedule_type": schedule_type.value,
            **payload,
        },
        trace_id=None,
    )


def _build_schedule_detail(snapshot: SupportingScheduleSnapshot) -> SupportingScheduleDetail:
    return SupportingScheduleDetail(
        schedule=SupportingScheduleSummary(
            id=serialize_uuid(snapshot.schedule.id),
            close_run_id=serialize_uuid(snapshot.schedule.close_run_id),
            schedule_type=snapshot.schedule.schedule_type,
            label=snapshot.schedule.schedule_type.label,
            status=snapshot.schedule.status,
            row_count=len(snapshot.rows),
            note=snapshot.schedule.note,
            reviewed_by_user_id=serialize_uuid(snapshot.schedule.reviewed_by_user_id)
            if snapshot.schedule.reviewed_by_user_id is not None
            else None,
            reviewed_at=snapshot.schedule.reviewed_at,
            updated_at=_resolve_schedule_updated_at(snapshot),
        ),
        rows=tuple(
            _build_row_summary(snapshot.schedule.schedule_type, snapshot.schedule.id, row)
            for row in snapshot.rows
        ),
    )


def _build_row_summary(
    schedule_type: SupportingScheduleType,
    schedule_id: UUID,
    row: SupportingScheduleRowRecord,
) -> SupportingScheduleRowSummary:
    return SupportingScheduleRowSummary(
        id=serialize_uuid(row.id),
        schedule_id=serialize_uuid(schedule_id),
        schedule_type=schedule_type,
        row_ref=row.row_ref,
        line_no=row.line_no,
        payload=dict(row.payload),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _resolve_schedule_updated_at(snapshot: SupportingScheduleSnapshot):
    if not snapshot.rows:
        return snapshot.schedule.updated_at
    return max(snapshot.schedule.updated_at, *(row.updated_at for row in snapshot.rows))

