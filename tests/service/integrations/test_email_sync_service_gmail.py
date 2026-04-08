"""Tests for Gmail-specific field extraction in EmailSyncService."""

from __future__ import annotations

import base64

from src.service.integrations.email_sync_service import (
    EmailSyncService,
    _parse_email_address,
    _parse_email_list,
)

# ---------------------------------------------------------------------------
# Folder field extraction
# ---------------------------------------------------------------------------


class TestExtractGmailFolderFields:
    def test_inbox_label(self):
        label = {
            "id": "INBOX",
            "name": "INBOX",
            "type": "system",
            "messagesTotal": 150,
            "messagesUnread": 10,
        }
        fields = EmailSyncService._extract_gmail_folder_fields(label)

        assert fields["folder_name"] == "INBOX"
        assert fields["folder_type"] == "inbox"
        assert fields["is_monitored"] == "true"
        assert fields["total_count"] == 150
        assert fields["unread_count"] == 10

    def test_sent_label(self):
        label = {"id": "SENT", "name": "SENT", "type": "system", "messagesTotal": 80}
        fields = EmailSyncService._extract_gmail_folder_fields(label)

        assert fields["folder_type"] == "sent"
        assert fields["is_monitored"] == "true"

    def test_user_label(self):
        label = {
            "id": "Label_123",
            "name": "Deals/Acme",
            "type": "user",
            "messagesTotal": 5,
            "messagesUnread": 2,
        }
        fields = EmailSyncService._extract_gmail_folder_fields(label)

        assert fields["folder_name"] == "Deals/Acme"
        assert fields["folder_type"] == "user"
        assert fields["is_monitored"] == "false"

    def test_category_label(self):
        label = {
            "id": "CATEGORY_PROMOTIONS",
            "name": "CATEGORY_PROMOTIONS",
            "type": "system",
        }
        fields = EmailSyncService._extract_gmail_folder_fields(label)

        assert fields["folder_type"] == "category"
        assert fields["is_monitored"] == "false"

    def test_trash_label(self):
        label = {"id": "TRASH", "name": "TRASH", "type": "system"}
        fields = EmailSyncService._extract_gmail_folder_fields(label)
        assert fields["folder_type"] == "trash"

    def test_spam_label(self):
        label = {"id": "SPAM", "name": "SPAM", "type": "system"}
        fields = EmailSyncService._extract_gmail_folder_fields(label)
        assert fields["folder_type"] == "junk"

    def test_draft_label(self):
        label = {"id": "DRAFT", "name": "DRAFT", "type": "system"}
        fields = EmailSyncService._extract_gmail_folder_fields(label)
        assert fields["folder_type"] == "drafts"


# ---------------------------------------------------------------------------
# Message field extraction
# ---------------------------------------------------------------------------


def _make_gmail_message(
    *,
    msg_id: str = "abc123",
    thread_id: str = "thread1",
    history_id: str = "500",
    label_ids: list[str] | None = None,
    subject: str = "Test Subject",
    from_header: str = "Alice Smith <alice@example.com>",
    to_header: str = "bob@example.com, carol@example.com",
    body_text: str = "Hello",
    internal_date: str = "1700000000000",
) -> dict:
    """Build a realistic Gmail message dict for testing."""
    text_encoded = base64.urlsafe_b64encode(body_text.encode()).decode().rstrip("=")

    return {
        "id": msg_id,
        "threadId": thread_id,
        "historyId": history_id,
        "internalDate": internal_date,
        "labelIds": label_ids or ["INBOX"],
        "snippet": body_text[:50],
        "sizeEstimate": 1234,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_header},
                {"name": "To", "value": to_header},
                {"name": "Message-ID", "value": f"<{msg_id}@mail.gmail.com>"},
            ],
            "body": {"data": text_encoded},
        },
    }


class TestExtractGmailMessageFields:
    def test_basic_fields(self):
        msg = _make_gmail_message()
        fields = EmailSyncService._extract_gmail_message_fields(msg)

        assert fields["subject"] == "Test Subject"
        assert fields["from_email"] == "alice@example.com"
        assert fields["from_name"] == "Alice Smith"
        assert fields["thread_id"] == "thread1"
        assert fields["internet_message_id"] == "<abc123@mail.gmail.com>"
        assert fields["to_emails"] == ["bob@example.com", "carol@example.com"]
        assert fields["body_text"] == "Hello"
        assert fields["snippet"] == "Hello"

    def test_read_status(self):
        # Unread message (UNREAD label present)
        msg_unread = _make_gmail_message(label_ids=["INBOX", "UNREAD"])
        fields = EmailSyncService._extract_gmail_message_fields(msg_unread)
        assert fields["is_read"] == "false"

        # Read message (no UNREAD label)
        msg_read = _make_gmail_message(label_ids=["INBOX"])
        fields = EmailSyncService._extract_gmail_message_fields(msg_read)
        assert fields["is_read"] == "true"

    def test_starred_and_important(self):
        msg = _make_gmail_message(label_ids=["INBOX", "STARRED", "IMPORTANT"])
        fields = EmailSyncService._extract_gmail_message_fields(msg)

        assert fields["is_starred"] == "true"
        assert fields["is_important"] == "true"

    def test_draft_label(self):
        msg = _make_gmail_message(label_ids=["DRAFT"])
        fields = EmailSyncService._extract_gmail_message_fields(msg)
        assert fields["is_draft"] == "true"

    def test_received_at_from_internal_date(self):
        # 1700000000000 ms = 2023-11-14T22:13:20 UTC
        msg = _make_gmail_message(internal_date="1700000000000")
        fields = EmailSyncService._extract_gmail_message_fields(msg)

        assert fields["received_at"].year == 2023
        assert fields["received_at"].month == 11

    def test_metadata_includes_gmail_id(self):
        msg = _make_gmail_message(msg_id="xyz789")
        fields = EmailSyncService._extract_gmail_message_fields(msg)

        assert fields["email_metadata"]["gmail_id"] == "xyz789"
        assert fields["email_metadata"]["history_id"] == "500"

    def test_sent_at_from_rfc2822_date_header(self):
        """Gmail Date header is RFC 2822 — must parse to sent_at."""
        msg = _make_gmail_message()
        msg["payload"]["headers"].append(
            {"name": "Date", "value": "Sun, 16 Feb 2026 10:30:45 +0530"}
        )
        fields = EmailSyncService._extract_gmail_message_fields(msg)

        assert fields["sent_at"] is not None
        assert fields["sent_at"].year == 2026
        assert fields["sent_at"].month == 2
        assert fields["sent_at"].day == 16

    def test_sent_at_none_when_no_date_header(self):
        """Missing Date header should result in sent_at=None."""
        msg = _make_gmail_message()
        fields = EmailSyncService._extract_gmail_message_fields(msg)
        assert fields["sent_at"] is None

    def test_body_text_fallback_from_html(self):
        """When only HTML body exists, body_text should be generated by stripping tags."""
        html = "<p>Hello <b>World</b></p><p>Second paragraph</p>"
        html_encoded = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")

        msg = _make_gmail_message()
        msg["payload"] = {
            "mimeType": "text/html",
            "headers": msg["payload"]["headers"],
            "body": {"data": html_encoded},
        }
        fields = EmailSyncService._extract_gmail_message_fields(msg)

        assert fields["body_html"] == html
        assert fields["body_text"] is not None
        assert "Hello" in fields["body_text"]
        assert "World" in fields["body_text"]
        assert "<p>" not in fields["body_text"]
        assert "<b>" not in fields["body_text"]

    def test_body_text_preserved_when_plain_text_exists(self):
        """When text/plain part is present, it should be used as-is (no HTML stripping)."""
        msg = _make_gmail_message(body_text="Original plain text")
        fields = EmailSyncService._extract_gmail_message_fields(msg)

        assert fields["body_text"] == "Original plain text"


class TestExtractOutlookMessageFields:
    def test_received_at_uses_received_datetime_when_present(self):
        msg = {
            "id": "msg-1",
            "conversationId": "thread-1",
            "internetMessageId": "<msg-1@example.com>",
            "from": {"emailAddress": {"address": "alice@example.com", "name": "Alice"}},
            "toRecipients": [],
            "ccRecipients": [],
            "bccRecipients": [],
            "subject": "Test",
            "body": {"contentType": "text", "content": "Hello"},
            "bodyPreview": "Hello",
            "receivedDateTime": "2026-04-08T16:30:00Z",
            "sentDateTime": "2026-04-08T16:29:00Z",
        }

        fields = EmailSyncService._extract_outlook_message_fields(msg)

        assert fields["received_at"].isoformat() == "2026-04-08T16:30:00+00:00"
        assert fields["sent_at"].isoformat() == "2026-04-08T16:29:00+00:00"

    def test_received_at_falls_back_to_sent_datetime(self):
        msg = {
            "id": "msg-2",
            "conversationId": "thread-2",
            "internetMessageId": "<msg-2@example.com>",
            "from": {"emailAddress": {"address": "alice@example.com", "name": "Alice"}},
            "toRecipients": [],
            "ccRecipients": [],
            "bccRecipients": [],
            "subject": "Draft",
            "body": {"contentType": "text", "content": "Hello"},
            "bodyPreview": "Hello",
            "receivedDateTime": None,
            "sentDateTime": "2026-04-08T16:29:00Z",
        }

        fields = EmailSyncService._extract_outlook_message_fields(msg)

        assert fields["received_at"].isoformat() == "2026-04-08T16:29:00+00:00"
        assert fields["sent_at"].isoformat() == "2026-04-08T16:29:00+00:00"

    def test_received_at_falls_back_to_created_datetime(self):
        msg = {
            "id": "msg-3",
            "conversationId": "thread-3",
            "internetMessageId": "<msg-3@example.com>",
            "from": {"emailAddress": {"address": "alice@example.com", "name": "Alice"}},
            "toRecipients": [],
            "ccRecipients": [],
            "bccRecipients": [],
            "subject": "System message",
            "body": {"contentType": "text", "content": "Hello"},
            "bodyPreview": "Hello",
            "receivedDateTime": None,
            "sentDateTime": None,
            "createdDateTime": "2026-04-08T16:28:00Z",
        }

        fields = EmailSyncService._extract_outlook_message_fields(msg)

        assert fields["received_at"].isoformat() == "2026-04-08T16:28:00+00:00"
        assert fields["sent_at"] is None


# ---------------------------------------------------------------------------
# Email address parsing helpers
# ---------------------------------------------------------------------------


class TestParseEmailAddress:
    def test_name_and_email(self):
        email, name = _parse_email_address("Alice Smith <alice@example.com>")
        assert email == "alice@example.com"
        assert name == "Alice Smith"

    def test_bare_email(self):
        email, name = _parse_email_address("alice@example.com")
        assert email == "alice@example.com"
        assert name is None

    def test_quoted_name(self):
        email, name = _parse_email_address('"Bob Jones" <bob@example.com>')
        assert email == "bob@example.com"
        assert name == "Bob Jones"


class TestParseEmailList:
    def test_multiple_addresses(self):
        result = _parse_email_list("alice@a.com, bob@b.com, carol@c.com")
        assert result == ["alice@a.com", "bob@b.com", "carol@c.com"]

    def test_empty_header(self):
        assert _parse_email_list(None) == []
        assert _parse_email_list("") == []

    def test_single_address(self):
        assert _parse_email_list("alice@example.com") == ["alice@example.com"]

    def test_addresses_with_names(self):
        result = _parse_email_list("Alice <alice@a.com>, Bob <bob@b.com>")
        assert result == ["alice@a.com", "bob@b.com"]
