"""Tests for email sync database writer helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert
from src.domain.models.organisation_tables.sync.email_models import Email, EmailFolder
from src.service.integrations.email.writer import EmailSyncWriter


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        """Return all mapped rows from the in-memory result stub."""
        return self._rows

    def one(self):
        """Return the only mapped row from the in-memory result stub."""
        assert len(self._rows) == 1
        return self._rows[0]

    def first(self):
        """Return the first mapped row, if present."""
        return self._rows[0] if self._rows else None


class _FakeExecuteResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        """Return a stubbed SQLAlchemy scalar result for tests."""
        return _FakeScalarResult(self._rows)

    def scalar_one_or_none(self):
        """Return the optional mapped row from the in-memory result stub."""
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None


def test_message_upsert_preserves_internal_email_metadata() -> None:
    """Provider refreshes must not erase attachment extraction state."""
    writer = EmailSyncWriter(AsyncMock(), uuid4())
    values = {
        "organisation_id": uuid4(),
        "integration_id": uuid4(),
        "external_message_id": "message-1",
        "email_metadata": {"history_id": "provider-version"},
    }
    statement = insert(Email).values(**values)

    update_expression = writer.message_upsert_update_dict(statement, values)["email_metadata"]
    sql = str(update_expression.compile(dialect=postgresql.dialect()))

    assert "emails.email_metadata || excluded.email_metadata" in sql


@pytest.mark.asyncio
async def test_writer_single_folder_upsert_uses_tenant_identity() -> None:
    """Fallback folder writes must target the tenant-scoped unique constraint."""
    row = EmailFolder(external_folder_id="folder-1", folder_name="Inbox")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeExecuteResult([row]))
    writer = EmailSyncWriter(session, uuid4())

    await writer.upsert_folder(
        uuid4(),
        {"id": "folder-1", "displayName": "Inbox"},
        "outlook",
        "folder-1",
    )

    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (organisation_id, integration_id, external_folder_id)" in sql


@pytest.mark.asyncio
async def test_writer_single_message_upsert_uses_tenant_identity() -> None:
    """Fallback message writes must target the tenant-scoped unique constraint."""
    row = Email(external_message_id="message-1", folder_id=uuid4())
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeExecuteResult([row]))
    writer = EmailSyncWriter(session, uuid4())

    await writer.upsert_message(
        uuid4(),
        {"id": "message-1", "parentFolderId": "folder-1"},
        "outlook",
        "message-1",
        row.folder_id,
    )

    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (organisation_id, integration_id, external_message_id)" in sql


@pytest.mark.asyncio
async def test_writer_batch_upserts_folders_in_one_statement() -> None:
    """Folder writes should be batched into one SQL statement."""
    integration_id = uuid4()
    rows = [
        SimpleNamespace(external_folder_id="folder-1", folder_id=uuid4()),
        SimpleNamespace(external_folder_id="folder-2", folder_id=uuid4()),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeExecuteResult(rows))
    writer = EmailSyncWriter(session, uuid4())

    result = await writer.batch_upsert_folders(
        integration_id,
        [
            ({"id": "folder-1", "displayName": "Inbox"}, "folder-1"),
            ({"id": "folder-2", "displayName": "Sent Items"}, "folder-2"),
        ],
        "outlook",
    )

    assert session.execute.await_count == 1
    assert result == {"folder-1": rows[0], "folder-2": rows[1]}
    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (organisation_id, integration_id, external_folder_id)" in sql
    assert "RETURNING" in sql
    assert writer.folder_cache == {
        (integration_id, "folder-1"): rows[0].folder_id,
        (integration_id, "folder-2"): rows[1].folder_id,
    }


@pytest.mark.asyncio
async def test_writer_loads_folder_ids_once_and_warms_cache() -> None:
    """Folder-ID resolution should be one query plus cache population."""
    folder_1 = uuid4()
    folder_2 = uuid4()
    rows = [
        SimpleNamespace(external_folder_id="folder-1", folder_id=folder_1),
        SimpleNamespace(external_folder_id="folder-2", folder_id=folder_2),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeExecuteResult(rows))
    writer = EmailSyncWriter(session, uuid4())

    integration_id = uuid4()
    result = await writer.load_folder_ids_by_external_ids(
        integration_id,
        ["folder-1", "folder-2", "folder-1"],
    )

    assert session.execute.await_count == 1
    assert result == {"folder-1": folder_1, "folder-2": folder_2}
    assert writer.folder_cache == {
        (integration_id, "folder-1"): folder_1,
        (integration_id, "folder-2"): folder_2,
    }


@pytest.mark.asyncio
async def test_writer_folder_lookups_are_organisation_scoped() -> None:
    """Every reusable folder/message lookup must carry the writer's tenant boundary."""
    organisation_id = uuid4()
    integration_id = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeExecuteResult())
    writer = EmailSyncWriter(session, organisation_id)

    await writer.get_folder_by_external_id(integration_id, "folder-1")
    await writer.load_folder_ids_by_external_ids(integration_id, ["folder-1"])
    await writer.load_existing_folders_by_external_ids(integration_id, ["folder-1"])
    await writer.load_existing_messages_by_external_ids(integration_id, ["message-1"])

    assert session.execute.await_count == 4
    for call in session.execute.await_args_list:
        sql = str(call.args[0].compile(dialect=postgresql.dialect()))
        assert "organisation_id" in sql
        assert organisation_id in call.args[0].compile().params.values()


@pytest.mark.asyncio
async def test_writer_folder_cache_is_isolated_by_integration() -> None:
    """The same provider folder ID in two mailboxes must resolve independently."""
    first_integration_id = uuid4()
    second_integration_id = uuid4()
    first_folder_id = uuid4()
    second_folder_id = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _FakeExecuteResult(
                [SimpleNamespace(external_folder_id="inbox", folder_id=first_folder_id)]
            ),
            _FakeExecuteResult(
                [SimpleNamespace(external_folder_id="inbox", folder_id=second_folder_id)]
            ),
        ]
    )
    writer = EmailSyncWriter(session, uuid4())

    assert await writer.resolve_folder_id(first_integration_id, "inbox") == first_folder_id
    assert await writer.resolve_folder_id(second_integration_id, "inbox") == second_folder_id
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_writer_validates_supplied_folder_ownership() -> None:
    """A caller-provided folder must belong to the current organisation and integration."""
    organisation_id = uuid4()
    integration_id = uuid4()
    folder_id = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FakeExecuteResult([SimpleNamespace(folder_id=folder_id)])
    )
    writer = EmailSyncWriter(session, organisation_id)

    resolved = await writer.ensure_folder_id(
        integration_id,
        {"id": "message-1"},
        "outlook",
        folder_id,
        "message-1",
    )

    assert resolved == folder_id
    sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "email_folders.organisation_id" in sql
    assert "email_folders.integration_id" in sql
    assert "email_folders.folder_id" in sql


@pytest.mark.asyncio
async def test_writer_rejects_supplied_folder_from_another_integration() -> None:
    """An unowned folder ID must fail before an email row is written."""
    writer = EmailSyncWriter(AsyncMock(), uuid4())
    writer.session.execute = AsyncMock(return_value=_FakeExecuteResult())

    with pytest.raises(ValueError, match="does not belong to integration"):
        await writer.ensure_folder_id(
            uuid4(),
            {"id": "message-1"},
            "outlook",
            uuid4(),
            "message-1",
        )


@pytest.mark.asyncio
async def test_writer_validates_supplied_folder_once_for_batch() -> None:
    """Bulk writes must validate one shared folder before assigning it to every message."""
    organisation_id = uuid4()
    integration_id = uuid4()
    folder_id = uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=_FakeExecuteResult([SimpleNamespace(folder_id=folder_id)])
    )
    writer = EmailSyncWriter(session, organisation_id)

    resolved = await writer.resolve_message_folder_ids_for_batch(
        integration_id,
        {"message-1": {"id": "message-1"}, "message-2": {"id": "message-2"}},
        "outlook",
        folder_id,
    )

    assert resolved == {"message-1": folder_id, "message-2": folder_id}
    assert session.execute.await_count == 1
    sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "email_folders.organisation_id" in sql
    assert "email_folders.integration_id" in sql


@pytest.mark.asyncio
async def test_writer_batch_upserts_messages_in_bounded_chunks() -> None:
    """Large message batches should use bounded INSERT statements."""
    folder_id = uuid4()
    rows = [
        SimpleNamespace(external_message_id="msg-1"),
        SimpleNamespace(external_message_id="msg-2"),
        SimpleNamespace(external_message_id="msg-3"),
        SimpleNamespace(external_message_id="msg-4"),
        SimpleNamespace(external_message_id="msg-5"),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _FakeExecuteResult([SimpleNamespace(folder_id=folder_id)]),
            _FakeExecuteResult(rows[:2]),
            _FakeExecuteResult(rows[2:4]),
            _FakeExecuteResult(rows[4:]),
        ]
    )
    writer = EmailSyncWriter(session, uuid4(), message_upsert_chunk_size=2)

    result = await writer.batch_upsert_messages(
        uuid4(),
        [
            ({"id": "msg-1", "parentFolderId": "folder-1"}, "msg-1"),
            ({"id": "msg-2", "parentFolderId": "folder-1"}, "msg-2"),
            ({"id": "msg-3", "parentFolderId": "folder-1"}, "msg-3"),
            ({"id": "msg-4", "parentFolderId": "folder-1"}, "msg-4"),
            ({"id": "msg-5", "parentFolderId": "folder-1"}, "msg-5"),
        ],
        "outlook",
        folder_id,
    )

    assert session.execute.await_count == 4
    assert result == {row.external_message_id: row for row in rows}
    for call in session.execute.await_args_list[1:]:
        sql = str(call.args[0].compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT (organisation_id, integration_id, external_message_id)" in sql
