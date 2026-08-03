"""Canonical Gmail mailbox inclusion and storage-label policy."""

from __future__ import annotations

from typing import Any

GMAIL_MAILBOX_LABEL_ID = "ALL_MAIL"
GMAIL_EXCLUDED_MESSAGE_LABELS = frozenset(
    {
        "SPAM",
        "TRASH",
        "CHAT",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_SOCIAL",
        "CATEGORY_FORUMS",
    }
)
GMAIL_EXCLUDED_CATEGORY_SEARCH_TERMS = ("promotions", "social", "forums")


def gmail_mailbox_folder_payload() -> dict[str, Any]:
    """Return the synthetic folder that owns Gmail's single global history cursor."""
    return {
        "id": GMAIL_MAILBOX_LABEL_ID,
        "name": "All Mail",
        "type": "system",
        "messagesTotal": 0,
        "messagesUnread": 0,
        "_sync_metadata": {"source": "copilotgtm_mailbox_policy"},
    }


def gmail_message_is_excluded(message: dict[str, Any]) -> bool:
    """Return whether current Gmail labels place a message outside ingestion policy."""
    return bool(set(message.get("labelIds", [])) & GMAIL_EXCLUDED_MESSAGE_LABELS)


def gmail_storage_label(message: dict[str, Any]) -> str:
    """Choose one persisted folder while retaining every Gmail label in metadata."""
    labels = set(message.get("labelIds", []))
    for label_id in ("DRAFT", "SENT", "INBOX"):
        if label_id in labels:
            return label_id
    return GMAIL_MAILBOX_LABEL_ID
