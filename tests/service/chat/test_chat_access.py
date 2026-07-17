"""Authorization-query contracts for owner and shared-viewer chat access."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.service.chat.chat_access import (
    resolve_owned_chat_session,
    resolve_readable_chat_session,
)


def _result_returning(row: ChatSession | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    return result


@pytest.mark.asyncio
async def test_readable_resolver_uses_owner_or_explicit_grant() -> None:
    """Readable access must stay tenant-scoped and require ownership or a grant."""
    db = AsyncMock()
    row = MagicMock(spec=ChatSession)
    db.execute.return_value = _result_returning(row)

    resolved = await resolve_readable_chat_session(
        db,
        organisation_id=uuid4(),
        user_id=uuid4(),
        session_id=uuid4(),
    )

    sql = str(db.execute.await_args.args[0]).lower()
    assert resolved is row
    assert "chat_sessions.organisation_id" in sql
    assert "chat_sessions.is_deleted" in sql
    assert "chat_sessions.user_id" in sql
    assert "exists" in sql
    assert "chat_session_access.viewer_user_id" in sql


@pytest.mark.asyncio
async def test_owned_resolver_requires_owner_without_viewer_grants() -> None:
    """Mutation access must remain owner-only even after read sharing is enabled."""
    db = AsyncMock()
    row = MagicMock(spec=ChatSession)
    db.execute.return_value = _result_returning(row)

    resolved = await resolve_owned_chat_session(
        db,
        organisation_id=uuid4(),
        user_id=uuid4(),
        session_id=uuid4(),
    )

    sql = str(db.execute.await_args.args[0]).lower()
    assert resolved is row
    assert "chat_sessions.organisation_id" in sql
    assert "chat_sessions.is_deleted" in sql
    assert "chat_sessions.user_id" in sql
    assert "chat_session_access" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolver",
    [resolve_owned_chat_session, resolve_readable_chat_session],
)
async def test_resolvers_hide_missing_or_unauthorized_sessions(resolver) -> None:
    """Both access paths return the same not-found response to avoid disclosure."""
    db = AsyncMock()
    db.execute.return_value = _result_returning(None)

    with pytest.raises(ValueError, match="Chat session not found"):
        await resolver(
            db,
            organisation_id=uuid4(),
            user_id=uuid4(),
            session_id=uuid4(),
        )
