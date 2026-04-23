"""
Purpose: Persist and query chat threads and messages through SQLAlchemy.
Scope: Chat-specific CRUD operations, thread-scoped message history,
and grounding-context reads for the service layer.
Dependencies: SQLAlchemy ORM sessions plus the canonical chat models under
services/db/models/chat.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from services.db.models.chat import ChatMessage, ChatThread
from services.db.models.entity import EntityMembership
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ChatThreadRecord:
    """Describe the subset of a chat thread row needed by chat services and responses."""

    id: UUID
    entity_id: UUID
    close_run_id: UUID | None
    title: str | None
    context_payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ChatThreadWithCountRecord:
    """Join a thread with its message count for list-response hydration."""

    thread: ChatThreadRecord
    message_count: int
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChatMessageRecord:
    """Describe the subset of a message row needed by chat services and responses."""

    id: UUID
    thread_id: UUID
    message_order: int
    role: str
    content: str
    message_type: str
    linked_action_id: UUID | None
    grounding_payload: dict[str, Any]
    model_metadata: dict[str, Any] | None
    created_at: datetime


class ChatRepository:
    """Execute canonical chat persistence operations within one SQLAlchemy session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request-scoped SQLAlchemy session used by the chat service."""

        self._db_session = db_session

    def create_thread(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        context_payload: dict[str, Any],
        title: str | None,
    ) -> ChatThreadRecord:
        """Stage a new chat thread and flush it so messages can reference it."""

        thread = ChatThread(
            entity_id=entity_id,
            close_run_id=close_run_id,
            context_payload=context_payload,
            title=title,
        )
        self._db_session.add(thread)
        self._db_session.flush()
        return _map_thread(thread)

    def get_thread_by_id(self, *, thread_id: UUID) -> ChatThreadRecord | None:
        """Return one chat thread by UUID or None when it does not exist."""

        statement = select(ChatThread).where(ChatThread.id == thread_id)
        thread = self._db_session.execute(statement).scalar_one_or_none()
        if thread is None:
            return None

        return _map_thread(thread)

    def get_thread_for_entity(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
    ) -> ChatThreadRecord | None:
        """Return one thread by UUID when it belongs to the specified entity."""

        statement = select(ChatThread).where(
            ChatThread.id == thread_id,
            ChatThread.entity_id == entity_id,
        )
        thread = self._db_session.execute(statement).scalar_one_or_none()
        if thread is None:
            return None

        return _map_thread(thread)

    def delete_thread(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
    ) -> bool:
        """Delete one thread when it belongs to the specified entity."""

        statement = select(ChatThread).where(
            ChatThread.id == thread_id,
            ChatThread.entity_id == entity_id,
        )
        thread = self._db_session.execute(statement).scalar_one_or_none()
        if thread is None:
            return False

        self._db_session.delete(thread)
        self._db_session.flush()
        return True

    def update_thread_context(
        self,
        *,
        thread_id: UUID,
        context_payload: dict[str, Any],
    ) -> ChatThreadRecord | None:
        """Replace one thread's context payload and flush the updated row."""

        statement = select(ChatThread).where(ChatThread.id == thread_id)
        thread = self._db_session.execute(statement).scalar_one_or_none()
        if thread is None:
            return None
        thread.context_payload = dict(context_payload)
        self._db_session.flush()
        return _map_thread(thread)

    def update_thread_scope(
        self,
        *,
        thread_id: UUID,
        entity_id: UUID,
        close_run_id: UUID | None,
        context_payload: dict[str, Any],
    ) -> ChatThreadRecord | None:
        """Replace one thread's workspace scope, close-run scope, and context payload together."""

        statement = select(ChatThread).where(ChatThread.id == thread_id)
        thread = self._db_session.execute(statement).scalar_one_or_none()
        if thread is None:
            return None
        thread.entity_id = entity_id
        thread.close_run_id = close_run_id
        thread.context_payload = dict(context_payload)
        self._db_session.flush()
        return _map_thread(thread)

    def list_threads_for_entity(
        self,
        *,
        entity_id: UUID,
        close_run_id: UUID | None,
        limit: int,
    ) -> tuple[ChatThreadWithCountRecord, ...]:
        """Return threads for an entity with message counts, newest-first."""

        subquery = (
            select(
                ChatMessage.thread_id,
                func.count(ChatMessage.id).label("message_count"),
                func.max(ChatMessage.created_at).label("last_message_at"),
            )
            .group_by(ChatMessage.thread_id)
            .subquery()
        )

        statement = (
            select(ChatThread, subquery.c.message_count, subquery.c.last_message_at)
            .outerjoin(subquery, ChatThread.id == subquery.c.thread_id)
            .where(ChatThread.entity_id == entity_id)
        )

        if close_run_id is not None:
            statement = statement.where(ChatThread.close_run_id == close_run_id)
        else:
            statement = statement.where(ChatThread.close_run_id.is_(None))

        statement = statement.order_by(desc(ChatThread.created_at)).limit(limit)

        rows = self._db_session.execute(statement).all()
        return tuple(
            ChatThreadWithCountRecord(
                thread=_map_thread(thread),
                message_count=int(message_count) if message_count is not None else 0,
                last_message_at=last_message_at,
            )
            for thread, message_count, last_message_at in rows
        )

    def list_recent_threads_for_entity_any_scope(
        self,
        *,
        entity_id: UUID,
        limit: int,
        exclude_thread_id: UUID | None = None,
    ) -> tuple[ChatThreadRecord, ...]:
        """Return recent threads for an entity across all scopes, newest-first."""

        statement = select(ChatThread).where(ChatThread.entity_id == entity_id)
        if exclude_thread_id is not None:
            statement = statement.where(ChatThread.id != exclude_thread_id)
        statement = statement.order_by(
            desc(ChatThread.updated_at),
            desc(ChatThread.id),
        ).limit(limit)
        threads = self._db_session.execute(statement).scalars().all()
        return tuple(_map_thread(thread) for thread in threads)

    def list_recent_threads_for_user_any_scope(
        self,
        *,
        user_id: UUID,
        limit: int,
        exclude_thread_id: UUID | None = None,
    ) -> tuple[ChatThreadRecord, ...]:
        """Return recent threads across all workspaces accessible to one user."""

        statement = (
            select(ChatThread)
            .join(EntityMembership, EntityMembership.entity_id == ChatThread.entity_id)
            .where(EntityMembership.user_id == user_id)
        )
        if exclude_thread_id is not None:
            statement = statement.where(ChatThread.id != exclude_thread_id)
        statement = statement.order_by(
            desc(ChatThread.updated_at),
            desc(ChatThread.id),
        ).limit(limit)
        threads = self._db_session.execute(statement).scalars().all()
        return tuple(_map_thread(thread) for thread in threads)

    def list_threads_for_user_any_scope(
        self,
        *,
        user_id: UUID,
        limit: int,
    ) -> tuple[ChatThreadWithCountRecord, ...]:
        """Return all accessible user threads with counts, newest activity first."""

        subquery = (
            select(
                ChatMessage.thread_id,
                func.count(ChatMessage.id).label("message_count"),
                func.max(ChatMessage.created_at).label("last_message_at"),
            )
            .group_by(ChatMessage.thread_id)
            .subquery()
        )

        statement = (
            select(ChatThread, subquery.c.message_count, subquery.c.last_message_at)
            .join(EntityMembership, EntityMembership.entity_id == ChatThread.entity_id)
            .outerjoin(subquery, ChatThread.id == subquery.c.thread_id)
            .where(EntityMembership.user_id == user_id)
            .order_by(desc(ChatThread.updated_at), desc(ChatThread.id))
            .limit(limit)
        )

        rows = self._db_session.execute(statement).all()
        return tuple(
            ChatThreadWithCountRecord(
                thread=_map_thread(thread),
                message_count=int(message_count) if message_count is not None else 0,
                last_message_at=last_message_at,
            )
            for thread, message_count, last_message_at in rows
        )

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
    ) -> ChatMessageRecord:
        """Stage a new chat message and flush it immediately."""

        thread = self._db_session.execute(
            select(ChatThread)
            .where(ChatThread.id == thread_id)
            .with_for_update()
        ).scalar_one()
        next_message_order = int(
            self._db_session.execute(
                select(func.coalesce(func.max(ChatMessage.message_order), 0) + 1)
                .where(ChatMessage.thread_id == thread.id)
            ).scalar_one()
        )
        message = ChatMessage(
            thread_id=thread_id,
            message_order=next_message_order,
            role=role,
            content=content,
            message_type=message_type,
            linked_action_id=linked_action_id,
            grounding_payload=grounding_payload,
            model_metadata=model_metadata,
        )
        self._db_session.add(message)
        self._db_session.flush()
        return _map_message(message)

    def list_messages_for_thread(
        self,
        *,
        thread_id: UUID,
        limit: int | None = None,
    ) -> tuple[ChatMessageRecord, ...]:
        """Return messages for a thread ordered oldest-first (chronological)."""

        statement = (
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id)
            .order_by(ChatMessage.message_order)
        )

        if limit is not None:
            statement = statement.limit(limit)

        messages = self._db_session.execute(statement).scalars().all()
        return tuple(_map_message(message) for message in messages)

    def get_message_count_for_thread(self, *, thread_id: UUID) -> int:
        """Return the total number of messages in a thread."""

        statement = (
            select(func.count(ChatMessage.id))
            .where(ChatMessage.thread_id == thread_id)
        )
        count = self._db_session.execute(statement).scalar_one()
        return int(count)

    def get_last_message_time_for_thread(self, *, thread_id: UUID) -> datetime | None:
        """Return the created_at of the most recent message in a thread."""

        statement = (
            select(func.max(ChatMessage.created_at))
            .where(ChatMessage.thread_id == thread_id)
        )
        return self._db_session.execute(statement).scalar_one()

    def commit(self) -> None:
        """Commit the current chat transaction and surface integrity problems unchanged."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current chat transaction after an expected or unexpected failure."""

        self._db_session.rollback()


def _map_thread(model: ChatThread) -> ChatThreadRecord:
    """Convert an ORM chat thread model into the immutable record consumed by services."""

    return ChatThreadRecord(
        id=model.id,
        entity_id=model.entity_id,
        close_run_id=model.close_run_id,
        title=model.title,
        context_payload=model.context_payload,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _map_message(model: ChatMessage) -> ChatMessageRecord:
    """Convert an ORM chat message model into the immutable record consumed by services."""

    return ChatMessageRecord(
        id=model.id,
        thread_id=model.thread_id,
        message_order=model.message_order,
        role=model.role,
        content=model.content,
        message_type=model.message_type,
        linked_action_id=model.linked_action_id,
        grounding_payload=model.grounding_payload,
        model_metadata=model.model_metadata,
        created_at=model.created_at,
    )


__all__ = [
    "ChatMessageRecord",
    "ChatRepository",
    "ChatThreadRecord",
    "ChatThreadWithCountRecord",
]
