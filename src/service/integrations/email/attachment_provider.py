"""Email attachment downloader for Gmail and Outlook.

Handles listing and downloading file attachments from Gmail (via Google API)
and Outlook (via Microsoft Graph API). Includes junk filtering so callers
receive only meaningful attachments worth processing.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)

GMAIL_BASE_URL = "https://gmail.googleapis.com"
GRAPH_BASE_URL = "https://graph.microsoft.com"

# 25 MB — maximum attachment size worth processing
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# 1 KB — minimum size; anything smaller is likely a tracking pixel or stub
_MIN_ATTACHMENT_BYTES = 1024

# 100 KB — inline images smaller than this are decorative/signature art
_MIN_IMAGE_BYTES = 100 * 1024

# Outlook Winmail.dat embedded image filenames follow this pattern (e.g. image001.png)
_SIGNATURE_IMAGE_RE = re.compile(r"^image\d{3}\.\w+$", re.IGNORECASE)


@dataclass
class AttachmentMeta:
    """Lightweight metadata for a single email attachment."""

    filename: str
    mime_type: str
    size_bytes: int
    attachment_id: str


@dataclass
class DownloadedAttachment:
    """Raw bytes of a downloaded attachment."""

    content: bytes
    size_bytes: int


def _ensure_download_size(content: bytes) -> None:
    if len(content) > _MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Decoded attachment exceeds {_MAX_ATTACHMENT_BYTES} bytes: {len(content)}"
        )


def _coerce_size_bytes(value: Any) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 0
    return max(size, 0)


def _decode_base64_payload(encoded: str, *, urlsafe: bool, provider: str) -> bytes:
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        if urlsafe:
            return base64.b64decode(padded, altchars=b"-_", validate=True)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid {provider} attachment encoding") from exc


def _quote_path_segment(value: str) -> str:
    return quote(value, safe="")


# ---------------------------------------------------------------------------
# Junk filtering
# ---------------------------------------------------------------------------


def filter_junk_attachments(attachments: list[AttachmentMeta]) -> list[AttachmentMeta]:
    """Remove attachments that are not worth processing.

    Filters out: oversized files, tracking pixels, calendar invites, small
    inline images, and Outlook signature image patterns.
    """
    kept: list[AttachmentMeta] = []

    for att in attachments:
        reason = _junk_reason(att)
        if reason:
            logger.debug("Skipping attachment %r: %s", att.filename, reason)
            continue
        kept.append(att)

    return kept


def _junk_reason(att: AttachmentMeta) -> str | None:
    """Return a reason string if the attachment should be filtered, else None."""
    mime_type = att.mime_type.lower()

    if att.size_bytes > _MAX_ATTACHMENT_BYTES:
        return f"oversized ({att.size_bytes} bytes)"

    if att.size_bytes < _MIN_ATTACHMENT_BYTES:
        return f"too small ({att.size_bytes} bytes)"

    # Calendar invites add no deal context
    if att.filename.lower().endswith(".ics"):
        return "calendar file (.ics)"
    if mime_type == "text/calendar":
        return "calendar MIME type"

    # Forwarded emails as attachments are not viewable in the assets UI
    if att.filename.lower().endswith(".eml"):
        return "forwarded email (.eml)"
    if mime_type == "message/rfc822":
        return "forwarded email (message/rfc822)"

    # Signature/decorative images embedded in email HTML
    if _SIGNATURE_IMAGE_RE.match(att.filename):
        return f"signature image pattern ({att.filename})"

    # Small images are almost always logos, icons, or inline decorations
    if mime_type.startswith("image/") and att.size_bytes < _MIN_IMAGE_BYTES:
        return f"small inline image ({att.size_bytes} bytes)"

    return None


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


async def download_gmail_attachment(
    client: httpx.AsyncClient,
    message_id: str,
    attachment_id: str,
) -> DownloadedAttachment:
    """Download a single Gmail attachment and return its decoded bytes.

    Gmail encodes attachment data with URL-safe base64 (RFC 4648 §5).
    """
    url = (
        f"{GMAIL_BASE_URL}/gmail/v1/users/me/messages/{_quote_path_segment(message_id)}"
        f"/attachments/{_quote_path_segment(attachment_id)}"
    )
    response = await client.get(url)
    response.raise_for_status()

    data = response.json()
    # Gmail omits padding — restore it before decoding
    encoded = data["data"]
    content = _decode_base64_payload(encoded, urlsafe=True, provider="Gmail")
    _ensure_download_size(content)

    return DownloadedAttachment(content=content, size_bytes=len(content))


def list_gmail_attachments(payload: dict[str, Any]) -> list[AttachmentMeta]:
    """Extract attachment metadata by recursively walking Gmail MIME parts.

    Only parts with an attachmentId (stored separately by Gmail) are returned.
    Parts with inline body data are skipped — those are body content, not attachments.
    """
    results: list[AttachmentMeta] = []
    _walk_gmail_parts(payload, results)
    return results


async def list_gmail_message_attachments(
    client: httpx.AsyncClient,
    message_id: str,
) -> list[AttachmentMeta]:
    """Fetch a full Gmail message and list its separately stored attachments."""
    url = f"{GMAIL_BASE_URL}/gmail/v1/users/me/messages/{_quote_path_segment(message_id)}"
    response = await client.get(url, params={"format": "full"})
    response.raise_for_status()
    return list_gmail_attachments(response.json().get("payload", {}))


def _walk_gmail_parts(part: dict[str, Any], results: list[AttachmentMeta]) -> None:
    """Collect IDs stored outside Gmail's arbitrarily nested MIME tree."""
    if not isinstance(part, dict):
        return

    body = part.get("body", {})
    attachment_id = body.get("attachmentId")

    if attachment_id:
        results.append(
            AttachmentMeta(
                filename=part.get("filename", ""),
                mime_type=part.get("mimeType", "application/octet-stream"),
                size_bytes=_coerce_size_bytes(body.get("size")),
                attachment_id=attachment_id,
            )
        )

    for child in part.get("parts", []):
        if isinstance(child, dict):
            _walk_gmail_parts(child, results)


# ---------------------------------------------------------------------------
# Outlook / Microsoft Graph
# ---------------------------------------------------------------------------


async def download_outlook_attachment(
    client: httpx.AsyncClient,
    message_id: str,
    attachment_id: str,
) -> DownloadedAttachment:
    """Download a single Outlook attachment and return its decoded bytes.

    Microsoft Graph returns attachment content as standard base64 in the
    `contentBytes` field.
    """
    url = (
        f"{GRAPH_BASE_URL}/v1.0/me/messages/{_quote_path_segment(message_id)}"
        f"/attachments/{_quote_path_segment(attachment_id)}"
    )
    response = await client.get(url)
    response.raise_for_status()

    data = response.json()
    content = _decode_base64_payload(
        data["contentBytes"],
        urlsafe=False,
        provider="Outlook",
    )
    _ensure_download_size(content)

    return DownloadedAttachment(content=content, size_bytes=len(content))


async def list_outlook_attachments(
    client: httpx.AsyncClient,
    message_id: str,
) -> list[AttachmentMeta]:
    """List file attachments on an Outlook message via Microsoft Graph.

    Only `#microsoft.graph.fileAttachment` entries are returned. `itemAttachment`
    entries (embedded email messages) are skipped — they're not file assets.
    """
    url = f"{GRAPH_BASE_URL}/v1.0/me/messages/{_quote_path_segment(message_id)}/attachments"
    response = await client.get(url, params={"$select": "id,name,contentType,size"})
    response.raise_for_status()

    items = response.json().get("value", [])
    results: list[AttachmentMeta] = []

    for item in items:
        # itemAttachment = forwarded/embedded email, not a file — skip
        if item.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue

        attachment_id = item.get("id")
        if not attachment_id:
            logger.debug("Skipping Outlook attachment without id: %r", item.get("name"))
            continue

        results.append(
            AttachmentMeta(
                filename=item.get("name", ""),
                mime_type=item.get("contentType", "application/octet-stream"),
                size_bytes=_coerce_size_bytes(item.get("size")),
                attachment_id=attachment_id,
            )
        )

    return results
