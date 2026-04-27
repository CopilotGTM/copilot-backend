"""Tests for email row change detection against normalized payloads."""

from __future__ import annotations

from types import SimpleNamespace

from src.service.integrations.email.change_detector import (
    has_meaningful_folder_changes,
    has_meaningful_message_changes,
)
from src.service.integrations.email.types import NormalizedEmailFolder, NormalizedEmailMessage


def _normalized_folder(**field_overrides) -> NormalizedEmailFolder:
    fields = {
        "folder_name": "Inbox",
        "folder_type": "inbox",
        "total_count": 100,
        "unread_count": 10,
    }
    fields.update(field_overrides)
    return NormalizedEmailFolder(
        external_id="folder-1",
        provider="outlook",
        fields=fields,
        data_payload={},
        debug_sample={},
    )


def _normalized_message(**field_overrides) -> NormalizedEmailMessage:
    fields = {
        "subject": "Test Subject",
        "from_email": "sender@example.com",
        "is_read": "false",
        "is_starred": "false",
        "is_important": "false",
        "is_draft": "false",
        "labels": ["important", "work"],
    }
    fields.update(field_overrides)
    return NormalizedEmailMessage(
        external_id="msg-1",
        provider="outlook",
        external_folder_id="folder-123",
        fields=fields,
        data_payload={},
        debug_sample={},
    )


def test_folder_change_detector_skips_unchanged_folder() -> None:
    """Unchanged folder shape should not be treated as a meaningful change."""
    existing = SimpleNamespace(
        folder_name="Inbox",
        folder_type="inbox",
        total_count=100,
        unread_count=10,
    )

    assert has_meaningful_folder_changes(existing, _normalized_folder()) is False


def test_folder_change_detector_detects_count_change() -> None:
    """Unread count drift should force a folder sync update."""
    existing = SimpleNamespace(
        folder_name="Inbox",
        folder_type="inbox",
        total_count=100,
        unread_count=10,
    )

    assert has_meaningful_folder_changes(existing, _normalized_folder(unread_count=11)) is True


def test_message_change_detector_skips_unchanged_message() -> None:
    """Identical message metadata should skip persistence work."""
    existing = SimpleNamespace(
        subject="Test Subject",
        from_email="sender@example.com",
        is_read="false",
        is_starred="false",
        is_important="false",
        is_draft="false",
        labels=["work", "important"],
        email_metadata={"parent_folder_id": "folder-123"},
    )

    assert has_meaningful_message_changes(existing, _normalized_message()) is False


def test_message_change_detector_detects_folder_move() -> None:
    """Folder moves are meaningful, so normalization changes trigger sync."""
    existing = SimpleNamespace(
        subject="Test Subject",
        from_email="sender@example.com",
        is_read="false",
        is_starred="false",
        is_important="false",
        is_draft="false",
        labels=["important", "work"],
        email_metadata={"parent_folder_id": "folder-123"},
    )
    incoming = _normalized_message()
    incoming = NormalizedEmailMessage(
        external_id=incoming.external_id,
        provider=incoming.provider,
        external_folder_id="folder-456",
        fields=incoming.fields,
        data_payload=incoming.data_payload,
        debug_sample=incoming.debug_sample,
    )

    assert has_meaningful_message_changes(existing, incoming) is True
