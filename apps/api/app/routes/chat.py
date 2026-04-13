"""
Purpose: Expose grounded chat thread and message routes for the finance
copilot experience, including action routing for proposed edits, approvals,
and workflow assistance.
Scope: Thread creation, listing, detail reads, send-message with read-only
analysis responses, action intent classification, proposed edit approval/rejection,
and pending action plan listing.
Dependencies: FastAPI, auth session validation, chat contracts and services,
action router, proposed changes service, and the shared DB/settings dependencies.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.routes.auth import (
    _build_http_exception,
    _read_session_cookie,
    get_auth_service,
)
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.chat.grounding import ChatGroundingService
from services.chat.service import ChatService, ChatServiceError
from services.chat.action_router import ChatActionRouter, ChatActionRouterError
from services.chat.proposed_changes import ProposedChangesError, ProposedChangesService
from services.common.settings import AppSettings, get_settings
from services.contracts.chat_models import (
    ChatMessageResponse,
    ChatThreadListResponse,
    ChatThreadWithMessages,
    CreateChatThreadRequest,
    SendChatMessageRequest,
)
from services.chat.action_models import (
    ApproveChatActionRequest,
    ChatActionResponse,
    ChatActionSummary,
    RejectChatActionRequest,
    SendChatActionRequest,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.chat_repo import ChatRepository
from services.db.repositories.chat_action_repo import ChatActionRepository
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.entity_repo import EntityRepository
from services.model_gateway.client import ModelGateway

router = APIRouter(prefix="/chat", tags=["chat"])

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
EntityIdQuery = Annotated[UUID, Query(description="Entity workspace UUID.")]
CloseRunIdQuery = Annotated[UUID | None, Query(description="Optional close run UUID.")]
ThreadLimitQuery = Annotated[int, Query(ge=1, le=200, description="Maximum items.")]
MessageLimitQuery = Annotated[int, Query(ge=1, le=500, description="Maximum messages.")]


def get_chat_service(
    db_session: DatabaseSessionDependency,
    settings: SettingsDependency,
) -> ChatService:
    """Construct the canonical chat service from request-scoped persistence and settings."""

    entity_repo = EntityRepository(db_session=db_session)
    close_run_repo = CloseRunRepository(db_session=db_session)
    grounding_service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )
    model_gateway = ModelGateway()
    chat_repo = ChatRepository(db_session=db_session)

    return ChatService(
        repository=chat_repo,
        grounding_service=grounding_service,
        model_gateway=model_gateway,
        entity_repo=entity_repo,
    )


ChatServiceDependency = Annotated[ChatService, Depends(get_chat_service)]


def get_chat_action_router(
    db_session: DatabaseSessionDependency,
    settings: SettingsDependency,
) -> ChatActionRouter:
    """Construct the chat action router from request-scoped persistence and settings."""

    entity_repo = EntityRepository(db_session=db_session)
    close_run_repo = CloseRunRepository(db_session=db_session)
    grounding_service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )
    model_gateway = ModelGateway()
    chat_repo = ChatRepository(db_session=db_session)
    action_repo = ChatActionRepository(db_session=db_session)

    return ChatActionRouter(
        action_repository=action_repo,
        chat_repository=chat_repo,
        model_gateway=model_gateway,
        grounding_service=grounding_service,
        entity_repo=entity_repo,
    )


ChatActionRouterDependency = Annotated[ChatActionRouter, Depends(get_chat_action_router)]


def get_proposed_changes_service(
    db_session: DatabaseSessionDependency,
) -> ProposedChangesService:
    """Construct the proposed changes service from request-scoped persistence."""

    action_repo = ChatActionRepository(db_session=db_session)
    return ProposedChangesService(action_repository=action_repo)


ProposedChangesServiceDependency = Annotated[ProposedChangesService, Depends(get_proposed_changes_service)]


def _require_authenticated_browser_session(
    *,
    request: Request,
    auth_service: AuthService,
    settings: AppSettings,
) -> AuthenticatedSessionResult:
    """Validate the caller's session cookie and return the active auth context or raise 401."""

    cookie_value = _read_session_cookie(request=request, settings=settings)
    if cookie_value is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_error_payload(
                code=AuthErrorCode.NOT_AUTHENTICATED.value,
                message="Authentication is required to access chat threads.",
            ),
        )

    try:
        session_result = auth_service.validate_session(session_token=cookie_value)
    except AuthServiceError as error:
        raise _build_http_exception(error) from error

    return session_result


def _error_payload(*, code: str, message: str) -> dict[str, str]:
    """Build a deterministic error response body for generated SDK consumers."""

    return {"code": code, "message": message}


@router.post(
    "/threads",
    response_model=ChatThreadWithMessages,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new grounded chat thread",
)
def create_chat_thread(
    payload: CreateChatThreadRequest,
    request: Request,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    chat_service: ChatServiceDependency,
) -> ChatThreadWithMessages:
    """Create a new chat thread grounded to an entity and optional close run."""

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )
    trace_id = getattr(request.state, "request_id", None)

    try:
        thread_summary = chat_service.create_thread(
            request=payload,
            user_id=session_result.user.id,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except ChatServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error

    return ChatThreadWithMessages(
        thread=thread_summary,
        messages=(),
    )


@router.get(
    "/threads",
    response_model=ChatThreadListResponse,
    summary="List chat threads for an entity or close run",
)
def list_chat_threads(
    entity_id: EntityIdQuery,
    close_run_id: CloseRunIdQuery = None,
    limit: ThreadLimitQuery = 50,
    request: Request = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
) -> ChatThreadListResponse:
    """Return threads for an entity or close run, ordered newest-first."""

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )

    return chat_service.list_threads(
        entity_id=entity_id,
        close_run_id=close_run_id,
        user_id=session_result.user.id,
        limit=limit,
    )


@router.get(
    "/threads/{thread_id}",
    response_model=ChatThreadWithMessages,
    summary="Read a chat thread with message history",
)
def get_chat_thread(
    thread_id: UUID,
    entity_id: EntityIdQuery,
    message_limit: MessageLimitQuery = 100,
    request: Request = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
) -> ChatThreadWithMessages:
    """Return one thread with its message history for detail views."""

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )

    try:
        return chat_service.get_thread(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=session_result.user.id,
            message_limit=message_limit,
        )
    except ChatServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error


@router.post(
    "/threads/{thread_id}/messages",
    response_model=ChatMessageResponse,
    summary="Send a message and get a read-only analysis response",
)
def send_chat_message(
    thread_id: UUID,
    payload: SendChatMessageRequest,
    entity_id: EntityIdQuery,
    request: Request = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
) -> ChatMessageResponse:
    """Send a user message and receive a grounded read-only copilot analysis response."""

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )
    trace_id = getattr(request.state, "request_id", None)

    try:
        return chat_service.send_message(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=str(session_result.user.id),
            content=payload.content,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except ChatServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error


# ---------------------------------------------------------------------------
# Action routing endpoints (Step 35)
# ---------------------------------------------------------------------------


@router.post(
    "/threads/{thread_id}/actions",
    response_model=ChatActionResponse,
    summary="Send a message with action intent detection",
)
def send_chat_action(
    thread_id: UUID,
    payload: SendChatActionRequest,
    entity_id: EntityIdQuery,
    request: Request = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
    action_router: ChatActionRouterDependency = None,  # type: ignore[assignment]
) -> ChatActionResponse:
    """Send a user message that may contain action intents.

    When an action intent is detected (proposed edit, approval request, etc.),
    the system classifies the intent, creates an action execution plan,
    persists it for review, and returns it alongside the assistant response.
    When no action intent is detected, falls back to read-only analysis.
    """

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )
    user_id = session_result.user.id
    trace_id = getattr(request.state, "request_id", None)

    # First, send through the standard chat service for message persistence
    try:
        chat_response = chat_service.send_message(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=str(user_id),
            content=payload.content,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except ChatServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error

    # Classify action intent using the action router
    try:
        grounding_result = action_router._grounding.resolve_context(
            entity_id=entity_id,
            close_run_id=None,
            user_id=user_id,
        )
    except Exception:
        # If grounding fails, return read-only response
        return ChatActionResponse(
            message_id=str(chat_response.message.id),
            content=chat_response.message.content,
            action_plan=None,
            is_read_only=True,
        )

    try:
        intent = action_router.classify_action_intent(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=user_id,
            content=payload.content,
            grounding=grounding_result,
        )
    except ChatActionRouterError as error:
        # Classification failed -- return read-only response
        return ChatActionResponse(
            message_id=str(chat_response.message.id),
            content=chat_response.message.content,
            action_plan=None,
            is_read_only=True,
        )

    if intent is None:
        # No action intent detected -- pure analysis
        return ChatActionResponse(
            message_id=str(chat_response.message.id),
            content=chat_response.message.content,
            action_plan=None,
            is_read_only=True,
        )

    # Build and persist the action execution plan
    plan = action_router.build_execution_plan(
        thread_id=thread_id,
        message_id=UUID(chat_response.message.id),
        entity_id=entity_id,
        close_run_id=grounding_result.close_run.id if grounding_result.close_run else None,
        actor_user_id=user_id,
        intent=intent,
        grounding=grounding_result,
        reasoning=f"Detected {intent.intent} intent from user message.",
    )

    try:
        record = action_router.persist_action_plan(
            plan=plan,
            entity_id=entity_id,
            actor_user_id=user_id,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except Exception:
        # Persistence failed -- return read-only response
        return ChatActionResponse(
            message_id=str(chat_response.message.id),
            content=chat_response.message.content,
            action_plan=None,
            is_read_only=True,
        )

    # Build the response with the action plan
    action_plan = ChatActionSummary(
        id=str(record.id),
        thread_id=str(record.thread_id),
        intent=record.intent,
        target_type=record.target_type,
        target_id=str(record.target_id) if record.target_id else None,
        status=record.status,
        requires_human_approval=record.requires_human_approval,
        created_at=str(record.created_at),
    )

    return ChatActionResponse(
        message_id=str(chat_response.message.id),
        content=chat_response.message.content,
        action_plan=action_plan,
        is_read_only=False,
    )


@router.get(
    "/threads/{thread_id}/actions",
    response_model=list[ChatActionSummary],
    summary="List pending action plans for a thread",
)
def list_thread_actions(
    thread_id: UUID,
    entity_id: EntityIdQuery,
    limit: ThreadLimitQuery = 50,
    request: Request = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_router: ChatActionRouterDependency = None,  # type: ignore[assignment]
) -> list[ChatActionSummary]:
    """Return pending action plans for a chat thread for review-queue rendering."""

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )

    plans = action_router.list_pending_actions(
        thread_id=thread_id,
        entity_id=entity_id,
        user_id=session_result.user.id,
        limit=limit,
    )

    return [
        ChatActionSummary(
            id=str(p.id),
            thread_id=str(p.thread_id),
            intent=p.intent,
            target_type=p.target_type,
            target_id=str(p.target_id) if p.target_id else None,
            status=p.status,
            requires_human_approval=p.requires_human_approval,
            created_at=str(p.created_at),
        )
        for p in plans
    ]


@router.post(
    "/actions/{action_plan_id}/approve",
    response_model=ChatActionSummary,
    summary="Approve a pending chat action plan",
)
def approve_chat_action(
    action_plan_id: UUID = Path(description="Action plan UUID to approve."),
    thread_id: UUID = Query(description="Thread UUID for access verification."),
    entity_id: EntityIdQuery = None,  # type: ignore[assignment]
    payload: ApproveChatActionRequest = None,  # type: ignore[assignment]
    request: Request = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_router: ChatActionRouterDependency = None,  # type: ignore[assignment]
) -> ChatActionSummary:
    """Approve a pending chat-originated action plan."""

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )
    trace_id = getattr(request.state, "request_id", None)

    try:
        record = action_router.approve_action_plan(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user_id=session_result.user.id,
            reason=payload.reason if payload else None,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except ChatActionRouterError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error

    return ChatActionSummary(
        id=str(record.id),
        thread_id=str(record.thread_id),
        intent=record.intent,
        target_type=record.target_type,
        target_id=str(record.target_id) if record.target_id else None,
        status=record.status,
        requires_human_approval=record.requires_human_approval,
        created_at=str(record.created_at),
    )


@router.post(
    "/actions/{action_plan_id}/reject",
    response_model=ChatActionSummary,
    summary="Reject a pending chat action plan",
)
def reject_chat_action(
    action_plan_id: UUID = Path(description="Action plan UUID to reject."),
    thread_id: UUID = Query(description="Thread UUID for access verification."),
    entity_id: EntityIdQuery = None,  # type: ignore[assignment]
    payload: RejectChatActionRequest = None,  # type: ignore[assignment]
    request: Request = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_router: ChatActionRouterDependency = None,  # type: ignore[assignment]
) -> ChatActionSummary:
    """Reject a pending chat-originated action plan with a required reason."""

    session_result = _require_authenticated_browser_session(
        request=request,
        auth_service=auth_service,
        settings=settings,
    )
    trace_id = getattr(request.state, "request_id", None)

    if payload is None or not payload.reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_payload(
                code="validation_failed",
                message="A reason is required when rejecting a chat action.",
            ),
        )

    try:
        record = action_router.reject_action_plan(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user_id=session_result.user.id,
            reason=payload.reason,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except ChatActionRouterError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error

    return ChatActionSummary(
        id=str(record.id),
        thread_id=str(record.thread_id),
        intent=record.intent,
        target_type=record.target_type,
        target_id=str(record.target_id) if record.target_id else None,
        status=record.status,
        requires_human_approval=record.requires_human_approval,
        created_at=str(record.created_at),
    )


__all__ = ["router"]
