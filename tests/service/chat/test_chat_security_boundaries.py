"""Security boundary tests that verify query-level ownership enforcement.

These tests are MUTATION-RESISTANT: they capture the actual SQL statements
and verify that organisation_id and user_id filters are present. If someone
removes an ownership check from the service, these tests WILL fail.

This addresses the problem where mocked tests pass even when security
checks are removed, because the mock doesn't care about query parameters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.service.chat.chat_message_service import ChatMessageService
from src.service.chat.chat_session_service import ChatSessionService

# ---------------------------------------------------------------------------
# SQL Capture Helper
# ---------------------------------------------------------------------------


def _not_found_result():
    """Mock Result that returns None / empty for all query patterns."""
    mock_scalars = MagicMock()
    mock_scalars.one_or_none.return_value = None
    mock_scalars.all.return_value = []
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value = mock_scalars
    mock_result.all.return_value = []
    return mock_result


def _make_capturing_db():
    """Create an AsyncMock db that captures SQL statement strings.

    Returns (db, captured_statements) where captured_statements is a list
    that will be populated with the string representations of every
    SQLAlchemy statement passed to db.execute().
    """
    captured: list[str] = []

    async def _capturing_execute(stmt, *args, **kwargs):
        try:
            compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            captured.append(str(compiled))
        except Exception:
            captured.append(repr(stmt))
        return _not_found_result()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_capturing_execute)
    db.add = MagicMock()
    db.rollback = AsyncMock()
    return db, captured


def assert_filters_in_where_clause(captured: list[str], *filter_names: str):
    """Assert that specific column names appear in the WHERE clause, not just SELECT.

    This is the key difference from naive string matching. A column can appear
    in SELECT (it's being returned) without being in WHERE (it's being filtered).
    We need to verify the column is used as a FILTER, not just returned.
    """
    for stmt_sql in captured:
        lower = stmt_sql.lower()
        where_idx = lower.find("where")
        if where_idx == -1:
            continue
        where_clause = lower[where_idx:]

        for name in filter_names:
            if name.lower() not in where_clause:
                raise AssertionError(
                    f"SECURITY: Column '{name}' appears in SELECT but NOT in WHERE clause.\n"
                    f"This means the query returns {name} but does NOT filter by it.\n"
                    f"A developer may have removed the ownership check.\n"
                    f"WHERE clause: {stmt_sql[where_idx:]}\n"
                    f"Full SQL: {stmt_sql}"
                )
        return  # Found a WHERE clause with all filters — pass

    # No statement had a WHERE clause at all
    raise AssertionError(
        f"SECURITY: No SQL statement contained a WHERE clause.\n"
        f"Expected filters on: {filter_names}\n"
        f"Captured SQL: {captured}"
    )


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
def other_org_id():
    return uuid4()


@pytest.fixture
def other_user_id():
    return uuid4()


# =============================================================================
# ChatSessionService — Query-Level Ownership Tests
# =============================================================================


class TestChatSessionServiceOwnership:
    """Verify that ChatSessionService queries always filter by org+user."""

    @pytest.mark.asyncio
    async def test_delete_session_query_filters_by_org_and_user(self, org_id, user_id):
        """DELETE query must include organisation_id AND user_id AND is_deleted filters."""
        db, captured = _make_capturing_db()
        service = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError):
            await service.delete_session(session_id=uuid4())

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_rename_session_query_filters_by_org_and_user(self, org_id, user_id):
        """RENAME query must include organisation_id AND user_id filters."""
        db, captured = _make_capturing_db()
        service = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError):
            await service.rename_session(session_id=uuid4(), title="New")

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_list_sessions_query_filters_by_org_and_user(self, org_id, user_id):
        """LIST query must include organisation_id AND user_id filters."""
        db, captured = _make_capturing_db()
        service = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)
        await service.list_sessions()

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_list_sessions_excludes_teams_chat(self, org_id, user_id):
        """LIST query must exclude TEAMS_CHAT type."""
        db, captured = _make_capturing_db()
        service = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)
        await service.list_sessions()

        assert_filters_in_where_clause(captured, "chat_type")

    @pytest.mark.asyncio
    async def test_search_sessions_query_filters_by_org_and_user(self, org_id, user_id):
        """SEARCH query must include organisation_id AND user_id filters."""
        db, captured = _make_capturing_db()
        service = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)
        await service.search_sessions(q="test")

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_set_favorite_query_filters_by_org_and_user(self, org_id, user_id):
        """FAVORITE query must include organisation_id AND user_id filters."""
        db, captured = _make_capturing_db()
        service = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError):
            await service.set_favorite(session_id=uuid4(), favorite=True)

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")


# =============================================================================
# ChatMessageService — Query-Level Ownership Tests
# =============================================================================


class TestChatMessageServiceOwnership:
    """Verify that ChatMessageService queries always filter by org+user."""

    @pytest.mark.asyncio
    async def test_create_message_query_filters_by_org_and_user(self, org_id, user_id):
        """CREATE MESSAGE session lookup must filter by organisation_id AND user_id."""
        db, captured = _make_capturing_db()
        service = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.create_user_message(session_id=uuid4(), content="Hello")

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_list_messages_query_filters_by_org_and_user(self, org_id, user_id):
        """LIST MESSAGES session lookup must filter by organisation_id AND user_id."""
        db, captured = _make_capturing_db()
        service = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError, match="Chat session not found"):
            await service.list_messages(session_id=uuid4())

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_resolve_attachments_filters_by_org_and_user(self, org_id, user_id):
        """RESOLVE ATTACHMENTS query must filter by organisation_id AND user_id."""
        db, captured = _make_capturing_db()
        service = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)
        await service._resolve_attachments([uuid4()])

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_get_message_attachments_filters_by_org_and_user(self, org_id, user_id):
        """GET ATTACHMENTS query must filter by organisation_id AND user_id."""
        db, captured = _make_capturing_db()
        msg = MagicMock()
        msg.message_id = uuid4()

        service = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)
        await service.get_message_attachments([msg])

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")

    @pytest.mark.asyncio
    async def test_feedback_query_filters_by_org_and_user(self, org_id, user_id):
        """FEEDBACK session join must filter by organisation_id AND user_id."""
        db, captured = _make_capturing_db()
        service = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError, match="Message not found"):
            await service.update_message_feedback(message_id=uuid4(), feedback_score=1)

        assert_filters_in_where_clause(captured, "organisation_id", "user_id", "is_deleted")


# =============================================================================
# Cross-User Isolation — Behavioral Tests
# =============================================================================


class TestCrossUserIsolation:
    """Verify that User A cannot access User B's data.

    These tests create services with different user/org IDs and verify
    that operations correctly reject cross-boundary access.
    """

    @pytest.mark.asyncio
    async def test_user_a_cannot_delete_user_b_session(self, org_id, user_id, other_user_id):
        """Service initialized with User A's IDs cannot delete User B's session."""
        db, captured = _make_capturing_db()
        service_a = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError, match="Chat session not found"):
            await service_a.delete_session(session_id=uuid4())

        assert_filters_in_where_clause(captured, "user_id")

    @pytest.mark.asyncio
    async def test_org_a_cannot_access_org_b_sessions(self, org_id, other_org_id, user_id):
        """Service initialized with Org A cannot access Org B's sessions."""
        db, captured = _make_capturing_db()
        service_a = ChatSessionService(db=db, organisation_id=org_id, user_id=user_id)
        await service_a.list_sessions()

        assert_filters_in_where_clause(captured, "organisation_id")

    @pytest.mark.asyncio
    async def test_user_a_cannot_send_message_to_user_b_session(
        self, org_id, user_id, other_user_id
    ):
        """Service with User A's IDs cannot create messages in User B's session."""
        db, captured = _make_capturing_db()
        service_a = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError, match="Chat session not found"):
            await service_a.create_user_message(session_id=uuid4(), content="Hacking User B")

        assert_filters_in_where_clause(captured, "user_id", "organisation_id")

    @pytest.mark.asyncio
    async def test_user_a_cannot_read_user_b_messages(self, org_id, user_id, other_user_id):
        """Service with User A's IDs cannot list messages from User B's session."""
        db, captured = _make_capturing_db()
        service_a = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError, match="Chat session not found"):
            await service_a.list_messages(session_id=uuid4())

        assert_filters_in_where_clause(captured, "user_id", "organisation_id")

    @pytest.mark.asyncio
    async def test_user_a_cannot_give_feedback_on_user_b_message(
        self, org_id, user_id, other_user_id
    ):
        """Service with User A's IDs cannot update feedback on User B's message."""
        db, captured = _make_capturing_db()
        service_a = ChatMessageService(db=db, organisation_id=org_id, user_id=user_id)

        with pytest.raises(ValueError, match="Message not found"):
            await service_a.update_message_feedback(message_id=uuid4(), feedback_score=1)

        assert_filters_in_where_clause(captured, "organisation_id", "user_id")
