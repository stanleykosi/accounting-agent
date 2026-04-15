"""
Purpose: Verify the accountant-facing chat workspace and MCP runtime routes.
Scope: Workspace context reads, MCP lifecycle/tool discovery, and deterministic
tool execution envelopes.
Dependencies: FastAPI TestClient, chat route dependencies, and strict chat contracts.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from uuid import uuid4


_ORIGINAL_MODULES: dict[str, ModuleType | None] = {}


def _install_temporary_module(module_name: str, module: ModuleType) -> None:
    """Install one import stub for the duration of this module's chat-route import."""

    if module_name not in _ORIGINAL_MODULES:
        existing_module = sys.modules.get(module_name)
        _ORIGINAL_MODULES[module_name] = (
            existing_module if isinstance(existing_module, ModuleType) else None
        )
    sys.modules[module_name] = module


def _restore_temporary_modules() -> None:
    """Restore any real modules that were replaced during chat-route import setup."""

    for module_name, original_module in tuple(_ORIGINAL_MODULES.items()):
        if original_module is None:
            sys.modules.pop(module_name, None)
            continue
        sys.modules[module_name] = original_module
    _ORIGINAL_MODULES.clear()


def _install_service_stub(module_name: str, **symbols: object) -> None:
    """Install one lightweight module stub before importing the chat routes."""

    module = ModuleType(module_name)
    for name, value in symbols.items():
        setattr(module, name, value)
    _install_temporary_module(module_name, module)


def _dummy_class(name: str) -> type[object]:
    """Create a minimal import-safe service class stub."""

    return type(name, (), {"__init__": lambda self, *args, **kwargs: None})


def _dummy_error(name: str) -> type[Exception]:
    """Create a minimal import-safe service error stub."""

    return type(name, (Exception,), {})


def _chat_action_execution_error_class() -> type[Exception]:
    """Create the chat-action execution error stub with route-compatible attributes."""

    class ChatActionExecutionError(Exception):
        def __init__(
            self,
            *,
            status_code: int = 500,
            code: str = "execution_failed",
            message: str = "",
        ) -> None:
            super().__init__(message)
            self.status_code = status_code
            self.code = SimpleNamespace(value=code)
            self.message = message

    return ChatActionExecutionError


_task_dependency_stub = ModuleType("apps.api.app.dependencies.tasks")
_task_dependency_stub.TaskDispatcherDependency = object
_install_temporary_module("apps.api.app.dependencies.tasks", _task_dependency_stub)
_xlsxwriter_stub = ModuleType("xlsxwriter")
_xlsxwriter_stub.Workbook = object
_install_temporary_module("xlsxwriter", _xlsxwriter_stub)
_install_service_stub(
    "services.chat.grounding",
    ChatGroundingService=_dummy_class("ChatGroundingService"),
    ChatGroundingError=_dummy_error("ChatGroundingError"),
    ChatGroundingErrorCode=SimpleNamespace(ACCESS_DENIED="access_denied"),
    GroundingContextRecord=_dummy_class("GroundingContextRecord"),
)
_install_service_stub(
    "services.chat.service",
    ChatService=_dummy_class("ChatService"),
    ChatServiceError=_dummy_error("ChatServiceError"),
    ChatServiceErrorCode=SimpleNamespace(THREAD_NOT_FOUND="thread_not_found"),
)
_install_service_stub(
    "services.chat.action_router",
    ChatActionRouter=_dummy_class("ChatActionRouter"),
    ChatActionRouterError=_dummy_error("ChatActionRouterError"),
    ChatActionRouterErrorCode=SimpleNamespace(ROUTING_FAILED="routing_failed"),
)
_install_service_stub(
    "services.chat.action_execution",
    ChatActionExecutor=_dummy_class("ChatActionExecutor"),
    ChatActionExecutionError=_chat_action_execution_error_class(),
)
_install_service_stub(
    "services.chat.proposed_changes",
    ProposedChangesService=_dummy_class("ProposedChangesService"),
    ProposedChangesError=_dummy_error("ProposedChangesError"),
    ProposedChangesErrorCode=SimpleNamespace(NOT_FOUND="not_found"),
)
_install_service_stub(
    "services.documents.review_service",
    DocumentReviewService=_dummy_class("DocumentReviewService"),
)
_install_service_stub(
    "services.accounting.recommendation_apply",
    RecommendationApplyService=_dummy_class("RecommendationApplyService"),
)
_install_service_stub(
    "services.audit.service",
    AuditService=_dummy_class("AuditService"),
)
_install_service_stub(
    "services.close_runs.service",
    CloseRunService=_dummy_class("CloseRunService"),
)
_install_service_stub(
    "services.exports.service",
    ExportService=_dummy_class("ExportService"),
)
_install_service_stub(
    "services.jobs.service",
    JobService=_dummy_class("JobService"),
    JobRecord=_dummy_class("JobRecord"),
)
_install_service_stub(
    "services.model_gateway.client",
    ModelGateway=_dummy_class("ModelGateway"),
)
_install_service_stub(
    "services.reconciliation.service",
    ReconciliationService=_dummy_class("ReconciliationService"),
)
_install_service_stub(
    "services.reporting.service",
    ReportService=_dummy_class("ReportService"),
)

try:
    from apps.api.app.routes import chat as chat_routes
finally:
    _restore_temporary_modules()

from apps.api.app.routes.request_auth import AuthenticatedRequestContext
from dataclasses import dataclass
from fastapi import HTTPException, Response
from starlette.datastructures import UploadFile
from starlette.requests import Request
from services.contracts.chat_models import (
    AgentCoaSummary,
    AgentMemorySummary,
    AgentRunReadiness,
    AgentRunPhaseState,
    AgentToolManifestItem,
    AgentTraceRecord,
    ChatThreadWorkspaceResponse,
    GroundingContext,
)
from services.db.models.auth import UserStatus
from services.db.repositories.auth_repo import AuthUserRecord


@dataclass(frozen=True, slots=True)
class McpToolCallOutcome:
    """Capture one MCP tool execution result returned by the fake executor."""

    message_id: str
    tool_name: str
    status: str
    requires_human_approval: bool
    action_plan_id: str | None
    summary: str
    result: dict[str, object] | None


class FakeChatActionExecutor:
    """Provide deterministic chat-executor responses for API route tests."""

    def __init__(self) -> None:
        now = datetime.now(tz=timezone.utc)
        self.workspace = ChatThreadWorkspaceResponse(
            thread_id=str(uuid4()),
            grounding=GroundingContext(
                entity_id=str(uuid4()),
                entity_name="Acme Finance",
                close_run_id=str(uuid4()),
                period_label="Mar 2026",
                autonomy_mode="human_review",
                base_currency="USD",
            ),
            progress_summary="Documents parsed and recommendations are pending final approval.",
            coa=AgentCoaSummary(
                is_available=True,
                status="active",
                source="manual_upload",
                version_no=3,
                account_count=128,
                postable_account_count=104,
                requires_operator_upload=False,
                activated_at=now,
                summary="manual upload COA version 3 is active with 128 active accounts.",
                accounts=(),
            ),
            readiness=AgentRunReadiness(
                has_close_run=True,
                status="attention_required",
                blockers=(),
                warnings=("Pending chat approvals are waiting for operator review.",),
                next_actions=(
                    "Review and approve pending recommendations or journal drafts.",
                    "Generate reports and commentary for the current close run.",
                ),
                document_count=4,
                has_source_documents=True,
                parsed_document_count=4,
                phase_states=(
                    AgentRunPhaseState(
                        phase="document_collection",
                        label="Document collection",
                        status="completed",
                        blocking_reason=None,
                        completed_at=now,
                    ),
                ),
            ),
            memory=AgentMemorySummary(
                last_operator_message="Generate reports",
                last_assistant_response="Queued reporting run.",
                last_tool_name="generate_reports",
                last_action_status="applied",
                last_trace_id="trace-123",
                pending_action_count=2,
                progress_summary="Close run is waiting on approval and reporting.",
                recent_tool_names=("generate_recommendations", "generate_reports"),
                updated_at=now,
            ),
            tools=(
                AgentToolManifestItem(
                    name="generate_reports",
                    prompt_signature="generate_reports(template_id?, generate_commentary?, use_llm_commentary?)",
                    description="Create a report run and queue report generation.",
                    intent="report_action",
                    requires_human_approval=False,
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "generate_commentary": {"type": "boolean"},
                        },
                    },
                ),
            ),
            recent_traces=(
                AgentTraceRecord(
                    message_id=str(uuid4()),
                    created_at=now,
                    mode="mcp",
                    tool_name="generate_reports",
                    trace_id="trace-123",
                    action_status="applied",
                    summary="generate_reports completed",
                ),
            ),
            mcp_manifest={
                "protocol": "model-context-protocol",
                "version": "2025-11-25",
                "tools": [
                    {
                        "name": "generate_reports",
                        "description": "Create a report run and queue report generation.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "generate_commentary": {"type": "boolean"},
                            },
                        },
                    }
                ],
            },
        )
        self.tool_call_arguments: dict[str, object] | None = None
        self.sent_action_message: dict[str, object] | None = None

    def get_thread_workspace(
        self,
        *,
        thread_id,
        entity_id,
        actor_user,
    ) -> ChatThreadWorkspaceResponse:
        del thread_id, entity_id, actor_user
        return self.workspace

    def list_registered_tools(self) -> tuple[AgentToolManifestItem, ...]:
        return self.workspace.tools

    def execute_registered_tool(
        self,
        *,
        thread_id,
        entity_id,
        actor_user,
        tool_name,
        tool_arguments,
        trace_id,
        source_surface,
    ) -> McpToolCallOutcome:
        self.tool_call_arguments = {
            "thread_id": str(thread_id),
            "entity_id": str(entity_id),
            "actor_user_id": str(actor_user.id),
            "tool_name": tool_name,
            "tool_arguments": tool_arguments,
            "trace_id": trace_id,
            "source_surface": source_surface.value,
        }
        return McpToolCallOutcome(
            message_id=str(uuid4()),
            tool_name=tool_name,
            status="applied",
            requires_human_approval=False,
            action_plan_id=str(uuid4()),
            summary=f"Tool '{tool_name}' executed successfully.",
            result={"tool": tool_name, "status": "queued"},
        )

    def send_action_message(
        self,
        *,
        thread_id,
        entity_id,
        actor_user,
        content,
        message_grounding_payload=None,
        source_surface,
        trace_id,
    ):
        self.sent_action_message = {
            "thread_id": str(thread_id),
            "entity_id": str(entity_id),
            "actor_user_id": str(actor_user.id),
            "content": content,
            "message_grounding_payload": message_grounding_payload,
            "source_surface": source_surface.value,
            "trace_id": trace_id,
        }
        return SimpleNamespace(
            assistant_message_id=str(uuid4()),
            assistant_content="Inline attachments acknowledged.",
            action_plan=None,
            is_read_only=True,
        )


class FakeChatRepository:
    def __init__(self, *, close_run_id) -> None:
        self.thread = SimpleNamespace(close_run_id=close_run_id)
        self.messages: list[dict[str, object]] = []
        self.commit_count = 0
        self.rollback_count = 0

    def get_thread_for_entity(self, *, thread_id, entity_id):
        del thread_id, entity_id
        return self.thread

    def create_message(
        self,
        *,
        thread_id,
        role,
        content,
        message_type,
        linked_action_id,
        grounding_payload,
        model_metadata,
    ):
        message = SimpleNamespace(
            id=uuid4(),
            thread_id=thread_id,
            role=role,
            content=content,
            message_type=message_type,
            linked_action_id=linked_action_id,
            grounding_payload=grounding_payload,
            model_metadata=model_metadata,
        )
        self.messages.append(
            {
                "thread_id": str(thread_id),
                "role": role,
                "content": content,
                "message_type": message_type,
                "grounding_payload": grounding_payload,
                "model_metadata": model_metadata,
            }
        )
        return message

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


class FakeDocumentUploadService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def upload_documents(
        self,
        *,
        actor_user,
        entity_id,
        close_run_id,
        files,
        source_surface,
        trace_id,
    ):
        self.calls.append(
            {
                "actor_user_id": str(actor_user.id),
                "entity_id": str(entity_id),
                "close_run_id": str(close_run_id),
                "file_count": len(files),
                "source_surface": source_surface.value,
                "trace_id": trace_id,
            }
        )
        return SimpleNamespace(
            uploaded_documents=(
                SimpleNamespace(
                    document=SimpleNamespace(
                        id=str(uuid4()),
                        original_filename=files[0].filename,
                        file_size_bytes=len(files[0].payload),
                        mime_type="application/pdf",
                        status="uploaded",
                    )
                ),
            )
        )


class FakeCoaService:
    def upload_manual_coa(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            active_set=SimpleNamespace(
                source="manual_upload",
                version_no=4,
                account_count=132,
            )
        )


class FailingChatActionExecutor(FakeChatActionExecutor):
    """Raise the shared chat execution error when attachment follow-up planning fails."""

    def send_action_message(self, **kwargs):
        del kwargs
        raise chat_routes.ChatActionExecutionError(
            status_code=422,
            code="planning_failed",
            message="Unable to plan the attachment follow-up.",
        )


def test_chat_workspace_endpoint_returns_memory_tools_and_traces(monkeypatch) -> None:
    """Ensure the workspace endpoint exposes thread memory, tools, and traces."""

    executor = FakeChatActionExecutor()
    _install_browser_auth_stub(monkeypatch)
    thread_id = uuid4()
    entity_id = uuid4()

    result = chat_routes.read_chat_thread_workspace(
        thread_id=thread_id,
        entity_id=entity_id,
        action_executor=executor,
    )

    payload = result.model_dump(mode="json")
    assert payload["coa"]["source"] == "manual_upload"
    assert payload["readiness"]["status"] == "attention_required"
    assert payload["memory"]["pending_action_count"] == 2
    assert payload["tools"][0]["name"] == "generate_reports"
    assert payload["recent_traces"][0]["tool_name"] == "generate_reports"
    assert payload["mcp_manifest"]["version"] == "2025-11-25"
    assert executor.workspace.progress_summary in payload["progress_summary"]


def test_chat_tool_manifest_route_authenticates_with_current_session_api(monkeypatch) -> None:
    """Ensure the MCP manifest route uses authenticate_session for browser auth."""

    executor = FakeChatActionExecutor()
    captured_call: dict[str, object] = {}
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(version="0.1.0"),
            "method": "GET",
            "path": "/api/chat/tools/mcp",
            "headers": [],
        }
    )

    monkeypatch.setattr(chat_routes, "_read_session_cookie", lambda **kwargs: "session-token")

    class FakeAuthService:
        def authenticate_session(self, **kwargs):
            captured_call.update(kwargs)
            return SimpleNamespace(user=TEST_USER, session_token=None, rotated=False)

    manifest = chat_routes.read_chat_tool_manifest(
        request=request,
        response=Response(),
        settings=SimpleNamespace(),
        auth_service=FakeAuthService(),
        action_executor=executor,
    )

    assert captured_call["session_token"] == "session-token"
    assert manifest["version"] == "2025-11-25"
    assert manifest["tools"][0]["name"] == "generate_reports"


def test_chat_mcp_initialize_and_list_tools(monkeypatch) -> None:
    """Ensure the MCP endpoint serves initialize and tools/list responses."""

    del monkeypatch
    executor = FakeChatActionExecutor()
    response = Response()
    initialize = _run_mcp_call(
        payload={
            "jsonrpc": "2.0",
            "id": "init-1",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        response=response,
        executor=executor,
    )
    list_tools = _run_mcp_call(
        payload={"jsonrpc": "2.0", "id": "tools-1", "method": "tools/list"},
        response=Response(),
        executor=executor,
    )

    assert response.headers["MCP-Protocol-Version"] == "2025-11-25"
    assert initialize["result"]["protocolVersion"] == "2025-11-25"
    assert list_tools["result"]["tools"][0]["name"] == "generate_reports"


def test_chat_mcp_tool_call_executes_through_shared_executor(monkeypatch) -> None:
    """Ensure MCP tools/call uses the shared deterministic executor and returns structured content."""

    del monkeypatch
    executor = FakeChatActionExecutor()
    thread_id = uuid4()
    entity_id = uuid4()

    payload = _run_mcp_call(
        payload={
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": "generate_reports",
                "arguments": {"generate_commentary": True},
                "context": {
                    "entity_id": str(entity_id),
                    "thread_id": str(thread_id),
                    "trace_id": "trace-789",
                },
            },
        },
        response=Response(),
        executor=executor,
    )

    assert payload["result"]["structuredContent"]["status"] == "applied"
    assert payload["result"]["structuredContent"]["result"]["tool"] == "generate_reports"
    assert executor.tool_call_arguments == {
        "thread_id": str(thread_id),
        "entity_id": str(entity_id),
        "actor_user_id": str(TEST_USER.id),
        "tool_name": "generate_reports",
        "tool_arguments": {"generate_commentary": True},
        "trace_id": "trace-789",
        "source_surface": "cli",
    }


def test_chat_mcp_tool_call_uses_desktop_surface_for_browser_sessions(monkeypatch) -> None:
    """Ensure cookie-authenticated MCP calls preserve the desktop audit surface."""

    del monkeypatch
    executor = FakeChatActionExecutor()
    thread_id = uuid4()
    entity_id = uuid4()

    _run_mcp_call(
        payload={
            "jsonrpc": "2.0",
            "id": "call-browser-1",
            "method": "tools/call",
            "params": {
                "name": "generate_reports",
                "arguments": {},
                "context": {
                    "entity_id": str(entity_id),
                    "thread_id": str(thread_id),
                },
            },
        },
        response=Response(),
        executor=executor,
        authenticated_via="browser_session",
    )

    assert executor.tool_call_arguments is not None
    assert executor.tool_call_arguments["source_surface"] == "desktop"


def test_chat_mcp_tool_call_requires_context_ids(monkeypatch) -> None:
    """Ensure MCP tools/call fails fast when required context identifiers are missing."""

    del monkeypatch
    payload = _run_mcp_call(
        payload={
            "jsonrpc": "2.0",
            "id": "call-2",
            "method": "tools/call",
            "params": {
                "name": "generate_reports",
                "arguments": {},
                "context": {"entity_id": str(uuid4())},
            },
        },
        response=Response(),
        executor=FakeChatActionExecutor(),
    )

    assert payload["error"]["code"] == -32602
    assert "thread_id" in payload["error"]["message"]


def test_chat_action_attachment_route_ingests_source_documents(monkeypatch) -> None:
    """Ensure inline source-document attachments route through canonical upload before chat."""

    _install_browser_auth_stub(monkeypatch)
    monkeypatch.setattr(chat_routes, "require_active_close_run_phase", lambda **kwargs: None)
    executor = FakeChatActionExecutor()
    repository = FakeChatRepository(close_run_id=uuid4())
    document_upload_service = FakeDocumentUploadService()
    entity_id = uuid4()
    thread_id = uuid4()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(version="0.1.0"),
            "method": "POST",
            "path": f"/api/chat/threads/{thread_id}/actions/attachments",
            "headers": [],
        }
    )
    file = UploadFile(filename="invoice.pdf", file=BytesIO(b"%PDF-1.4 test"))

    result = asyncio.run(
        chat_routes.send_chat_action_with_attachments(
            thread_id=thread_id,
            entity_id=entity_id,
            request=request,
            response=Response(),
            settings=SimpleNamespace(),
            auth_service=SimpleNamespace(),
            action_executor=executor,
            chat_repository=repository,
            db_session=SimpleNamespace(),
            document_upload_service=document_upload_service,
            coa_service=FakeCoaService(),
            content="Start recommendations after intake.",
            attachment_intent="source_documents",
            files=(file,),
        )
    )

    assert result.content == "Inline attachments acknowledged."
    assert document_upload_service.calls[0]["file_count"] == 1
    assert executor.sent_action_message is not None
    assert executor.sent_action_message["message_grounding_payload"]["attachment_intent"] == (
        "source_documents"
    )
    attachments = executor.sent_action_message["message_grounding_payload"]["attachments"]
    assert attachments[0]["filename"] == "invoice.pdf"
    assert "queued for parsing" in executor.sent_action_message["content"]


def test_chat_action_attachment_route_reports_partial_success_when_follow_up_fails(monkeypatch) -> None:
    """Ensure successful attachment ingestion is not reported as a hard failure."""

    _install_browser_auth_stub(monkeypatch)
    monkeypatch.setattr(chat_routes, "require_active_close_run_phase", lambda **kwargs: None)
    repository = FakeChatRepository(close_run_id=uuid4())
    document_upload_service = FakeDocumentUploadService()
    entity_id = uuid4()
    thread_id = uuid4()
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(version="0.1.0"),
            "method": "POST",
            "path": f"/api/chat/threads/{thread_id}/actions/attachments",
            "headers": [],
        }
    )
    file = UploadFile(filename="invoice.pdf", file=BytesIO(b"%PDF-1.4 test"))

    result = asyncio.run(
        chat_routes.send_chat_action_with_attachments(
            thread_id=thread_id,
            entity_id=entity_id,
            request=request,
            response=Response(),
            settings=SimpleNamespace(),
            auth_service=SimpleNamespace(),
            action_executor=FailingChatActionExecutor(),
            chat_repository=repository,
            db_session=SimpleNamespace(),
            document_upload_service=document_upload_service,
            coa_service=FakeCoaService(),
            content="Start recommendations after intake.",
            attachment_intent="source_documents",
            files=(file,),
        )
    )

    assert result.is_read_only is True
    assert result.action_plan is None
    assert "upload completed successfully" in result.content.lower()
    assert "without re-uploading the files" in result.content
    assert document_upload_service.calls[0]["file_count"] == 1
    assert repository.commit_count == 1
    assert repository.rollback_count == 0
    assert [message["role"] for message in repository.messages] == ["user", "assistant"]
    assert repository.messages[1]["message_type"] == "warning"


def _install_browser_auth_stub(monkeypatch) -> None:
    """Install a deterministic browser auth stub for direct route tests."""

    monkeypatch.setattr(
        chat_routes,
        "_require_authenticated_browser_session",
        lambda **kwargs: type("SessionResult", (), {"user": TEST_USER})(),
    )

def _run_mcp_call(
    *,
    payload: dict[str, object],
    response: Response,
    executor: FakeChatActionExecutor,
    authenticated_via: str = "api_token",
) -> dict[str, object]:
    """Execute the async MCP route directly and return its JSON-RPC payload."""

    import asyncio

    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(version="0.1.0"),
            "method": "POST",
            "path": "/api/chat/mcp",
            "headers": [],
        }
    )
    result = asyncio.run(
        chat_routes.handle_chat_mcp_request(
            payload=payload,
            request=request,
            response=response,
            auth_context=AuthenticatedRequestContext(
                user=TEST_USER,
                authenticated_via=authenticated_via,
            ),
            action_executor=executor,
        )
    )
    assert isinstance(result, dict)
    return result


TEST_USER = AuthUserRecord(
    id=uuid4(),
    email="operator@example.com",
    password_hash="hashed",
    full_name="Operator Example",
    status=UserStatus.ACTIVE,
    last_login_at=None,
)
