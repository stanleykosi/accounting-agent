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

import psycopg
from services.chat.message_events import (
    CHAT_MESSAGE_NOTIFY_CHANNEL,
    build_chat_message_notification_payload,
)
from services.common.settings import get_settings
from services.db.models.chat import ChatMessage, ChatThread
from services.db.models.entity import EntityMembership
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session, aliased


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


@dataclass(frozen=True, slots=True)
class ChatThreadMessageStatsRecord:
    """Describe aggregate message stats for one chat thread."""

    message_count: int
    last_message_at: datetime | None
    latest_message_order: int


class ChatRepository:
    """Execute canonical chat persistence operations within one SQLAlchemy session."""

    def __init__(self, *, db_session: Session) -> None:
        """Capture the request-scoped SQLAlchemy session used by the chat service."""

        self._db_session = db_session
        self._turn_lock_connection: psycopg.Connection[Any] | None = None

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

    def lock_thread_for_turn(self, *, thread_id: UUID) -> ChatThreadRecord | None:
        """Serialize worker-side operator turns for one thread without blocking readers."""

        statement = select(ChatThread).where(ChatThread.id == thread_id)
        thread = self._db_session.execute(statement).scalar_one_or_none()
        if thread is None:
            return None

        bind = self._db_session.get_bind()
        if bind.dialect.name == "postgresql":
            self._acquire_thread_turn_lock(thread_id=thread_id)
        return _map_thread(thread)

    def try_lock_thread_for_turn(self, *, thread_id: UUID) -> ChatThreadRecord | None:
        """Acquire the turn lock only when it is immediately available."""

        statement = select(ChatThread).where(ChatThread.id == thread_id)
        thread = self._db_session.execute(statement).scalar_one_or_none()
        if thread is None:
            return None

        bind = self._db_session.get_bind()
        if bind.dialect.name == "postgresql" and not self._try_acquire_thread_turn_lock(
            thread_id=thread_id,
        ):
            return None
        return _map_thread(thread)

    def release_thread_turn_lock(self, *, thread_id: UUID) -> None:
        """Release the dedicated Postgres advisory lock connection for one operator turn."""

        bind = self._db_session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        if self._turn_lock_connection is None:
            return

        connection = self._turn_lock_connection
        self._turn_lock_connection = None
        try:
            connection.execute(
                "SELECT pg_advisory_unlock(%s)",
                (_thread_advisory_lock_key(thread_id),),
            )
        finally:
            connection.close()

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

        thread_statement = select(ChatThread).where(ChatThread.entity_id == entity_id)

        if close_run_id is not None:
            thread_statement = thread_statement.where(ChatThread.close_run_id == close_run_id)
        else:
            thread_statement = thread_statement.where(ChatThread.close_run_id.is_(None))

        candidate_threads = (
            thread_statement.order_by(desc(ChatThread.created_at), desc(ChatThread.id))
            .limit(limit)
            .subquery()
        )
        thread_alias = aliased(ChatThread, candidate_threads)
        message_stats = _build_message_stats_subquery(candidate_threads)

        statement = (
            select(thread_alias, message_stats.c.message_count, message_stats.c.last_message_at)
            .outerjoin(message_stats, thread_alias.id == message_stats.c.thread_id)
            .order_by(desc(thread_alias.created_at), desc(thread_alias.id))
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

        candidate_threads = (
            select(ChatThread)
            .join(EntityMembership, EntityMembership.entity_id == ChatThread.entity_id)
            .where(EntityMembership.user_id == user_id)
            .order_by(desc(ChatThread.updated_at), desc(ChatThread.id))
            .limit(limit)
            .subquery()
        )
        thread_alias = aliased(ChatThread, candidate_threads)
        message_stats = _build_message_stats_subquery(candidate_threads)

        statement = (
            select(thread_alias, message_stats.c.message_count, message_stats.c.last_message_at)
            .outerjoin(message_stats, thread_alias.id == message_stats.c.thread_id)
            .order_by(desc(thread_alias.updated_at), desc(thread_alias.id))
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
            select(ChatThread).where(ChatThread.id == thread_id).with_for_update()
        ).scalar_one()
        next_message_order = int(
            self._db_session.execute(
                select(func.coalesce(func.max(ChatMessage.message_order), 0) + 1).where(
                    ChatMessage.thread_id == thread.id
                )
            ).scalar_one()
        )
        model_metadata = _inherit_turn_metadata(
            db_session=self._db_session,
            thread_id=thread_id,
            role=role,
            model_metadata=model_metadata,
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
        self._notify_message_created(message)
        return _map_message(message)

    def list_messages_for_thread(
        self,
        *,
        thread_id: UUID,
        limit: int | None = None,
    ) -> tuple[ChatMessageRecord, ...]:
        """Return thread messages ordered oldest-first, limited to the newest messages."""

        if limit is None:
            statement = (
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id)
                .order_by(ChatMessage.message_order.asc(), ChatMessage.id.asc())
            )
            messages = self._db_session.execute(statement).scalars().all()
            return tuple(_map_message(message) for message in messages)

        newest_messages = (
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id)
            .order_by(ChatMessage.message_order.desc(), ChatMessage.id.desc())
            .limit(limit)
            .subquery()
        )
        message_alias = aliased(ChatMessage, newest_messages)
        statement = select(message_alias).order_by(
            message_alias.message_order.asc(),
            message_alias.id.asc(),
        )

        messages = self._db_session.execute(statement).scalars().all()
        return tuple(_map_message(message) for message in messages)

    def list_messages_for_thread_after_order(
        self,
        *,
        thread_id: UUID,
        after_message_order: int,
        limit: int | None = None,
    ) -> tuple[ChatMessageRecord, ...]:
        """Return messages committed after a known per-thread message order."""

        statement = (
            select(ChatMessage)
            .where(
                ChatMessage.thread_id == thread_id,
                ChatMessage.message_order > after_message_order,
            )
            .order_by(ChatMessage.message_order.asc(), ChatMessage.id.asc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        messages = self._db_session.execute(statement).scalars().all()
        return tuple(_map_message(message) for message in messages)

    def get_message_stats_for_thread(self, *, thread_id: UUID) -> ChatThreadMessageStatsRecord:
        """Return message count, latest timestamp, and latest order in one aggregate query."""

        statement = select(
            func.count(ChatMessage.id),
            func.max(ChatMessage.created_at),
            func.coalesce(func.max(ChatMessage.message_order), 0),
        ).where(ChatMessage.thread_id == thread_id)
        message_count, last_message_at, latest_message_order = self._db_session.execute(
            statement
        ).one()
        return ChatThreadMessageStatsRecord(
            message_count=int(message_count),
            last_message_at=last_message_at,
            latest_message_order=int(latest_message_order),
        )

    def get_message_count_for_thread(self, *, thread_id: UUID) -> int:
        """Return the total number of messages in a thread."""

        return self.get_message_stats_for_thread(thread_id=thread_id).message_count

    def get_last_message_time_for_thread(self, *, thread_id: UUID) -> datetime | None:
        """Return the created_at of the most recent message in a thread."""

        return self.get_message_stats_for_thread(thread_id=thread_id).last_message_at

    def get_latest_message_order_for_thread(self, *, thread_id: UUID) -> int:
        """Return the current high-water message order for one thread."""

        return self.get_message_stats_for_thread(thread_id=thread_id).latest_message_order

    def commit(self) -> None:
        """Commit the current chat transaction and surface integrity problems unchanged."""

        self._db_session.commit()

    def rollback(self) -> None:
        """Rollback the current chat transaction after an expected or unexpected failure."""

        self._db_session.rollback()

    def _acquire_thread_turn_lock(self, *, thread_id: UUID) -> None:
        """Acquire a turn-long advisory lock on a dedicated non-pooled connection."""

        if self._turn_lock_connection is not None:
            raise RuntimeError("A chat turn advisory lock is already held by this repository.")

        connection = _open_thread_turn_lock_connection()
        try:
            connection.execute(
                "SELECT pg_advisory_lock(%s)",
                (_thread_advisory_lock_key(thread_id),),
            )
        except Exception:
            connection.close()
            raise
        self._turn_lock_connection = connection

    def _try_acquire_thread_turn_lock(self, *, thread_id: UUID) -> bool:
        """Return False instead of blocking behind an active operator turn."""

        if self._turn_lock_connection is not None:
            raise RuntimeError("A chat turn advisory lock is already held by this repository.")

        connection = _open_thread_turn_lock_connection()
        try:
            cursor = connection.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_thread_advisory_lock_key(thread_id),),
            )
            row = cursor.fetchone()
            acquired = bool(row[0]) if row is not None else False
        except Exception:
            connection.close()
            raise
        if not acquired:
            connection.close()
            return False
        self._turn_lock_connection = connection
        return True

    def _notify_message_created(self, message: ChatMessage) -> None:
        """Stage a Postgres notification that fires when this message commits."""

        bind = self._db_session.get_bind()
        if bind.dialect.name != "postgresql":
            return

        self._db_session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {
                "channel": CHAT_MESSAGE_NOTIFY_CHANNEL,
                "payload": build_chat_message_notification_payload(
                    thread_id=message.thread_id,
                    message_id=message.id,
                    message_order=message.message_order,
                    role=message.role,
                ),
            },
        )


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


def _thread_advisory_lock_key(thread_id: UUID) -> int:
    """Return a stable positive bigint key for Postgres advisory locks."""

    return thread_id.int % (2**63)


def _open_thread_turn_lock_connection() -> psycopg.Connection[Any]:
    """Open the dedicated connection used for turn-long advisory locks."""

    settings = get_settings()
    preferred_hostaddr = settings.database.resolve_preferred_hostaddr()
    if preferred_hostaddr is not None:
        return psycopg.connect(
            settings.database.connection_url,
            autocommit=True,
            hostaddr=preferred_hostaddr,
        )
    return psycopg.connect(
        settings.database.connection_url,
        autocommit=True,
    )


def _build_message_stats_subquery(candidate_threads: Any) -> Any:
    """Build message aggregates for a pre-limited thread candidate set."""

    return (
        select(
            ChatMessage.thread_id,
            func.count(ChatMessage.id).label("message_count"),
            func.max(ChatMessage.created_at).label("last_message_at"),
        )
        .where(ChatMessage.thread_id.in_(select(candidate_threads.c.id)))
        .group_by(ChatMessage.thread_id)
        .subquery()
    )


def _inherit_turn_metadata(
    *,
    db_session: Session,
    thread_id: UUID,
    role: str,
    model_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Attach the active client turn key to the assistant reply that closes it."""

    if role == "user" or not isinstance(model_metadata, dict):
        return model_metadata
    if model_metadata.get("chat_turn_id") is not None:
        return model_metadata

    active_user_message = db_session.execute(
        select(ChatMessage)
        .where(ChatMessage.thread_id == thread_id, ChatMessage.role == "user")
        .order_by(desc(ChatMessage.message_order))
        .limit(1)
    ).scalar_one_or_none()
    active_metadata = (
        active_user_message.model_metadata
        if active_user_message is not None and isinstance(active_user_message.model_metadata, dict)
        else {}
    )
    client_turn_id = active_metadata.get("chat_turn_id")
    if not isinstance(client_turn_id, str):
        return model_metadata

    return {
        **model_metadata,
        "chat_turn_id": client_turn_id,
        "turn_status": _turn_status_from_action_status(
            model_metadata.get("action_status"),
        ),
    }


def _turn_status_from_action_status(action_status: object) -> str:
    """Map internal action state to a durable idempotent-turn state."""

    if action_status == "pending":
        return "pending"
    if action_status == "waiting_async":
        return "waiting_async"
    if action_status == "partial":
        return "partial"
    if action_status == "failed":
        return "failed"
    return "completed"


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
    "ChatThreadMessageStatsRecord",
    "ChatThreadRecord",
    "ChatThreadWithCountRecord",
]
