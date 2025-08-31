import asyncio
from uuid import uuid4
import pytest

from src.api.dependencies.organisation_dependencies import get_current_user, UserIDs
from src.api.routers.chat.sessions_handler import get_chat_session_service
from src.api.schemas.chat_models import ChatSessionOut


class FakeChatSessionService:
    def __init__(self, organisation_id, user_id):
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self):
        pass

    # Mimic the real service signature
    def create_session(self, *, user_id, organisation_id, title, chat_type=None, properties=None, tags=None):
        if not title or not str(title).strip():
            raise ValueError("Title cannot be null or empty")
        # Return data compatible with ChatSessionOut
        return ChatSessionOut.model_validate({
            "session_id": str(uuid4()),
            "title": title,
            "chat_type": str(chat_type) if chat_type else "GENERAL_CHAT",
            "chat_version": "v1",
            "tags": tags or [],
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        })


@pytest.fixture(scope="function")
def dependency_overrides(client, test_user_ids):
    """
    Fixture to override dependencies for the chat sessions tests.
    Uses a fake service to avoid DB access.
    """

    def _override_user():
        return UserIDs(user_id=test_user_ids["user_id"], organisation_id=test_user_ids["organisation_id"])

    async def _override_service():
        svc = FakeChatSessionService(test_user_ids["organisation_id"], test_user_ids["user_id"])
        try:
            yield svc
        finally:
            await svc.close()

    from src.infra.utils.app_state import fastapp as app
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_chat_session_service] = _override_service
    yield
    from src.infra.utils.app_state import fastapp as app
    app.dependency_overrides = {}
