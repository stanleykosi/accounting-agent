"""
Purpose: Orchestrate chat threads, messages, and read-only finance copilot
analysis responses grounded in workflow state.
Scope: Thread creation, listing, message history, and the send-message flow
that answers questions using workflow state, extracted values, and evidence
without allowing state changes. This is the read-only analysis foundation
that Step 35 extends with action routing.
Dependencies: Chat repository, grounding service, model gateway, audit
service, and the canonical chat contracts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from services.auth.service import serialize_uuid
from services.chat.grounding import (
    ChatGroundingError,
    ChatGroundingService,
)
from services.chat.operator_memory import seed_context_payload_with_operator_memory
from services.contracts.chat_models import (
    ChatMessageRecord,
    ChatMessageResponse,
    ChatThreadDeleteResponse,
    ChatThreadListResponse,
    ChatThreadSummary,
    ChatThreadWithMessages,
    CreateChatThreadRequest,
    GroundingContext,
)
from services.db.models.audit import AuditSourceSurface
from services.db.repositories.chat_repo import (
    ChatMessageRecord as RepoChatMessage,
)
from services.db.repositories.chat_repo import (
    ChatThreadRecord,
    ChatThreadWithCountRecord,
)


class ChatServiceErrorCode(StrEnum):
    """Enumerate the stable error codes surfaced by chat workflows."""

    THREAD_NOT_FOUND = "thread_not_found"
    THREAD_ACCESS_DENIED = "thread_access_denied"
    INVALID_MESSAGE = "invalid_message"
    GROUNDING_ERROR = "grounding_error"
    MODEL_ERROR = "model_error"


class ChatServiceError(Exception):
    """Represent an expected chat-domain failure that API routes expose cleanly."""

    def __init__(
        self,
        *,
        status_code: int,
        code: ChatServiceErrorCode,
        message: str,
    ) -> None:
        """Capture the HTTP status, stable error code, and operator-facing recovery message."""

        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ModelGatewayProtocol(Protocol):
    """Describe the model gateway operations required by the chat service."""

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
    ) -> str:
        """Send a chat-completion request and return the assistant content string."""


class ChatRepositoryProtocol(Protocol):
    """Describe the persistence operations required by the chat service."""

    def create_thread(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        context_payload: dict[str, Any],
        title: str | None,
    ) -> ChatThreadRecord:
        """Persist a new chat thread."""

    def get_thread_for_entity(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
    ) -> ChatThreadRecord | None:
        """Return one thread when it belongs to the specified entity."""

    def list_threads_for_entity(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        limit: int,
    ) -> tuple[ChatThreadWithCountRecord, ...]:
        """Return threads for an entity with message counts."""

    def list_recent_threads_for_entity_any_scope(
        self,
        *,
        entity_id: UUID,
        limit: int,
        exclude_thread_id: UUID | None = None,
    ) -> tuple[ChatThreadRecord, ...]:
        """Return recent threads across all scopes for one entity."""

    def list_recent_threads_for_user_any_scope(
        self,
        *,
        user_id: UUID,
        limit: int,
        exclude_thread_id: UUID | None = None,
    ) -> tuple[ChatThreadRecord, ...]:
        """Return recent threads across all accessible workspaces for one user."""

    def create_message(
        self,
        *,
        thread_id: UUID,
        role: str,
        content: str,
        message_type: str,
        linked_action_id: UUID | None,
        grounding_payload: dict[str, Any],
        model_metadata: dict[str, Any] | None,
    ) -> RepoChatMessage:
        """Persist a new chat message."""

    def list_messages_for_thread(
        self,
        *,
        thread_id: UUID,
        limit: int | None = None,
    ) -> tuple[RepoChatMessage, ...]:
        """Return messages for a thread ordered oldest-first."""

    def get_message_count_for_thread(self, *, thread_id: UUID) -> int:
        """Return the total number of messages in a thread."""

    def get_last_message_time_for_thread(self, *, thread_id: UUID) -> Any:
        """Return the timestamp of the most recent message in a thread."""

    def delete_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
    ) -> bool:
        """Delete one thread when it belongs to the specified entity."""

    def commit(self) -> None:
        """Commit the current transaction."""

    def rollback(self) -> None:
        """Rollback the current transaction."""


class EntityMembershipProtocol(Protocol):
    """Describe the entity membership check required to gate chat access."""

    def get_entity_for_user(
        self,
        *,
        entity_id: UUID,
        user_id: UUID,
    ) -> Any:
        """Return entity access when the user is a member, or None."""


class ChatService:
    """Provide the canonical chat workflow used by the API and desktop UI.

    This service implements read-only analysis flows that answer questions
    using workflow state, extracted values, rules, and evidence. It does not
    allow state changes -- that capability is added in Step 35 with action
    routing and proposed edits.
    """

    def __init__(
        self,
        *,
        repository: ChatRepositoryProtocol,
        grounding_service: ChatGroundingService,
        model_gateway: ModelGatewayProtocol,
        entity_repo: EntityMembershipProtocol,
    ) -> None:
        """Capture the persistence boundary, grounding resolver, and model gateway."""

        self._repository = repository
        self._grounding = grounding_service
        self._model_gateway = model_gateway
        self._entity_repo = entity_repo

    def create_thread(
        self,
        *,
        request: CreateChatThreadRequest,
        user_id: UUID,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatThreadSummary:
        """Create a new grounded chat thread scoped to an entity and optional close run."""

        try:
            grounding_record = self._grounding.resolve_context(
                entity_id=UUID(request.entity_id),
                close_run_id=UUID(request.close_run_id) if request.close_run_id else None,
                user_id=user_id,
            )
            context_payload = self._grounding.build_context_payload(
                context=grounding_record.context
            )
            recent_threads = self._repository.list_recent_threads_for_entity_any_scope(
                entity_id=grounding_record.entity.id,
                limit=8,
            )
            cross_workspace_recent_threads = (
                self._repository.list_recent_threads_for_user_any_scope(
                    user_id=user_id,
                    limit=12,
                )
            )
            context_payload = seed_context_payload_with_operator_memory(
                context_payload=context_payload,
                recent_context_payloads=tuple(
                    thread.context_payload for thread in recent_threads
                ),
                cross_workspace_recent_context_payloads=tuple(
                    thread.context_payload for thread in cross_workspace_recent_threads
                ),
            )

            thread = self._repository.create_thread(
                entity_id=grounding_record.entity.id,
                close_run_id=grounding_record.close_run.id if grounding_record.close_run else None,
                context_payload=context_payload,
                title=request.title,
            )
            self._repository.commit()
        except ChatGroundingError as error:
            raise ChatServiceError(
                status_code=error.status_code,
                code=ChatServiceErrorCode.GROUNDING_ERROR,
                message=error.message,
            ) from error
        except Exception:
            self._repository.rollback()
            raise

        return self._build_thread_summary(thread, message_count=0, last_message_at=None)

    def list_threads(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        user_id: UUID,
        limit: int = 50,
    ) -> ChatThreadListResponse:
        """Return threads for an entity or close run, ordered newest-first.

        Requires explicit entity membership so that multi-user deployments
        cannot enumerate threads from workspaces the caller does not belong to.
        """

        self._require_entity_membership(entity_id=entity_id, user_id=user_id)
        records = self._repository.list_threads_for_entity(
            entity_id=entity_id,
            close_run_id=close_run_id,
            limit=limit,
        )
        threads = tuple(
            self._build_thread_summary(
                record.thread,
                message_count=record.message_count,
                last_message_at=record.last_message_at,
            )
            for record in records
        )
        return ChatThreadListResponse(threads=threads)

    def get_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        user_id: UUID,
        message_limit: int = 100,
    ) -> ChatThreadWithMessages:
        """Return one thread with its message history for detail views.

        Requires entity membership so users cannot read threads from
        workspaces they do not belong to.
        """

        self._require_entity_membership(entity_id=entity_id, user_id=user_id)
        thread = self._repository.get_thread_for_entity(
            thread_id=thread_id,
            entity_id=entity_id,
        )
        if thread is None:
            raise ChatServiceError(
                status_code=404,
                code=ChatServiceErrorCode.THREAD_NOT_FOUND,
                message="That chat thread does not exist or is not in this workspace.",
            )

        messages = self._repository.list_messages_for_thread(
            thread_id=thread_id,
            limit=message_limit,
        )
        message_count = self._repository.get_message_count_for_thread(thread_id=thread_id)
        last_message_at = self._repository.get_last_message_time_for_thread(thread_id=thread_id)

        thread_summary = self._build_thread_summary(
            thread,
            message_count=message_count,
            last_message_at=last_message_at,
        )
        message_records = tuple(
            self._map_message_to_contract(message) for message in messages
        )
        return ChatThreadWithMessages(
            thread=thread_summary,
            messages=message_records,
        )

    def delete_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        user_id: UUID,
    ) -> ChatThreadDeleteResponse:
        """Delete one accessible chat thread and its message history."""

        self._require_entity_membership(entity_id=entity_id, user_id=user_id)
        thread = self._repository.get_thread_for_entity(
            thread_id=thread_id,
            entity_id=entity_id,
        )
        if thread is None:
            raise ChatServiceError(
                status_code=404,
                code=ChatServiceErrorCode.THREAD_NOT_FOUND,
                message="That chat thread does not exist or is not in this workspace.",
            )

        message_count = self._repository.get_message_count_for_thread(thread_id=thread_id)
        try:
            deleted = self._repository.delete_thread(thread_id=thread_id, entity_id=entity_id)
            if not deleted:
                raise ChatServiceError(
                    status_code=404,
                    code=ChatServiceErrorCode.THREAD_NOT_FOUND,
                    message="That chat thread does not exist or is not in this workspace.",
                )
            self._repository.commit()
        except ChatServiceError:
            self._repository.rollback()
            raise
        except Exception:
            self._repository.rollback()
            raise

        return ChatThreadDeleteResponse(
            deleted_thread_id=serialize_uuid(thread.id),
            deleted_thread_title=thread.title,
            deleted_message_count=message_count,
        )

    def send_message(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        user_id: str,
        content: str,
        source_surface: AuditSourceSurface,
        trace_id: str | None,
    ) -> ChatMessageResponse:
        """Persist a user message and generate a grounded read-only
        assistant response.

        This is the core read-only copilot flow. The assistant answers questions
        using the thread's grounding context (entity, close run, period) without
        modifying any workflow state. Action intents are classified but not executed
        -- they are returned as analysis responses with evidence references.

        Requires entity membership so that messages cannot be sent to threads
        in workspaces the caller does not belong to.
        """

        self._require_entity_membership(entity_id=entity_id, user_id=UUID(user_id))
        thread = self._repository.get_thread_for_entity(
            thread_id=thread_id,
            entity_id=entity_id,
        )
        if thread is None:
            raise ChatServiceError(
                status_code=404,
                code=ChatServiceErrorCode.THREAD_ACCESS_DENIED,
                message="That chat thread does not exist or is not in this workspace.",
            )

        grounding = self._grounding.parse_context_payload(payload=thread.context_payload)

        try:
            user_message = self._repository.create_message(
                thread_id=thread_id,
                role="user",
                content=content,
                message_type="analysis",
                linked_action_id=None,
                grounding_payload={},
                model_metadata=None,
            )

            messages = self._repository.list_messages_for_thread(thread_id=thread_id, limit=50)
            system_prompt = self._build_system_prompt(grounding=grounding)
            conversation_history = self._build_conversation_history(
                messages, system_prompt=system_prompt
            )

            assistant_text = self._model_gateway.complete(messages=conversation_history)

            grounding_evidence = self._build_grounding_evidence(grounding=grounding)
            message_type = self._classify_message_type(content=content)

            assistant_message = self._repository.create_message(
                thread_id=thread_id,
                role="assistant",
                content=assistant_text,
                message_type=message_type,
                linked_action_id=None,
                grounding_payload=grounding_evidence,
                model_metadata={"provider": "openrouter", "model": "default"},
            )
            self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise

        return ChatMessageResponse(
            message=self._map_message_to_contract(assistant_message),
            user_message=self._map_message_to_contract(user_message),
        )

    def _build_thread_summary(
        self,
        thread: ChatThreadRecord,
        *,
        message_count: int,
        last_message_at: Any,
    ) -> ChatThreadSummary:
        """Convert a thread record into the shared summary contract."""

        grounding = self._grounding.parse_context_payload(payload=thread.context_payload)
        return ChatThreadSummary(
            id=serialize_uuid(thread.id),
            entity_id=serialize_uuid(thread.entity_id),
            close_run_id=(
                serialize_uuid(thread.close_run_id) if thread.close_run_id else None
            ),
            title=thread.title,
            grounding=grounding,
            message_count=message_count,
            last_message_at=last_message_at,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
        )

    def _map_message_to_contract(self, record: RepoChatMessage) -> ChatMessageRecord:
        """Convert a repository message record into the shared contract."""

        return ChatMessageRecord(
            id=serialize_uuid(record.id),
            thread_id=serialize_uuid(record.thread_id),
            role=record.role,  # type: ignore[arg-type]
            content=record.content,
            message_type=record.message_type,  # type: ignore[arg-type]
            linked_action_id=(
                serialize_uuid(record.linked_action_id) if record.linked_action_id else None
            ),
            grounding_payload=record.grounding_payload,
            model_metadata=record.model_metadata,
            created_at=record.created_at,
        )

    def _build_conversation_history(
        self,
        messages: tuple[RepoChatMessage, ...],
        *,
        system_prompt: str,
    ) -> list[dict[str, str]]:
        """Convert stored messages into the chat-completion message list format.

        Prepends the system prompt as the first message so the model receives
        grounding context before any user questions.
        """

        history: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        for message in messages:
            history.append({"role": message.role, "content": message.content})
        return history

    def _build_system_prompt(self, *, grounding: GroundingContext) -> str:
        """Build the system prompt grounded in the current entity and close run context."""

        context_lines = [
            f"You are an accounting copilot assistant for the workspace '{grounding.entity_name}'.",
            f"Base currency: {grounding.base_currency}.",
            f"Autonomy mode: {grounding.autonomy_mode}.",
        ]

        if grounding.close_run_id and grounding.period_label:
            context_lines.append(
                f"Current close run period: {grounding.period_label}. "
                "Answer questions using evidence from this period's documents and workflow state."
            )

        context_lines.extend([
            "Your responses must be:",
            "- Grounded in actual workflow state, extracted values, and source documents.",
            "- Factual and evidence-based. Do not hallucinate values or statuses.",
            "- Read-only analysis. You cannot modify workflow state or approve changes.",
            "- Clear about uncertainty. If confidence is low, say so explicitly.",
            "Reference evidence sources when available.",
        ])

        return "\n".join(context_lines)

    def _build_grounding_evidence(
        self,
        *,
        grounding: GroundingContext,
    ) -> dict[str, Any]:
        """Build the evidence snapshot attached to assistant responses."""

        evidence: dict[str, Any] = {
            "entity_id": grounding.entity_id,
            "entity_name": grounding.entity_name,
            "autonomy_mode": grounding.autonomy_mode,
            "base_currency": grounding.base_currency,
        }

        if grounding.close_run_id:
            evidence["close_run_id"] = grounding.close_run_id
            evidence["period_label"] = grounding.period_label

        return evidence

    def _require_entity_membership(self, *, entity_id: UUID, user_id: UUID) -> None:
        """Fail fast when the caller is not a member of the target entity workspace.

        This guard prevents cross-workspace enumeration or message injection in
        multi-user deployments by checking membership before any thread or message
        data is returned or created.
        """

        access = self._entity_repo.get_entity_for_user(
            entity_id=entity_id,
            user_id=user_id,
        )
        if access is None:
            raise ChatServiceError(
                status_code=403,
                code=ChatServiceErrorCode.THREAD_ACCESS_DENIED,
                message="You are not a member of this workspace.",
            )

    def _classify_message_type(self, *, content: str) -> str:
        """Classify the user message intent for downstream UI rendering.

        This is a simple heuristic classifier for read-only analysis messages.
        Step 35 adds the full action router that creates proposed edits and
        approval requests.
        """

        content_lower = content.lower()

        action_keywords = ("approve", "reject", "post", "export", "delete", "create journal")
        if any(keyword in content_lower for keyword in action_keywords):
            return "action"

        warning_keywords = ("risk", "error", "fraud", "discrepancy", "anomal")
        if any(keyword in content_lower for keyword in warning_keywords):
            return "warning"

        workflow_keywords = ("phase", "workflow", "next step", "status", "progress")
        if any(keyword in content_lower for keyword in workflow_keywords):
            return "workflow"

        return "analysis"


__all__ = [
    "ChatService",
    "ChatServiceError",
    "ChatServiceErrorCode",
]
