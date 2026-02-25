"""Outlook Email fetcher via Microsoft Graph API (direct calls).

Fetches email folders and messages from Outlook/Office 365 mailboxes.
OAuth tokens are managed by Nango (token storage + refresh) but API calls
bypass the Nango proxy to avoid proxy request quota limits.

Uses Microsoft Graph Delta Query for efficient incremental sync:
- Initial sync: /messages/delta with receivedDateTime filter (last 90 days)
- Incremental sync: Use stored deltaLink (returns ONLY new/changed/deleted messages)

Microsoft Graph API documentation:
- List folders: https://learn.microsoft.com/en-us/graph/api/user-list-mailfolders
- Delta messages: https://learn.microsoft.com/en-us/graph/delta-query-messages
- message: delta: https://learn.microsoft.com/en-us/graph/api/message-delta
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.providers._microsoft_graph import (
    get_access_token,
    graph_api_request,
    graph_api_request_url,
)

logger = get_logger(__name__)


# Batch size for body backfill requests (Microsoft Graph $batch supports up to 20)
_BODY_BATCH_SIZE = 20


@dataclass
class FetchMessagesResult:
    """Result from fetch_messages containing messages and optional delta token."""

    messages: list[dict[str, Any]]
    delta_link: str | None = None


def _mark_as_deleted(message: dict[str, Any]) -> dict[str, Any]:
    """
    Mark a message as deleted for proper handling downstream.

    Microsoft Graph delta sync returns deleted items with @removed annotation.
    We translate this to _sync_metadata for consistent handling in sync services.
    """
    message["_sync_metadata"] = {
        "sync_action": "DELETED",
        "sync_type": "INCREMENTAL",
        "inferred": True,
        "source": "delta_removed",
    }
    return message


def _filter_other_inbox_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """
    Filter out "Other" inbox messages (less important emails).

    Outlook's Focused Inbox classifies emails as:
    - "focused": Important emails from real people
    - "other": Less important (newsletters, automated, no-reply emails)

    We skip "other" emails to reduce noise and sync only relevant emails.

    Note: Deleted messages (@removed) are always kept regardless of classification.

    Args:
        messages: List of messages from Microsoft Graph

    Returns:
        Tuple of (filtered_messages, skipped_count)
    """
    filtered: list[dict[str, Any]] = []
    skipped = 0

    for message in messages:
        # Always keep deleted messages - need to mark as deleted in DB
        if (
            "@removed" in message
            or message.get("_sync_metadata", {}).get("sync_action") == "DELETED"
        ):
            filtered.append(message)
            continue

        # Skip "other" inbox messages (less important)
        classification = message.get("inferenceClassification", "").lower()
        if classification == "other":
            skipped += 1
            continue

        filtered.append(message)

    return filtered, skipped


async def _backfill_bodies(
    client: httpx.AsyncClient,
    messages: list[dict[str, Any]],
) -> None:
    """Fetch body content for messages that don't have it.

    Delta query excludes body to ensure Microsoft Graph returns @odata.deltaLink.
    This function backfills body content via $batch requests.
    Deleted messages (@removed) are skipped.
    """
    ids_to_fetch = [
        m["id"] for m in messages if "body" not in m and "@removed" not in m and m.get("id")
    ]

    if not ids_to_fetch:
        return

    logger.info("Backfilling body for %d messages", len(ids_to_fetch))

    msg_by_id: dict[str, dict[str, Any]] = {m["id"]: m for m in messages if m.get("id")}

    for i in range(0, len(ids_to_fetch), _BODY_BATCH_SIZE):
        batch_ids = ids_to_fetch[i : i + _BODY_BATCH_SIZE]

        batch_requests = [
            {
                "id": str(idx),
                "method": "GET",
                "url": f"/me/messages/{msg_id}?$select=id,body",
            }
            for idx, msg_id in enumerate(batch_ids)
        ]

        try:
            batch_resp = await client.post(
                "https://graph.microsoft.com/v1.0/$batch",
                json={"requests": batch_requests},
            )
            batch_resp.raise_for_status()
            batch_data = batch_resp.json()

            for response in batch_data.get("responses", []):
                if response.get("status") == 200:
                    body_data = response.get("body", {})
                    msg_id = body_data.get("id")
                    if msg_id and msg_id in msg_by_id:
                        msg_by_id[msg_id]["body"] = body_data.get("body")
        except Exception:
            logger.warning(
                "Body backfill batch failed (batch %d-%d), continuing without body",
                i,
                i + len(batch_ids),
                exc_info=True,
            )

    fetched = sum(1 for mid in ids_to_fetch if "body" in msg_by_id.get(mid, {}))
    logger.info("Body backfill complete: %d/%d messages", fetched, len(ids_to_fetch))


async def fetch_folders(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Outlook email folders via direct Microsoft Graph API calls.

    Uses Microsoft Graph API: GET /v1.0/me/mailFolders

    Returns:
        List of folder dictionaries with structure:
        {
            "id": "AAMkAGI...",
            "displayName": "Inbox",
            "parentFolderId": "...",
            "childFolderCount": 0,
            "unreadItemCount": 5,
            "totalItemCount": 150,
            "isHidden": false
        }
    """
    _ = data_type  # intentionally unused
    _ = is_initial  # folders are fetched in full each time
    _ = last_synced_at  # not used for folder sync

    provider = provider_config_key or to_nango_connect_integration_key(
        IntegrationType.OUTLOOK_EMAIL
    )

    # Microsoft Graph endpoint for mail folders
    # Using $top to get more folders if needed
    endpoint = "/v1.0/me/mailFolders"
    params = {
        "$top": "250",  # Get up to 250 folders
        "$select": "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount,isHidden",
    }

    token = await get_access_token(nango, connection_id, provider)

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=30.0
    ) as client:
        resp = await graph_api_request(client, endpoint, params=params)

    items: list[dict[str, Any]] = []
    if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
        items = resp["value"]

    return items


async def fetch_messages(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    delta_link: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
    folder_id: str | None = None,
) -> FetchMessagesResult:
    """
    Fetch Outlook email messages via direct Microsoft Graph API using Delta Query.

    Uses Microsoft Graph Delta API for efficient incremental sync:
    - Initial sync: /messages/delta with receivedDateTime filter (last 90 days)
    - Incremental sync: Use stored deltaLink (returns ONLY changes)

    Args:
        delta_link: Optional stored deltaLink from previous sync. If provided,
                    only messages changed since that deltaLink are returned.
        folder_id: Folder ID to fetch messages from (required for delta query)

    Returns:
        FetchMessagesResult with messages and new deltaLink for next sync.

        Message structure:
        {
            "id": "AAMkAGI...",
            "conversationId": "AAQkAGI...",
            "subject": "Meeting notes",
            "bodyPreview": "Here are the notes...",
            "from": {"emailAddress": {"name": "John Doe", "address": "john@example.com"}},
            "toRecipients": [...],
            "sentDateTime": "2025-11-12T10:30:00Z",
            "receivedDateTime": "2025-11-12T10:30:05Z",
            "hasAttachments": true,
            "importance": "normal",
            "isRead": false,
            ...
        }

        Deleted messages appear with @removed annotation:
        {
            "id": "AAMkAGI...",
            "@removed": {"reason": "deleted"}
        }
    """
    _ = data_type  # intentionally unused

    provider = provider_config_key or to_nango_connect_integration_key(
        IntegrationType.OUTLOOK_EMAIL
    )

    if not folder_id:
        raise ValueError("folder_id is required for delta query")

    now = datetime.now(UTC)
    using_stored_delta_link = False

    # stored_delta_url: when resuming incremental sync, use the opaque URL as-is
    stored_delta_url: str | None = None

    if delta_link:
        # True incremental sync: use stored deltaLink URL directly
        stored_delta_url = delta_link
        using_stored_delta_link = True
        logger.info(
            "Outlook Email: using stored deltaLink for folder (true incremental)",
        )

    # Fresh delta query params (used when no delta_link and no next_link)
    time_min = now - timedelta(days=365)
    time_min_str = time_min.strftime("%Y-%m-%dT%H:%M:%SZ")
    initial_endpoint = f"/v1.0/me/mailFolders/{folder_id}/messages/delta"
    initial_params: dict[str, Any] = {
        "$filter": f"receivedDateTime ge {time_min_str}",
        # body excluded — including it prevents Microsoft Graph from returning
        # @odata.deltaLink. Body is backfilled separately after the delta query.
        "$select": (
            "id,conversationId,subject,bodyPreview,from,toRecipients,"
            "ccRecipients,bccRecipients,replyTo,sentDateTime,receivedDateTime,"
            "hasAttachments,importance,isRead,isDraft,internetMessageId,"
            "parentFolderId,inferenceClassification"
        ),
    }
    if not delta_link:
        logger.info(
            "Outlook Email: fresh delta query for folder=%s (since=%s)",
            folder_id,
            time_min_str,
        )

    all_items: list[dict[str, Any]] = []
    next_link: str | None = None
    new_delta_link: str | None = None
    max_pages = 500  # Safety limit (500 pages * 50 items = 25,000 messages max)

    token = await get_access_token(nango, connection_id, provider)

    async with httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token}",
            "Prefer": "odata.maxpagesize=50",
        },
        timeout=30.0,
    ) as client:
        for page_num in range(max_pages):
            try:
                if next_link:
                    resp = await graph_api_request_url(client, next_link)
                elif stored_delta_url:
                    resp = await graph_api_request_url(client, stored_delta_url)
                    stored_delta_url = None  # only used for the first request
                else:
                    resp = await graph_api_request(client, initial_endpoint, params=initial_params)
            except httpx.HTTPStatusError as exc:
                # Handle expired deltaLink (Microsoft returns 410 Gone)
                if using_stored_delta_link and page_num == 0 and exc.response.status_code == 410:
                    logger.warning(
                        "Outlook Email: stored deltaLink expired (HTTP 410), "
                        "retrying with fresh delta query"
                    )
                    return await fetch_messages(
                        nango=nango,
                        connection_id=connection_id,
                        is_initial=True,
                        last_synced_at=last_synced_at,
                        delta_link=None,  # Force fresh delta query
                        data_type=data_type,
                        provider_config_key=provider_config_key,
                        folder_id=folder_id,
                    )
                raise
            except Exception as e:
                # Handle expired deltaLink via string-based detection
                if using_stored_delta_link and page_num == 0:
                    error_msg = str(e).lower()
                    if "410" in error_msg or "gone" in error_msg or "sync state" in error_msg:
                        logger.warning(
                            "Outlook Email: stored deltaLink expired, "
                            "retrying with fresh delta query"
                        )
                        return await fetch_messages(
                            nango=nango,
                            connection_id=connection_id,
                            is_initial=True,
                            last_synced_at=last_synced_at,
                            delta_link=None,  # Force fresh delta query
                            data_type=data_type,
                            provider_config_key=provider_config_key,
                            folder_id=folder_id,
                        )
                raise

            if isinstance(resp, dict):
                # Check for error response (deltaLink might be expired)
                if "error" in resp and using_stored_delta_link and page_num == 0:
                    error_code = resp.get("error", {}).get("code", "").lower()
                    if error_code in (
                        "syncstatenotfound",
                        "resyncresquired",
                        "invalidrequest",
                    ):
                        logger.warning(
                            "Outlook Email: deltaLink invalid (error=%s), retrying fresh",
                            error_code,
                        )
                        return await fetch_messages(
                            nango=nango,
                            connection_id=connection_id,
                            is_initial=True,
                            last_synced_at=last_synced_at,
                            delta_link=None,
                            data_type=data_type,
                            provider_config_key=provider_config_key,
                            folder_id=folder_id,
                        )

                # Extract messages from response
                if "value" in resp and isinstance(resp["value"], list):
                    for item in resp["value"]:
                        # Check for deleted items (delta sync returns @removed annotation)
                        if "@removed" in item:
                            logger.debug(
                                "Delta sync: detected deleted message id=%s reason=%s",
                                item.get("id"),
                                item.get("@removed", {}).get("reason"),
                            )
                            # Mark for downstream handling with proper sync metadata
                            _mark_as_deleted(item)
                        all_items.append(item)

                # Check for next page or deltaLink
                next_link = resp.get("@odata.nextLink")
                if not next_link:
                    # Capture deltaLink for next sync (only on last page)
                    new_delta_link = resp.get("@odata.deltaLink")
                    if new_delta_link:
                        logger.info(
                            "Delta sync complete after %d pages, new deltaLink received"
                            " (length=%d, folder=%s)",
                            page_num + 1,
                            len(new_delta_link),
                            folder_id,
                        )
                    else:
                        # Log response keys to diagnose missing deltaLink
                        resp_keys = [k for k in resp if k != "value"]
                        logger.warning(
                            "Microsoft Graph did NOT return @odata.deltaLink on last"
                            " page (folder=%s, pages=%d, items=%d, resp_keys=%s)",
                            folder_id,
                            page_num + 1,
                            len(all_items),
                            resp_keys,
                        )
                    break
            else:
                break

        # Warn if we hit the safety limit — pagination was truncated
        if not new_delta_link and next_link:
            logger.warning(
                "Outlook Email: hit max_pages=%d safety limit with nextLink still"
                " present — deltaLink will be missing (folder=%s, items=%d)",
                max_pages,
                folder_id,
                len(all_items),
            )

        # Backfill body content (excluded from delta $select to get deltaLink)
        if all_items:
            await _backfill_bodies(client, all_items)

    # Filter out "Other" inbox messages (less important emails like newsletters)
    filtered_messages, skipped_count = _filter_other_inbox_messages(all_items)

    if skipped_count > 0:
        logger.info(
            "Outlook Email: skipped %d 'Other' inbox messages (less important)",
            skipped_count,
        )

    logger.info(
        "Outlook Email fetch complete: %d messages (%d after filtering, initial=%s, used_delta_link=%s, folder=%s)",
        len(all_items),
        len(filtered_messages),
        is_initial,
        using_stored_delta_link,
        folder_id,
    )

    return FetchMessagesResult(messages=filtered_messages, delta_link=new_delta_link)
