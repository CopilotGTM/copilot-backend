"""Persistence and owner-boundary contracts for completed realtime voice turns."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessage,
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.service.voice_realtime.turn_persistence_service import (
    VoiceTurnInput,
    VoiceTurnPersistenceService,
    VoiceTurnPersistenceServiceError,
)


def _mock_session_result(row: ChatSession | None):
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = row
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    return mock_result


def _mock_existing_ids_result(rows: list[str]):
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = rows
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    return mock_result


def _make_chat_session(*, title: str = "Voice Session") -> MagicMock:
    session = MagicMock(spec=ChatSession)
    session.session_id = uuid4()
    session.user_id = uuid4()
    session.organisation_id = uuid4()
    session.chat_type = ChatType.VOICE_CHAT
    session.title = title
    session.properties = {"model": "gpt-realtime"}
    session.is_deleted = False
    session.updated_at = datetime(2024, 1, 1)
    return session


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def organisation_id():
    return uuid4()


@pytest.fixture
def user_id():
    return uuid4()


@pytest.fixture
def service(mock_db, organisation_id, user_id):
    return VoiceTurnPersistenceService(
        db=mock_db,
        organisation_id=organisation_id,
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_persist_turns_ignores_duplicate_realtime_item_ids(service, mock_db) -> None:
    chat_session = _make_chat_session()
    mock_db.execute = AsyncMock(
        side_effect=[
            _mock_session_result(chat_session),
            _mock_existing_ids_result(["existing-item"]),
        ]
    )

    result = await service.persist_turns(
        chat_session_id=chat_session.session_id,
        turns=[
            VoiceTurnInput(
                realtime_item_id="existing-item",
                speaker="user",
                text="already saved",
                turn_index=0,
            ),
            VoiceTurnInput(
                realtime_item_id="new-user-item",
                speaker="user",
                text="fresh user turn",
                turn_index=1,
            ),
            VoiceTurnInput(
                realtime_item_id="new-user-item",
                speaker="user",
                text="duplicate payload turn",
                turn_index=1,
            ),
            VoiceTurnInput(
                realtime_item_id="new-assistant-item",
                speaker="assistant",
                text="fresh assistant turn",
                turn_index=2,
            ),
        ],
    )

    added_messages = [call.args[0] for call in mock_db.add.call_args_list]

    assert result.persisted_count == 2
    assert [message.extra["realtime_item_id"] for message in added_messages] == [
        "new-user-item",
        "new-assistant-item",
    ]
    mock_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_turns_persists_mixed_user_and_assistant_turns(service, mock_db) -> None:
    chat_session = _make_chat_session()
    mock_db.execute = AsyncMock(
        side_effect=[
            _mock_session_result(chat_session),
            _mock_existing_ids_result([]),
        ]
    )

    result = await service.persist_turns(
        chat_session_id=chat_session.session_id,
        turns=[
            VoiceTurnInput(
                realtime_item_id="user-item",
                speaker="user",
                text="What changed in this deal?",
                turn_index=0,
            ),
            VoiceTurnInput(
                realtime_item_id="assistant-item",
                speaker="assistant",
                text="The stage moved forward yesterday.",
                turn_index=1,
            ),
        ],
    )

    added_messages = [call.args[0] for call in mock_db.add.call_args_list]

    assert result.persisted_count == 2
    assert [message.role for message in added_messages] == [
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
    ]
    assert all(isinstance(message, ChatMessage) for message in added_messages)
    assert [message.message_type for message in added_messages] == [
        ChatMessageType.GENERAL_MESSAGE,
        ChatMessageType.GENERAL_MESSAGE,
    ]
    assert [message.content for message in added_messages] == [
        {"text": "What changed in this deal?"},
        {"text": "The stage moved forward yesterday."},
    ]
    assert [message.extra["turn_index"] for message in added_messages] == [0, 1]
    assert [message.extra["source"] for message in added_messages] == [
        "voice_transcription",
        "voice_transcription",
    ]
    assert added_messages[0].extra["input_modality"] == "audio"
    assert added_messages[0].extra["audio_origin"] == "voice_realtime"
    assert added_messages[1].parent_message_id == added_messages[0].message_id
    assert added_messages[1].model_name == "gpt-realtime"
    assert chat_session.title == "What changed in this deal?"
    assert chat_session.updated_at == added_messages[0].created_at


@pytest.mark.asyncio
async def test_persist_turns_auto_titles_voice_session_from_first_user_turn(
    service, mock_db
) -> None:
    chat_session = _make_chat_session()
    long_text = "Please summarize the latest call and highlight blockers for procurement review"
    mock_db.execute = AsyncMock(
        side_effect=[
            _mock_session_result(chat_session),
            _mock_existing_ids_result([]),
        ]
    )

    await service.persist_turns(
        chat_session_id=chat_session.session_id,
        turns=[
            VoiceTurnInput(
                realtime_item_id="user-item",
                speaker="user",
                text=long_text,
                turn_index=0,
            )
        ],
    )

    assert chat_session.title == long_text[:60].strip() + "..."


@pytest.mark.asyncio
async def test_persist_turns_does_not_touch_session_for_assistant_only_turns(
    service, mock_db
) -> None:
    chat_session = _make_chat_session(title="Existing Voice Chat")
    original_updated_at = chat_session.updated_at
    mock_db.execute = AsyncMock(
        side_effect=[
            _mock_session_result(chat_session),
            _mock_existing_ids_result([]),
        ]
    )

    await service.persist_turns(
        chat_session_id=chat_session.session_id,
        turns=[
            VoiceTurnInput(
                realtime_item_id="assistant-item",
                speaker="assistant",
                text="Here is the answer.",
                turn_index=0,
            )
        ],
    )

    assert chat_session.updated_at == original_updated_at


@pytest.mark.asyncio
async def test_persist_turns_rejects_missing_or_wrong_session(service, mock_db) -> None:
    mock_db.execute = AsyncMock(return_value=_mock_session_result(None))

    with pytest.raises(VoiceTurnPersistenceServiceError, match="Voice chat session not found."):
        await service.persist_turns(
            chat_session_id=uuid4(),
            turns=[
                VoiceTurnInput(
                    realtime_item_id="user-item",
                    speaker="user",
                    text="hello",
                    turn_index=0,
                )
            ],
        )

    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_session_lookup_requires_owner_and_voice_invariants(service, mock_db) -> None:
    """A shared viewer cannot use the owner-only voice persistence path."""
    mock_db.execute = AsyncMock(return_value=_mock_session_result(None))

    with pytest.raises(VoiceTurnPersistenceServiceError, match="Voice chat session not found."):
        await service._get_owned_voice_session(uuid4())

    statement = mock_db.execute.await_args.args[0]
    where_sql = str(statement.whereclause.compile(compile_kwargs={"literal_binds": True}))
    assert "chat_sessions.session_id" in where_sql
    assert "chat_sessions.user_id" in where_sql
    assert "chat_sessions.organisation_id" in where_sql
    assert "chat_sessions.chat_type" in where_sql
    assert "VOICE_CHAT" in where_sql
    assert "chat_sessions.is_deleted" in where_sql
