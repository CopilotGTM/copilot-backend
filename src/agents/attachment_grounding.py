"""Select and ground recent chat attachments for an agent run."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agno.media import Image as AgnoImage
from agno.utils.tokens import count_text_tokens

from src.domain.models.organisation_tables import ChatMessage
from src.infra.media.chat_attachment_text import (
    is_audio_mimetype,
    is_image_mimetype,
)
from src.service.chat.chat_attachment_manifest import (
    AttachmentContextItem,
    extract_attachment_items_from_manifest,
)
from src.service.chat.chat_attachment_reader import (
    AttachmentReadResult,
    read_attachment_content,
)

logger = logging.getLogger(__name__)

AttachmentReader = Callable[..., Awaitable[AttachmentReadResult]]


@dataclass(slots=True)
class GroundedAttachments:
    """Inputs produced by automatic attachment grounding for one agent run."""

    images: list[Any] | None = None
    text: str | None = None
    selected_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    text_tokens_used: int = 0


def _message_role(message: ChatMessage | dict[str, Any]) -> str:
    role = message.get("role", "user") if isinstance(message, dict) else message.role
    return role.value if hasattr(role, "value") else str(role)


def automatic_attachment_items(
    message_list: Sequence[ChatMessage | dict[str, Any]],
    *,
    max_items: int,
) -> list[AttachmentContextItem]:
    """Select current attachments or carry the previous user turn exactly once."""
    user_manifests: list[str | None] = []
    for message in reversed(message_list):
        if _message_role(message) != "user":
            continue
        manifest = message.get("attachment_manifest") if isinstance(message, dict) else None
        user_manifests.append(manifest)
        if len(user_manifests) == 2:
            break

    if not user_manifests:
        return []

    current_items = extract_attachment_items_from_manifest(user_manifests[0])
    selected = current_items or (
        extract_attachment_items_from_manifest(user_manifests[1])
        if len(user_manifests) == 2
        else []
    )
    return selected[: max(max_items, 0)]


def truncate_to_token_budget(
    text: str,
    max_tokens: int,
    model_id: str,
) -> tuple[str, bool, int]:
    """Return the longest text prefix that fits the model-specific token budget."""
    if not text or max_tokens <= 0:
        return "", bool(text), 0

    used = count_text_tokens(text, model_id)
    if used <= max_tokens:
        return text, False, used

    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if count_text_tokens(text[:midpoint], model_id) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1

    prefix = text[:low]
    return prefix, True, count_text_tokens(prefix, model_id)


async def ground_chat_attachments(
    *,
    message_list: Sequence[ChatMessage | dict[str, Any]],
    context: Any,
    images: Sequence[Any] | None,
    attachment_text: str | None,
    max_items: int,
    max_text_tokens: int,
    max_chars: int,
    model_id: str,
    reader: AttachmentReader | None = None,
) -> GroundedAttachments:
    """Open current or one-turn-carried attachments under one shared budget."""
    selected = automatic_attachment_items(message_list, max_items=max_items)
    attachment_reader = reader or read_attachment_content
    grounded_images = list(images or [])
    grounded_image_ids = {str(image.id) for image in grounded_images if getattr(image, "id", None)}
    text_parts: list[str] = []
    remaining_tokens = max(max_text_tokens, 0)
    failed_ids: list[str] = []

    def append_text(block: str) -> None:
        nonlocal remaining_tokens
        if not block or remaining_tokens <= 0:
            return
        separator = "\n\n" if text_parts else ""
        bounded, _truncated, used = truncate_to_token_budget(
            f"{separator}{block}", remaining_tokens, model_id
        )
        if bounded:
            text_parts.append(bounded)
            remaining_tokens -= used

    if attachment_text:
        append_text(
            "--- Existing attachment context (bounded excerpt; may be partial) ---\n"
            f"{attachment_text}"
        )

    for item in selected:
        if item.attachment_id in grounded_image_ids:
            continue
        if remaining_tokens <= 0 and not is_image_mimetype(item.mimetype):
            continue
        if not item.mimetype or is_audio_mimetype(item.mimetype):
            continue

        try:
            result = await attachment_reader(
                item.attachment_id, context=context, max_chars=max_chars
            )
        except Exception:
            # A single corrupt file must not discard other attachments selected
            # for the same run; the failed ID remains observable to the caller.
            logger.exception("Failed to ground chat attachment %s", item.attachment_id)
            failed_ids.append(item.attachment_id)
            continue

        filename = result.filename or item.filename or item.attachment_id
        if result.kind == "image" and result.content_bytes is not None:
            mimetype = result.mimetype or item.mimetype or "image/png"
            grounded_images.append(
                AgnoImage(
                    id=result.attachment_id,
                    content=result.content_bytes,
                    format=mimetype.split("/")[-1],
                    mime_type=mimetype,
                )
            )
            grounded_image_ids.add(result.attachment_id)
            continue

        if result.error_code:
            failed_ids.append(item.attachment_id)
            append_text(
                f"--- Attachment unavailable: {filename} ---\n"
                f"{result.error or 'Attachment content unavailable'}"
            )
            continue

        if result.text:
            # copilotgtm: this is a bounded prefix, not full-document retrieval.
            # Add chunked retrieval when complete large-file Q&A is required.
            append_text(
                "--- Grounded attachment "
                f"({result.kind}, bounded excerpt; may be partial): {filename} ---\n"
                f"{result.text}"
            )

    used_tokens = max(max_text_tokens, 0) - remaining_tokens
    logger.info(
        "Automatically grounded chat attachments: selected=%d images=%d "
        "text_tokens_used=%d failed=%d",
        len(selected),
        len(grounded_images),
        used_tokens,
        len(failed_ids),
    )
    return GroundedAttachments(
        images=grounded_images or None,
        text="".join(text_parts) or None,
        selected_ids=[item.attachment_id for item in selected],
        failed_ids=failed_ids,
        text_tokens_used=used_tokens,
    )
