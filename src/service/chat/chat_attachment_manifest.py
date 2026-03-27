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
