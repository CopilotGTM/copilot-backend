from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.routers.chat.messages_handler import get_chat_message_service
from src.api.routers.chat.sessions_handler import get_chat_session_service
from src.domain.models.organisation_tables.chat_message_models import (
    ChatMessageRole,
    ChatMessageType,
)
from src.domain.models.organisation_tables.job_models import JobStatus
from src.infra.utils.cursor import decode_cursor, encode_cursor


class FakeChatSessionService:
    _MAX_TITLE_LENGTH = 100

    def __init__(self, organisation_id, user_id):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.sessions = {}  # session_id (str) -> dict

    async def close(self):
        pass

    # Mimic the real service signature - user_id/organisation_id come from constructor
    async def create_session(self, *, title, chat_type=None, properties=None, tags=None):
        if not title or not str(title).strip():
            raise ValueError("Title cannot be null or empty")
        title = str(title).strip()
        if len(title) > self._MAX_TITLE_LENGTH:
            title = title[: self._MAX_TITLE_LENGTH - 3].rstrip() + "..."
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
            "properties": {},
        }
        self.sessions[sid] = rec
        return SimpleNamespace(**rec)

    async def delete_session(self, *, session_id: UUID):
        sid = str(session_id)
        rec = self.sessions.get(sid)
        if not rec or rec.get("is_deleted"):
            raise ValueError("Chat session not found")
        rec["is_deleted"] = True
        return SimpleNamespace(**rec)

    async def rename_session(self, *, session_id: UUID, title: str):
        if not title or not str(title).strip():
            raise ValueError("Title cannot be null or empty")
        title = title.strip()
        if len(title) > self._MAX_TITLE_LENGTH:
            title = title[: self._MAX_TITLE_LENGTH - 3].rstrip() + "..."
        sid = str(session_id)
        rec = self.sessions.get(sid)
        if not rec or rec.get("is_deleted"):
            raise ValueError("Chat session not found")
        rec["title"] = title
        return SimpleNamespace(**rec)

    async def list_sessions(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        chat_type: str | None = None,
    ):
        # Collect non-deleted sessions
        rows = [rec for rec in self.sessions.values() if not rec.get("is_deleted")]
        if chat_type:
            rows = [rec for rec in rows if rec.get("chat_type") == chat_type]
        # Sort by updated_at desc, session_id desc to match the real service.
        rows.sort(key=lambda r: (r["updated_at"], r["session_id"]), reverse=True)

        if cursor:
            ts, sid = decode_cursor(cursor)

            def is_after(r):
                return (r["updated_at"] < ts) or (
                    r["updated_at"] == ts and r["session_id"] < str(sid)
                )

            rows = [r for r in rows if is_after(r)]

        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(items[-1]["updated_at"], UUID(items[-1]["session_id"]))
            if has_more
            else None
        )
        # Return records with attribute access (like domain model)
        return [SimpleNamespace(**r) for r in items], next_cursor

    async def search_sessions(self, *, q: str, limit: int = 10):
        query = (q or "").strip().lower()
        if not query:
            return []
        rows = [
            r
            for r in self.sessions.values()
            if not r.get("is_deleted") and query in r["title"].lower()
        ]
        rows.sort(key=lambda r: (r["updated_at"], r["created_at"]), reverse=True)
        items = rows[:limit]
        return [SimpleNamespace(**r) for r in items]

    async def set_favorite(self, *, session_id: UUID, favorite: bool):
        sid = str(session_id)
        rec = self.sessions.get(sid)
        if not rec or rec.get("is_deleted"):
            raise ValueError("Chat session not found")
        props = dict(rec.get("properties") or {})
        if favorite:
            props["favorite"] = True
        else:
            props.pop("favorite", None)
        rec["properties"] = props
        return SimpleNamespace(**rec)


@pytest.fixture(scope="function")
def dependency_overrides(client, test_user_ids):
    """
    Fixture to override dependencies for the chat sessions tests.
    Uses a fake service to avoid DB access.
    """

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name=test_user_ids.get("user_name", "Test User"),
            user_email=test_user_ids.get("user_email", "test@example.com"),
            user_timezone=test_user_ids.get("user_timezone", "Asia/Kolkata"),
        )

    # Create a single service instance per test function to preserve state
    fake_service = FakeChatSessionService(
        test_user_ids["organisation_id"], test_user_ids["user_id"]
    )

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


class FakeJob:
    """Fake Job for testing."""

    def __init__(self, job_id: UUID, status: JobStatus = JobStatus.PENDING):
        self.job_id = job_id
        self.status = status
        self.error_message = None
        self.job_metadata = {}


class FakeMessage:
    """Fake ChatMessage for testing."""

    def __init__(
        self,
        message_id: UUID,
        session_id: UUID,
        role: ChatMessageRole,
        content: str,
        message_type: ChatMessageType = ChatMessageType.GENERAL_MESSAGE,
        is_edited: bool = False,
        feedback_score: int | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ):
        self.message_id = message_id
        self.session_id = session_id
        self.role = role
        self.content = {"text": content}
        self.message_type = message_type
        self.is_edited = is_edited
        self.feedback_score = feedback_score
        self.created_at = created_at or datetime(2024, 1, 1, 0, 0, 0)
        self.updated_at = updated_at or datetime(2024, 1, 1, 0, 0, 0)
        self.extra = {}
        self.agent_data = {}


class FakeDB:
    """Fake database session for testing."""

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def add(self, obj):
        pass


class FakeChatMessageService:
    """Fake ChatMessageService for testing."""

    def __init__(self, organisation_id: UUID, user_id: UUID):
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.db = FakeDB()
        self.sessions = {}  # session_id (str) -> session data
        self.messages = {}  # message_id (str) -> FakeMessage
        self.jobs = {}  # job_id (str) -> FakeJob
        self.last_create_user_message_call = None

    async def close(self):
        pass

    def _get_or_create_session(self, session_id: UUID) -> dict:
        """Get or create a session for testing."""
        sid = str(session_id)
        if sid not in self.sessions:
            self.sessions[sid] = {
                "session_id": sid,
                "title": "Test Session",
                "user_id": str(self.user_id),
                "organisation_id": str(self.organisation_id),
            }
        return self.sessions[sid]

    async def create_user_message(
        self,
        session_id: UUID,
        content: str,
        user_timezone: str | None = None,
        attachment_ids: list[UUID] | None = None,
        message_type: ChatMessageType = ChatMessageType.GENERAL_MESSAGE,
        role: ChatMessageRole = ChatMessageRole.USER,
    ):
        """Create a user message and return the message, job, and assistant message ID."""
        self.last_create_user_message_call = {
            "session_id": session_id,
            "content": content,
            "user_timezone": user_timezone,
            "attachment_ids": attachment_ids,
            "message_type": message_type,
            "role": role,
        }

        # Verify session exists (or create for testing)
        self._get_or_create_session(session_id)

        # Create user message
        user_msg_id = uuid4()
        user_msg = FakeMessage(
            message_id=user_msg_id,
            session_id=session_id,
            role=role,
            content=content,
            message_type=message_type,
        )
        self.messages[str(user_msg_id)] = user_msg

        # Create job
        job_id = uuid4()
        job = FakeJob(job_id=job_id)
        self.jobs[str(job_id)] = job

        # Create assistant message placeholder
        assistant_msg_id = uuid4()
        assistant_msg = FakeMessage(
            message_id=assistant_msg_id,
            session_id=session_id,
            role=ChatMessageRole.ASSISTANT,
            content="",
            message_type=message_type,
        )
        self.messages[str(assistant_msg_id)] = assistant_msg

        return user_msg, job, str(assistant_msg_id)

    async def list_messages(
        self,
        session_id: UUID,
        limit: int = 20,
        cursor: str | None = None,
        role: ChatMessageRole | None = None,
        message_type: ChatMessageType | None = None,
    ):
        """List messages for a session with pagination."""
        sid = str(session_id)

        # Filter messages by session_id
        msgs = [m for m in self.messages.values() if str(m.session_id) == sid]

        # Apply role filter
        if role:
            msgs = [m for m in msgs if m.role == role]

        # Apply message_type filter
        if message_type:
            msgs = [m for m in msgs if m.message_type == message_type]

        # Sort by created_at desc
        msgs.sort(key=lambda m: (m.created_at, str(m.message_id)), reverse=True)

        # Apply cursor pagination
        if cursor:
            ts, mid = decode_cursor(cursor)

            def is_after(m):
                return (m.created_at < ts) or (m.created_at == ts and str(m.message_id) < str(mid))

            msgs = [m for m in msgs if is_after(m)]

        has_more = len(msgs) > limit
        items = msgs[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].message_id)
            if has_more and items
            else None
        )

        return items, next_cursor

    async def get_message_attachments(self, messages: list):
        """Get attachments for messages (returns empty for testing)."""
        return {}

    async def build_attachment_urls(self, attachments_by_msg: dict):
        """Build attachment URLs (returns empty for testing)."""
        return {}

    def build_message_list_items(self, messages: list, atts_urls: dict):
        """Build message list items for response."""
        items = []
        for msg in messages:
            content_text = ""
            if isinstance(msg.content, dict):
                content_text = msg.content.get("text", "")
            elif isinstance(msg.content, str):
                content_text = msg.content

            items.append(
                {
                    "message_id": msg.message_id,
                    "session_id": msg.session_id,
                    "role": msg.role,
                    "message_type": msg.message_type,
                    "content": content_text,
                    "is_edited": msg.is_edited,
                    "created_at": msg.created_at,
                    "updated_at": msg.updated_at,
                    "attachments": [],
                    "confidence_score": msg.extra.get("confidence_score") if msg.extra else None,
                    "sources": msg.agent_data.get("sources") if msg.agent_data else None,
                    "suggestions": msg.agent_data.get("suggestions") if msg.agent_data else None,
                    "feedback_score": msg.feedback_score,
                }
            )
        return items

    async def update_message_feedback(self, message_id: UUID, feedback_score: int):
        """Update feedback score for a message."""
        mid = str(message_id)
        if mid not in self.messages:
            raise ValueError("Message not found")

        msg = self.messages[mid]
        # Set to None if feedback_score is 0 (clear feedback)
        msg.feedback_score = feedback_score if feedback_score != 0 else None
        return msg


@pytest.fixture(scope="function")
def messages_dependency_overrides(client, test_user_ids):
    """
    Fixture to override dependencies for the chat messages tests.
    Uses a fake service to avoid DB access.
    """

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name=test_user_ids.get("user_name", "Test User"),
            user_email=test_user_ids.get("user_email", "test@example.com"),
            user_timezone=test_user_ids.get("user_timezone", "Asia/Kolkata"),
        )

    # Create a single service instance per test function to preserve state
    fake_service = FakeChatMessageService(
        test_user_ids["organisation_id"], test_user_ids["user_id"]
    )

    async def _override_service():
        try:
            yield fake_service
        finally:
            await fake_service.close()

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_chat_message_service] = _override_service
    yield fake_service
    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides = {}
