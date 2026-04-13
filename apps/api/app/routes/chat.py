"""
Purpose: Expose grounded chat thread and message routes for the finance
copilot experience.
Scope: Thread creation, listing, detail reads, and the send-message flow
that returns read-only analysis responses backed by workflow evidence.
Dependencies: FastAPI, auth session validation, chat contracts and services,
and the shared DB/settings dependencies.
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
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.chat.grounding import ChatGroundingService
from services.chat.service import ChatService, ChatServiceError
from services.common.settings import AppSettings, get_settings
from services.contracts.chat_models import (
    ChatMessageResponse,
    ChatThreadListResponse,
    ChatThreadWithMessages,
    CreateChatThreadRequest,
    SendChatMessageRequest,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.chat_repo import ChatRepository
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


__all__ = ["router"]
