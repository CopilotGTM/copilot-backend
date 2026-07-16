from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agno.models.message import Message

from src.domain.models.organisation_tables import ChatMessage

# ~250k chars ≈ ~62k tokens — leaves room for system prompt, history, and response
_MAX_ATTACHMENT_TEXT_CHARS = 250_000


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
