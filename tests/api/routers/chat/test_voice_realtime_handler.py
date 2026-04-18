from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.routers.chat.voice_realtime_handler import get_voice_turn_persistence_service
from src.service.voice_realtime.turn_persistence_service import (
    PersistVoiceTurnsResult,
    VoiceTurnPersistenceServiceError,
)


class FakeVoiceTurnPersistenceService:
    def __init__(self):
        self.last_chat_session_id = None
        self.last_turns = None
        self.result = PersistVoiceTurnsResult(
            persisted_count=2,
            message_ids=[uuid4(), uuid4()],
        )
        self.error: Exception | None = None

    async def persist_turns(self, *, chat_session_id, turns):
        self.last_chat_session_id = chat_session_id
        self.last_turns = turns
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def voice_turn_client(client, test_user_ids):
    fake_service = FakeVoiceTurnPersistenceService()

    async def override_get_current_user() -> UserIDs:
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def override_rate_limit_user_write() -> None:
        return None

    async def override_get_voice_turn_persistence_service():
        yield fake_service

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[rate_limit_user_write] = override_rate_limit_user_write
    app.dependency_overrides[get_voice_turn_persistence_service] = (
        override_get_voice_turn_persistence_service
    )

    yield {"client": client, "service": fake_service}

    app.dependency_overrides = {}


def test_persist_voice_turns_success(voice_turn_client) -> None:
    client = voice_turn_client["client"]
    service = voice_turn_client["service"]
    chat_session_id = str(uuid4())

    response = client.post(
        "/users/chats/voice/turns",
        json={
            "chat_session_id": chat_session_id,
            "turns": [
                {
                    "realtime_item_id": "item-1",
                    "speaker": "user",
                    "text": "hello there",
                    "turn_index": 0,
                },
                {
                    "realtime_item_id": "item-2",
                    "speaker": "assistant",
                    "text": "hi",
                    "turn_index": 1,
                },
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Persisted 2 voice turns."
    assert body["data"]["persisted_count"] == 2
    assert service.last_chat_session_id == UUID(chat_session_id)
    assert len(service.last_turns) == 2
    assert service.last_turns[0].speaker == "user"
    assert service.last_turns[1].speaker == "assistant"


def test_persist_voice_turns_maps_service_errors(voice_turn_client) -> None:
    client = voice_turn_client["client"]
    service = voice_turn_client["service"]
    service.error = VoiceTurnPersistenceServiceError("Voice chat session not found.", 404)

    response = client.post(
        "/users/chats/voice/turns",
        json={
            "chat_session_id": str(uuid4()),
            "turns": [
                {
                    "realtime_item_id": "item-1",
                    "speaker": "user",
                    "text": "hello there",
                    "turn_index": 0,
                }
            ],
        },
    )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["message"] == "Voice chat session not found."


def test_persist_voice_turns_requires_speaker_and_realtime_item_id(voice_turn_client) -> None:
    client = voice_turn_client["client"]

    response = client.post(
        "/users/chats/voice/turns",
        json={
            "chat_session_id": str(uuid4()),
            "turns": [
                {
                    "text": "first spoken turn",
                    "turn_index": 0,
                },
                {
                    "text": "assistant reply",
                    "turn_index": 1,
                },
            ],
        },
    )

    assert response.status_code == 422, response.text
