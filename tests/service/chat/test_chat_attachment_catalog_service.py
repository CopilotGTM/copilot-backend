"""Sent-attachment filtering and response projection contracts."""

from datetime import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.chat_message_models import (
    AttachmentSourceProvider,
)
from src.service.chat.chat_attachment_catalog_service import ChatAttachmentCatalogService


def _result(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


@pytest.mark.asyncio
async def test_list_attachments_returns_persisted_audio_metadata_and_uploader() -> None:
    """The catalogue exposes stored metadata and never computes audio duration."""
    organisation_id = uuid4()
    user_id = uuid4()
    attachment = SimpleNamespace(
        attachment_id=uuid4(),
        organisation_id=organisation_id,
        user_id=user_id,
        filename="voice-note.ogg",
        storage_path="chat_uploads/user/audio.ogg",
        mimetype="audio/ogg",
        size_bytes=1234,
        attachment_metadata={"provider_payload": {"duration_seconds": 8.5}},
        source_provider=AttachmentSourceProvider.WHATSAPP,
        created_at=datetime(2026, 7, 18, 10, 0, 0),
    )
    user = SimpleNamespace(
        user_id=user_id,
        first_name="Asha",
        last_name="Rao",
        email="asha@example.com",
    )
    org_session = SimpleNamespace(execute=AsyncMock(return_value=_result([attachment])))
    master_session = SimpleNamespace(execute=AsyncMock(return_value=_result([user])))
    service = ChatAttachmentCatalogService(
        cast(AsyncSession, org_session),
        cast(AsyncSession, master_session),
        organisation_id,
    )
    service.storage.generate_read_sas = AsyncMock(return_value="https://blob.test/audio.ogg?sas=1")

    items, cursor = await service.list_attachments(
        media_type="audio",
        limit=20,
        cursor=None,
    )

    assert cursor is None
    assert items[0].duration_seconds == 8.5
    assert items[0].uploaded_by is not None
    assert items[0].uploaded_by.name == "Asha Rao"
    assert str(items[0].playback_url) == "https://blob.test/audio.ogg?sas=1"
    statement = str(org_session.execute.await_args.args[0])
    assert "EXISTS" in statement
    assert "chat_message_attachment_link" in statement
    assert "lower(attachments.mimetype) LIKE lower" in statement


@pytest.mark.asyncio
async def test_list_attachments_leaves_unknown_duration_null() -> None:
    """Missing provider duration stays null instead of triggering blob inspection."""
    organisation_id = uuid4()
    attachment = SimpleNamespace(
        attachment_id=uuid4(),
        organisation_id=organisation_id,
        user_id=uuid4(),
        filename="call.wav",
        storage_path="chat_uploads/user/call.wav",
        mimetype="audio/wav",
        size_bytes=42,
        attachment_metadata={},
        source_provider=AttachmentSourceProvider.WEB,
        created_at=datetime(2026, 7, 18, 9, 0, 0),
    )
    org_session = SimpleNamespace(execute=AsyncMock(return_value=_result([attachment])))
    master_session = SimpleNamespace(execute=AsyncMock(return_value=_result([])))
    service = ChatAttachmentCatalogService(
        cast(AsyncSession, org_session),
        cast(AsyncSession, master_session),
        organisation_id,
    )
    service.storage.generate_read_sas = AsyncMock(return_value="https://blob.test/call.wav?sas=1")

    items, _ = await service.list_attachments(media_type="audio", limit=20, cursor=None)

    assert items[0].duration_seconds is None
    assert items[0].uploaded_by is None
