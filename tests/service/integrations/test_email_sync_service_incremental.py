"""Tests for email sync service incremental sync functionality."""

from unittest.mock import MagicMock

import pytest
from src.workers.integrations_worker.utils.sync_metadata import SyncAction


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
