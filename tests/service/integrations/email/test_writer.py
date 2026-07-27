"""Tests for email sync database writer helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert
from src.domain.models.organisation_tables.sync.email_models import Email
from src.service.integrations.email.writer import EmailSyncWriter


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        """Return all mapped rows from the in-memory result stub."""
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        """Return a stubbed SQLAlchemy scalar result for tests."""
        return _FakeScalarResult(self._rows)


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
async def test_writer_batch_upserts_folders_in_one_statement() -> None:
    """Folder writes should be batched into one SQL statement."""
    rows = [
        SimpleNamespace(external_folder_id="folder-1", folder_id=uuid4()),
        SimpleNamespace(external_folder_id="folder-2", folder_id=uuid4()),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_FakeExecuteResult(rows))
    writer = EmailSyncWriter(session, uuid4())

    result = await writer.batch_upsert_folders(
        uuid4(),
        [
            ({"id": "folder-1", "displayName": "Inbox"}, "folder-1"),
            ({"id": "folder-2", "displayName": "Sent Items"}, "folder-2"),
        ],
        "outlook",
    )

    assert session.execute.await_count == 1
    assert result == {"folder-1": rows[0], "folder-2": rows[1]}
    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql
    assert "RETURNING" in sql
    assert writer.folder_cache == {"folder-1": rows[0].folder_id, "folder-2": rows[1].folder_id}


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

    result = await writer.load_folder_ids_by_external_ids(
        uuid4(),
        ["folder-1", "folder-2", "folder-1"],
    )

    assert session.execute.await_count == 1
    assert result == {"folder-1": folder_1, "folder-2": folder_2}
    assert writer.folder_cache == result


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

    assert session.execute.await_count == 3
    assert result == {row.external_message_id: row for row in rows}
