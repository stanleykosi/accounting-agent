"""
Purpose: Verify grounded chat-service thread lifecycle behavior.
Scope: Unit coverage for thread creation carry-forward, membership-gated deletion,
and structured not-found failures.
Dependencies: Chat service, grounding contracts, and repository record dataclasses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from services.chat.grounding import GroundingContextRecord
from services.chat.service import ChatService, ChatServiceError, ChatServiceErrorCode
from services.common.enums import AutonomyMode
from services.contracts.chat_models import CreateChatThreadRequest, GroundingContext
from services.db.models.audit import AuditSourceSurface
from services.db.models.entity import EntityStatus
from services.db.repositories.chat_repo import ChatThreadRecord
from services.db.repositories.entity_repo import (
    EntityAccessRecord,
    EntityMembershipRecord,
    EntityRecord,
    EntityUserRecord,
)


def test_delete_thread_removes_thread_and_returns_deleted_message_count() -> None:
    """Deleting a thread should remove it and return a stable summary payload."""

    repository = InMemoryChatRepository()
    service = build_chat_service(repository=repository)

    response = service.delete_thread(
        thread_id=repository.thread.id,
        entity_id=repository.thread.entity_id,
        user_id=repository.member.id,
    )

    assert response.deleted_thread_id == str(repository.thread.id)
    assert response.deleted_thread_title == repository.thread.title
    assert response.deleted_message_count == 4
    assert repository.deleted_thread_ids == [repository.thread.id]
    assert repository.committed is True


def test_delete_thread_requires_workspace_membership() -> None:
    """Deleting a thread should fail fast when the caller is not a member of the workspace."""

    repository = InMemoryChatRepository()
    entity_repo = InMemoryChatEntityRepository(member_has_access=False)
    service = build_chat_service(repository=repository, entity_repo=entity_repo)

    with pytest.raises(ChatServiceError) as error:
        service.delete_thread(
            thread_id=repository.thread.id,
            entity_id=repository.thread.entity_id,
            user_id=uuid4(),
        )

    assert error.value.status_code == 403
    assert error.value.code is ChatServiceErrorCode.THREAD_ACCESS_DENIED
    assert repository.deleted_thread_ids == []


def test_delete_thread_rejects_missing_thread() -> None:
    """Deleting an unknown thread should raise the canonical not-found error."""

    repository = InMemoryChatRepository(thread_exists=False)
    service = build_chat_service(repository=repository)

    with pytest.raises(ChatServiceError) as error:
        service.delete_thread(
            thread_id=uuid4(),
            entity_id=repository.entity.id,
            user_id=repository.member.id,
        )

    assert error.value.status_code == 404
    assert error.value.code is ChatServiceErrorCode.THREAD_NOT_FOUND
    assert repository.committed is False


def test_create_thread_seeds_cross_thread_operator_memory() -> None:
    """New threads should carry operator preferences and recent targets forward."""

    repository = InMemoryChatRepository()
    recent_thread = ChatThreadRecord(
        id=uuid4(),
        entity_id=repository.entity.id,
        close_run_id=None,
        title="Prior period thread",
        context_payload={
            "agent_memory": {
                "preferred_explanation_depth": "brief",
                "preferred_confirmation_style": "direct_when_clear",
            },
            "agent_recent_objectives": ("Close March quickly.",),
            "agent_recent_entity_names": ("Acme Workspace",),
            "agent_recent_period_labels": ("Mar 2026",),
            "agent_last_async_turn": {
                "status": "completed",
                "objective": "Generate the report pack.",
                "final_note": "Report pack delivered.",
            },
        },
        created_at=repository.thread.created_at,
        updated_at=repository.thread.updated_at,
    )
    repository.recent_threads = [recent_thread]
    service = build_chat_service(repository=repository)

    response = service.create_thread(
        request=CreateChatThreadRequest(
            entity_id=str(repository.entity.id),
            title="New operator thread",
        ),
        user_id=repository.member.id,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-create-thread",
    )

    assert response.title == "New operator thread"
    created_payload = repository.created_threads[-1].context_payload
    memory = created_payload["agent_memory"]
    assert memory["preferred_explanation_depth"] == "brief"
    assert memory["preferred_confirmation_style"] == "direct_when_clear"
    assert memory["recent_objectives"] == ("Close March quickly.",)
    assert created_payload["agent_recent_entity_names"] == ("Acme Workspace",)
    assert created_payload["agent_recent_period_labels"] == ("Mar 2026",)
    assert memory["last_async_status"] == "completed"
    assert memory["last_async_note"] == "Report pack delivered."
    assert repository.committed is True


def test_create_thread_carries_preferences_across_workspaces() -> None:
    """New threads should inherit operator preferences from recent chats in other workspaces."""

    repository = InMemoryChatRepository()
    cross_workspace_thread = ChatThreadRecord(
        id=uuid4(),
        entity_id=uuid4(),
        close_run_id=None,
        title="Cross-workspace preference thread",
        context_payload={
            "agent_memory": {
                "preferred_explanation_depth": "brief",
                "preferred_confirmation_style": "direct_when_clear",
                "recent_tool_names": ("generate_reports",),
                "recent_tool_namespaces": ("reporting_and_release",),
            }
        },
        created_at=repository.thread.created_at,
        updated_at=repository.thread.updated_at,
    )
    repository.recent_user_threads = [cross_workspace_thread]
    service = build_chat_service(repository=repository)

    response = service.create_thread(
        request=CreateChatThreadRequest(
            entity_id=str(repository.entity.id),
            title="Cross-workspace carry-forward",
        ),
        user_id=repository.member.id,
        source_surface=AuditSourceSurface.DESKTOP,
        trace_id="trace-cross-workspace-thread",
    )

    assert response.title == "Cross-workspace carry-forward"
    created_payload = repository.created_threads[-1].context_payload
    memory = created_payload["agent_memory"]
    assert memory["preferred_explanation_depth"] == "brief"
    assert memory["preferred_confirmation_style"] == "direct_when_clear"
    assert created_payload["agent_recent_tool_names"] == ("generate_reports",)
    assert created_payload["agent_recent_tool_namespaces"] == ("reporting_and_release",)


def build_chat_service(
    *,
    repository: InMemoryChatRepository,
    entity_repo: InMemoryChatEntityRepository | None = None,
) -> ChatService:
    """Construct the chat service with deterministic in-memory doubles."""

    return ChatService(
        repository=repository,
        grounding_service=InMemoryGroundingService(),
        model_gateway=InMemoryModelGateway(),
        entity_repo=entity_repo or InMemoryChatEntityRepository(
            member_has_access=True,
            entity=repository.entity,
            member=repository.member,
        ),
    )


class InMemoryChatRepository:
    """Provide the minimal chat repository surface used by the thread lifecycle tests."""

    def __init__(self, *, thread_exists: bool = True) -> None:
        now = datetime.now(tz=UTC)
        self.member = EntityUserRecord(
            id=uuid4(),
            email="operator@example.com",
            full_name="Operator User",
        )
        self.entity = EntityRecord(
            id=uuid4(),
            name="Acme Workspace",
            legal_name="Acme Workspace LLC",
            base_currency="USD",
            country_code="US",
            timezone="America/New_York",
            accounting_standard="US GAAP",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            default_confidence_thresholds={
                "classification": 0.85,
                "coding": 0.85,
                "reconciliation": 0.9,
                "posting": 0.95,
            },
            status=EntityStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self.membership = EntityMembershipRecord(
            id=uuid4(),
            entity_id=self.entity.id,
            user_id=self.member.id,
            role="owner",
            is_default_actor=True,
            created_at=now,
            updated_at=now,
            user=self.member,
        )
        self.thread = ChatThreadRecord(
            id=uuid4(),
            entity_id=self.entity.id,
            close_run_id=None,
            title="March review thread",
            context_payload={
                "entity_id": str(self.entity.id),
                "entity_name": self.entity.name,
                "close_run_id": None,
                "period_label": "Mar 2026",
                "autonomy_mode": "human_review",
                "base_currency": "USD",
            },
            created_at=now,
            updated_at=now,
        )
        self.thread_exists = thread_exists
        self.recent_threads: list[ChatThreadRecord] = []
        self.recent_user_threads: list[ChatThreadRecord] = []
        self.created_threads: list[ChatThreadRecord] = []
        self.deleted_thread_ids: list[UUID] = []
        self.committed = False
        self.rolled_back = False

    def create_thread(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        context_payload: dict[str, object],
        title: str | None,
    ) -> ChatThreadRecord:
        created_thread = ChatThreadRecord(
            id=uuid4(),
            entity_id=entity_id,
            close_run_id=close_run_id,
            title=title,
            context_payload=dict(context_payload),
            created_at=self.thread.created_at,
            updated_at=self.thread.updated_at,
        )
        self.created_threads.append(created_thread)
        self.thread = created_thread
        self.thread_exists = True
        return created_thread

    def get_thread_for_entity(self, *, thread_id: UUID, entity_id: UUID) -> ChatThreadRecord | None:
        if not self.thread_exists:
            return None
        if thread_id != self.thread.id or entity_id != self.thread.entity_id:
            return None
        return self.thread

    def get_message_count_for_thread(self, *, thread_id: UUID) -> int:
        if thread_id != self.thread.id or not self.thread_exists:
            return 0
        return 4

    def get_last_message_time_for_thread(self, *, thread_id: UUID) -> datetime | None:
        if thread_id != self.thread.id or not self.thread_exists:
            return None
        return self.thread.updated_at

    def list_recent_threads_for_entity_any_scope(
        self,
        *,
        entity_id: UUID,
        limit: int,
        exclude_thread_id: UUID | None = None,
    ) -> tuple[ChatThreadRecord, ...]:
        if entity_id != self.entity.id:
            return ()
        threads = [
            thread
            for thread in self.recent_threads
            if exclude_thread_id is None or thread.id != exclude_thread_id
        ]
        return tuple(threads[:limit])

    def list_recent_threads_for_user_any_scope(
        self,
        *,
        user_id: UUID,
        limit: int,
        exclude_thread_id: UUID | None = None,
    ) -> tuple[ChatThreadRecord, ...]:
        if user_id != self.member.id:
            return ()
        threads = [
            thread
            for thread in self.recent_user_threads
            if exclude_thread_id is None or thread.id != exclude_thread_id
        ]
        return tuple(threads[:limit])

    def delete_thread(self, *, thread_id: UUID, entity_id: UUID) -> bool:
        if (
            not self.thread_exists
            or thread_id != self.thread.id
            or entity_id != self.thread.entity_id
        ):
            return False
        self.deleted_thread_ids.append(thread_id)
        self.thread_exists = False
        return True

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryChatEntityRepository:
    """Provide the membership gate required by the chat service."""

    def __init__(
        self,
        *,
        member_has_access: bool,
        entity: EntityRecord | None = None,
        member: EntityUserRecord | None = None,
    ) -> None:
        self.member_has_access = member_has_access
        self.entity = entity
        self.member = member

    def get_entity_for_user(self, *, entity_id: UUID, user_id: UUID) -> EntityAccessRecord | None:
        if not self.member_has_access or self.entity is None or self.member is None:
            return None
        if entity_id != self.entity.id or user_id != self.member.id:
            return None
        membership = EntityMembershipRecord(
            id=uuid4(),
            entity_id=self.entity.id,
            user_id=self.member.id,
            role="owner",
            is_default_actor=True,
            created_at=self.entity.created_at,
            updated_at=self.entity.updated_at,
            user=self.member,
        )
        return EntityAccessRecord(entity=self.entity, membership=membership)


class InMemoryGroundingService:
    """Return deterministic grounding payloads for chat-service unit coverage."""

    def resolve_context(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        user_id: UUID,
    ) -> GroundingContextRecord:
        now = datetime.now(tz=UTC)
        entity = EntityRecord(
            id=entity_id,
            name="Acme Workspace",
            legal_name="Acme Workspace LLC",
            base_currency="USD",
            country_code="US",
            timezone="America/New_York",
            accounting_standard="US GAAP",
            autonomy_mode=AutonomyMode.HUMAN_REVIEW,
            default_confidence_thresholds={
                "classification": 0.85,
                "coding": 0.85,
                "reconciliation": 0.9,
                "posting": 0.95,
            },
            status=EntityStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        return GroundingContextRecord(
            entity=entity,
            close_run=None,
            context=GroundingContext(
                entity_id=str(entity_id),
                entity_name=entity.name,
                close_run_id=str(close_run_id) if close_run_id is not None else None,
                period_label=None,
                autonomy_mode="human_review",
                base_currency="USD",
            ),
        )

    def build_context_payload(self, *, context: GroundingContext) -> dict[str, object]:
        return {
            "entity_id": context.entity_id,
            "entity_name": context.entity_name,
            "close_run_id": context.close_run_id,
            "period_label": context.period_label,
            "autonomy_mode": context.autonomy_mode,
            "base_currency": context.base_currency,
        }

    def parse_context_payload(self, *, payload: dict[str, object]) -> GroundingContext:
        return GroundingContext(
            entity_id=str(payload["entity_id"]),
            entity_name=str(payload["entity_name"]),
            close_run_id=(
                str(payload["close_run_id"])
                if payload.get("close_run_id") is not None
                else None
            ),
            period_label=(
                str(payload["period_label"])
                if payload.get("period_label") is not None
                else None
            ),
            autonomy_mode=str(payload["autonomy_mode"]),
            base_currency=str(payload["base_currency"]),
        )


class InMemoryModelGateway:
    """Satisfy the chat-service constructor for delete-only unit coverage."""
