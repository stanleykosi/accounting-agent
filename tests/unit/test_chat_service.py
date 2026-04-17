"""
Purpose: Verify chat-thread delete behavior in the grounded chat service.
Scope: Unit coverage for membership-gated thread deletion and structured not-found failures.
Dependencies: Chat service, chat contracts, and repository record dataclasses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from services.chat.service import ChatService, ChatServiceError, ChatServiceErrorCode
from services.common.enums import AutonomyMode
from services.contracts.chat_models import GroundingContext
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
    """Provide the minimal chat repository surface used by the delete-thread flow."""

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
        self.deleted_thread_ids: list[UUID] = []
        self.committed = False
        self.rolled_back = False

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
    """Return parsed chat grounding from the stored thread payload."""

    def parse_context_payload(self, *, payload: dict[str, object]) -> GroundingContext:
        return GroundingContext.model_validate(payload)


class InMemoryModelGateway:
    """Satisfy the chat-service constructor for delete-only unit coverage."""
