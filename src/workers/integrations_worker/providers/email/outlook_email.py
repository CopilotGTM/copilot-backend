"""Outlook Email fetcher via Microsoft Graph API (direct calls).

Fetches email folders and messages from Outlook/Office 365 mailboxes.
OAuth tokens are managed by custom OAuth via token_resolution module.
API calls go directly to Microsoft Graph.

Uses Microsoft Graph Delta Query for efficient incremental sync:
- Initial sync: /messages/delta with receivedDateTime filter (last 365 days)
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

from src.infra.utils.logger import get_logger
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers._delta_sync import (
    DeltaExpiredError,
    paginate_delta,
)
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_request,
)

logger = get_logger(__name__)


# Batch size for body backfill requests (Microsoft Graph $batch supports up to 20)
_BODY_BATCH_SIZE = 20
_DELTA_PAGE_SIZE = 500
_FULL_MESSAGE_SELECT = (
    "id,conversationId,subject,bodyPreview,from,toRecipients,"
    "ccRecipients,bccRecipients,replyTo,sentDateTime,receivedDateTime,"
    "hasAttachments,importance,isRead,isDraft,internetMessageId,"
    "parentFolderId,inferenceClassification,categories,body"
)


@dataclass
class FetchMessagesResult:
    """Result from fetch_messages containing messages and optional delta token."""

    messages: list[dict[str, Any]]
    delta_link: str | None = None


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


def _is_deleted_message(message: dict[str, Any]) -> bool:
    """Return true for Graph delta tombstones that should bypass hydration."""
    return (
        "@removed" in message
        or message.get("_sync_metadata", {}).get("sync_action") == "DELETED"
    )


def _missing_required_message_fields(message: dict[str, Any]) -> list[str]:
    """List stable Outlook fields required before a message can be persisted."""
    missing: list[str] = []

    if not message.get("id"):
        missing.append("id")
    if not message.get("internetMessageId"):
        missing.append("internetMessageId")
    if "subject" not in message or message.get("subject") is None:
        missing.append("subject")
    from_address = message.get("from", {}).get("emailAddress", {})
    if not from_address.get("address"):
        missing.append("from")
    if not message.get("parentFolderId"):
        missing.append("parentFolderId")
    if not (message.get("receivedDateTime") or message.get("sentDateTime")):
        missing.append("receivedDateTime|sentDateTime")

    return missing


def _is_complete_message(message: dict[str, Any]) -> bool:
    """Return true when a non-deleted Outlook message has stable identity fields."""
    return not _missing_required_message_fields(message)


def _merge_hydrated_message(
    base_message: dict[str, Any],
    hydrated_message: dict[str, Any],
) -> dict[str, Any]:
    """Overlay full Graph details while preserving delta-only sync metadata."""
    merged = dict(base_message)
    merged.update(hydrated_message)
    if "_sync_metadata" in base_message:
        merged["_sync_metadata"] = base_message["_sync_metadata"]
    return merged


async def _hydrate_incomplete_messages(
    client: httpx.AsyncClient,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fetch full details for sparse delta items before they reach persistence."""
    messages_with_identity: list[dict[str, Any]] = []
    for message in messages:
        if not _is_deleted_message(message) and not message.get("id"):
            raise RuntimeError(
                "Outlook Email partial hydration failed: "
                "non-deleted message missing id"
            )
        messages_with_identity.append(message)

    messages = messages_with_identity
    candidates = [
        message
        for message in messages
        if not _is_deleted_message(message)
        and message.get("id")
        and not _is_complete_message(message)
    ]
    if not candidates:
        return messages

    logger.warning(
        "Outlook Email: hydrating %d partial delta messages before persistence",
        len(candidates),
    )

    candidate_by_id: dict[str, dict[str, Any]] = {
        str(message["id"]): message for message in candidates
    }
    hydrated_by_id: dict[str, dict[str, Any]] = {}
    dropped_ids: set[str] = set()

    for i in range(0, len(candidates), _BODY_BATCH_SIZE):
        batch_messages = candidates[i : i + _BODY_BATCH_SIZE]
        request_id_to_message_id: dict[str, str] = {}
        batch_requests: list[dict[str, str]] = []

        for idx, message in enumerate(batch_messages):
            request_id = str(idx)
            message_id = str(message["id"])
            request_id_to_message_id[request_id] = message_id
            batch_requests.append(
                {
                    "id": request_id,
                    "method": "GET",
                    "url": f"/me/messages/{message_id}?$select={_FULL_MESSAGE_SELECT}",
                }
            )

        batch_resp = await client.post(
            "https://graph.microsoft.com/v1.0/$batch",
            json={"requests": batch_requests},
        )
        batch_resp.raise_for_status()
        batch_data = batch_resp.json()

        seen_request_ids: set[str] = set()
        for response in batch_data.get("responses", []):
            request_id = str(response.get("id"))
            seen_request_ids.add(request_id)
            message_id = request_id_to_message_id.get(request_id)
            if not message_id:
                continue

            status = int(response.get("status") or 0)
            if status == 200:
                body_data = response.get("body", {})
                merged = _merge_hydrated_message(candidate_by_id[message_id], body_data)
                missing = _missing_required_message_fields(merged)
                if missing:
                    raise RuntimeError(
                        "Outlook Email partial hydration returned incomplete message "
                        f"id={message_id} missing={missing}"
                    )
                hydrated_by_id[message_id] = merged
                continue

            if status == 404:
                logger.info(
                    "Outlook Email: dropping partial message because hydration returned 404 id=%s",
                    message_id,
                )
                dropped_ids.add(message_id)
                continue

            raise RuntimeError(
                "Outlook Email partial hydration failed "
                f"id={message_id} status={status}"
            )

        missing_responses = set(request_id_to_message_id) - seen_request_ids
        if missing_responses:
            raise RuntimeError(
                "Outlook Email partial hydration batch missing responses "
                f"request_ids={sorted(missing_responses)}"
            )

    hydrated_messages: list[dict[str, Any]] = []
    partial_detected = len(candidates)
    partial_hydrated = 0
    partial_dropped = 0

    for message in messages:
        if _is_deleted_message(message):
            hydrated_messages.append(message)
            continue

        message_id = message.get("id")
        if _is_complete_message(message):
            hydrated_messages.append(message)
            continue

        replacement = hydrated_by_id.get(str(message_id))
        if replacement:
            hydrated_messages.append(replacement)
            partial_hydrated += 1
        elif str(message_id) in dropped_ids:
            partial_dropped += 1
        else:
            missing = _missing_required_message_fields(message)
            logger.warning(
                "Outlook Email: dropping unhydrated partial message id=%s missing=%s",
                message_id,
                missing,
            )
            partial_dropped += 1

    logger.info(
        "Outlook Email partial hydration complete: detected=%d hydrated=%d dropped=%d",
        partial_detected,
        partial_hydrated,
        partial_dropped,
    )
    return hydrated_messages


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
        m["id"]
        for m in messages
        if "body" not in m and not _is_deleted_message(m) and m.get("id")
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
    token_info: IntegrationTokenInfo,
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
    _ = connection_id  # kept for registry interface compatibility
    _ = is_initial  # folders are fetched in full each time
    _ = last_synced_at  # not used for folder sync
    _ = provider_config_key  # no longer needed — tokens come from local credentials

    endpoint = "/v1.0/me/mailFolders"
    params = {
        "$top": "250",
        "$select": "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount,isHidden",
    }

    async def _do_fetch(token: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=30.0
        ) as client:
            resp = await graph_api_request(client, endpoint, params=params)

        items: list[dict[str, Any]] = []
        if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]
        return items

    return await call_with_token_retry(token_info, _do_fetch)


async def fetch_messages(
    *,
    token_info: IntegrationTokenInfo,
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
    - Initial sync: /messages/delta with receivedDateTime filter (last 365 days)
    - Incremental sync: Use stored deltaLink (returns ONLY changes)

    Args:
        token_info: OAuth token info for this integration.
        delta_link: Optional stored deltaLink from previous sync. If provided,
                    only messages changed since that deltaLink are returned.
        folder_id: Folder ID to fetch messages from (required for delta query)

    Returns:
        FetchMessagesResult with messages and new deltaLink for next sync.
    """
    _ = data_type  # intentionally unused
    _ = provider_config_key  # no longer needed — tokens come from local credentials

    if not folder_id:
        raise ValueError("folder_id is required for delta query")

    now = datetime.now(UTC)

    stored_delta_url: str | None = None
    if delta_link:
        stored_delta_url = delta_link
        logger.info(
            "Outlook Email: using stored deltaLink for folder (true incremental)",
        )

    # body excluded from $select — including it prevents Microsoft Graph from
    # returning @odata.deltaLink. Body is backfilled separately after pagination.
    time_min = now - timedelta(days=365)
    time_min_str = time_min.strftime("%Y-%m-%dT%H:%M:%SZ")
    initial_endpoint = f"/v1.0/me/mailFolders/{folder_id}/messages/delta"
    initial_params: dict[str, Any] = {
        "$filter": f"receivedDateTime ge {time_min_str}",
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

    async def _do_fetch(token: str) -> tuple[list[dict[str, Any]], str | None]:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": f"odata.maxpagesize={_DELTA_PAGE_SIZE}",
            },
            timeout=30.0,
        ) as client:
            delta_result = await paginate_delta(
                client,
                initial_endpoint=initial_endpoint,
                initial_params=initial_params,
                stored_delta_url=stored_delta_url,
                label="Outlook Email",
            )

            items = delta_result.items

            if not delta_result.delta_link:
                logger.warning(
                    "Microsoft Graph did NOT return @odata.deltaLink (folder=%s, items=%d)",
                    folder_id,
                    len(items),
                )

            if items:
                items = await _hydrate_incomplete_messages(client, items)
                await _backfill_bodies(client, items)

        return items, delta_result.delta_link

    try:
        all_items, new_delta_link = await call_with_token_retry(token_info, _do_fetch)
    except DeltaExpiredError:
        logger.warning("Outlook Email: stored deltaLink expired, retrying with fresh delta query")
        return await fetch_messages(
            token_info=token_info,
            connection_id=connection_id,
            is_initial=True,
            last_synced_at=last_synced_at,
            delta_link=None,
            folder_id=folder_id,
        )

    # Filter out "Other" inbox messages (less important emails like newsletters)
    filtered_messages, skipped_count = _filter_other_inbox_messages(all_items)

    if skipped_count > 0:
        logger.info(
            "Outlook Email: skipped %d 'Other' inbox messages (less important)",
            skipped_count,
        )

    logger.info(
        "Outlook Email fetch complete: %d messages (%d after filtering,"
        " initial=%s, used_delta_link=%s, folder=%s)",
        len(all_items),
        len(filtered_messages),
        is_initial,
        bool(delta_link),
        folder_id,
    )

    return FetchMessagesResult(messages=filtered_messages, delta_link=new_delta_link)
