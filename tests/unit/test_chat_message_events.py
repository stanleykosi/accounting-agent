"""
Purpose: Verify the compact Postgres notification payloads used by chat SSE.
Scope: Pure payload construction/parsing without requiring a live database.
Dependencies: services.chat.message_events.
"""

from __future__ import annotations

from uuid import uuid4

from services.chat.message_events import (
    build_chat_message_notification_payload,
    parse_chat_message_notification,
)


def test_chat_message_notification_payload_round_trips() -> None:
    """Ensure persisted chat message notifications carry the SSE wakeup fields."""

    thread_id = uuid4()
    message_id = uuid4()

    payload = build_chat_message_notification_payload(
        thread_id=thread_id,
        message_id=message_id,
        message_order=7,
        role="assistant",
    )
    notification = parse_chat_message_notification(payload)

    assert notification is not None
    assert notification.thread_id == thread_id
    assert notification.message_id == message_id
    assert notification.message_order == 7
    assert notification.role == "assistant"


def test_chat_message_notification_parser_rejects_malformed_payload() -> None:
    """Malformed database notifications should not break active SSE streams."""

    assert parse_chat_message_notification("not-json") is None
    assert parse_chat_message_notification("{}") is None
