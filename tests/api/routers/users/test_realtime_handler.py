from __future__ import annotations

import importlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient
from src.domain.models.user_models import (
    RealtimeTurnMode,
    UserRealtimeRuntimeConfig,
    UserRealtimeSessionResponse,
)
from src.infra.auth.user_context import UserIDs
from src.service.users.realtime_session_service import RealtimeSessionServiceError


class FakeRealtimeSessionService:
    def __init__(self):
        self.last_request = None
        self.last_attach_request = None
        self.error: RealtimeSessionServiceError | None = None
        self.response = UserRealtimeSessionResponse(
            session_id="bootstrap-session-123",
            ephemeral_key="ephemeral-key-123",
            endpoint="https://example.openai.azure.com/openai/v1/realtime/calls",
            model="gpt-realtime",
            voice="alloy",
            expires_at=datetime(2026, 4, 11, 10, 45, 0, tzinfo=UTC),
            turn_mode=RealtimeTurnMode.PUSH_TO_TALK,
            chat_session_id=str(uuid4()),
            runtime_config=UserRealtimeRuntimeConfig(
                chat_session_id=str(uuid4()),
                control_mode="backend_sideband",
                tool_policy="guardrailed_auto",
                allowed_tools=["search_internal_knowledge", "get_deal_details"],
                session_update={
                    "type": "session.update",
                    "session": {
                        "instructions": "You are Kai.",
                        "tools": [
                            {
                                "type": "function",
                                "name": "search_internal_knowledge",
                                "description": "Search internal knowledge.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string"},
                                    },
                                    "required": ["query"],
                                },
                            }
                        ],
                    },
                },
                issued_at=datetime(2026, 4, 11, 9, 45, 0, tzinfo=UTC),
                expires_at=datetime(2026, 4, 11, 10, 45, 0, tzinfo=UTC),
                signature="signed-payload",
            ),
        )

    async def create_session(self, session_request, db=None):
        self.last_request = session_request
        if self.error is not None:
            raise self.error
        return self.response

    async def attach_call(self, attach_request, db=None):
        self.last_attach_request = attach_request
        if self.error is not None:
            raise self.error
        return {
            "call_id": "call-123",
            "chat_session_id": self.response.chat_session_id,
            "control_mode": "backend_sideband",
            "status": "queued",
        }

    async def close(self) -> None:
        return None


@pytest.fixture(scope="function")
def realtime_client(mocker):
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    mocker.patch(
        "src.infra.messaging.redis_client.get_redis_client", return_value=fakeredis.FakeRedis()
    )
    mocker.patch("src.api.fastapi_app.initialize_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.close_redis_pool", return_value=None)
    mocker.patch("src.api.fastapi_app.initialize_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.close_async_redis_pool", new=AsyncMock())
    mocker.patch("src.api.fastapi_app.close_database_providers", new=AsyncMock())
    mocker.patch(
        "src.api.fastapi_app.validate_current_shared_tenant_runtime_startup",
        return_value=None,
    )
    mocker.patch(
        "src.api.fastapi_app.validate_current_shared_tenant_runtime_password",
        new=AsyncMock(),
    )
    mocker.patch(
        "src.api.fastapi_app.security_secrets_config.preload_required",
        new=AsyncMock(),
    )
    mocker.patch("src.api.fastapi_app.init_sentry", return_value=None)
    mocker.patch("src.api.fastapi_app.async_init_otel", new=AsyncMock())
    mocker.patch(
        "src.infra.auth.supabase_admin_client.supabase_admin_client.initialize",
        new=AsyncMock(),
    )
    mocker.patch(
        "src.infra.auth.supabase_user_client.supabase_user_client.initialize",
        new=AsyncMock(),
    )
    mocker.patch(
        "src.infra.security.token_revocation.token_revocation_service.initialize",
        new=AsyncMock(),
    )

    asgi_app = fastapi_app.create_fastapi_app()

    from src.api.dependencies.organisation_dependencies import get_current_user
    from src.api.dependencies.rate_limit import rate_limit_user_write
    from src.api.routers.users.realtime_handler import get_realtime_session_service
    from src.infra.utils.app_state import fastapp as _fastapp

    fake_service = FakeRealtimeSessionService()

    async def override_get_current_user() -> UserIDs:
        return UserIDs(
            user_id=uuid4(),
            organisation_id=uuid4(),
            user_name="Test User",
            user_email="test@example.com",
        )

    async def override_rate_limit_user_write() -> None:
        return None

    async def override_get_realtime_session_service():
        yield fake_service

    _fastapp.dependency_overrides[get_current_user] = override_get_current_user
    _fastapp.dependency_overrides[rate_limit_user_write] = override_rate_limit_user_write
    _fastapp.dependency_overrides[get_realtime_session_service] = (
        override_get_realtime_session_service
    )

    with TestClient(asgi_app) as client:
        yield {"client": client, "service": fake_service}


def test_create_realtime_session_success(realtime_client) -> None:
    client = realtime_client["client"]
    service = realtime_client["service"]

    response = client.post(
        "/users/realtime/session",
        json={
            "device": {
                "platform": "android",
                "app_version": "1.0.0",
            },
            "conversation_mode": "push_to_talk",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Realtime session created successfully."
    assert body["data"]["session_id"] == "bootstrap-session-123"
    assert body["data"]["ephemeral_key"] == "ephemeral-key-123"
    assert body["data"]["endpoint"] == "https://example.openai.azure.com/openai/v1/realtime/calls"
    assert body["data"]["runtime_config"]["control_mode"] == "backend_sideband"
    assert body["data"]["runtime_config"]["tool_policy"] == "guardrailed_auto"
    assert body["data"]["runtime_config"]["session_update"]["session"]["tools"][0]["name"] == (
        "search_internal_knowledge"
    )
    assert body["data"]["runtime_config"]["signature"] == "signed-payload"
    assert service.last_request is not None
    assert service.last_request.device.platform.value == "android"
    assert service.last_request.conversation_mode.value == "push_to_talk"


def test_create_realtime_session_propagates_service_errors(realtime_client) -> None:
    client = realtime_client["client"]
    service = realtime_client["service"]
    service.error = RealtimeSessionServiceError("Failed to create realtime session.", 502)

    response = client.post(
        "/users/realtime/session",
        json={
            "device": {
                "platform": "android",
                "app_version": "1.0.0",
            },
            "conversation_mode": "push_to_talk",
        },
    )

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["message"] == "Failed to create realtime session."


def test_attach_realtime_call_success(realtime_client) -> None:
    client = realtime_client["client"]
    service = realtime_client["service"]

    response = client.post(
        "/users/realtime/calls/attach",
        json={
            "session_id": "bootstrap-session-123",
            "call_id": "call-123",
            "chat_session_id": service.response.chat_session_id,
            "runtime_config": {
                "chat_session_id": service.response.chat_session_id,
                "control_mode": "backend_sideband",
                "tool_policy": "guardrailed_auto",
                "allowed_tools": ["search_internal_knowledge"],
                "session_update": {
                    "type": "session.update",
                    "session": {
                        "instructions": "You are Kai.",
                        "tools": [
                            {
                                "type": "function",
                                "name": "search_internal_knowledge",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                },
                            }
                        ],
                    },
                },
                "issued_at": "2026-04-11T09:45:00Z",
                "expires_at": "2026-04-11T10:45:00Z",
                "signature": "signed-payload",
            },
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Realtime call attached successfully."
    assert body["data"]["call_id"] == "call-123"
    assert body["data"]["control_mode"] == "backend_sideband"
    assert body["data"]["status"] == "queued"
    assert service.last_attach_request is not None


def test_attach_realtime_call_propagates_service_errors(realtime_client) -> None:
    client = realtime_client["client"]
    service = realtime_client["service"]
    service.error = RealtimeSessionServiceError("Failed to attach realtime call.", 502)

    response = client.post(
        "/users/realtime/calls/attach",
        json={
            "session_id": "bootstrap-session-123",
            "call_id": "call-123",
            "chat_session_id": service.response.chat_session_id,
            "runtime_config": {
                "chat_session_id": service.response.chat_session_id,
                "control_mode": "backend_sideband",
                "tool_policy": "guardrailed_auto",
                "allowed_tools": ["search_internal_knowledge"],
                "session_update": {
                    "type": "session.update",
                    "session": {"instructions": "You are Kai.", "tools": []},
                },
                "issued_at": "2026-04-11T09:45:00Z",
                "expires_at": "2026-04-11T10:45:00Z",
                "signature": "signed-payload",
            },
        },
    )

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["message"] == "Failed to attach realtime call."
