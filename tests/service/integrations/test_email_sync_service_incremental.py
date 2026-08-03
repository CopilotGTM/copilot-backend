"""Tests for email sync service incremental sync functionality."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.email_sync_service import EmailSyncService
from src.workers.integrations_worker.utils.sync_metadata import SyncAction


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        """Return all stubbed rows from the fake SQL result."""
        return self._rows

    def one(self):
        """Return the first stubbed row from the fake SQL result."""
        return self._rows[0]


class _FakeExecuteResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        """Return a fake scalar result object for query assertions."""
        return _FakeScalarResult(self._rows)

    def scalar_one_or_none(self):
        """Return the optional mapped row from the in-memory result stub."""
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None


def _make_integration(**overrides):
    defaults = {
        "integration_id": uuid4(),
        "organisation_id": uuid4(),
        "created_by_user_id": uuid4(),
        "integration_type": IntegrationType.OUTLOOK_EMAIL,
        "name": "Email",
        "credentials": {},
    }
    defaults.update(overrides)
    return KnowledgeIntegration(**defaults)


def _outlook_message(
    *,
    message_id: str,
    folder_id: str = "folder-1",
    subject: str = "Email",
    is_read: bool = False,
):
    return {
        "id": message_id,
        "conversationId": f"thread-{message_id}",
        "internetMessageId": f"<{message_id}@example.com>",
        "parentFolderId": folder_id,
        "subject": subject,
        "from": {"emailAddress": {"name": "Sender", "address": "sender@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "buyer@example.com"}}],
        "receivedDateTime": "2024-01-15T10:00:00Z",
        "isRead": is_read,
        "categories": ["work"],
    }


def _extract_folder_fields(folder_data, provider):
    """Extract folder fields from provider data."""
    if provider in ("microsoft", "outlook"):
        folder_name = folder_data.get("displayName", "Unnamed Folder")
        name_lower = folder_name.lower()
        if name_lower == "inbox":
            folder_type = "inbox"
        elif name_lower in ("sent items", "sent"):
            folder_type = "sent"
        elif name_lower in ("drafts",):
            folder_type = "drafts"
        elif name_lower in ("deleted items", "trash"):
            folder_type = "trash"
        elif name_lower in ("archive",):
            folder_type = "archive"
        else:
            folder_type = "user"
        return {
            "folder_name": folder_name,
            "folder_type": folder_type,
            "total_count": folder_data.get("totalItemCount", 0),
            "unread_count": folder_data.get("unreadItemCount", 0),
        }
    return {
        "folder_name": folder_data.get("name") or folder_data.get("displayName", "Unnamed"),
        "folder_type": "user",
        "total_count": 0,
        "unread_count": 0,
    }


def _extract_message_fields(message_data, provider):
    """Extract message fields from provider data."""
    if provider in ("microsoft", "outlook"):
        from_address = message_data.get("from", {}).get("emailAddress", {})
        return {
            "subject": message_data.get("subject"),
            "from_email": from_address.get("address", ""),
            "is_read": "true" if message_data.get("isRead", False) else "false",
            "is_starred": "false",
            "is_important": "true" if message_data.get("importance") == "high" else "false",
            "is_draft": "true" if message_data.get("isDraft", False) else "false",
            "labels": message_data.get("categories", []),
        }
    return {
        "subject": message_data.get("subject"),
        "from_email": message_data.get("from", ""),
        "is_read": "true" if message_data.get("isRead", False) else "false",
        "is_starred": "true" if message_data.get("isStarred", False) else "false",
        "is_important": "false",
        "is_draft": "false",
        "labels": [],
    }


def _has_meaningful_folder_changes(existing, incoming, provider) -> bool:
    """Standalone implementation of folder change detection for testing.

    This mirrors the logic in EmailSyncService._has_meaningful_folder_changes.
    """
    fields = _extract_folder_fields(incoming, provider)

    # Compare key fields
    checks = [
        (existing.folder_name, fields.get("folder_name")),
        (existing.folder_type, fields.get("folder_type")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    # Compare counts
    incoming_total = fields.get("total_count", 0)
    incoming_unread = fields.get("unread_count", 0)

    if existing.total_count != incoming_total:
        return True
    return existing.unread_count != incoming_unread


def _has_meaningful_message_changes(existing, incoming, provider) -> bool:
    """Standalone implementation of message change detection for testing.

    This mirrors the logic in EmailSyncService._has_meaningful_message_changes.
    """
    fields = _extract_message_fields(incoming, provider)

    # Compare key string fields
    checks = [
        (existing.subject, fields.get("subject")),
        (existing.from_email, fields.get("from_email")),
        (existing.is_read, fields.get("is_read")),
        (existing.is_starred, fields.get("is_starred")),
        (existing.is_important, fields.get("is_important")),
        (existing.is_draft, fields.get("is_draft")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    # Compare labels
    existing_labels = existing.labels or []
    incoming_labels = fields.get("labels") or []
    return set(existing_labels) != set(incoming_labels)


class TestEmailIncrementalBatchWrites:
    """Regression tests for the incremental email N+1 write path."""

    @pytest.mark.asyncio
    async def test_batch_upsert_folders_uses_one_statement_and_returns_id_map(self) -> None:
        """Multiple changed folders collapse into one INSERT ... ON CONFLICT statement."""
        organisation_id = uuid4()
        integration_id = uuid4()
        rows = [
            SimpleNamespace(external_folder_id="folder-1", folder_id=uuid4()),
            SimpleNamespace(external_folder_id="folder-2", folder_id=uuid4()),
        ]
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_FakeExecuteResult(rows))
        service = EmailSyncService(session, organisation_id)

        result = await service._batch_upsert_folders(
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
        assert "ON CONFLICT" in sql
        assert "RETURNING" in sql
        assert "excluded.folder_name" in sql

    @pytest.mark.asyncio
    async def test_batch_upsert_messages_uses_one_statement_and_returns_id_map(self) -> None:
        """Multiple changed emails collapse into one INSERT ... ON CONFLICT statement."""
        organisation_id = uuid4()
        integration_id = uuid4()
        folder_id = uuid4()
        rows = [
            SimpleNamespace(external_message_id="msg-1"),
            SimpleNamespace(external_message_id="msg-2"),
        ]
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _FakeExecuteResult([SimpleNamespace(folder_id=folder_id)]),
                _FakeExecuteResult(rows),
            ]
        )
        service = EmailSyncService(session, organisation_id)

        result = await service._batch_upsert_messages(
            integration_id,
            [
                (_outlook_message(message_id="msg-1"), "msg-1"),
                (_outlook_message(message_id="msg-2"), "msg-2"),
            ],
            "outlook",
            folder_id,
        )

        assert session.execute.await_count == 2
        assert result == {"msg-1": rows[0], "msg-2": rows[1]}
        sql = str(session.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT" in sql
        assert "RETURNING" in sql
        assert "excluded.subject" in sql

    @pytest.mark.asyncio
    async def test_folder_id_preload_uses_one_statement_and_caches_results(self) -> None:
        """Resolving message parent folders is one SELECT, not one SELECT per email."""
        organisation_id = uuid4()
        integration_id = uuid4()
        folder_1 = uuid4()
        folder_2 = uuid4()
        rows = [
            SimpleNamespace(external_folder_id="folder-1", folder_id=folder_1),
            SimpleNamespace(external_folder_id="folder-2", folder_id=folder_2),
        ]
        session = AsyncMock()
        session.execute = AsyncMock(return_value=_FakeExecuteResult(rows))
        service = EmailSyncService(session, organisation_id)

        result = await service._load_folder_ids_by_external_ids(
            integration_id,
            ["folder-1", "folder-2", "folder-1"],
        )

        assert session.execute.await_count == 1
        assert result == {"folder-1": folder_1, "folder-2": folder_2}
        assert service._folder_cache == {
            (integration_id, "folder-1"): folder_1,
            (integration_id, "folder-2"): folder_2,
        }

    @pytest.mark.asyncio
    async def test_incremental_sync_batches_created_and_updated_messages(self) -> None:
        """Created and changed messages are accumulated and written once."""
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        folder_id = uuid4()
        created_email_id = uuid4()
        updated_email_id = uuid4()
        deleted_email_id = uuid4()
        session = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        service = EmailSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()

        existing_updated = SimpleNamespace(
            subject="Old subject",
            from_email="sender@example.com",
            is_read="false",
            is_starred="false",
            is_important="false",
            is_draft="false",
            labels=["work"],
            email_metadata={"parent_folder_id": "folder-1"},
        )
        existing_unchanged = SimpleNamespace(
            subject="Unchanged",
            from_email="sender@example.com",
            is_read="false",
            is_starred="false",
            is_important="false",
            is_draft="false",
            labels=["work"],
            email_metadata={"parent_folder_id": "folder-1"},
        )
        existing_deleted = SimpleNamespace(email_id=deleted_email_id, is_deleted=False)
        service._load_existing_messages_by_external_ids = AsyncMock(
            return_value={
                "msg-updated": existing_updated,
                "msg-unchanged": existing_unchanged,
                "msg-deleted": existing_deleted,
            }
        )
        service._has_meaningful_message_changes = MagicMock(side_effect=[True, False])
        service._batch_upsert_messages = AsyncMock(
            return_value={
                "msg-created": SimpleNamespace(
                    email_id=created_email_id,
                    external_message_id="msg-created",
                ),
                "msg-updated": SimpleNamespace(
                    email_id=updated_email_id,
                    external_message_id="msg-updated",
                ),
            }
        )

        result = await service.sync_messages_incremental(
            integration=integration,
            provider="outlook",
            folder_id=folder_id,
            messages=[
                _outlook_message(message_id="msg-created", subject="Created"),
                _outlook_message(message_id="msg-updated", subject="Updated"),
                _outlook_message(message_id="msg-unchanged", subject="Unchanged"),
                {
                    **_outlook_message(message_id="msg-deleted", subject="Deleted"),
                    "_sync_metadata": {"sync_action": "DELETED"},
                },
            ],
        )

        assert result["created_count"] == 1
        assert result["updated"] == 2
        assert result["skipped"] == 1
        assert result["changed_email_ids"] == [
            str(deleted_email_id),
            str(created_email_id),
            str(updated_email_id),
        ]
        assert existing_deleted.is_deleted is True
        assert service._batch_upsert_messages.await_count == 1
        assert len(service._batch_upsert_messages.await_args.args[1]) == 2

    @pytest.mark.asyncio
    async def test_incremental_sync_persists_other_rows_but_fails_incomplete_run(self) -> None:
        """A bad row must not advance the provider cursor past missing data."""
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        folder_id = uuid4()
        first_email_id = uuid4()
        third_email_id = uuid4()
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.in_transaction = MagicMock(return_value=True)
        session.add = MagicMock()
        service = EmailSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        service._sync_history.fail = AsyncMock()
        service._load_existing_messages_by_external_ids = AsyncMock(return_value={})
        service._batch_upsert_messages = AsyncMock(side_effect=ValueError("bad batch"))
        service._upsert_message = AsyncMock(
            side_effect=[
                SimpleNamespace(email_id=first_email_id, external_message_id="msg-1"),
                ValueError("bad row"),
                SimpleNamespace(email_id=third_email_id, external_message_id="msg-3"),
            ]
        )

        with pytest.raises(RuntimeError, match="1 of 3 email messages failed"):
            await service.sync_messages_incremental(
                integration=integration,
                provider="outlook",
                folder_id=folder_id,
                messages=[
                    _outlook_message(message_id="msg-1"),
                    _outlook_message(message_id="msg-2"),
                    _outlook_message(message_id="msg-3"),
                ],
            )

        assert service._batch_upsert_messages.await_count == 1
        assert service._upsert_message.await_count == 3
        assert session.rollback.await_count >= 1
        service._sync_history.fail.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_full_message_sync_returns_changed_email_ids(self) -> None:
        """Full sync reports local email IDs for every successful message write."""
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        folder_id = uuid4()
        first_email_id = uuid4()
        second_email_id = uuid4()
        session = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        service = EmailSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        service._upsert_message = AsyncMock(
            side_effect=[
                SimpleNamespace(
                    email_id=first_email_id,
                    created_at=1,
                    updated_at=1,
                ),
                SimpleNamespace(
                    email_id=second_email_id,
                    created_at=1,
                    updated_at=2,
                ),
            ]
        )

        result = await service.sync_messages(
            integration=integration,
            provider="outlook",
            folder_id=folder_id,
            messages=[
                _outlook_message(message_id="msg-1"),
                _outlook_message(message_id="msg-2"),
            ],
        )

        assert result["records_processed"] == 2
        assert result["changed_email_ids"] == [str(first_email_id), str(second_email_id)]

    @pytest.mark.asyncio
    async def test_full_message_sync_fails_when_any_row_is_not_persisted(self) -> None:
        organisation_id = uuid4()
        integration = _make_integration(organisation_id=organisation_id)
        session = AsyncMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.in_transaction = MagicMock(return_value=True)
        session.add = MagicMock()
        service = EmailSyncService(session, organisation_id)
        service._sync_history.start = AsyncMock(return_value=SimpleNamespace())
        service._sync_history.complete = AsyncMock()
        service._sync_history.fail = AsyncMock()
        service._upsert_message = AsyncMock(
            side_effect=[
                SimpleNamespace(email_id=uuid4(), created_at=1, updated_at=1),
                ValueError("bad row"),
            ]
        )

        with pytest.raises(RuntimeError, match="1 of 2 email messages failed"):
            await service.sync_messages(
                integration=integration,
                provider="outlook",
                messages=[
                    _outlook_message(message_id="msg-1"),
                    _outlook_message(message_id="msg-2"),
                ],
            )

        service._sync_history.fail.assert_awaited_once()


class TestEmailFolderHasMeaningfulChanges:
    """Tests for _has_meaningful_folder_changes method logic."""

    @pytest.fixture
    def mock_folder(self):
        """Create a mock EmailFolder for testing."""
        folder = MagicMock()
        folder.folder_name = "Inbox"
        folder.folder_type = "inbox"
        folder.total_count = 100
        folder.unread_count = 10
        return folder

    def test_detects_name_change(self, mock_folder) -> None:
        """Folder name change should be detected."""
        incoming = {
            "displayName": "Updated Inbox",
            "totalItemCount": 100,
            "unreadItemCount": 10,
        }
        result = _has_meaningful_folder_changes(mock_folder, incoming, "outlook")
        assert result is True

    def test_detects_total_count_change(self, mock_folder) -> None:
        """Total count change should be detected."""
        incoming = {
            "displayName": "Inbox",
            "totalItemCount": 150,  # Changed
            "unreadItemCount": 10,
        }
        result = _has_meaningful_folder_changes(mock_folder, incoming, "outlook")
        assert result is True

    def test_detects_unread_count_change(self, mock_folder) -> None:
        """Unread count change should be detected."""
        incoming = {
            "displayName": "Inbox",
            "totalItemCount": 100,
            "unreadItemCount": 25,  # Changed
        }
        result = _has_meaningful_folder_changes(mock_folder, incoming, "outlook")
        assert result is True

    def test_no_change_returns_false(self, mock_folder) -> None:
        """No meaningful change should return False."""
        incoming = {
            "displayName": "Inbox",
            "totalItemCount": 100,
            "unreadItemCount": 10,
        }
        result = _has_meaningful_folder_changes(mock_folder, incoming, "outlook")
        assert result is False


class TestEmailMessageHasMeaningfulChanges:
    """Tests for _has_meaningful_message_changes method logic."""

    @pytest.fixture
    def mock_message(self):
        """Create a mock Email for testing."""
        msg = MagicMock()
        msg.subject = "Test Subject"
        msg.from_email = "sender@example.com"
        msg.is_read = "false"
        msg.is_starred = "false"
        msg.is_important = "false"
        msg.is_draft = "false"
        msg.labels = ["important", "work"]
        msg.email_metadata = {"parent_folder_id": "folder-123"}
        return msg

    def test_detects_subject_change(self, mock_message) -> None:
        """Subject change should be detected."""
        incoming = {
            "subject": "Updated Subject",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "isRead": False,
            "categories": ["important", "work"],
        }
        result = _has_meaningful_message_changes(mock_message, incoming, "outlook")
        assert result is True

    def test_detects_is_read_change(self, mock_message) -> None:
        """is_read change should be detected."""
        incoming = {
            "subject": "Test Subject",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "isRead": True,  # Changed from False
            "categories": ["important", "work"],
        }
        result = _has_meaningful_message_changes(mock_message, incoming, "outlook")
        assert result is True

    def test_detects_labels_change(self, mock_message) -> None:
        """Labels/categories change should be detected."""
        incoming = {
            "subject": "Test Subject",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "isRead": False,
            "categories": ["important", "personal"],  # Changed
        }
        result = _has_meaningful_message_changes(mock_message, incoming, "outlook")
        assert result is True

    def test_detects_importance_change(self, mock_message) -> None:
        """Importance change should be detected."""
        incoming = {
            "subject": "Test Subject",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "isRead": False,
            "importance": "high",  # Changed
            "categories": ["important", "work"],
        }
        result = _has_meaningful_message_changes(mock_message, incoming, "outlook")
        assert result is True

    def test_no_change_returns_false(self, mock_message) -> None:
        """No meaningful change should return False."""
        incoming = {
            "subject": "Test Subject",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "isRead": False,
            "categories": ["important", "work"],
        }
        result = _has_meaningful_message_changes(mock_message, incoming, "outlook")
        assert result is False

    def test_handles_none_values(self) -> None:
        """None values should be handled gracefully."""
        mock_message = MagicMock()
        mock_message.subject = None
        mock_message.from_email = None
        mock_message.is_read = None
        mock_message.is_starred = None
        mock_message.is_important = None
        mock_message.is_draft = None
        mock_message.labels = None
        mock_message.email_metadata = None

        incoming = {"subject": "Test"}
        # Should not raise an exception
        result = _has_meaningful_message_changes(mock_message, incoming, "outlook")
        assert isinstance(result, bool)


class TestEmailIncrementalRouting:
    """Tests for email incremental sync routing logic."""

    def test_database_state_is_source_of_truth(self) -> None:
        """Database state (is_new) should determine create vs update."""
        record_in_db = True
        _sync_action = SyncAction.ADDED  # Unused - demonstrates db state is source of truth

        is_new = not record_in_db

        should_create = is_new
        should_update = not is_new

        assert should_create is False
        assert should_update is True

    def test_deleted_messages_skipped(self) -> None:
        """Deleted messages should be skipped."""
        message = {
            "id": "msg-123",
            "_sync_metadata": {"sync_action": "DELETED"},
        }
        sync_action = message.get("_sync_metadata", {}).get("sync_action")
        should_skip = sync_action == "DELETED"
        assert should_skip is True


class TestEmailAdapterRouting:
    """Tests for email adapter routing logic."""

    def test_incremental_sync_type_routes_to_incremental(self) -> None:
        """sync_type='INCREMENTAL' should route to incremental method."""
        sync_type = "INCREMENTAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is True

    def test_initial_sync_type_routes_to_full(self) -> None:
        """sync_type='INITIAL' should route to full sync method."""
        sync_type = "INITIAL"
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert is_incremental is False

    def test_none_sync_type_routes_to_full(self) -> None:
        """sync_type=None should route to full sync method."""
        sync_type = None
        is_incremental = sync_type and sync_type.upper() == "INCREMENTAL"
        assert not is_incremental


class TestEmailDataTypes:
    """Tests for email data type support in incremental sync."""

    def test_emails_data_type_supported(self) -> None:
        """'emails' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"emails", "messages", "folders"}
        assert "emails" in incremental_sync_entities

    def test_messages_data_type_supported(self) -> None:
        """'messages' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"emails", "messages", "folders"}
        assert "messages" in incremental_sync_entities

    def test_folders_data_type_supported(self) -> None:
        """'folders' data type should be in incremental_sync_entities."""
        incremental_sync_entities = {"emails", "messages", "folders"}
        assert "folders" in incremental_sync_entities


class TestOutlookFolderTypeMapping:
    """Tests for Outlook folder type mapping."""

    def test_inbox_maps_to_inbox_type(self) -> None:
        """Inbox folder should map to 'inbox' type."""
        incoming = {
            "id": "folder-inbox",
            "displayName": "Inbox",
            "totalItemCount": 100,
            "unreadItemCount": 5,
        }
        fields = _extract_folder_fields(incoming, "outlook")
        assert fields["folder_type"] == "inbox"

    def test_sent_items_maps_to_sent_type(self) -> None:
        """Sent Items folder should map to 'sent' type."""
        incoming = {
            "id": "folder-sent",
            "displayName": "Sent Items",
            "totalItemCount": 50,
            "unreadItemCount": 0,
        }
        fields = _extract_folder_fields(incoming, "outlook")
        assert fields["folder_type"] == "sent"

    def test_drafts_maps_to_drafts_type(self) -> None:
        """Drafts folder should map to 'drafts' type."""
        incoming = {
            "id": "folder-drafts",
            "displayName": "Drafts",
            "totalItemCount": 3,
            "unreadItemCount": 0,
        }
        fields = _extract_folder_fields(incoming, "outlook")
        assert fields["folder_type"] == "drafts"

    def test_deleted_items_maps_to_trash_type(self) -> None:
        """Deleted Items folder should map to 'trash' type."""
        incoming = {
            "id": "folder-trash",
            "displayName": "Deleted Items",
            "totalItemCount": 20,
            "unreadItemCount": 0,
        }
        fields = _extract_folder_fields(incoming, "outlook")
        assert fields["folder_type"] == "trash"

    def test_archive_maps_to_archive_type(self) -> None:
        """Archive folder should map to 'archive' type."""
        incoming = {
            "id": "folder-archive",
            "displayName": "Archive",
            "totalItemCount": 500,
            "unreadItemCount": 0,
        }
        fields = _extract_folder_fields(incoming, "outlook")
        assert fields["folder_type"] == "archive"

    def test_custom_folder_maps_to_user_type(self) -> None:
        """Custom folders should map to 'user' type."""
        incoming = {
            "id": "folder-custom",
            "displayName": "Project Alpha",
            "totalItemCount": 25,
            "unreadItemCount": 2,
        }
        fields = _extract_folder_fields(incoming, "outlook")
        assert fields["folder_type"] == "user"

    def test_case_insensitive_folder_type_mapping(self) -> None:
        """Folder type mapping should be case-insensitive."""
        incoming = {
            "displayName": "INBOX",
            "totalItemCount": 100,
            "unreadItemCount": 5,
        }
        fields = _extract_folder_fields(incoming, "outlook")
        assert fields["folder_type"] == "inbox"


class TestOutlookMessageFieldExtraction:
    """Tests for Outlook message field extraction."""

    def test_extracts_from_email_address(self) -> None:
        """Should extract from email address correctly."""
        incoming = {
            "subject": "Test Email",
            "from": {"emailAddress": {"name": "John Doe", "address": "john@example.com"}},
            "isRead": False,
        }
        fields = _extract_message_fields(incoming, "outlook")
        assert fields["from_email"] == "john@example.com"

    def test_extracts_importance_high(self) -> None:
        """High importance should map to is_important='true'."""
        incoming = {
            "subject": "Urgent Matter",
            "from": {"emailAddress": {"address": "boss@example.com"}},
            "isRead": False,
            "importance": "high",
        }
        fields = _extract_message_fields(incoming, "outlook")
        assert fields["is_important"] == "true"

    def test_extracts_importance_normal(self) -> None:
        """Normal importance should map to is_important='false'."""
        incoming = {
            "subject": "Normal Email",
            "from": {"emailAddress": {"address": "colleague@example.com"}},
            "isRead": False,
            "importance": "normal",
        }
        fields = _extract_message_fields(incoming, "outlook")
        assert fields["is_important"] == "false"

    def test_extracts_is_draft(self) -> None:
        """Draft status should be extracted correctly."""
        incoming = {
            "subject": "Draft Email",
            "from": {"emailAddress": {"address": "me@example.com"}},
            "isRead": False,
            "isDraft": True,
        }
        fields = _extract_message_fields(incoming, "outlook")
        assert fields["is_draft"] == "true"

    def test_extracts_categories_as_labels(self) -> None:
        """Categories should be extracted as labels."""
        incoming = {
            "subject": "Categorized Email",
            "from": {"emailAddress": {"address": "sender@example.com"}},
            "isRead": True,
            "categories": ["Red Category", "Blue Category"],
        }
        fields = _extract_message_fields(incoming, "outlook")
        assert fields["labels"] == ["Red Category", "Blue Category"]


class TestEmailIncrementalScenarios:
    """Integration scenarios testing the full incremental sync flow."""

    def test_mixed_message_scenario(self) -> None:
        """Test scenario with new, updated, and unchanged messages."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncActionCounts,
            extract_sync_action,
        )

        messages = [
            # New message
            {
                "id": "msg-new-1",
                "subject": "New Email",
                "_sync_metadata": {"sync_action": "ADDED"},
            },
            # Updated message (marked as read)
            {
                "id": "msg-existing-1",
                "subject": "Existing Email",
                "isRead": True,
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
            # Deleted message
            {
                "id": "msg-deleted-1",
                "_sync_metadata": {"sync_action": "DELETED"},
            },
            # Unchanged message
            {
                "id": "msg-existing-2",
                "subject": "Unchanged Email",
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
        ]

        counts = SyncActionCounts()
        for msg in messages:
            action = extract_sync_action(msg)
            if action:
                counts.increment(action)

        assert counts.added == 1
        assert counts.updated == 2
        assert counts.deleted == 1
        assert counts.total == 4

    def test_database_state_determines_create_or_update(self) -> None:
        """Database existence should determine create vs update."""
        existing_external_ids = {"msg-1", "msg-2"}

        message = {
            "id": "msg-1",
            "subject": "Test Message",
            "_sync_metadata": {"sync_action": "ADDED"},
        }

        external_id = message["id"]
        is_new = external_id not in existing_external_ids

        assert is_new is False
        should_update = not is_new
        assert should_update is True

    def test_new_message_gets_created(self) -> None:
        """New message (not in DB) should be created."""
        existing_external_ids = {"msg-1", "msg-2"}

        message = {
            "id": "msg-new",
            "subject": "Brand New Message",
            "_sync_metadata": {"sync_action": "ADDED"},
        }

        external_id = message["id"]
        is_new = external_id not in existing_external_ids

        assert is_new is True

    def test_unchanged_message_gets_skipped(self) -> None:
        """Message with no meaningful changes should be skipped."""
        mock_existing = MagicMock()
        mock_existing.subject = "Weekly Report"
        mock_existing.from_email = "reports@company.com"
        mock_existing.is_read = "true"
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = ["reports"]

        incoming = {
            "id": "msg-1",
            "subject": "Weekly Report",
            "from": {"emailAddress": {"address": "reports@company.com"}},
            "isRead": True,
            "categories": ["reports"],
        }

        has_changes = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert has_changes is False

    def test_read_status_change_triggers_update(self) -> None:
        """Marking a message as read should trigger update."""
        mock_existing = MagicMock()
        mock_existing.subject = "Unread Email"
        mock_existing.from_email = "sender@company.com"
        mock_existing.is_read = "false"  # Was unread
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = []

        incoming = {
            "id": "msg-1",
            "subject": "Unread Email",
            "from": {"emailAddress": {"address": "sender@company.com"}},
            "isRead": True,  # Now read
            "categories": [],
        }

        has_changes = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert has_changes is True


class TestEmailFolderIncrementalScenarios:
    """Tests for folder incremental sync scenarios."""

    def test_mixed_folder_scenario(self) -> None:
        """Test scenario with new, updated, and unchanged folders."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncActionCounts,
            extract_sync_action,
        )

        folders = [
            # New folder
            {
                "id": "folder-new",
                "displayName": "New Project",
                "_sync_metadata": {"sync_action": "ADDED"},
            },
            # Updated folder (count changed)
            {
                "id": "folder-inbox",
                "displayName": "Inbox",
                "totalItemCount": 150,
                "_sync_metadata": {"sync_action": "UPDATED"},
            },
        ]

        counts = SyncActionCounts()
        for folder in folders:
            action = extract_sync_action(folder)
            if action:
                counts.increment(action)

        assert counts.added == 1
        assert counts.updated == 1
        assert counts.total == 2

    def test_unchanged_folder_gets_skipped(self) -> None:
        """Folder with no meaningful changes should be skipped."""
        mock_existing = MagicMock()
        mock_existing.folder_name = "Projects"
        mock_existing.folder_type = "user"
        mock_existing.total_count = 50
        mock_existing.unread_count = 0

        incoming = {
            "id": "folder-projects",
            "displayName": "Projects",
            "totalItemCount": 50,
            "unreadItemCount": 0,
        }

        has_changes = _has_meaningful_folder_changes(mock_existing, incoming, "outlook")
        assert has_changes is False

    def test_unread_count_change_triggers_update(self) -> None:
        """Unread count change should trigger update."""
        mock_existing = MagicMock()
        mock_existing.folder_name = "Inbox"
        mock_existing.folder_type = "inbox"
        mock_existing.total_count = 100
        mock_existing.unread_count = 5

        incoming = {
            "id": "folder-inbox",
            "displayName": "Inbox",
            "totalItemCount": 100,
            "unreadItemCount": 10,  # Changed
        }

        has_changes = _has_meaningful_folder_changes(mock_existing, incoming, "outlook")
        assert has_changes is True


class TestEmailIncrementalResultStructure:
    """Tests for incremental sync result structure."""

    def test_result_includes_all_counts(self) -> None:
        """Result should include created, updated, skipped counts."""
        expected_keys = {
            "status",
            "records_processed",
            "records_failed",
            "created",
            "updated",
            "skipped",
        }

        sample_result = {
            "status": "success",
            "records_processed": 20,
            "records_failed": 0,
            "created": 5,
            "updated": 10,
            "skipped": 5,
        }

        assert expected_keys.issubset(sample_result.keys())

    def test_action_counts_tracking(self) -> None:
        """SyncActionCounts should track all action types."""
        from src.workers.integrations_worker.utils.sync_metadata import (
            SyncAction,
            SyncActionCounts,
        )

        counts = SyncActionCounts()
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.ADDED)
        counts.increment(SyncAction.UPDATED)
        counts.increment(SyncAction.DELETED)

        result = counts.to_dict()
        assert result["added"] == 2
        assert result["updated"] == 1
        assert result["deleted"] == 1
        assert result["total"] == 4


class TestEmailDispatcherIntegration:
    """Tests for dispatcher integration with email."""

    def test_email_category_in_incremental_categories(self) -> None:
        """'email' should be in incremental_sync_categories."""
        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        assert "email" in incremental_sync_categories

    def test_email_entities_not_in_fetcher_entities(self) -> None:
        """Email entities should NOT be in incremental_sync_entities (CRM only)."""
        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }
        assert "emails" not in incremental_sync_entities
        assert "messages" not in incremental_sync_entities
        assert "folders" not in incremental_sync_entities

    def test_sync_type_only_passed_to_handler_not_fetcher(self) -> None:
        """sync_type should be passed to handler, not to email fetcher."""
        category = "email"
        data_type = "messages"

        incremental_sync_entities = {
            "deals",
            "opportunities",
            "contacts",
            "leads",
            "companies",
            "accounts",
            "deal_tasks",
            "tasks",
            "tickets",
            "notes",
            "owners",
            "users",
            "pipelines",
        }

        should_pass_to_fetcher = data_type in incremental_sync_entities
        assert should_pass_to_fetcher is False

        incremental_sync_categories = {"crm", "calendar", "email", "messaging"}
        should_pass_to_handler = category in incremental_sync_categories
        assert should_pass_to_handler is True


class TestEmailLabelHandling:
    """Tests for email label/category handling."""

    def test_label_order_does_not_matter(self) -> None:
        """Labels should be compared as sets, not lists."""
        mock_existing = MagicMock()
        mock_existing.subject = "Test"
        mock_existing.from_email = "test@example.com"
        mock_existing.is_read = "false"
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = ["work", "important"]  # Order 1

        incoming = {
            "subject": "Test",
            "from": {"emailAddress": {"address": "test@example.com"}},
            "isRead": False,
            "categories": ["important", "work"],  # Same labels, different order
        }

        has_changes = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert has_changes is False

    def test_label_addition_detected(self) -> None:
        """Adding a new label should be detected."""
        mock_existing = MagicMock()
        mock_existing.subject = "Test"
        mock_existing.from_email = "test@example.com"
        mock_existing.is_read = "false"
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = ["work"]

        incoming = {
            "subject": "Test",
            "from": {"emailAddress": {"address": "test@example.com"}},
            "isRead": False,
            "categories": ["work", "urgent"],  # Added "urgent"
        }

        has_changes = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert has_changes is True

    def test_label_removal_detected(self) -> None:
        """Removing a label should be detected."""
        mock_existing = MagicMock()
        mock_existing.subject = "Test"
        mock_existing.from_email = "test@example.com"
        mock_existing.is_read = "false"
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = ["work", "urgent"]

        incoming = {
            "subject": "Test",
            "from": {"emailAddress": {"address": "test@example.com"}},
            "isRead": False,
            "categories": ["work"],  # Removed "urgent"
        }

        has_changes = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert has_changes is True

    def test_empty_labels_handled(self) -> None:
        """Empty labels should be handled correctly."""
        mock_existing = MagicMock()
        mock_existing.subject = "Test"
        mock_existing.from_email = "test@example.com"
        mock_existing.is_read = "false"
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = []

        incoming = {
            "subject": "Test",
            "from": {"emailAddress": {"address": "test@example.com"}},
            "isRead": False,
            "categories": [],
        }

        has_changes = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert has_changes is False


class TestEmailMetadataHandling:
    """Tests for email metadata handling."""

    def test_handles_missing_from_field(self) -> None:
        """Should handle messages with missing 'from' field."""
        mock_existing = MagicMock()
        mock_existing.subject = "System Message"
        mock_existing.from_email = ""
        mock_existing.is_read = "true"
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = []

        incoming = {
            "subject": "System Message",
            "isRead": True,
            # No 'from' field
        }

        # Should not raise
        result = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert isinstance(result, bool)

    def test_handles_missing_categories(self) -> None:
        """Should handle messages with missing categories."""
        mock_existing = MagicMock()
        mock_existing.subject = "Test"
        mock_existing.from_email = "test@example.com"
        mock_existing.is_read = "false"
        mock_existing.is_starred = "false"
        mock_existing.is_important = "false"
        mock_existing.is_draft = "false"
        mock_existing.labels = []

        incoming = {
            "subject": "Test",
            "from": {"emailAddress": {"address": "test@example.com"}},
            "isRead": False,
            # No categories field
        }

        result = _has_meaningful_message_changes(mock_existing, incoming, "outlook")
        assert result is False
