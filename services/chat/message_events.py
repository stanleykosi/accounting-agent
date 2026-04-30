"""
Purpose: Publish and consume Postgres notifications for chat message delivery.
Scope: Shared LISTEN/NOTIFY channel naming, compact payloads, and async listener
helpers used by chat persistence and Server-Sent Events.
Dependencies: psycopg async connections plus app database settings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from services.common.settings import get_settings

CHAT_MESSAGE_NOTIFY_CHANNEL = "accounting_chat_messages"
CHAT_MESSAGE_NOTIFY_TIMEOUT_SECONDS = 25.0


@dataclass(frozen=True, slots=True)
class ChatMessageNotification:
    """Describe one committed chat message notification from Postgres."""

    thread_id: UUID
    message_id: UUID
    message_order: int
    role: str


def build_chat_message_notification_payload(
    *,
    thread_id: UUID,
    message_id: UUID,
    message_order: int,
    role: str,
) -> str:
    """Build the compact JSON payload sent through pg_notify."""

    return json.dumps(
        {
            "thread_id": str(thread_id),
            "message_id": str(message_id),
            "message_order": message_order,
            "role": role,
        },
        separators=(",", ":"),
    )


def parse_chat_message_notification(payload: str) -> ChatMessageNotification | None:
    """Parse a pg_notify payload, returning None for unrelated or malformed messages."""

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        thread_id = UUID(str(data["thread_id"]))
        message_id = UUID(str(data["message_id"]))
        message_order = int(data["message_order"])
        role = str(data["role"])
    except (KeyError, TypeError, ValueError):
        return None
    return ChatMessageNotification(
        thread_id=thread_id,
        message_id=message_id,
        message_order=message_order,
        role=role,
    )


class ChatMessageNotificationListener:
    """Hold one async Postgres LISTEN connection for an SSE stream."""

    def __init__(self) -> None:
        self._connection: psycopg.AsyncConnection[Any] | None = None

    async def open(self) -> None:
        """Open the LISTEN connection and subscribe to the chat message channel."""

        settings = get_settings()
        connect_kwargs: dict[str, object] = {}
        preferred_hostaddr = settings.database.resolve_preferred_hostaddr()
        if preferred_hostaddr is not None:
            connect_kwargs["hostaddr"] = preferred_hostaddr
        connection = await psycopg.AsyncConnection.connect(
            settings.database.connection_url,
            autocommit=True,
            **connect_kwargs,
        )
        await connection.execute(
            sql.SQL("LISTEN {}").format(sql.Identifier(CHAT_MESSAGE_NOTIFY_CHANNEL))
        )
        self._connection = connection

    async def wait(
        self,
        *,
        timeout_seconds: float,
    ) -> ChatMessageNotification | None:
        """Wait for one chat message notification or return None on timeout."""

        if self._connection is None:
            raise RuntimeError("Chat notification listener has not been opened.")

        async for notification in self._connection.notifies(
            timeout=timeout_seconds,
            stop_after=1,
        ):
            if notification.channel != CHAT_MESSAGE_NOTIFY_CHANNEL:
                continue
            return parse_chat_message_notification(notification.payload)
        return None

    async def close(self) -> None:
        """Close the LISTEN connection."""

        if self._connection is None:
            return
        await self._connection.close()
        self._connection = None


__all__ = [
    "CHAT_MESSAGE_NOTIFY_CHANNEL",
    "CHAT_MESSAGE_NOTIFY_TIMEOUT_SECONDS",
    "ChatMessageNotification",
    "ChatMessageNotificationListener",
    "build_chat_message_notification_payload",
    "parse_chat_message_notification",
]
