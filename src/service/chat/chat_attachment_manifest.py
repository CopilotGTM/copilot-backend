from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.infra.config.configs import chat_file_upload_config


@dataclass(slots=True)
class AttachmentContextItem:
    attachment_id: str
    filename: str
    mimetype: str
    source_provider: str | None = None


def build_attachment_manifest(
    attachments: Iterable[AttachmentContextItem | dict[str, Any]],
    *,
    max_items: int | None = None,
) -> str:
    """Build a compact manifest that can be appended to a chat message."""
    items = [_coerce_item(attachment) for attachment in attachments]
    if not items:
        return ""

    limit = max_items or chat_file_upload_config.ATTACHMENT_MANIFEST_MAX_ITEMS
    lines = ["[Attached files]"]

    for item in items[:limit]:
        lines.append(f"- id: {item.attachment_id}")
        lines.append(f"  name: {item.filename}")
        lines.append(f"  type: {item.mimetype}")
        if item.source_provider:
            lines.append(f"  source: {item.source_provider}")

    if len(items) > limit:
        lines.append(f"- ... and {len(items) - limit} more attachment(s)")

    return "\n".join(lines)


def append_attachment_manifest(
    content: str | None,
    attachments: Iterable[AttachmentContextItem | dict[str, Any]],
) -> str:
    """Append an attachment manifest to message text when attachments exist."""
    manifest = build_attachment_manifest(attachments)
    if not manifest:
        return content or ""
    if not content:
        return manifest
    return f"{content}\n\n{manifest}"


def serialize_attachment_context(
    attachments: Iterable[AttachmentContextItem | dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "attachment_id": item.attachment_id,
            "filename": item.filename,
            "mimetype": item.mimetype,
            "source_provider": item.source_provider,
        }
        for item in (_coerce_item(attachment) for attachment in attachments)
    ]


def extract_attachment_items_from_manifest(
    manifest: str | None,
) -> list[AttachmentContextItem]:
    """Parse attachment metadata from the compact chat-history manifest."""
    if not manifest:
        return []

    items: list[AttachmentContextItem] = []
    current: dict[str, str] | None = None

    def append_current() -> None:
        if current and current.get("attachment_id"):
            items.append(
                AttachmentContextItem(
                    attachment_id=current["attachment_id"],
                    filename=current.get("filename", "attachment"),
                    mimetype=current.get("mimetype", ""),
                    source_provider=current.get("source_provider"),
                )
            )

    for raw_line in manifest.splitlines():
        line = raw_line.strip()
        if line.startswith("- id:"):
            append_current()
            current = {"attachment_id": line.removeprefix("- id:").strip()}
        elif current is not None and line.startswith("name:"):
            current["filename"] = line.removeprefix("name:").strip()
        elif current is not None and line.startswith("type:"):
            current["mimetype"] = line.removeprefix("type:").strip()
        elif current is not None and line.startswith("source:"):
            current["source_provider"] = line.removeprefix("source:").strip()

    append_current()
    return items


def extract_attachment_ids_from_manifest(manifest: str | None) -> list[str]:
    """Return attachment IDs from the compact manifest format used in chat history.

    The manifest is already the durable, model-visible representation of chat
    attachments. Parsing it keeps follow-up grounding aligned with what the
    agent can see, instead of introducing a second attachment reference format.
    """
    if not manifest:
        return []
    return [item.attachment_id for item in extract_attachment_items_from_manifest(manifest)]


def _coerce_item(attachment: AttachmentContextItem | dict[str, Any]) -> AttachmentContextItem:
    if isinstance(attachment, AttachmentContextItem):
        return attachment
    return AttachmentContextItem(
        attachment_id=str(attachment.get("attachment_id") or ""),
        filename=str(attachment.get("filename") or "attachment"),
        mimetype=str(attachment.get("mimetype") or "application/octet-stream"),
        source_provider=(
            str(attachment.get("source_provider")) if attachment.get("source_provider") else None
        ),
    )
