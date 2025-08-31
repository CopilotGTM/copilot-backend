import asyncio
from uuid import uuid4, UUID
from datetime import datetime
from src.infra.utils.cursor import encode_cursor, decode_cursor
import pytest

from src.api.dependencies.organisation_dependencies import get_current_user, UserIDs
from src.api.routers.chat.sessions_handler import get_chat_session_service
from src.api.schemas.chat_models import ChatSessionOut


class FakeChatSessionService:
    def __init__(self, organisation_id, user_id):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.sessions = {}  # session_id (str) -> dict

    async def close(self):
        pass

    # Mimic the real service signature
    def create_session(self, *, user_id, organisation_id, title, chat_type=None, properties=None, tags=None):
        if not title or not str(title).strip():
            raise ValueError("Title cannot be null or empty")
        sid = str(uuid4())
        rec = {
            "session_id": sid,
            "title": title,
            "chat_type": str(chat_type) if chat_type else "GENERAL_CHAT",
            "chat_version": "v1",
            "tags": tags or [],
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0),
            "is_deleted": False,
        }
        self.sessions[sid] = rec
        return ChatSessionOut.model_validate(rec)

    def delete_session(self, *, session_id: UUID, user_id: UUID, organisation_id: UUID):
        sid = str(session_id)
        rec = self.sessions.get(sid)
        if not rec or rec.get("is_deleted"):
            raise ValueError("Chat session not found")
        rec["is_deleted"] = True
        return ChatSessionOut.model_validate(rec)

    def rename_session(self, *, session_id: UUID, user_id: UUID, organisation_id: UUID, title: str):
        if not title or not str(title).strip():
            raise ValueError("Title cannot be null or empty")
        sid = str(session_id)
        rec = self.sessions.get(sid)
        if not rec or rec.get("is_deleted"):
            raise ValueError("Chat session not found")
        rec["title"] = title.strip()
        return ChatSessionOut.model_validate(rec)

    def list_sessions(self, *, user_id: UUID, organisation_id: UUID, limit: int = 20, cursor: str | None = None):
        # Collect non-deleted sessions
        rows = [rec for rec in self.sessions.values() if not rec.get("is_deleted")]
        # Sort by created_at desc, session_id desc
        rows.sort(key=lambda r: (r["created_at"], r["session_id"]), reverse=True)

        if cursor:
            ts, sid = decode_cursor(cursor)
            def is_after(r):
                return (r["created_at"] < ts) or (r["created_at"] == ts and r["session_id"] < str(sid))
            rows = [r for r in rows if is_after(r)]

        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = encode_cursor(items[-1]["created_at"], UUID(items[-1]["session_id"])) if has_more else None
        # Return records as objects compatible with ChatSessionOut
        return [ChatSessionOut.model_validate(r) for r in items], next_cursor

    def search_sessions(self, *, user_id: UUID, organisation_id: UUID, q: str, limit: int = 10):
        query = (q or "").strip().lower()
        if not query:
            return []
        rows = [
            r for r in self.sessions.values()
            if not r.get("is_deleted") and query in r["title"].lower()
        ]
        rows.sort(key=lambda r: (r["updated_at"], r["created_at"]), reverse=True)
        items = rows[:limit]
        return [ChatSessionOut.model_validate(r) for r in items]


@pytest.fixture(scope="function")
def dependency_overrides(client, test_user_ids):
    """
    Fixture to override dependencies for the chat sessions tests.
    Uses a fake service to avoid DB access.
    """

    def _override_user():
        return UserIDs(user_id=test_user_ids["user_id"], organisation_id=test_user_ids["organisation_id"])

    # Create a single service instance per test function to preserve state
    fake_service = FakeChatSessionService(test_user_ids["organisation_id"], test_user_ids["user_id"])

    async def _override_service():
        try:
            yield fake_service
        finally:
            await fake_service.close()

    from src.infra.utils.app_state import fastapp as app
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_chat_session_service] = _override_service
    yield
    from src.infra.utils.app_state import fastapp as app
    app.dependency_overrides = {}
