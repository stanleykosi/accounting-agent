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

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from apps.api.app.dependencies.db import DatabaseSessionDependency
from apps.api.app.dependencies.tasks import TaskDispatcherDependency
from apps.api.app.routes.auth import (
    _build_http_exception,
    _clear_session_cookie,
    _read_session_cookie,
    _resolve_ip_address,
    _set_session_cookie,
    get_auth_service,
)
from apps.api.app.routes.request_auth import (
    AuthenticatedRequestContext,
    RequestAuthDependency,
)
from apps.api.app.routes.workflow_phase import require_active_close_run_phase
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from services.accounting.recommendation_apply import RecommendationApplyService
from services.audit.service import AuditService
from services.auth.service import (
    AuthenticatedSessionResult,
    AuthErrorCode,
    AuthService,
    AuthServiceError,
)
from services.chat.action_execution import (
    ChatActionExecutionError,
    ChatActionExecutor,
)
from services.chat.action_models import (
    ApproveChatActionRequest,
    ChatActionResponse,
    ChatActionSummary,
    RejectChatActionRequest,
    SendChatActionRequest,
)
from services.chat.action_router import ChatActionRouter
from services.chat.grounding import ChatGroundingService
from services.chat.proposed_changes import ProposedChangesService
from services.chat.service import ChatService, ChatServiceError
from services.close_runs.service import CloseRunService
from services.coa.service import CoaRepository, CoaService, CoaServiceError
from services.common.enums import WorkflowPhase
from services.common.settings import AppSettings, get_settings
from services.contracts.chat_models import (
    ChatMessageResponse,
    ChatThreadDeleteResponse,
    ChatThreadListResponse,
    ChatThreadWithMessages,
    ChatThreadWorkspaceResponse,
    CreateChatThreadRequest,
    SendChatMessageRequest,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.chat_action_repo import ChatActionRepository
from services.db.repositories.chat_repo import ChatRepository
from services.db.repositories.close_run_repo import CloseRunRepository
from services.db.repositories.document_repo import DocumentRepository
from services.db.repositories.entity_repo import EntityRepository, EntityUserRecord
from services.db.repositories.integration_repo import IntegrationRepository
from services.db.repositories.recommendation_journal_repo import RecommendationJournalRepository
from services.db.repositories.reconciliation_repo import ReconciliationRepository
from services.db.repositories.report_repo import ReportRepository
from services.documents.review_service import DocumentReviewService
from services.documents.upload_service import (
    DocumentUploadService,
    DocumentUploadServiceError,
    UploadFilePayload,
)
from services.exports.service import ExportService
from services.jobs.service import JobService
from services.model_gateway.client import ModelGateway
from services.reconciliation.service import ReconciliationService
from services.reporting.service import ReportService
from services.storage.repository import StorageRepository

router = APIRouter(prefix="/chat", tags=["chat"])
MCP_PROTOCOL_VERSION = "2025-11-25"

SettingsDependency = Annotated[AppSettings, Depends(get_settings)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
EntityIdQuery = Annotated[UUID, Query(description="Entity workspace UUID.")]
CloseRunIdQuery = Annotated[UUID | None, Query(description="Optional close run UUID.")]
ThreadLimitQuery = Annotated[int, Query(ge=1, le=200, description="Maximum items.")]
MessageLimitQuery = Annotated[int, Query(ge=1, le=500, description="Maximum messages.")]
ActionPlanIdPath = Annotated[UUID, Path(description="Action plan UUID to approve or reject.")]
ThreadAccessQuery = Annotated[UUID, Query(description="Thread UUID for access verification.")]
INLINE_ATTACHMENT_INTENTS = ("source_documents",)


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


def get_chat_action_executor(
    db_session: DatabaseSessionDependency,
    settings: SettingsDependency,
    task_dispatcher: TaskDispatcherDependency,
) -> ChatActionExecutor:
    """Construct the deterministic chat action executor from request-scoped services."""

    entity_repo = EntityRepository(db_session=db_session)
    close_run_repo = CloseRunRepository(db_session=db_session)
    grounding_service = ChatGroundingService(
        entity_repo=entity_repo,
        close_run_repo=close_run_repo,
    )
    model_gateway = ModelGateway()
    chat_repo = ChatRepository(db_session=db_session)
    action_repo = ChatActionRepository(db_session=db_session)
    document_repo = DocumentRepository(db_session=db_session)
    report_repo = ReportRepository(db_session=db_session)
    recommendation_repo = RecommendationJournalRepository(db_session=db_session)
    audit_service = AuditService(db_session=db_session)
    export_service = ExportService(
        db_session=db_session,
        report_repository=report_repo,
    )

    return ChatActionExecutor(
        db_session=db_session,
        chat_repository=chat_repo,
        action_repository=action_repo,
        grounding_service=grounding_service,
        entity_repository=entity_repo,
        close_run_service=CloseRunService(repository=close_run_repo),
        close_run_repository=close_run_repo,
        document_review_service=DocumentReviewService(
            db_session=db_session,
            repository=document_repo,
        ),
        document_repository=document_repo,
        recommendation_service=RecommendationApplyService(
            repository=recommendation_repo,
            audit_service=audit_service,
            db_session=db_session,
            integration_repository=IntegrationRepository(db_session=db_session),
            storage_repository=StorageRepository(),
        ),
        recommendation_repository=recommendation_repo,
        reconciliation_service=ReconciliationService(
            repository=ReconciliationRepository(session=db_session),
        ),
        reconciliation_repository=ReconciliationRepository(session=db_session),
        report_service=ReportService(repository=report_repo),
        report_repository=report_repo,
        export_service=export_service,
        model_gateway=model_gateway,
        job_service=JobService(db_session=db_session),
        task_dispatcher=task_dispatcher,
    )


ChatActionExecutorDependency = Annotated[
    ChatActionExecutor,
    Depends(get_chat_action_executor),
]


def get_chat_repository(
    db_session: DatabaseSessionDependency,
) -> ChatRepository:
    """Construct the canonical chat repository from request-scoped persistence."""

    return ChatRepository(db_session=db_session)


ChatRepositoryDependency = Annotated[ChatRepository, Depends(get_chat_repository)]


def get_document_upload_service(
    db_session: DatabaseSessionDependency,
    task_dispatcher: TaskDispatcherDependency,
) -> DocumentUploadService:
    """Construct the canonical document upload service for chat attachments."""

    return DocumentUploadService(
        repository=DocumentRepository(db_session=db_session),
        storage_repository=StorageRepository(),
        job_service=JobService(db_session=db_session),
        task_dispatcher=task_dispatcher,
    )


DocumentUploadServiceDependency = Annotated[
    DocumentUploadService,
    Depends(get_document_upload_service),
]


def get_coa_service(db_session: DatabaseSessionDependency) -> CoaService:
    """Construct the canonical COA service for chat attachments."""

    return CoaService(repository=CoaRepository(db_session=db_session))


CoaServiceDependency = Annotated[CoaService, Depends(get_coa_service)]


def get_proposed_changes_service(
    db_session: DatabaseSessionDependency,
) -> ProposedChangesService:
    """Construct the proposed changes service from request-scoped persistence."""

    action_repo = ChatActionRepository(db_session=db_session)
    return ProposedChangesService(action_repository=action_repo)


ProposedChangesServiceDependency = Annotated[
    ProposedChangesService,
    Depends(get_proposed_changes_service),
]


def _require_authenticated_browser_session(
    *,
    request: Request,
    response: Response | None,
    auth_service: AuthService,
    settings: AppSettings,
) -> AuthenticatedSessionResult:
    """Validate the caller's session cookie and return the active auth context or raise 401."""

    cookie_value = _read_session_cookie(request=request, settings=settings)
    if cookie_value is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_error_payload(
                code=AuthErrorCode.SESSION_REQUIRED.value,
                message="Authentication is required to access chat threads.",
            ),
        )

    try:
        session_result = auth_service.authenticate_session(
            session_token=cookie_value,
            user_agent=request.headers.get("user-agent"),
            ip_address=_resolve_ip_address(request),
        )
    except AuthServiceError as error:
        if response is not None:
            _clear_session_cookie(response=response, settings=settings)
        raise _build_http_exception(error) from error

    if response is not None and session_result.session_token is not None:
        _set_session_cookie(
            response=response,
            settings=settings,
            session_token=session_result.session_token,
        )

    return session_result


def _error_payload(*, code: str, message: str) -> dict[str, str]:
    """Build a deterministic error response body for generated SDK consumers."""

    return {"code": code, "message": message}


async def _ingest_chat_attachments(
    *,
    actor_user: EntityUserRecord,
    attachment_intent: str,
    chat_thread,
    coa_service: CoaService,
    content: str | None,
    db_session: DatabaseSessionDependency,
    document_upload_service: DocumentUploadService,
    entity_id: UUID,
    files: tuple[UploadFile, ...],
    trace_id: str | None,
) -> ChatInlineAttachmentResult:
    """Route inline chat attachments through the canonical upload services."""

    normalized_intent = attachment_intent.strip().lower()
    if normalized_intent not in INLINE_ATTACHMENT_INTENTS:
        raise HTTPException(
            status_code=400,
            detail=_error_payload(
                code="invalid_attachment_intent",
                message="Attachment intent must be 'source_documents'.",
            ),
        )

    if not files:
        raise HTTPException(
            status_code=400,
            detail=_error_payload(
                code="empty_batch",
                message="Attach at least one file before sending the message.",
            ),
        )

    if normalized_intent == "source_documents":
        if chat_thread.close_run_id is None:
            raise HTTPException(
                status_code=409,
                detail=_error_payload(
                    code="close_run_required",
                    message=(
                        "Source-document attachments require a close-run-scoped chat thread. "
                        "Open a close run and retry the upload."
                    ),
                ),
            )
        require_active_close_run_phase(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=chat_thread.close_run_id,
            required_phase=WorkflowPhase.COLLECTION,
            action_label="Chat document upload",
            db_session=db_session,
        )
        upload_payloads_list: list[UploadFilePayload] = []
        for file in files:
            upload_payloads_list.append(
                UploadFilePayload(
                    filename=file.filename or "",
                    payload=await file.read(),
                    declared_content_type=file.content_type,
                )
            )
        upload_payloads = tuple(upload_payloads_list)
        result = document_upload_service.upload_documents(
            actor_user=actor_user,
            entity_id=entity_id,
            close_run_id=chat_thread.close_run_id,
            files=upload_payloads,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
        attachments = tuple(
            {
                "filename": uploaded.document.original_filename,
                "file_size_bytes": uploaded.document.file_size_bytes,
                "mime_type": uploaded.document.mime_type,
                "document_id": uploaded.document.id,
                "intent": normalized_intent,
                "status": uploaded.document.status,
            }
            for uploaded in result.uploaded_documents
        )
        return ChatInlineAttachmentResult(
            attachment_intent=normalized_intent,
            files=attachments,
            summary=(
                f"{len(attachments)} source document"
                f"{'' if len(attachments) == 1 else 's'} uploaded and staged for parsing."
            ),
            operator_prompt=_build_inline_attachment_prompt(
                attachment_intent=normalized_intent,
                content=content,
                filenames=tuple(
                    str(item["filename"])
                    for item in attachments
                    if isinstance(item["filename"], str)
                ),
                summary=(
                    f"{len(attachments)} source document"
                    f"{'' if len(attachments) == 1 else 's'} uploaded and staged for parsing."
                ),
            ),
        )

    raise HTTPException(
        status_code=400,
        detail=_error_payload(
            code="invalid_attachment_intent",
            message="Only source-document uploads are supported in chat.",
        ),
    )


def _build_inline_attachment_prompt(
    *,
    attachment_intent: str,
    content: str | None,
    filenames: tuple[str, ...],
    summary: str,
) -> str:
    """Build the operator message passed into the agent after inline ingestion."""

    cleaned_content = content.strip() if isinstance(content, str) else ""
    file_list = ", ".join(filenames)
    attachment_label = attachment_intent.replace("_", " ")
    preamble = (
        f"Inline {attachment_label} uploaded through chat: {file_list}. {summary} "
        "Acknowledge the upload result and continue using the updated workspace state."
    )
    if cleaned_content:
        return f"{cleaned_content}\n\n{preamble}"
    return (
        "Source documents were attached through chat. Confirm parsing has started and explain "
        f"the next close-run step. {preamble}"
    )


def _persist_inline_attachment_partial_success(
    *,
    chat_repository: ChatRepository,
    content: str | None,
    inline_result: ChatInlineAttachmentResult,
    thread_id: UUID,
    trace_id: str | None,
) -> ChatActionResponse:
    """Return a success response when upload succeeded but agent follow-up did not."""

    cleaned_content = content.strip() if isinstance(content, str) else ""
    user_content = cleaned_content or inline_result.summary
    grounding_payload = {
        "attachment_intent": inline_result.attachment_intent,
        "attachments": list(inline_result.files),
        "ingestion_summary": inline_result.summary,
        "original_operator_content": cleaned_content or None,
    }
    assistant_content = (
        f"{inline_result.summary} The upload completed successfully, but the agent could not "
        "finish the chat follow-up for this turn. Continue in this thread without re-uploading "
        "the files."
    )
    message_id = f"inline-attachment:{thread_id}"

    try:
        chat_repository.create_message(
            thread_id=thread_id,
            role="user",
            content=user_content,
            message_type="action",
            linked_action_id=None,
            grounding_payload=grounding_payload,
            model_metadata=None,
        )
        assistant_message = chat_repository.create_message(
            thread_id=thread_id,
            role="assistant",
            content=assistant_content,
            message_type="warning",
            linked_action_id=None,
            grounding_payload=grounding_payload,
            model_metadata={
                "provider": "system",
                "mode": "attachment_ingestion",
                "action_status": "completed_with_warning",
                "summary": inline_result.summary,
                "trace_id": trace_id,
            },
        )
        chat_repository.commit()
        message_id = str(assistant_message.id)
    except Exception:
        chat_repository.rollback()

    return ChatActionResponse(
        message_id=message_id,
        content=assistant_content,
        action_plan=None,
        is_read_only=True,
    )


@router.post(
    "/threads",
    response_model=ChatThreadWithMessages,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new grounded chat thread",
)
def create_chat_thread(
    payload: CreateChatThreadRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    chat_service: ChatServiceDependency,
) -> ChatThreadWithMessages:
    """Create a new chat thread grounded to an entity and optional close run."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
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
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
) -> ChatThreadListResponse:
    """Return threads for an entity or close run, ordered newest-first."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
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
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
) -> ChatThreadWithMessages:
    """Return one thread with its message history for detail views."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
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


@router.delete(
    "/threads/{thread_id}",
    response_model=ChatThreadDeleteResponse,
    summary="Delete one chat thread",
)
def delete_chat_thread(
    thread_id: UUID,
    entity_id: EntityIdQuery,
    request: Request = None,  # type: ignore[assignment]
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
) -> ChatThreadDeleteResponse:
    """Delete one chat thread together with its persisted message history."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        auth_service=auth_service,
        settings=settings,
    )

    try:
        return chat_service.delete_thread(
            thread_id=thread_id,
            entity_id=entity_id,
            user_id=session_result.user.id,
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
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    chat_service: ChatServiceDependency = None,  # type: ignore[assignment]
) -> ChatMessageResponse:
    """Send a user message and receive a grounded read-only copilot analysis response."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
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
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_executor: ChatActionExecutorDependency = None,  # type: ignore[assignment]
) -> ChatActionResponse:
    """Send a user message through the deterministic accounting action agent."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        auth_service=auth_service,
        settings=settings,
    )
    trace_id = getattr(request.state, "request_id", None)

    try:
        outcome = action_executor.send_action_message(
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=_to_entity_user(session_result),
            content=payload.content,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except ChatActionExecutionError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error

    return ChatActionResponse(
        message_id=outcome.assistant_message_id,
        content=outcome.assistant_content,
        action_plan=_to_chat_action_summary(outcome.action_plan),
        is_read_only=outcome.is_read_only,
    )


@dataclass(frozen=True, slots=True)
class ChatInlineAttachmentResult:
    """Describe inline chat attachments after canonical ingestion succeeds."""

    attachment_intent: str
    files: tuple[dict[str, object], ...]
    operator_prompt: str
    summary: str


@router.post(
    "/threads/{thread_id}/actions/attachments",
    response_model=ChatActionResponse,
    summary="Send a message with inline file attachments through the action agent",
)
async def send_chat_action_with_attachments(
    thread_id: UUID,
    entity_id: EntityIdQuery,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    auth_service: AuthServiceDependency,
    action_executor: ChatActionExecutorDependency,
    chat_repository: ChatRepositoryDependency,
    db_session: DatabaseSessionDependency,
    document_upload_service: DocumentUploadServiceDependency,
    coa_service: CoaServiceDependency,
    files: Annotated[tuple[UploadFile, ...], File(description="Inline chat attachments.")] = (),
    content: Annotated[str | None, Form()] = None,
    attachment_intent: Annotated[str, Form()] = "source_documents",
) -> ChatActionResponse:
    """Accept inline chat attachments and route them through canonical upload services."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        auth_service=auth_service,
        settings=settings,
    )
    trace_id = getattr(request.state, "request_id", None)
    inline_result: ChatInlineAttachmentResult | None = None

    try:
        thread = chat_repository.get_thread_for_entity(thread_id=thread_id, entity_id=entity_id)
        if thread is None:
            raise HTTPException(
                status_code=404,
                detail=_error_payload(
                    code="thread_not_found",
                    message="That chat thread does not exist in this workspace.",
                ),
            )

        inline_result = await _ingest_chat_attachments(
            actor_user=_to_entity_user(session_result),
            attachment_intent=attachment_intent,
            chat_thread=thread,
            coa_service=coa_service,
            content=content,
            db_session=db_session,
            document_upload_service=document_upload_service,
            entity_id=entity_id,
            files=files,
            trace_id=trace_id,
        )
        outcome = action_executor.send_action_message(
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=_to_entity_user(session_result),
            content=inline_result.operator_prompt,
            message_grounding_payload={
                "attachment_intent": inline_result.attachment_intent,
                "attachments": list(inline_result.files),
                "ingestion_summary": inline_result.summary,
                "original_operator_content": content.strip() if isinstance(content, str) else None,
            },
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except HTTPException:
        raise
    except ChatActionExecutionError as error:
        if inline_result is None:
            raise HTTPException(
                status_code=error.status_code,
                detail=_error_payload(code=error.code.value, message=error.message),
            ) from error
        return _persist_inline_attachment_partial_success(
            chat_repository=chat_repository,
            content=content,
            inline_result=inline_result,
            thread_id=thread_id,
            trace_id=trace_id,
        )
    except (CoaServiceError, DocumentUploadServiceError) as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=str(error.code), message=error.message),
        ) from error
    finally:
        for file in files:
            await file.close()

    return ChatActionResponse(
        message_id=outcome.assistant_message_id,
        content=outcome.assistant_content,
        action_plan=_to_chat_action_summary(outcome.action_plan),
        is_read_only=outcome.is_read_only,
    )


@router.get(
    "/threads/{thread_id}/workspace",
    response_model=ChatThreadWorkspaceResponse,
    summary="Read the agent workspace context for a chat thread",
)
def read_chat_thread_workspace(
    thread_id: UUID,
    entity_id: EntityIdQuery,
    request: Request = None,  # type: ignore[assignment]
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_executor: ChatActionExecutorDependency = None,  # type: ignore[assignment]
) -> ChatThreadWorkspaceResponse:
    """Return the agent memory, tool manifest, and recent traces for one thread."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        auth_service=auth_service,
        settings=settings,
    )

    try:
        return action_executor.get_thread_workspace(
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=_to_entity_user(session_result),
        )
    except ChatActionExecutionError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error


@router.get(
    "/tools/mcp",
    summary="Read the MCP-style manifest for registered accounting agent tools",
)
def read_chat_tool_manifest(
    request: Request = None,  # type: ignore[assignment]
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_executor: ChatActionExecutorDependency = None,  # type: ignore[assignment]
) -> dict[str, object]:
    """Return the portable MCP-style tool manifest for the accounting agent."""

    _require_authenticated_browser_session(
        request=request,
        response=response,
        auth_service=auth_service,
        settings=settings,
    )
    workspace = {
        "tools": [tool.model_dump() for tool in action_executor.list_registered_tools()],
    }
    return {
        "protocol": "model-context-protocol",
        "version": MCP_PROTOCOL_VERSION,
        "tools": [
            {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["input_schema"],
            }
            for tool in workspace["tools"]
        ],
    }


@router.post(
    "/mcp",
    summary="Serve the accounting agent over a canonical MCP-compatible JSON-RPC endpoint",
    response_model=None,
)
async def handle_chat_mcp_request(
    payload: dict[str, object],
    request: Request,
    response: Response,
    auth_context: RequestAuthDependency,
    action_executor: ChatActionExecutorDependency,
) -> Response | dict[str, object]:
    """Handle initialize, tools/list, and tools/call requests for MCP clients."""

    response.headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return _mcp_error_response(
            request_id=payload.get("id"),
            code=-32600,
            message="Invalid JSON-RPC request.",
        )

    method = str(payload["method"])
    request_id = payload.get("id")
    params = payload.get("params")
    if params is None:
        resolved_params: dict[str, object] = {}
    elif isinstance(params, dict):
        resolved_params = params
    else:
        return _mcp_error_response(
            request_id=request_id,
            code=-32602,
            message="Request params must be a JSON object.",
        )

    if method == "notifications/initialized":
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if method == "initialize":
        return _mcp_success_response(
            request_id=request_id,
            result={
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {
                        "listChanged": False,
                    }
                },
                "serverInfo": {
                    "name": "accounting-ai-agent",
                    "title": "Accounting AI Agent",
                    "version": getattr(request.app, "version", "0.1.0"),
                },
                "instructions": (
                    "Use tools/list to inspect the registered deterministic accounting tools and "
                    "tools/call to execute them. Provide entity_id and thread_id in the call "
                    "context. High-risk actions stage for approval and appear in the operator UI."
                ),
            },
        )
    if method == "ping":
        return _mcp_success_response(request_id=request_id, result={})
    if method == "tools/list":
        return _mcp_success_response(
            request_id=request_id,
            result={
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                    }
                    for tool in action_executor.list_registered_tools()
                ]
            },
        )
    if method == "tools/call":
        name = resolved_params.get("name")
        arguments = resolved_params.get("arguments", {})
        context = resolved_params.get("context")
        if not isinstance(name, str) or not name.strip():
            return _mcp_error_response(
                request_id=request_id,
                code=-32602,
                message="tools/call requires a non-empty tool name.",
            )
        if not isinstance(arguments, dict):
            return _mcp_error_response(
                request_id=request_id,
                code=-32602,
                message="tools/call arguments must be a JSON object.",
            )
        if not isinstance(context, dict):
            return _mcp_error_response(
                request_id=request_id,
                code=-32602,
                message="tools/call requires a context object with entity_id and thread_id.",
            )
        entity_id_raw = context.get("entity_id")
        thread_id_raw = context.get("thread_id")
        if not isinstance(entity_id_raw, str) or not isinstance(thread_id_raw, str):
            return _mcp_error_response(
                request_id=request_id,
                code=-32602,
                message="tools/call context must include string entity_id and thread_id values.",
            )
        try:
            trace_id = context.get("trace_id") if isinstance(context.get("trace_id"), str) else None
            outcome = action_executor.execute_registered_tool(
                thread_id=UUID(thread_id_raw),
                entity_id=UUID(entity_id_raw),
                actor_user=_to_entity_user(auth_context),
                tool_name=name.strip(),
                tool_arguments=arguments,
                trace_id=trace_id,
                source_surface=_resolve_authenticated_source_surface(auth_context),
            )
        except ValueError as error:
            return _mcp_error_response(
                request_id=request_id,
                code=-32602,
                message=str(error),
            )
        except ChatActionExecutionError as error:
            return _mcp_error_response(
                request_id=request_id,
                code=-32000,
                message=error.message,
                data={"code": error.code.value, "status_code": error.status_code},
            )
        return _mcp_success_response(
            request_id=request_id,
            result={
                "content": [{"type": "text", "text": outcome.summary}],
                "structuredContent": {
                    "message_id": outcome.message_id,
                    "tool_name": outcome.tool_name,
                    "status": outcome.status,
                    "requires_human_approval": outcome.requires_human_approval,
                    "action_plan_id": outcome.action_plan_id,
                    "result": outcome.result,
                },
                "isError": False,
            },
        )

    return _mcp_error_response(
        request_id=request_id,
        code=-32601,
        message=f"MCP method '{method}' is not implemented.",
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
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_router: ChatActionRouterDependency = None,  # type: ignore[assignment]
) -> list[ChatActionSummary]:
    """Return pending action plans for a chat thread for review-queue rendering."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
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
    action_plan_id: ActionPlanIdPath,
    thread_id: ThreadAccessQuery,
    entity_id: EntityIdQuery = None,  # type: ignore[assignment]
    payload: ApproveChatActionRequest = None,  # type: ignore[assignment]
    request: Request = None,  # type: ignore[assignment]
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_executor: ChatActionExecutorDependency = None,  # type: ignore[assignment]
) -> ChatActionSummary:
    """Approve a pending chat-originated action plan."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        auth_service=auth_service,
        settings=settings,
    )
    trace_id = getattr(request.state, "request_id", None)

    try:
        record = action_executor.approve_action_plan(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=_to_entity_user(session_result),
            reason=payload.reason if payload else None,
            source_surface=AuditSourceSurface.DESKTOP,
            trace_id=trace_id,
        )
    except ChatActionExecutionError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error

    return _to_chat_action_summary(record)


@router.post(
    "/actions/{action_plan_id}/reject",
    response_model=ChatActionSummary,
    summary="Reject a pending chat action plan",
)
def reject_chat_action(
    action_plan_id: ActionPlanIdPath,
    thread_id: ThreadAccessQuery,
    entity_id: EntityIdQuery = None,  # type: ignore[assignment]
    payload: RejectChatActionRequest = None,  # type: ignore[assignment]
    request: Request = None,  # type: ignore[assignment]
    response: Response = None,  # type: ignore[assignment]
    settings: SettingsDependency = None,  # type: ignore[assignment]
    auth_service: AuthServiceDependency = None,  # type: ignore[assignment]
    action_executor: ChatActionExecutorDependency = None,  # type: ignore[assignment]
) -> ChatActionSummary:
    """Reject a pending chat-originated action plan with a required reason."""

    session_result = _require_authenticated_browser_session(
        request=request,
        response=response,
        auth_service=auth_service,
        settings=settings,
    )
    if payload is None or not payload.reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_error_payload(
                code="validation_failed",
                message="A reason is required when rejecting a chat action.",
            ),
        )

    try:
        record = action_executor.reject_action_plan(
            action_plan_id=action_plan_id,
            thread_id=thread_id,
            entity_id=entity_id,
            actor_user=_to_entity_user(session_result),
            reason=payload.reason,
        )
    except ChatActionExecutionError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=_error_payload(code=error.code.value, message=error.message),
        ) from error

    return _to_chat_action_summary(record)


def _to_chat_action_summary(record: object | None) -> ChatActionSummary | None:
    """Convert an action-plan record into the shared chat action summary contract."""

    if record is None:
        return None

    return ChatActionSummary(
        id=str(record.id),  # type: ignore[attr-defined]
        thread_id=str(record.thread_id),  # type: ignore[attr-defined]
        intent=record.intent,  # type: ignore[attr-defined]
        target_type=record.target_type,  # type: ignore[attr-defined]
        target_id=str(record.target_id) if record.target_id else None,  # type: ignore[attr-defined]
        status=record.status,  # type: ignore[attr-defined]
        requires_human_approval=record.requires_human_approval,  # type: ignore[attr-defined]
        created_at=str(record.created_at),  # type: ignore[attr-defined]
    )


def _to_entity_user(session_result: object) -> EntityUserRecord:
    """Project an authenticated request context into the entity actor contract."""

    user = getattr(session_result, "user", None)
    if user is None:
        raise TypeError("Authenticated request context is missing a user.")
    return EntityUserRecord(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
    )


def _resolve_authenticated_source_surface(
    auth_context: AuthenticatedRequestContext,
) -> AuditSourceSurface:
    """Map the authenticated caller mode to the audit surface used by tool execution."""

    if auth_context.authenticated_via == "browser_session":
        return AuditSourceSurface.DESKTOP
    return AuditSourceSurface.CLI


def _mcp_success_response(
    *,
    request_id: object,
    result: dict[str, object],
) -> dict[str, object]:
    """Build one canonical JSON-RPC success envelope for MCP responses."""

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _mcp_error_response(
    *,
    request_id: object,
    code: int,
    message: str,
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build one canonical JSON-RPC error envelope for MCP responses."""

    error: dict[str, object] = {
        "code": code,
        "message": message,
    }
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }


__all__ = ["router"]
