"""Tests for outbound notification chat-context persistence."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_persist_outbound_notification_creates_whatsapp_chat_context(monkeypatch):
    """Outbound notifications should become WhatsApp chat context for follow-up replies."""
    from src.domain.models.organisation_tables.chat_message_models import ChatMessage
    from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
    from src.service.channels.chat_context_service import OutboundChatContextService

    org_id = uuid4()
    user_id = uuid4()
    added: list[object] = []

    class _ScalarResult:
        def first(self):
            return None

    class _ExecuteResult:
        def scalars(self):
            return _ScalarResult()

    class _Session:
        async def execute(self, _stmt):
            return _ExecuteResult()

        def add(self, obj):
            added.append(obj)

        async def flush(self):
            return None

        async def commit(self):
            return None

    @asynccontextmanager
    async def _org_session_scope(_org_id):
        yield _Session()

    monkeypatch.setattr(
        "src.service.channels.chat_context_service.org_async_session_scope",
        _org_session_scope,
    )

    message_id = await OutboundChatContextService().persist_outbound_notification_context(
        organisation_id=org_id,
        user_id=user_id,
        chat_type=ChatType.WHATSAPP_CHAT,
        text="Reminder: Follow up with Acme",
        metadata={
            "notification_type": "task_assignment",
            "source_id": "task-1",
            "whatsapp_message_id": "wamid.test",
        },
    )

    assert message_id is not None
    chat_session = next(obj for obj in added if isinstance(obj, ChatSession))
    chat_message = next(obj for obj in added if isinstance(obj, ChatMessage))
    assert chat_session.organisation_id == org_id
    assert chat_session.user_id == user_id
    assert chat_session.chat_type == ChatType.WHATSAPP_CHAT
    assert chat_message.session_id == chat_session.session_id
    assert chat_message.content == {"text": "Reminder: Follow up with Acme"}
    assert chat_message.extra["source"] == "outbound_notification"
    assert chat_message.extra["whatsapp_message_id"] == "wamid.test"
