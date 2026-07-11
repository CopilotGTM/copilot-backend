from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agno.models.message import Message

from src.domain.models.organisation_tables import ChatMessage
from src.service.chat.chat_attachment_manifest import extract_attachment_ids_from_manifest

# ~250k chars ≈ ~62k tokens — leaves room for system prompt, history, and response
_MAX_ATTACHMENT_TEXT_CHARS = 250_000
_ATTACHMENT_SETUP_TEXT = {
    "",
    "attached",
    "uploaded",
    "reuploaded",
    "upload",
    "file attached",
    "image attached",
    "screenshot attached",
    "here",
    "here it is",
    "this",
}
_ATTACHMENT_ASSISTANT_TERMS = (
    "attachment",
    "attached",
    "file",
    "image",
    "screenshot",
    "upload",
    "document",
    "pdf",
)
_SHORT_CONTINUATION_MAX_WORDS = 8


def _truncate_attachment_text(attachment_text: str) -> str:
    if len(attachment_text) <= _MAX_ATTACHMENT_TEXT_CHARS:
        return attachment_text
    return (
        attachment_text[:_MAX_ATTACHMENT_TEXT_CHARS]
        + "\n\n[... content truncated due to length ...]"
    )


def _message_role(message: ChatMessage | dict[str, Any]) -> str:
    role = message.get("role", "user") if isinstance(message, dict) else message.role
    return role.value if hasattr(role, "value") else str(role)


def _message_content_text(message: ChatMessage | dict[str, Any]) -> str:
    content = message.get("content", "") if isinstance(message, dict) else message.content

    if isinstance(content, dict):
        content = content.get("text", "")
    return str(content or "")


def latest_user_text(message_list: Sequence[ChatMessage | dict[str, Any]]) -> str:
    """Return the latest user-authored text from an agent message list."""
    for message in reversed(message_list):
        if _message_role(message) == "user":
            return _message_content_text(message).strip()
    return ""


def _is_attachment_setup_text(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    return normalized in _ATTACHMENT_SETUP_TEXT


def _is_short_continuation_text(text: str) -> bool:
    return 0 < len(text.split()) <= _SHORT_CONTINUATION_MAX_WORDS


def _assistant_kept_attachment_context(messages: Sequence[ChatMessage | dict[str, Any]]) -> bool:
    for message in messages:
        if _message_role(message) != "assistant":
            continue

        lowered = _message_content_text(message).lower()
        if any(term in lowered for term in _ATTACHMENT_ASSISTANT_TERMS):
            return True

    return False


def pending_attachment_ids_for_followup(
    message_list: Sequence[ChatMessage | dict[str, Any]],
) -> list[str]:
    """Find attachments from an upload/setup turn that the next user turn should use.

    Chat history already carries per-message attachment manifests. If the latest
    user message has no attachments, the immediately previous attachment turn is
    reopened when it was either an upload/setup turn or an assistant clarification
    kept the attachment as the active subject. Older attachments remain
    accessible through manifests and the normal `read_chat_attachment` tool.
    """
    latest_user_index: int | None = None
    latest_user_attachment_ids: list[str] = []

    for index in range(len(message_list) - 1, -1, -1):
        message = message_list[index]
        if _message_role(message) != "user":
            continue

        latest_user_index = index
        latest_user_attachment_ids = extract_attachment_ids_from_manifest(
            message.get("attachment_manifest") if isinstance(message, dict) else None
        )
        break

    if latest_user_index is None or latest_user_attachment_ids:
        return []

    latest_user_text = _message_content_text(message_list[latest_user_index]).strip()

    for index in range(latest_user_index - 1, -1, -1):
        message = message_list[index]
        if _message_role(message) != "user":
            continue

        attachment_ids = extract_attachment_ids_from_manifest(
            message.get("attachment_manifest") if isinstance(message, dict) else None
        )
        if not attachment_ids:
            continue

        attachment_text = _message_content_text(message).strip()
        if _is_attachment_setup_text(attachment_text):
            return attachment_ids

        intervening_messages = message_list[index + 1 : latest_user_index]
        if _is_short_continuation_text(latest_user_text) and _assistant_kept_attachment_context(
            intervening_messages
        ):
            return attachment_ids

        return []

    return []


def prepare_messages(
    message_list: Sequence[ChatMessage | dict[str, Any]],
    *,
    attachment_text: str | None = None,
) -> list[Message]:
    """Prepare messages for the agent by extracting relevant content.

    Handles both ChatMessage objects and dict representations.
    Message-level attachment manifests are appended to the specific message they
    belong to. `attachment_text` remains only as a legacy fallback for flows that
    still inject current-turn extracted text directly.
    """
    processed_messages = []
    for message in message_list:
        attachment_manifest: str | None = None

        if isinstance(message, dict):
            content = message.get("content", "")
            role = message.get("role", "user")
            attachment_manifest = message.get("attachment_manifest")
        else:
            content = message.content
            role = message.role

        if isinstance(content, dict):
            content = content.get("text", "")

        if attachment_manifest:
            content = f"{content}\n\n{attachment_manifest}" if content else attachment_manifest

        processed_messages.append(Message(role=role, content=content))

    if attachment_text and processed_messages:
        attachment_text = _truncate_attachment_text(attachment_text)
        for msg in reversed(processed_messages):
            if msg.role == "user":
                existing = msg.content or ""
                msg.content = f"{existing}\n\n{attachment_text}" if existing else attachment_text
                break

    return processed_messages
