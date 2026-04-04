"""Microsoft Teams fetcher via Microsoft Graph API (direct calls).

Fetches Teams channels and messages using direct Microsoft Graph API calls.
OAuth tokens are managed by custom OAuth via token_resolution module.
API calls go directly to Microsoft Graph.

Uses Microsoft Graph Delta Query for efficient incremental sync of messages:
- Initial sync: /messages/delta with createdDateTime filter (last 90 days)
- Incremental sync: Use stored deltaLink (returns ONLY new/changed/deleted messages)

Microsoft Graph API documentation:
- List channels: https://learn.microsoft.com/en-us/graph/api/channel-list
- List messages: https://learn.microsoft.com/en-us/graph/api/channel-list-messages
- Delta messages: https://learn.microsoft.com/en-us/graph/api/chatmessage-delta
"""

from __future__ import annotations

import os
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
    extract_collection_page,
    paginate_delta,
)
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_request,
    graph_api_request_url,
)

logger = get_logger(__name__)


def _get_positive_int_env(var_name: str, default: int) -> int:
    """Read positive integer env var with safe fallback."""
    raw = os.getenv(var_name, str(default))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; falling back to default=%d",
            var_name,
            raw,
            default,
        )
        return default

    if value < 1:
        logger.warning(
            "Invalid %s=%r; value must be >= 1. Falling back to default=%d",
            var_name,
            raw,
            default,
        )
        return default

    return value


TEAMS_CHATS_INITIAL_LOOKBACK_DAYS = _get_positive_int_env(
    "TEAMS_CHATS_INITIAL_LOOKBACK_DAYS",
    540,
)
TEAMS_CHATS_RECURRING_LOOKBACK_DAYS = _get_positive_int_env(
    "TEAMS_CHATS_RECURRING_LOOKBACK_DAYS",
    30,
)


@dataclass
class FetchMessagesResult:
    """Result from fetch_messages containing messages and optional delta token."""

    messages: list[dict[str, Any]]
    delta_link: str | None = None


@dataclass
class FetchChatsResult:
    """Result from fetch_chats containing chats and optional delta token."""

    chats: list[dict[str, Any]]
    delta_link: str | None = None


async def fetch_channels(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch all Teams channels (across all teams user has access to).

    Uses Microsoft Graph API: GET /v1.0/me/joinedTeams and GET /v1.0/teams/{teamId}/channels
    """
    _ = data_type
    _ = connection_id  # kept for registry interface compatibility
    _ = is_initial
    _ = last_synced_at
    _ = provider_config_key

    async def _do_fetch(token: str) -> list[dict[str, Any]]:
        teams: list[dict[str, Any]] = []
        teams_endpoint = "/v1.0/me/joinedTeams"
        teams_params: dict[str, Any] = {"$select": "id,displayName"}
        next_link: str | None = None
        max_pages = 500

        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "odata.maxpagesize=50",
            },
            timeout=30.0,
        ) as client:
            for _ in range(max_pages):
                if next_link:
                    teams_resp = await graph_api_request_url(client, next_link)
                else:
                    teams_resp = await graph_api_request(
                        client, teams_endpoint, params=teams_params
                    )
                page_teams, next_link = extract_collection_page(teams_resp)
                teams.extend(page_teams)
                if not next_link:
                    break

            all_channels: list[dict[str, Any]] = []
            for team in teams:
                team_id = team.get("id")
                team_name = team.get("displayName", "Unknown Team")

                if not team_id:
                    continue

                channels_endpoint = f"/v1.0/teams/{team_id}/channels"
                channels_params: dict[str, Any] = {
                    "$select": (
                        "id,displayName,description,email,webUrl,membershipType,createdDateTime"
                    ),
                }

                next_link = None
                for _ in range(max_pages):
                    if next_link:
                        channels_resp = await graph_api_request_url(client, next_link)
                    else:
                        channels_resp = await graph_api_request(
                            client, channels_endpoint, params=channels_params
                        )
                    channels, next_link = extract_collection_page(channels_resp)

                    for channel in channels:
                        channel["teamId"] = team_id
                        channel["teamName"] = team_name
                        all_channels.append(channel)

                    if not next_link:
                        break

        return all_channels

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
    team_id: str | None = None,
    channel_id: str | None = None,
) -> FetchMessagesResult:
    """
    Fetch Teams messages from a specific channel using Delta Query.

    Uses Microsoft Graph Delta API for efficient incremental sync:
    - Initial sync: /messages/delta (fetches all messages, returns deltaLink)
    - Incremental sync: Use stored deltaLink (returns ONLY changes)

    Args:
        delta_link: Optional stored deltaLink from previous sync. If provided,
                    only messages changed since that deltaLink are returned.
        team_id: Required - Team ID containing the channel
        channel_id: Required - Channel ID to fetch messages from
        is_initial: Whether this is the first sync for this channel
        last_synced_at: Timestamp of last successful sync

    Returns:
        FetchMessagesResult with messages and new deltaLink for next sync.

        Message structure:
        {
            "id": "1234567890",
            "createdDateTime": "2025-11-12T10:30:00Z",
            "lastModifiedDateTime": "2025-11-12T10:30:00Z",
            "messageType": "message",
            "importance": "normal",
            "body": {
                "contentType": "html",
                "content": "<p>Message content</p>"
            },
            "from": {
                "user": {
                    "id": "user-uuid",
                    "displayName": "John Doe",
                    "userIdentityType": "aadUser"
                }
            },
            "attachments": [...],
            "mentions": [...],
            "reactions": [...]
        }

        Deleted messages appear with @removed annotation:
        {
            "id": "1234567890",
            "@removed": {"reason": "deleted"}
        }
    """
    _ = data_type
    _ = provider_config_key

    if not team_id or not channel_id:
        raise ValueError("Both team_id and channel_id are required for fetching Teams messages")

    stored_delta_url: str | None = None
    if delta_link:
        stored_delta_url = delta_link
        logger.info(
            "Teams Messages: using stored deltaLink for channel (true incremental)",
        )

    initial_endpoint = f"/v1.0/teams/{team_id}/channels/{channel_id}/messages/delta"
    initial_params: dict[str, Any] = {
        "$top": "50",
    }
    if not delta_link:
        logger.info(
            "Teams Messages: fresh delta query for team=%s channel=%s",
            team_id,
            channel_id,
        )

    def _enrich(item: dict[str, Any]) -> None:
        item["teamId"] = team_id
        item["channelId"] = channel_id

    async def _do_fetch(token: str) -> FetchMessagesResult:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "odata.maxpagesize=50",
            },
            timeout=30.0,
        ) as client:
            delta_result = await paginate_delta(
                client,
                initial_endpoint=initial_endpoint,
                initial_params=initial_params,
                stored_delta_url=stored_delta_url,
                enrich_item=_enrich,
                label="Teams Messages",
            )

        logger.info(
            "Teams Messages fetch complete: %d messages"
            " (initial=%s, used_delta_link=%s, team=%s, channel=%s)",
            len(delta_result.items),
            is_initial,
            bool(delta_link),
            team_id,
            channel_id,
        )

        return FetchMessagesResult(messages=delta_result.items, delta_link=delta_result.delta_link)

    try:
        return await call_with_token_retry(token_info, _do_fetch)
    except DeltaExpiredError:
        logger.warning("Teams Messages: stored deltaLink expired, retrying with fresh delta query")
        return await fetch_messages(
            token_info=token_info,
            connection_id=connection_id,
            is_initial=True,
            last_synced_at=last_synced_at,
            delta_link=None,
            team_id=team_id,
            channel_id=channel_id,
        )


def _resolve_chats_lookback_days(
    *,
    is_initial: bool,
    last_synced_at: datetime | None,
    max_age_days: int | None,
) -> tuple[int, str]:
    """Resolve Teams chats lookback window for initial/recurring/custom syncs."""
    if max_age_days is not None:
        return max_age_days, "custom"

    if is_initial or last_synced_at is None:
        return TEAMS_CHATS_INITIAL_LOOKBACK_DAYS, "initial"

    return TEAMS_CHATS_RECURRING_LOOKBACK_DAYS, "recurring"


def _enrich_chat_display_name(chat: dict[str, Any]) -> None:
    """Set a human-readable displayName on a chat dict if topic is missing."""
    chat_type = chat.get("chatType")
    topic = chat.get("topic")
    members = chat.get("members", [])

    if topic:
        chat["displayName"] = topic
        return

    if chat_type == "group":
        member_names = [
            (m.get("displayName") or m.get("email") or "Unknown")
            for m in members[:3]
            if isinstance(m, dict)
        ]
        chat["displayName"] = ", ".join(member_names) if member_names else "Group Chat"
        if len(members) > 3:
            chat["displayName"] += f" +{len(members) - 3} more"
    elif chat_type == "meeting":
        chat["displayName"] = "Meeting Chat"
    else:
        chat["displayName"] = "Group Chat"


def _normalize_chat_members(chat: dict[str, Any]) -> None:
    """Normalize member dicts to a consistent schema for storage."""
    members = chat.get("members", [])
    if not members:
        return
    chat["members"] = [
        {
            "id": m.get("id"),
            "userId": m.get("userId"),
            "displayName": m.get("displayName"),
            "email": m.get("email"),
            "roles": m.get("roles", []),
            "tenantId": m.get("tenantId"),
            "visibleHistoryStartDateTime": m.get("visibleHistoryStartDateTime"),
        }
        for m in members
    ]


async def fetch_chats(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    delta_link: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
    max_age_days: int | None = None,
) -> FetchChatsResult:
    """
    Fetch Teams chats (group and meeting chats only, excludes 1:1).

    Uses Microsoft Graph /me/chats endpoint to list chats, ordered by last activity.
    Only fetches chats with activity in a resolved lookback window.

    Note: Delta Query is NOT supported for /me/chats in the v1.0 API (only in beta).
    Delta Query IS supported for messages within a chat (/chats/{id}/messages/delta).

    Note: Excludes oneOnOne chats (1:1 direct messages) - only syncs group and meeting chats.

    Args:
        is_initial: Whether this is the first sync
        last_synced_at: Timestamp of last successful sync
        delta_link: Not used for chats listing (delta not supported in v1.0)
        max_age_days: Optional override for chat lookback window (days)

    Returns:
        FetchChatsResult with chats list. delta_link will always be None since
        delta queries are not supported for listing chats.

        Chat structure:
        {
            "id": "19:abc123...",
            "topic": "Chat with John Doe",
            "chatType": "group" | "meeting",  # oneOnOne excluded
            "createdDateTime": "2025-11-12T10:30:00Z",
            "lastUpdatedDateTime": "2025-11-12T11:45:00Z",
            "webUrl": "https://teams.microsoft.com/...",
            "tenantId": "tenant-uuid",
            "onlineMeetingInfo": {...}  # Present for meeting chats
        }
    """
    _ = data_type
    _ = connection_id  # kept for registry interface compatibility
    _ = delta_link  # Delta not supported for /me/chats in v1.0 API
    _ = provider_config_key

    resolved_max_age_days, sync_phase = _resolve_chats_lookback_days(
        is_initial=is_initial,
        last_synced_at=last_synced_at,
        max_age_days=max_age_days,
    )

    # Calculate cutoff date for resolved lookback window
    cutoff_date = datetime.now(UTC) - timedelta(days=resolved_max_age_days)

    # Use standard chats endpoint with ordering by last message (most recent first)
    # This allows us to stop early when we hit chats older than cutoff
    # Expand members to get chat participants, and lastMessagePreview for date filtering
    initial_endpoint = "/v1.0/me/chats"
    initial_params: dict[str, Any] = {
        "$select": "id,topic,chatType,createdDateTime,lastUpdatedDateTime,webUrl,tenantId,onlineMeetingInfo",
        "$expand": "lastMessagePreview,members",
        "$orderby": "lastMessagePreview/createdDateTime desc",
        "$top": "50",
    }
    logger.info(
        "Teams Chats: fetching chats (phase=%s) with activity in last %d days (cutoff: %s)",
        sync_phase,
        resolved_max_age_days,
        cutoff_date.isoformat(),
    )

    async def _do_fetch(token: str) -> FetchChatsResult:
        all_chats: list[dict[str, Any]] = []
        next_link: str | None = None
        max_pages = 500
        reached_cutoff = False

        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "odata.maxpagesize=50",
            },
            timeout=30.0,
        ) as client:
            for page_num in range(max_pages):  # noqa: B007
                if next_link:
                    resp = await graph_api_request_url(client, next_link)
                else:
                    resp = await graph_api_request(client, initial_endpoint, params=initial_params)

                if not isinstance(resp, dict):
                    break

                if "value" in resp and isinstance(resp["value"], list):
                    for chat in resp["value"]:
                        last_message = chat.get("lastMessagePreview", {})
                        last_message_time_str = (
                            last_message.get("createdDateTime") if last_message else None
                        )
                        if not last_message_time_str:
                            last_message_time_str = chat.get("lastUpdatedDateTime")

                        if last_message_time_str:
                            try:
                                last_message_time = datetime.fromisoformat(
                                    last_message_time_str.replace("Z", "+00:00")
                                )
                                if last_message_time < cutoff_date:
                                    logger.info(
                                        "Teams Chats: reached cutoff date after %d chats, stopping",
                                        len(all_chats),
                                    )
                                    reached_cutoff = True
                                    break
                            except (ValueError, TypeError):
                                pass

                        if chat.get("chatType") == "oneOnOne":
                            continue

                        _enrich_chat_display_name(chat)
                        _normalize_chat_members(chat)
                        chat.pop("lastMessagePreview", None)
                        all_chats.append(chat)

                if reached_cutoff:
                    break

                next_link = resp.get("@odata.nextLink")
                if not next_link:
                    break

        logger.info(
            "Teams Chats fetch complete: %d chats after %d pages"
            " (phase=%s, reached_cutoff=%s, max_age_days=%d)",
            len(all_chats),
            page_num + 1,
            sync_phase,
            reached_cutoff,
            resolved_max_age_days,
        )

        return FetchChatsResult(chats=all_chats, delta_link=None)

    return await call_with_token_retry(token_info, _do_fetch)


async def fetch_chat_messages(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    delta_link: str | None = None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
    chat_id: str | None = None,
) -> FetchMessagesResult:
    """
    Fetch Teams chat messages from a specific chat.

    NOTE: Microsoft Graph does NOT support delta queries for chat messages.
    Delta is only supported for Team channel messages (/teams/{id}/channels/{id}/messages/delta).
    For chat messages, we use standard pagination with date filtering.

    Args:
        chat_id: Required - Chat ID to fetch messages from
        delta_link: Ignored (delta not supported for chat messages)
        is_initial: Whether this is the first sync for this chat
        last_synced_at: Timestamp of last successful sync (used for incremental filtering)

    Returns:
        FetchMessagesResult with messages (delta_link is always None for chats).
    """
    _ = data_type
    _ = connection_id  # kept for registry interface compatibility
    _ = delta_link  # Ignored - delta not supported for chat messages
    _ = provider_config_key

    if not chat_id:
        raise ValueError("chat_id is required for fetching Teams chat messages")

    # Use standard messages endpoint (NOT delta - delta is not supported for chat messages)
    # NOTE: $filter on createdDateTime is also NOT supported for chat messages.
    # We use client-side filtering with early termination instead.
    initial_endpoint = f"/v1.0/chats/{chat_id}/messages"
    initial_params: dict[str, Any] = {
        "$top": "50",
        "$orderby": "createdDateTime desc",  # Newest first - enables early termination
    }

    # For incremental sync, we do client-side filtering (API doesn't support $filter)
    cutoff_date: datetime | None = None
    if not is_initial and last_synced_at:
        cutoff_date = last_synced_at
        logger.info(
            "Teams Chat Messages: incremental sync for chat=%s (client-side filter since %s)",
            chat_id,
            last_synced_at.isoformat(),
        )
    else:
        logger.info(
            "Teams Chat Messages: initial sync for chat=%s",
            chat_id,
        )

    async def _do_fetch(token: str) -> FetchMessagesResult:
        all_messages: list[dict[str, Any]] = []
        next_link: str | None = None
        max_pages = 500
        reached_cutoff = False

        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "odata.maxpagesize=50",
            },
            timeout=30.0,
        ) as client:
            for _page in range(max_pages):
                if next_link:
                    resp = await graph_api_request_url(client, next_link)
                else:
                    resp = await graph_api_request(client, initial_endpoint, params=initial_params)

                if not isinstance(resp, dict):
                    break

                if "value" in resp and isinstance(resp["value"], list):
                    for item in resp["value"]:
                        if cutoff_date:
                            created_str = item.get("createdDateTime")
                            if created_str:
                                try:
                                    created_at = datetime.fromisoformat(
                                        created_str.replace("Z", "+00:00")
                                    )
                                    if created_at <= cutoff_date:
                                        reached_cutoff = True
                                        break
                                except (ValueError, TypeError):
                                    pass

                        item["chatId"] = chat_id
                        all_messages.append(item)

                if reached_cutoff:
                    break

                next_link = resp.get("@odata.nextLink")
                if not next_link:
                    break

        logger.info(
            "Teams Chat Messages fetch complete: %d messages"
            " (initial=%s, chat=%s, reached_cutoff=%s)",
            len(all_messages),
            is_initial,
            chat_id,
            reached_cutoff,
        )

        return FetchMessagesResult(messages=all_messages, delta_link=None)

    return await call_with_token_retry(token_info, _do_fetch)
