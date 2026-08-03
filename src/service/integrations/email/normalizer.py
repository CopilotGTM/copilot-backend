"""Provider payload normalization for email sync.

This module owns Outlook/Gmail payload parsing and provider-specific defaults.
Sync orchestration should work with normalized fields instead of reading raw
provider keys directly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from src.service.integrations.email.gmail_policy import GMAIL_MAILBOX_LABEL_ID
from src.service.integrations.email.types import (
    NormalizedEmailFolder,
    NormalizedEmailMessage,
)


class EmailPayloadNormalizer:
    """Convert provider folder and message payloads into the email sync's internal shape.

    All provider-specific quirks are normalized here so the service and writer can
    work from a single contract for change detection, upserts, and writes.
    """

    MONITORED_FOLDER_TYPES = ("inbox", "sent")

    GMAIL_LABEL_TYPE_MAP: dict[str, str] = {
        "INBOX": "inbox",
        "SENT": "sent",
        "DRAFT": "drafts",
        "TRASH": "trash",
        "SPAM": "junk",
        "STARRED": "starred",
        "IMPORTANT": "important",
        GMAIL_MAILBOX_LABEL_ID: "archive",
    }
    _GMAIL_LABEL_TYPE_MAP = GMAIL_LABEL_TYPE_MAP

    def normalize_folder(
        self,
        folder_data: dict[str, Any],
        provider: str,
    ) -> NormalizedEmailFolder:
        """Build a normalized folder DTO from raw provider payload.

        Returns are immutable so batching logic can safely deduplicate and cache
        by `external_id` without accidental mutation.
        """
        fields = self.extract_folder_fields(folder_data, provider)
        external_id = self.extract_folder_id(folder_data, provider)
        return NormalizedEmailFolder(
            external_id=external_id,
            provider=provider,
            fields=fields,
            data_payload=folder_data,
            debug_sample={
                "external_id": external_id,
                "folder_name": fields.get("folder_name"),
                "folder_type": fields.get("folder_type"),
            },
        )

    def normalize_message(
        self,
        message_data: dict[str, Any],
        provider: str,
    ) -> NormalizedEmailMessage:
        """Build a normalized message DTO from raw provider payload.

        The normalizer keeps body extraction, timestamp parsing, and recipient mapping
        in one place so writer code only persists already-normalized fields.
        """
        fields = self.extract_message_fields(message_data, provider)
        external_id = self.extract_message_id(message_data, provider)
        external_folder_id = self.extract_external_folder_id_from_message(message_data, provider)
        return NormalizedEmailMessage(
            external_id=external_id,
            provider=provider,
            external_folder_id=external_folder_id,
            fields=fields,
            data_payload=message_data,
            debug_sample={
                "external_id": external_id,
                "subject": fields.get("subject"),
                "from_email": fields.get("from_email"),
                "external_folder_id": external_folder_id,
            },
        )

    @staticmethod
    def extract_folder_id(folder_data: dict[str, Any], _provider: str) -> str | None:
        """Read the canonical folder identifier from payload."""
        return folder_data.get("id")

    @staticmethod
    def extract_message_id(message_data: dict[str, Any], _provider: str) -> str | None:
        """Read the canonical message identifier from payload."""
        return message_data.get("id")

    @classmethod
    def extract_folder_fields(
        cls,
        folder_data: dict[str, Any],
        provider: str,
    ) -> dict[str, Any]:
        """Route folder payload parsing to the provider-specific extractor."""
        if provider in ("microsoft", "outlook"):
            return cls.extract_outlook_folder_fields(folder_data)
        if provider in ("gmail", "google"):
            return cls.extract_gmail_folder_fields(folder_data)
        return cls.extract_generic_folder_fields(folder_data)

    @classmethod
    def extract_outlook_folder_fields(cls, folder_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Outlook folder keys and map display names to internal folder types."""
        folder_type = cls.classify_folder_type(folder_data.get("displayName", ""))
        is_monitored = "true" if folder_type in cls.MONITORED_FOLDER_TYPES else "false"
        return {
            "folder_name": folder_data.get("displayName", "Unnamed Folder"),
            "folder_path": None,
            "folder_type": folder_type,
            "is_monitored": is_monitored,
            "total_count": folder_data.get("totalItemCount", 0),
            "unread_count": folder_data.get("unreadItemCount", 0),
            "folder_metadata": {
                "parent_folder_id": folder_data.get("parentFolderId"),
                "child_folder_count": folder_data.get("childFolderCount", 0),
                "is_hidden": folder_data.get("isHidden", False),
            },
        }

    @staticmethod
    def classify_folder_type(folder_name: str) -> str:
        """Map Outlook folder display names to the canonical mailbox type set."""
        name_lower = folder_name.lower()
        if name_lower == "inbox":
            return "inbox"
        if name_lower in ("sent items", "sent"):
            return "sent"
        if name_lower in ("drafts",):
            return "drafts"
        if name_lower in ("deleted items", "trash"):
            return "trash"
        if name_lower in ("junk email", "junk", "spam"):
            return "junk"
        if name_lower in ("archive",):
            return "archive"
        return "user"

    @classmethod
    def extract_gmail_folder_fields(cls, label_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Gmail labels into folder-like metadata for sync persistence."""
        label_id = label_data.get("id", "")
        label_name = label_data.get("name", label_id)

        folder_type = cls.GMAIL_LABEL_TYPE_MAP.get(label_id, "user")
        # Category labels remain labeled for reporting, not core folder semantics.
        if label_id.startswith("CATEGORY_"):
            folder_type = "category"

        is_monitored = "true" if label_id == GMAIL_MAILBOX_LABEL_ID else "false"

        return {
            "folder_name": label_name,
            "folder_path": None,
            "folder_type": folder_type,
            "is_monitored": is_monitored,
            "total_count": label_data.get("messagesTotal", 0),
            "unread_count": label_data.get("messagesUnread", 0),
            "folder_metadata": {
                "label_type": label_data.get("type"),
                "label_list_visibility": label_data.get("labelListVisibility"),
                "message_list_visibility": label_data.get("messageListVisibility"),
            },
        }

    @staticmethod
    def extract_generic_folder_fields(folder_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize unknown providers into the fallback folder contract."""
        return {
            "folder_name": folder_data.get("name") or folder_data.get("displayName", "Unnamed"),
            "folder_path": None,
            "folder_type": "user",
            "is_monitored": "false",
            "total_count": 0,
            "unread_count": 0,
            "folder_metadata": {},
        }

    @classmethod
    def extract_message_fields(
        cls,
        message_data: dict[str, Any],
        provider: str,
    ) -> dict[str, Any]:
        """Route message parsing to provider-specific extractor."""
        if provider in ("microsoft", "outlook"):
            return cls.extract_outlook_message_fields(message_data)
        if provider in ("gmail", "google"):
            return cls.extract_gmail_message_fields(message_data)
        return cls.extract_generic_message_fields(message_data)

    @classmethod
    def extract_outlook_message_fields(cls, message_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Outlook message fields, including nested emailAddress and body type."""
        from_address = message_data.get("from", {}).get("emailAddress", {})
        to_recipients = message_data.get("toRecipients", [])
        cc_recipients = message_data.get("ccRecipients", [])
        bcc_recipients = message_data.get("bccRecipients", [])

        body = message_data.get("body", {})
        body_type = body.get("contentType", "text")
        body_content = body.get("content", "")

        body_text = body_content if body_type == "text" else None
        body_html = body_content if body_type == "html" else None
        if not body_text and body_html:
            body_text = cls.html_to_text(body_html)

        sent_at = cls.parse_datetime(message_data.get("sentDateTime"))
        received_at = (
            cls.parse_datetime(message_data.get("receivedDateTime"))
            or sent_at
            or cls.parse_datetime(message_data.get("createdDateTime"))
            or datetime.now(UTC)
        )

        return {
            "thread_id": message_data.get("conversationId"),
            "internet_message_id": message_data.get("internetMessageId"),
            "in_reply_to": None,
            "from_email": from_address.get("address", ""),
            "from_name": from_address.get("name"),
            "to_emails": [r.get("emailAddress", {}).get("address") for r in to_recipients],
            "cc_emails": [r.get("emailAddress", {}).get("address") for r in cc_recipients],
            "bcc_emails": [r.get("emailAddress", {}).get("address") for r in bcc_recipients],
            "subject": message_data.get("subject"),
            "body_text": body_text,
            "body_html": body_html,
            "snippet": message_data.get("bodyPreview"),
            "received_at": received_at,
            "sent_at": sent_at,
            "is_read": "true" if message_data.get("isRead", False) else "false",
            "is_starred": "false",
            "is_important": "true" if message_data.get("importance") == "high" else "false",
            "is_draft": "true" if message_data.get("isDraft", False) else "false",
            "has_attachments": "true" if message_data.get("hasAttachments", False) else "false",
            "labels": message_data.get("categories", []),
            "email_metadata": {
                "importance": message_data.get("importance"),
                "flag": message_data.get("flag"),
                "inference_classification": message_data.get("inferenceClassification"),
                "internet_message_id": message_data.get("internetMessageId"),
                "parent_folder_id": message_data.get("parentFolderId"),
            },
        }

    @classmethod
    def extract_gmail_message_fields(cls, message_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Gmail message payload and headers into the canonical message shape."""
        from src.workers.integrations_worker.providers.email.gmail_email import (
            extract_body_from_payload,
            get_header,
        )

        headers = message_data.get("payload", {}).get("headers", [])
        payload = message_data.get("payload", {})

        from_raw = get_header(headers, "From") or ""
        from_email, from_name = cls.parse_email_address(from_raw)

        to_emails = cls.parse_email_list(get_header(headers, "To"))
        cc_emails = cls.parse_email_list(get_header(headers, "Cc"))
        bcc_emails = cls.parse_email_list(get_header(headers, "Bcc"))

        body_text, body_html = extract_body_from_payload(payload)
        if not body_text and body_html:
            body_text = cls.html_to_text(body_html)

        internal_date_ms = message_data.get("internalDate")
        received_at = (
            datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=UTC)
            if internal_date_ms
            else datetime.now(UTC)
        )

        sent_at = cls.parse_datetime(get_header(headers, "Date"))

        label_ids = message_data.get("labelIds", [])
        label_set = set(label_ids)

        has_attachments = cls.gmail_payload_has_attachment(payload)

        return {
            "thread_id": message_data.get("threadId"),
            "internet_message_id": get_header(headers, "Message-ID"),
            "in_reply_to": get_header(headers, "In-Reply-To"),
            "from_email": from_email,
            "from_name": from_name,
            "to_emails": to_emails,
            "cc_emails": cc_emails,
            "bcc_emails": bcc_emails,
            "subject": get_header(headers, "Subject"),
            "body_text": body_text,
            "body_html": body_html,
            "snippet": message_data.get("snippet"),
            "received_at": received_at,
            "sent_at": sent_at,
            "is_read": "false" if "UNREAD" in label_set else "true",
            "is_starred": "true" if "STARRED" in label_set else "false",
            "is_important": "true" if "IMPORTANT" in label_set else "false",
            "is_draft": "true" if "DRAFT" in label_set else "false",
            "has_attachments": "true" if has_attachments else "false",
            "labels": label_ids,
            "email_metadata": {
                "gmail_id": message_data.get("id"),
                "history_id": message_data.get("historyId"),
                "size_estimate": message_data.get("sizeEstimate"),
                "label_ids": label_ids,
            },
        }

    @classmethod
    def gmail_payload_has_attachment(cls, part: dict[str, Any]) -> bool:
        """Return true when any Gmail MIME part references a downloadable attachment."""
        body = part.get("body", {})
        if part.get("filename") and body.get("attachmentId"):
            return True

        return any(
            cls.gmail_payload_has_attachment(child)
            for child in part.get("parts", [])
            if isinstance(child, dict)
        )

    @staticmethod
    def extract_generic_message_fields(message_data: dict[str, Any]) -> dict[str, Any]:
        """Fallback message normalization for non-Outlook/Gmail payloads."""
        return {
            "thread_id": message_data.get("threadId"),
            "internet_message_id": message_data.get("internetMessageId")
            or message_data.get("messageId"),
            "from_email": message_data.get("from", ""),
            "from_name": None,
            "to_emails": message_data.get("to", []),
            "cc_emails": message_data.get("cc", []),
            "bcc_emails": message_data.get("bcc", []),
            "subject": message_data.get("subject"),
            "body_text": message_data.get("body"),
            "body_html": None,
            "snippet": message_data.get("snippet"),
            "received_at": datetime.now(UTC),
            "sent_at": None,
            "is_read": "true" if message_data.get("isRead", False) else "false",
            "is_starred": "true" if message_data.get("isStarred", False) else "false",
            "is_important": "false",
            "is_draft": "false",
            "has_attachments": "false",
            "labels": [],
            "email_metadata": {},
        }

    @staticmethod
    def parse_datetime(value: str | None) -> datetime | None:
        """Parse timestamp values from multiple provider formats.

        Outlook often sends ISO strings, while some providers fall back to
        RFC 2822-style values in headers.
        """
        if not value:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except (ValueError, AttributeError):
            pass
        try:
            return parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def html_to_text(html: str) -> str:
        """Convert HTML bodies into searchable text for persisted snippets."""
        text = re.sub(
            r"<(style|script)[^>]*>.*?</\1>",
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(?:p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"')
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def extract_external_folder_id_from_message(
        message_data: dict[str, Any],
        provider: str,
    ) -> str | None:
        """Resolve the provider-specific parent-folder id used for message routing."""
        if provider in ("microsoft", "outlook"):
            return message_data.get("parentFolderId") or message_data.get("parent_folder_id")
        if provider in ("gmail", "google"):
            return message_data.get("_gmail_label_id")
        return message_data.get("folderId")

    @staticmethod
    def parse_email_address(raw: str) -> tuple[str, str | None]:
        """Parse a sender header into `(email, display_name)` pair."""
        raw = raw.strip()
        if "<" in raw and raw.endswith(">"):
            name_part, email_part = raw.rsplit("<", 1)
            return email_part.rstrip(">").strip(), name_part.strip().strip('"') or None
        return raw, None

    @classmethod
    def parse_email_list(cls, header_value: str | None) -> list[str]:
        """Split a comma-separated header into cleaned email address values."""
        if not header_value:
            return []
        addresses: list[str] = []
        for part in header_value.split(","):
            email, _ = cls.parse_email_address(part)
            if email:
                addresses.append(email)
        return addresses


def parse_email_address(raw: str) -> tuple[str, str | None]:
    """Compatibility function for tests and legacy module imports."""
    return EmailPayloadNormalizer.parse_email_address(raw)


def parse_email_list(header_value: str | None) -> list[str]:
    """Compatibility function for tests and legacy module imports."""
    return EmailPayloadNormalizer.parse_email_list(header_value)
