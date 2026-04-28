"""Tests for provider-neutral email payload normalization."""

from __future__ import annotations

import base64

from src.service.integrations.email.normalizer import EmailPayloadNormalizer


def _make_gmail_message() -> dict:
    body = base64.urlsafe_b64encode(b"Hello from Gmail").decode().rstrip("=")
    return {
        "id": "gmail-msg-1",
        "threadId": "thread-1",
        "historyId": "42",
        "internalDate": "1700000000000",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Hello from Gmail",
        "_gmail_label_id": "INBOX",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Gmail subject"},
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "To", "value": "bob@example.com"},
                {"name": "Message-ID", "value": "<gmail-msg-1@mail.gmail.com>"},
            ],
            "body": {"data": body},
        },
    }


def test_normalize_gmail_folder_returns_provider_neutral_shape() -> None:
    """Provider-specific folder payload should normalize into generic storage fields."""
    normalizer = EmailPayloadNormalizer()

    normalized = normalizer.normalize_folder(
        {
            "id": "INBOX",
            "name": "INBOX",
            "type": "system",
            "messagesTotal": 10,
            "messagesUnread": 3,
        },
        "gmail",
    )

    assert normalized.external_id == "INBOX"
    assert normalized.provider == "gmail"
    assert normalized.fields["folder_type"] == "inbox"
    assert normalized.fields["is_monitored"] == "true"
    assert normalized.debug_sample == {
        "external_id": "INBOX",
        "folder_name": "INBOX",
        "folder_type": "inbox",
    }


def test_normalize_gmail_message_returns_provider_neutral_shape() -> None:
    """Provider-specific Gmail message payload should become provider-neutral shape."""
    normalizer = EmailPayloadNormalizer()

    normalized = normalizer.normalize_message(_make_gmail_message(), "gmail")

    assert normalized.external_id == "gmail-msg-1"
    assert normalized.provider == "gmail"
    assert normalized.external_folder_id == "INBOX"
    assert normalized.fields["subject"] == "Gmail subject"
    assert normalized.fields["from_email"] == "alice@example.com"
    assert normalized.fields["is_read"] == "false"
    assert normalized.debug_sample == {
        "external_id": "gmail-msg-1",
        "subject": "Gmail subject",
        "from_email": "alice@example.com",
        "external_folder_id": "INBOX",
    }
