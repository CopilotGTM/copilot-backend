"""Service contracts for managing explicit read-only chat viewers."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from src.domain.models.master_tables.super_admin_models import AdminUserStatus
from src.domain.models.master_tables.user_models import User
from src.domain.models.organisation_tables.chat_session_access_models import (
    ChatSessionAccess,
)
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.infra.utils.cursor import decode_cursor
from src.service.chat.chat_session_service import ChatSessionService


def _scalar_result(row) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalar_one.return_value = row
    return result


def _list_result(rows: list[ChatSessionAccess]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _rows_result(rows: list[tuple]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


def _owned_session(*, owner_id: UUID, organisation_id: UUID) -> MagicMock:
    row = MagicMock(spec=ChatSession)
    row.session_id = uuid4()
    row.user_id = owner_id
    row.organisation_id = organisation_id
    return row


def _active_viewer(*, user_id: UUID, organisation_id: UUID) -> MagicMock:
    row = MagicMock(spec=User)
    row.user_id = user_id
    row.organisation_id = organisation_id
    row.admin_status = AdminUserStatus.APPROVED
    return row


def _grant(*, session_id: UUID, viewer_user_id: UUID, owner_id: UUID) -> MagicMock:
    row = MagicMock(spec=ChatSessionAccess)
    row.session_id = session_id
    row.viewer_user_id = viewer_user_id
    row.granted_by_user_id = owner_id
    return row


@pytest.mark.asyncio
async def test_grant_rejects_owner() -> None:
    """The owner already has implicit access and must not receive a viewer row."""
    organisation_id = uuid4()
    owner_id = uuid4()
    session = _owned_session(owner_id=owner_id, organisation_id=organisation_id)
    db = AsyncMock()
    db.execute.return_value = _scalar_result(session)
    service = ChatSessionService(db, organisation_id, owner_id)

    with pytest.raises(ValueError, match="owner already has access"):
        await service.grant_viewer(
            session_id=session.session_id,
            viewer_user_id=owner_id,
        )


@pytest.mark.asyncio
async def test_grant_requires_approved_same_org_user() -> None:
    """Master validates the user while the org session remains tenant-table only."""
    organisation_id = uuid4()
    owner_id = uuid4()
    session = _owned_session(owner_id=owner_id, organisation_id=organisation_id)
    db = AsyncMock()
    db.execute.return_value = _scalar_result(session)
    master_db = AsyncMock()
    master_db.execute.return_value = _scalar_result(None)
    service = ChatSessionService(db, organisation_id, owner_id, master_db=master_db)

    with pytest.raises(ValueError, match="Viewer user not found"):
        await service.grant_viewer(
            session_id=session.session_id,
            viewer_user_id=uuid4(),
        )

    sql = str(master_db.execute.await_args.args[0]).lower()
    assert db.execute.await_count == 1
    assert "users.user_id" in sql
    assert "users.organisation_id" in sql
    assert "users.admin_status" in sql
    assert "and public.users.status =" not in sql
    assert "public.users.admin_status =" in sql


@pytest.mark.asyncio
async def test_non_owner_cannot_grant() -> None:
    """A caller without owner access must fail before target-user or grant queries."""
    db = AsyncMock()
    db.execute.return_value = _scalar_result(None)
    service = ChatSessionService(db, uuid4(), uuid4())

    with pytest.raises(ValueError, match="Chat session not found"):
        await service.grant_viewer(session_id=uuid4(), viewer_user_id=uuid4())

    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_grant_converges_on_one_row() -> None:
    """Concurrent or repeated grants must use the database uniqueness contract."""
    organisation_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session = _owned_session(owner_id=owner_id, organisation_id=organisation_id)
    viewer = _active_viewer(user_id=viewer_id, organisation_id=organisation_id)
    grant = _grant(session_id=session.session_id, viewer_user_id=viewer_id, owner_id=owner_id)
    db = AsyncMock()
    db.execute.side_effect = [_scalar_result(session), MagicMock(), _scalar_result(grant)]
    master_db = AsyncMock()
    master_db.execute.return_value = _scalar_result(viewer)
    service = ChatSessionService(db, organisation_id, owner_id, master_db=master_db)

    resolved = await service.grant_viewer(
        session_id=session.session_id,
        viewer_user_id=viewer_id,
    )

    insert_sql = str(db.execute.await_args_list[1].args[0]).lower()
    assert resolved is grant
    assert "on conflict (session_id, viewer_user_id) do nothing" in insert_sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_is_idempotent() -> None:
    """Revoking an absent grant still succeeds and commits the no-op transaction."""
    organisation_id = uuid4()
    owner_id = uuid4()
    session = _owned_session(owner_id=owner_id, organisation_id=organisation_id)
    db = AsyncMock()
    db.execute.side_effect = [_scalar_result(session), MagicMock()]
    service = ChatSessionService(db, organisation_id, owner_id)

    await service.revoke_viewer(
        session_id=session.session_id,
        viewer_user_id=uuid4(),
    )

    delete_sql = str(db.execute.await_args_list[1].args[0]).lower()
    assert "delete from chat_session_access" in delete_sql
    assert "chat_session_access.organisation_id" in delete_sql
    assert "chat_session_access.session_id" in delete_sql
    assert "chat_session_access.viewer_user_id" in delete_sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_owner_cannot_revoke() -> None:
    """A caller without owner access must not delete viewer grants."""
    db = AsyncMock()
    db.execute.return_value = _scalar_result(None)
    service = ChatSessionService(db, uuid4(), uuid4())

    with pytest.raises(ValueError, match="Chat session not found"):
        await service.revoke_viewer(session_id=uuid4(), viewer_user_id=uuid4())

    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_owner_can_list_viewers() -> None:
    """Viewer enumeration must not be available through a read-only grant."""
    db = AsyncMock()
    db.execute.return_value = _scalar_result(None)
    service = ChatSessionService(db, uuid4(), uuid4())

    with pytest.raises(ValueError, match="Chat session not found"):
        await service.list_viewers(session_id=uuid4())

    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_viewer_list_is_scoped_to_org_and_session() -> None:
    """Listing must constrain both tenant and chat before returning ordered grants."""
    organisation_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    session = _owned_session(owner_id=owner_id, organisation_id=organisation_id)
    grant = _grant(session_id=session.session_id, viewer_user_id=viewer_id, owner_id=owner_id)
    db = AsyncMock()
    db.execute.side_effect = [_scalar_result(session), _list_result([grant])]
    service = ChatSessionService(db, organisation_id, owner_id)

    viewers = await service.list_viewers(session_id=session.session_id)

    sql = str(db.execute.await_args_list[1].args[0]).lower()
    assert viewers == [grant]
    assert "chat_session_access.organisation_id" in sql
    assert "chat_session_access.session_id" in sql
    assert "order by chat_session_access.created_at" in sql
    assert "chat_session_access.viewer_user_id" in sql


@pytest.mark.asyncio
async def test_default_chat_list_includes_owned_or_explicitly_shared_sessions() -> None:
    """The primary list applies owner-or-grant access before one ordered cursor."""
    organisation_id = uuid4()
    viewer_id = uuid4()
    db = AsyncMock()
    db.execute.return_value = _list_result([])
    service = ChatSessionService(db, organisation_id, viewer_id)

    items, cursor = await service.list_sessions()

    sql = str(db.execute.await_args.args[0]).lower()
    assert items == []
    assert cursor is None
    assert "chat_sessions.user_id" in sql
    assert "exists (select chat_session_access.session_id" in sql
    assert "chat_session_access.organisation_id" in sql
    assert "chat_session_access.viewer_user_id" in sql
    assert "chat_sessions.organisation_id" in sql
    assert "chat_sessions.is_deleted" in sql
    assert "chat_sessions.chat_type" in sql
    assert "order by chat_sessions.updated_at desc" in sql
    assert "chat_sessions.session_id desc" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "has_owner_filter", "has_grant_filter"),
    [
        ("owned", True, False),
        ("shared", False, True),
    ],
)
async def test_chat_list_scope_filters_access_without_changing_the_collection(
    scope: str,
    has_owner_filter: bool,
    has_grant_filter: bool,
) -> None:
    """Owned and shared views are filters over the same chat collection."""
    db = AsyncMock()
    db.execute.return_value = _list_result([])
    service = ChatSessionService(db, uuid4(), uuid4())

    await service.list_sessions(scope=scope)

    sql = str(db.execute.await_args.args[0]).lower()
    assert ("chat_sessions.user_id =" in sql) is has_owner_filter
    assert ("chat_session_access.viewer_user_id" in sql) is has_grant_filter


@pytest.mark.asyncio
async def test_unified_chat_list_paginates_by_updated_at_and_session_id() -> None:
    """Mixed ownership uses one stable keyset over canonical chat metadata."""
    rows = []
    for day in (3, 2, 1):
        row = MagicMock(spec=ChatSession)
        row.session_id = uuid4()
        row.updated_at = datetime(2024, 1, day)
        rows.append(row)

    db = AsyncMock()
    db.execute.return_value = _list_result(rows)
    service = ChatSessionService(db, uuid4(), uuid4())

    items, cursor = await service.list_sessions(limit=2)

    assert items == rows[:2]
    assert cursor is not None
    cursor_time, cursor_id = decode_cursor(cursor)
    assert cursor_time == rows[1].updated_at
    assert cursor_id == rows[1].session_id

    db.execute.return_value = _list_result([])
    await service.list_sessions(cursor=cursor)
    cursor_sql = str(db.execute.await_args.args[0]).lower()
    assert "chat_sessions.updated_at <" in cursor_sql
    assert "chat_sessions.updated_at =" in cursor_sql
    assert "chat_sessions.session_id <" in cursor_sql


@pytest.mark.asyncio
async def test_shared_settings_list_includes_grantor_and_paginates_by_share_time() -> None:
    """Org rows and master identities combine behind one stable share-time cursor."""
    organisation_id = uuid4()
    viewer_id = uuid4()
    org_rows = []
    sharers = []
    expected_rows = []
    for day in (3, 2, 1):
        chat = MagicMock(spec=ChatSession)
        chat.session_id = uuid4()
        grant = MagicMock(spec=ChatSessionAccess)
        grant.session_id = chat.session_id
        grant.created_at = datetime(2024, 1, day)
        grant.granted_by_user_id = uuid4()
        sharer = MagicMock(spec=User)
        sharer.user_id = grant.granted_by_user_id
        org_rows.append((chat, grant))
        sharers.append(sharer)
        expected_rows.append((chat, grant, sharer))

    db = AsyncMock()
    db.execute.return_value = _rows_result(org_rows)
    master_db = AsyncMock()
    master_db.execute.return_value = _list_result(sharers)
    service = ChatSessionService(db, organisation_id, viewer_id, master_db=master_db)

    items, cursor = await service.list_shared_with_me(limit=2)

    assert items == expected_rows[:2]
    assert decode_cursor(cursor) == (org_rows[1][1].created_at, org_rows[1][1].session_id)
    sql = str(db.execute.await_args.args[0]).lower()
    assert "chat_session_access.viewer_user_id" in sql
    assert "chat_session_access.organisation_id" in sql
    assert "chat_sessions.organisation_id" in sql
    assert "chat_sessions.is_deleted" in sql
    assert "public.users" not in sql
    assert "chat_session_access.granted_by_user_id" in sql
    assert "order by chat_session_access.created_at desc" in sql
    master_sql = str(master_db.execute.await_args.args[0]).lower()
    assert "from public.users" in master_sql
    assert "users.organisation_id" in master_sql

    db.execute.return_value = _rows_result([])
    await service.list_shared_with_me(cursor=cursor)
    cursor_sql = str(db.execute.await_args.args[0]).lower()
    assert "chat_session_access.created_at <" in cursor_sql
    assert "chat_session_access.session_id <" in cursor_sql
    assert master_db.execute.await_count == 1
