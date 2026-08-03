"""Gmail email fetcher via Google Gmail API (direct calls).

Fetches Gmail labels and messages using direct Google API calls.
OAuth tokens are read from local credentials (custom OAuth).

Uses Gmail history.list for efficient incremental sync:
- Initial sync: messages.list with label + date filter, then messages.get per ID
- Incremental sync: history.list with stored historyId (returns changes only)

Gmail API documentation:
- Labels: https://developers.google.com/gmail/api/reference/rest/v1/users.labels
- Messages: https://developers.google.com/gmail/api/reference/rest/v1/users.messages
- History: https://developers.google.com/gmail/api/reference/rest/v1/users.history
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.api_error_utils import log_provider_api_error
from src.service.integrations.email.gmail_policy import (
    GMAIL_EXCLUDED_CATEGORY_SEARCH_TERMS,
    GMAIL_MAILBOX_LABEL_ID,
    gmail_mailbox_folder_payload,
    gmail_message_is_excluded,
    gmail_storage_label,
)
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers._google_api import (
    gmail_api_request,
)

logger = get_logger(__name__)

# Concurrency limit for messages.get calls (Gmail API quota: 250 units/sec, get = 5 units)
_MAX_CONCURRENT_GETS = 5
_MAX_FULL_SYNC_PAGES = 20
_MAX_HISTORY_PAGES = 50


@dataclass
class FetchMessagesResult:
    """Result from fetch_messages containing messages and the latest historyId."""

    messages: list[dict[str, Any]]
    history_id: str | None = None


def _is_history_expired_error(payload: dict[str, Any]) -> bool:
    """Detect 404 / expired historyId from Gmail API response payload."""
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return (
        str(error.get("code", "")) == "404" or "notfound" in str(error.get("message", "")).lower()
    )


def _is_history_expired_status(exc: httpx.HTTPStatusError) -> bool:
    """Detect expired/invalid historyId from HTTP 404 or 400 response."""
    return exc.response.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_labels(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Gmail labels via direct Google API.

    Returns list of label dicts with id, name, type, messagesTotal, messagesUnread.
    Uses call_with_token_retry for automatic 401 recovery.
    """
    _ = data_type
    _ = is_initial
    _ = last_synced_at
    _ = provider_config_key

    async def _do_fetch(token: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            resp = await gmail_api_request(client, "/gmail/v1/users/me/labels")
            labels = resp.get("labels", [])

            # Labels list endpoint returns minimal info; fetch details for each to get counts
            detailed_labels: list[dict[str, Any]] = []
            for label in labels:
                label_id = label.get("id", "")
                detail = await gmail_api_request(client, f"/gmail/v1/users/me/labels/{label_id}")
                detailed_labels.append(detail)
            detailed_labels.append(gmail_mailbox_folder_payload())

        logger.info(
            "Gmail labels fetched: count=%d connection=%s",
            len(detailed_labels),
            connection_id,
        )
        return detailed_labels

    result: list[dict[str, Any]] = await call_with_token_retry(token_info, _do_fetch)
    return result


async def fetch_messages(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    history_id: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
    label_id: str | None = None,
) -> FetchMessagesResult:
    """
    Fetch Gmail messages via direct Google API.

    Two modes:
    - Initial (no history_id): messages.list + messages.get per ID, 365-day lookback
    - Incremental (history_id present): history.list for changes since that point

    If historyId is expired (404), falls back to full sync automatically.
    Uses call_with_token_retry for automatic 401 recovery.
    """
    _ = data_type
    _ = last_synced_at  # retained in signature for call-site compatibility
    _ = provider_config_key

    # Force full sync on initial even if a stale history_id was passed
    if is_initial and history_id:
        logger.info(
            "Gmail initial sync ignoring stored historyId for connection=%s",
            connection_id,
        )
        history_id = None

    async def _do_fetch(token: str) -> FetchMessagesResult:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            if history_id:
                return await _fetch_incremental(
                    client=client,
                    connection_id=connection_id,
                    history_id=history_id,
                    label_id=label_id,
                )
            return await _fetch_full(
                client=client,
                connection_id=connection_id,
                label_id=label_id,
            )

    result: FetchMessagesResult = await call_with_token_retry(token_info, _do_fetch)
    return result


# ---------------------------------------------------------------------------
# Internal: full sync
# ---------------------------------------------------------------------------


async def _fetch_full(
    *,
    client: httpx.AsyncClient,
    connection_id: str,
    label_id: str | None,
) -> FetchMessagesResult:
    """Full sync: list message IDs then batch-get details."""
    initial_history_id: str | None = None
    if label_id == GMAIL_MAILBOX_LABEL_ID:
        profile = await gmail_api_request(client, "/gmail/v1/users/me/profile")
        initial_history_id = profile.get("historyId")

    cutoff = datetime.now(UTC) - timedelta(days=365)
    cutoff_epoch = int(cutoff.timestamp())

    q = f"after:{cutoff_epoch}"
    if label_id == GMAIL_MAILBOX_LABEL_ID:
        q += " in:anywhere -in:spam -in:trash -is:chat"
        q += "".join(f" -category:{category}" for category in GMAIL_EXCLUDED_CATEGORY_SEARCH_TERMS)
    params: dict[str, Any] = {"maxResults": 500, "q": q, "includeSpamTrash": False}
    if label_id and label_id != GMAIL_MAILBOX_LABEL_ID:
        params["labelIds"] = label_id

    # Paginate message IDs
    message_ids: list[str] = []
    page_token: str | None = None
    for _ in range(_MAX_FULL_SYNC_PAGES):
        if page_token:
            params["pageToken"] = page_token

        resp = await gmail_api_request(client, "/gmail/v1/users/me/messages", params=params)

        for msg in resp.get("messages", []):
            message_ids.append(msg["id"])

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if page_token:
        raise RuntimeError(
            f"Gmail full sync exceeded {_MAX_FULL_SYNC_PAGES * 500} messages; "
            "refusing to persist an incomplete cursor"
        )

    if not message_ids:
        logger.info("Gmail full sync: no messages found for connection=%s", connection_id)
        return FetchMessagesResult(messages=[], history_id=initial_history_id)

    logger.info(
        "Gmail full sync: listed %d message IDs for connection=%s label=%s",
        len(message_ids),
        connection_id,
        label_id,
    )

    # Batch get message details
    messages = await _batch_get_messages(client, message_ids)

    # The API query reduces detail calls; labels remain the authoritative safety net.
    if label_id == GMAIL_MAILBOX_LABEL_ID:
        messages, skipped = _filter_excluded_messages(messages)
        if skipped:
            logger.warning("Gmail: %d excluded messages slipped past initial query", skipped)

    latest_history_id = initial_history_id or _extract_latest_history_id(messages)

    # A single Gmail message can have many labels, while storage requires one folder.
    for msg in messages:
        msg["_gmail_label_id"] = (
            gmail_storage_label(msg) if label_id == GMAIL_MAILBOX_LABEL_ID else label_id
        )

    logger.info(
        "Gmail full sync complete: messages=%d connection=%s label=%s",
        len(messages),
        connection_id,
        label_id,
    )
    return FetchMessagesResult(messages=messages, history_id=latest_history_id)


# ---------------------------------------------------------------------------
# Internal: incremental sync
# ---------------------------------------------------------------------------


async def _fetch_incremental(
    *,
    client: httpx.AsyncClient,
    connection_id: str,
    history_id: str,
    label_id: str | None,
) -> FetchMessagesResult:
    """Incremental sync via history.list."""
    params: dict[str, Any] = {
        "startHistoryId": history_id,
        "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
        "maxResults": 500,
    }
    if label_id and label_id != GMAIL_MAILBOX_LABEL_ID:
        params["labelId"] = label_id

    changed_ids: list[str] = []
    deleted_ids: list[str] = []
    new_history_id: str | None = None
    page_token: str | None = None
    for page_idx in range(_MAX_HISTORY_PAGES):
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = await gmail_api_request(client, "/gmail/v1/users/me/history", params=params)
        except httpx.HTTPStatusError as exc:
            # Expired historyId -> fallback to full sync
            if page_idx == 0 and _is_history_expired_status(exc):
                logger.warning(
                    "Gmail historyId expired for connection=%s; falling back to full sync",
                    connection_id,
                )
                return await _fetch_full(
                    client=client,
                    connection_id=connection_id,
                    label_id=label_id,
                )
            raise

        # Check for error payload (Gmail can return error objects in 200 responses)
        if isinstance(resp, dict) and page_idx == 0 and _is_history_expired_error(resp):
            logger.warning(
                "Gmail historyId rejected for connection=%s; falling back to full sync",
                connection_id,
            )
            return await _fetch_full(
                client=client,
                connection_id=connection_id,
                label_id=label_id,
            )

        new_history_id = resp.get("historyId") or new_history_id

        for record in resp.get("history", []):
            for change_type in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                for changed in record.get(change_type, []):
                    changed_ids.append(changed["message"]["id"])
            for removed in record.get("messagesDeleted", []):
                deleted_ids.append(removed["message"]["id"])

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if page_token:
        raise RuntimeError(
            f"Gmail incremental sync exceeded {_MAX_HISTORY_PAGES * 500} history records; "
            "refusing to advance an incomplete cursor"
        )

    # Label transitions are content changes because they affect inclusion and folder state.
    messages: list[dict[str, Any]] = []
    excluded_ids: list[str] = []
    if changed_ids:
        unique_changed = list(dict.fromkeys(changed_ids))
        fetched_messages = await _batch_get_messages(client, unique_changed)

        if label_id == GMAIL_MAILBOX_LABEL_ID:
            excluded_ids = [
                message["id"] for message in fetched_messages if gmail_message_is_excluded(message)
            ]
            messages, skipped = _filter_excluded_messages(fetched_messages)
            if skipped:
                logger.info("Gmail incremental: filtered %d excluded messages", skipped)
        else:
            messages = fetched_messages

    # Removing a previously included message from policy is a soft delete locally.
    for msg_id in dict.fromkeys([*deleted_ids, *excluded_ids]):
        messages.append(
            {
                "id": msg_id,
                "_sync_metadata": {
                    "sync_action": "DELETED",
                    "sync_type": "INCREMENTAL",
                    "source": ("history_excluded" if msg_id in excluded_ids else "history_deleted"),
                },
            }
        )

    # Folder assignment is derived from current labels in the global mailbox pass.
    for msg in messages:
        msg["_gmail_label_id"] = (
            gmail_storage_label(msg) if label_id == GMAIL_MAILBOX_LABEL_ID else label_id
        )

    logger.info(
        "Gmail incremental sync complete: added=%d deleted=%d connection=%s",
        len(changed_ids),
        len(deleted_ids) + len(excluded_ids),
        connection_id,
    )
    return FetchMessagesResult(messages=messages, history_id=new_history_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _batch_get_messages(
    client: httpx.AsyncClient,
    message_ids: list[str],
) -> list[dict[str, Any]]:
    """Fetch full message details concurrently with bounded parallelism."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_GETS)

    async def _get_one(msg_id: str) -> dict[str, Any]:
        async with semaphore:
            try:
                return await gmail_api_request(
                    client,
                    f"/gmail/v1/users/me/messages/{msg_id}",
                    params={"format": "full"},
                )
            except Exception as exc:
                log_provider_api_error(
                    logger,
                    provider="gmail",
                    operation="messages.get",
                    exc=exc,
                    endpoint=f"/gmail/v1/users/me/messages/{msg_id}",
                    message_id=msg_id,
                )
                raise

    return await asyncio.gather(*[_get_one(mid) for mid in message_ids])


def _filter_excluded_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove messages outside the mailbox ingestion policy."""
    filtered: list[dict[str, Any]] = []
    skipped = 0

    for msg in messages:
        # Always keep deleted markers
        if msg.get("_sync_metadata", {}).get("sync_action") == "DELETED":
            filtered.append(msg)
            continue

        if gmail_message_is_excluded(msg):
            skipped += 1
            continue

        filtered.append(msg)

    return filtered, skipped


def _extract_latest_history_id(messages: list[dict[str, Any]]) -> str | None:
    """Get the highest historyId from a list of messages."""
    max_id: int = 0
    max_str: str | None = None
    for msg in messages:
        hid = msg.get("historyId")
        if hid:
            try:
                val = int(hid)
                if val > max_id:
                    max_id = val
                    max_str = str(hid)
            except (ValueError, TypeError):
                pass
    return max_str


def extract_body_from_payload(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Recursively extract plain text and HTML body from Gmail MIME payload.

    Returns (body_text, body_html).
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    # Leaf node with data
    if body_data:
        decoded = _decode_base64url(body_data)
        if "text/plain" in mime_type:
            return decoded, None
        if "text/html" in mime_type:
            return None, decoded

    # Recurse into parts
    parts = payload.get("parts", [])
    text_body: str | None = None
    html_body: str | None = None

    for part in parts:
        t, h = extract_body_from_payload(part)
        if t and not text_body:
            text_body = t
        if h and not html_body:
            html_body = h

    return text_body, html_body


def get_header(headers: list[dict[str, str]], name: str) -> str | None:
    """Case-insensitive header lookup from Gmail's payload.headers array."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value")
    return None


def _decode_base64url(data: str) -> str:
    """Decode Gmail's base64url-encoded body data."""
    # Gmail uses URL-safe base64 without padding
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
