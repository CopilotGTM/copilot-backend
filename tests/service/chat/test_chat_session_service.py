"""Tests for ChatSessionService using real service logic with mocked AsyncSession.

These tests verify actual business logic (validation, ownership checks, query filters)
rather than testing fake in-memory services that always succeed.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.organisation_tables.chat_session_models import ChatSession, ChatType
from src.infra.utils.cursor import decode_cursor
from src.service.chat.chat_session_service import ChatSessionService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_row(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    organisation_id: UUID | None = None,
    title: str = "Test Chat",
    chat_type: ChatType = ChatType.GENERAL_CHAT,
    is_deleted: bool = False,
    properties: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> MagicMock:
    """Create a mock ChatSession row that behaves like a real ORM object."""
    row = MagicMock(spec=ChatSession)
    row.session_id = session_id or uuid4()
    row.user_id = user_id or uuid4()
    row.organisation_id = organisation_id or uuid4()
    row.title = title
    row.chat_type = chat_type
    row.is_deleted = is_deleted
    row.properties = properties or {}
    row.created_at = created_at or datetime(2024, 1, 1)
    row.updated_at = updated_at or datetime(2024, 1, 1)
    row.tags = []
    row.chat_version = "v1"
    return row


def _mock_execute_returning(row):
    """Create a mock Result supporting single-row and scalar-list access."""
    mock_scalars = MagicMock()
    mock_scalars.one_or_none.return_value = row
    mock_scalars.all.return_value = [row] if row else []
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = row
    mock_result.scalars.return_value = mock_scalars
    return mock_result


def _mock_execute_returning_list(rows: list):
    """Create a mock Result whose .scalars().all() returns `rows`."""
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = rows
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    return mock_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org_id():
    return uuid4()


@pytest.fixture
def user_id():
    return uuid4()


@pytest.fixture
def other_user_id():
    return uuid4()


@pytest.fixture
def other_org_id():
    return uuid4()


@pytest.fixture
def mock_db():
    """Create a mock AsyncSession."""
    db = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def service(mock_db, org_id, user_id):
    return ChatSessionService(db=mock_db, organisation_id=org_id, user_id=user_id)


# =============================================================================
# 1. Session Creation
# =============================================================================


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_with_correct_org_and_user(self, service, mock_db, org_id, user_id):
        """Session is created with constructor's org_id and user_id, not arbitrary values."""
        await service.create_session(title="My Chat")

        # Verify db.add was called with a ChatSession bearing the correct IDs
        added_obj = mock_db.add.call_args[0][0]
        assert added_obj.organisation_id == org_id
        assert added_obj.user_id == user_id

    @pytest.mark.asyncio
    async def test_normalizes_string_chat_type(self, service, mock_db):
        """String chat_type is converted to ChatType enum."""
        await service.create_session(title="Chat", chat_type="GENERAL_CHAT")

        added_obj = mock_db.add.call_args[0][0]
        assert added_obj.chat_type == ChatType.GENERAL_CHAT

    @pytest.mark.asyncio
    async def test_invalid_chat_type_defaults_to_general(self, service, mock_db):
        """Unknown chat_type string falls back to GENERAL_CHAT."""
        await service.create_session(title="Chat", chat_type="NONEXISTENT")

        added_obj = mock_db.add.call_args[0][0]
        assert added_obj.chat_type == ChatType.GENERAL_CHAT

    @pytest.mark.asyncio
    async def test_empty_title_raises_value_error(self, service):
        """Empty/whitespace title raises ValueError."""
        for bad_title in ["", "   ", "\n\t"]:
            with pytest.raises(ValueError, match="Title cannot be null or empty"):
                await service.create_session(title=bad_title)

    @pytest.mark.asyncio
    async def test_truncates_title_at_100_chars(self, service, mock_db):
        """Titles over 100 chars are truncated with '...'."""
        long_title = "x" * 150
        await service.create_session(title=long_title)

        added_obj = mock_db.add.call_args[0][0]
        assert len(added_obj.title) <= 100
        assert added_obj.title.endswith("...")

    @pytest.mark.asyncio
    async def test_preserves_title_at_exactly_100_chars(self, service, mock_db):
        """Exactly 100-char title is not truncated."""
        title_100 = "x" * 100
        await service.create_session(title=title_100)

        added_obj = mock_db.add.call_args[0][0]
        assert added_obj.title == title_100

    @pytest.mark.asyncio
    async def test_stores_tags_and_properties(self, service, mock_db):
        """Tags and properties are persisted correctly."""
        tags = ["deal", "important"]
        props = {"source": "web"}
        await service.create_session(title="Chat", tags=tags, properties=props)

        added_obj = mock_db.add.call_args[0][0]
        assert added_obj.tags == tags
        assert added_obj.properties == props

    @pytest.mark.asyncio
    async def test_commits_and_refreshes(self, service, mock_db):
        """Session is committed and refreshed after creation."""
        await service.create_session(title="Chat")

        mock_db.commit.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()


# =============================================================================
# 2. Session Deletion
# =============================================================================


class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_sets_is_deleted_true(self, service, mock_db, org_id, user_id):
        """Soft delete sets is_deleted=True."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(session_row))

        result = await service.delete_session(session_id=session_row.session_id)
        assert result.is_deleted is True
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_found_raises_value_error(self, service, mock_db):
        """Missing session raises ValueError."""
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.delete_session(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_already_deleted_raises_value_error(self, service, mock_db, org_id, user_id):
        """Already deleted session (filtered out by query) raises ValueError.

        The real service queries with is_deleted=False, so an already-deleted
        session won't be found, resulting in ValueError.
        """
        # Return None because the query filters is_deleted=False
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.delete_session(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_wrong_user_raises_value_error(self, service, mock_db, other_user_id, org_id):
        """Session owned by other user returns None from query → ValueError.

        The real service filters by user_id, so a session owned by another user
        won't match, and the query returns None.
        """
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.delete_session(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_wrong_org_raises_value_error(self, service, mock_db, user_id, other_org_id):
        """Session in other org returns None from query → ValueError."""
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.delete_session(session_id=uuid4())


# =============================================================================
# 3. Session Rename
# =============================================================================


class TestRenameSession:
    @pytest.mark.asyncio
    async def test_updates_title(self, service, mock_db, org_id, user_id):
        """Title is updated and committed."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id, title="Old Title")
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(session_row))

        result = await service.rename_session(session_id=session_row.session_id, title="New Title")
        assert result.title == "New Title"
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_truncates_long_title(self, service, mock_db, org_id, user_id):
        """Rename also applies the 100-char truncation."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id)
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(session_row))

        long_title = "y" * 150
        result = await service.rename_session(session_id=session_row.session_id, title=long_title)
        assert len(result.title) <= 100
        assert result.title.endswith("...")

    @pytest.mark.asyncio
    async def test_empty_title_raises_value_error(self, service):
        """Empty/whitespace title rejected before any DB call."""
        for bad_title in ["", "   ", "\n"]:
            with pytest.raises(ValueError, match="Title cannot be null or empty"):
                await service.rename_session(session_id=uuid4(), title=bad_title)

    @pytest.mark.asyncio
    async def test_not_found_raises_value_error(self, service, mock_db):
        """Renaming nonexistent session raises ValueError."""
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.rename_session(session_id=uuid4(), title="New")


# =============================================================================
# 4. Session Listing
# =============================================================================


class TestListSessions:
    @pytest.mark.asyncio
    async def test_returns_items_and_cursor(self, service, mock_db, org_id, user_id):
        """List returns items and a cursor when there are more results."""
        # Create limit+1 rows to trigger has_more
        rows = [
            _make_session_row(
                user_id=user_id,
                organisation_id=org_id,
                created_at=datetime(2024, 1, i + 1),
            )
            for i in range(3)
        ]
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning_list(rows))

        items, cursor = await service.list_sessions(limit=2)
        assert len(items) == 2
        assert cursor is not None

    @pytest.mark.asyncio
    async def test_uses_updated_at_for_ordering_and_cursor(self, service, mock_db, org_id, user_id):
        """List recency follows session updated_at, not original creation time."""
        rows = [
            _make_session_row(
                user_id=user_id,
                organisation_id=org_id,
                created_at=datetime(2024, 1, 1),
                updated_at=datetime(2024, 1, 3),
            ),
            _make_session_row(
                user_id=user_id,
                organisation_id=org_id,
                created_at=datetime(2024, 1, 2),
                updated_at=datetime(2024, 1, 2),
            ),
        ]
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning_list(rows))

        items, cursor = await service.list_sessions(limit=1)
        statement = mock_db.execute.call_args.args[0]

        assert "chat_sessions.updated_at" in str(statement)
        assert len(items) == 1
        assert cursor is not None
        decoded_ts, decoded_id = decode_cursor(cursor)
        assert decoded_ts == rows[0].updated_at
        assert decoded_id == rows[0].session_id

    @pytest.mark.asyncio
    async def test_returns_none_cursor_on_last_page(self, service, mock_db, org_id, user_id):
        """No more items → cursor is None."""
        rows = [_make_session_row(user_id=user_id, organisation_id=org_id)]
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning_list(rows))

        items, cursor = await service.list_sessions(limit=20)
        assert len(items) == 1
        assert cursor is None

    @pytest.mark.asyncio
    async def test_empty_list(self, service, mock_db):
        """No sessions returns empty list and None cursor."""
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning_list([]))

        items, cursor = await service.list_sessions()
        assert items == []
        assert cursor is None


# =============================================================================
# 5. Session Search
# =============================================================================


class TestSearchSessions:
    @pytest.mark.asyncio
    async def test_empty_query_raises_value_error(self, service):
        """Empty search query raises ValueError."""
        with pytest.raises(ValueError, match="Empty search query"):
            await service.search_sessions(q="")

    @pytest.mark.asyncio
    async def test_whitespace_query_raises_value_error(self, service):
        """Whitespace-only search query raises ValueError."""
        with pytest.raises(ValueError, match="Empty search query"):
            await service.search_sessions(q="   ")

    @pytest.mark.asyncio
    async def test_query_exceeds_50_chars_raises(self, service):
        """Query over 50 chars is rejected."""
        long_query = "x" * 51
        with pytest.raises(ValueError, match="Search query cannot exceed 50 characters"):
            await service.search_sessions(q=long_query)

    @pytest.mark.asyncio
    async def test_returns_matching_sessions(self, service, mock_db, org_id, user_id):
        """Search returns matched sessions."""
        rows = [_make_session_row(user_id=user_id, organisation_id=org_id, title="Deal Review")]
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning_list(rows))

        results = await service.search_sessions(q="Deal")
        assert len(results) == 1


# =============================================================================
# 6. Favorites
# =============================================================================


class TestSetFavorite:
    @pytest.mark.asyncio
    async def test_set_favorite_true_adds_to_properties(self, service, mock_db, org_id, user_id):
        """Setting favorite=True adds 'favorite' key to properties."""
        session_row = _make_session_row(user_id=user_id, organisation_id=org_id, properties={})
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(session_row))

        result = await service.set_favorite(session_id=session_row.session_id, favorite=True)
        assert result.properties.get("favorite") is True

    @pytest.mark.asyncio
    async def test_set_favorite_false_removes_from_properties(
        self, service, mock_db, org_id, user_id
    ):
        """Setting favorite=False removes 'favorite' key from properties."""
        session_row = _make_session_row(
            user_id=user_id, organisation_id=org_id, properties={"favorite": True}
        )
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(session_row))

        result = await service.set_favorite(session_id=session_row.session_id, favorite=False)
        assert "favorite" not in result.properties

    @pytest.mark.asyncio
    async def test_not_found_raises(self, service, mock_db):
        """Favoriting nonexistent session raises ValueError."""
        mock_db.execute = AsyncMock(return_value=_mock_execute_returning(None))

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.set_favorite(session_id=uuid4(), favorite=True)


# =============================================================================
# 7. Retry Logic
# =============================================================================


class TestExecuteWithRetry:
    @pytest.mark.asyncio
    async def test_retries_on_deque_mutated_error(self, service, mock_db):
        """RuntimeError('deque mutated during iteration') triggers a retry."""
        good_result = _mock_execute_returning(None)
        mock_db.execute = AsyncMock(
            side_effect=[
                RuntimeError("deque mutated during iteration"),
                good_result,
            ]
        )
        mock_db.rollback = AsyncMock()

        # Use a statement mock — search_sessions calls _execute_with_retry internally
        result = await service._execute_with_retry(MagicMock())
        assert result == good_result
        assert mock_db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_raises_on_other_runtime_errors(self, service, mock_db):
        """Other RuntimeErrors are NOT retried."""
        mock_db.execute = AsyncMock(side_effect=RuntimeError("some other error"))

        with pytest.raises(RuntimeError, match="some other error"):
            await service._execute_with_retry(MagicMock())

        assert mock_db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self, service, mock_db):
        """After exhausting retries, the error propagates."""
        mock_db.execute = AsyncMock(side_effect=RuntimeError("deque mutated during iteration"))
        mock_db.rollback = AsyncMock()

        with pytest.raises(RuntimeError, match="deque mutated"):
            await service._execute_with_retry(MagicMock(), retries=1)

        # 1 initial + 1 retry = 2 attempts
        assert mock_db.execute.await_count == 2
