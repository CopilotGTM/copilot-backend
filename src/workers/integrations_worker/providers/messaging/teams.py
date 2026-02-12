"""Microsoft Teams fetcher via Microsoft Graph API through Nango proxy.

Fetches Teams channels and messages.

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
from urllib.parse import parse_qs, urlparse

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

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


def _extract_pagination_params(next_link: str) -> tuple[str, dict[str, Any]]:
    """
    Extract endpoint path and query params from Microsoft Graph @odata.nextLink or @odata.deltaLink.

    Microsoft Graph returns full URLs like:
    https://graph.microsoft.com/v1.0/teams/{id}/channels/{id}/messages/delta?$skiptoken=xxx...

    We need to extract the path and query params for Nango's proxy_request.
    """
    parsed = urlparse(next_link)
    endpoint = parsed.path

    # Parse query string into dict (parse_qs returns lists, we want single values)
    query_dict = parse_qs(parsed.query)
    params: dict[str, Any] = {k: v[0] if len(v) == 1 else v for k, v in query_dict.items()}

    return endpoint, params


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


def _extract_collection_page(response: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Extract list payload and optional @odata.nextLink from Graph collection responses."""
    if not isinstance(response, dict):
        return [], None

    value = response.get("value")
    items = value if isinstance(value, list) else []

    next_link = response.get("@odata.nextLink")
    if not isinstance(next_link, str) or not next_link:
        next_link = None

    return items, next_link


async def fetch_channels(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch all Teams channels (across all teams user has access to).

    Uses Microsoft Graph API: GET /v1.0/me/joinedTeams and GET /v1.0/teams/{teamId}/channels

    Returns:
        List of channel dictionaries with structure:
        {
            "id": "19:abc123...",
            "displayName": "Sales - Acme Corp",
            "description": "Channel for Acme Corp deal",
            "email": "sales-acme@company.onmicrosoft.com",
            "webUrl": "https://teams.microsoft.com/...",
            "membershipType": "standard",
            "teamId": "team-uuid",
            "teamName": "Sales Team"
        }
    """
    _ = data_type
    _ = is_initial
    _ = last_synced_at

    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)

    # Step 1: Get all teams user is part of (with pagination)
    teams: list[dict[str, Any]] = []
    teams_endpoint = "/v1.0/me/joinedTeams"
    teams_params: dict[str, Any] = {"$select": "id,displayName"}
    next_link: str | None = None
    max_pages = 100

    for _ in range(max_pages):
        if next_link:
            endpoint, params = _extract_pagination_params(next_link)
        else:
            endpoint, params = teams_endpoint, teams_params

        teams_resp = await nango.proxy_request(
            provider_config_key=provider,
            connection_id=connection_id,
            endpoint=endpoint,
            params=params,
        )
        page_teams, next_link = _extract_collection_page(teams_resp)
        teams.extend(page_teams)
        if not next_link:
            break

    # Step 2: For each team, fetch all channels (with pagination)
    all_channels: list[dict[str, Any]] = []
    for team in teams:
        team_id = team.get("id")
        team_name = team.get("displayName", "Unknown Team")

        if not team_id:
            continue

        channels_endpoint = f"/v1.0/teams/{team_id}/channels"
        channels_params: dict[str, Any] = {
            "$select": "id,displayName,description,email,webUrl,membershipType,createdDateTime",
        }

        next_link = None
        for _ in range(max_pages):
            if next_link:
                endpoint, params = _extract_pagination_params(next_link)
            else:
                endpoint, params = channels_endpoint, channels_params

            channels_resp = await nango.proxy_request(
                provider_config_key=provider,
                connection_id=connection_id,
                endpoint=endpoint,
                params=params,
            )
            channels, next_link = _extract_collection_page(channels_resp)

            # Enrich each channel with team context
            for channel in channels:
                channel["teamId"] = team_id
                channel["teamName"] = team_name
                all_channels.append(channel)

            if not next_link:
                break

    return all_channels


async def fetch_messages(
    *,
    nango: NangoClient,
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

    if not team_id or not channel_id:
        raise ValueError("Both team_id and channel_id are required for fetching Teams messages")

    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)

    using_stored_delta_link = False

    if delta_link:
        # True incremental sync: use stored deltaLink
        # Returns ONLY messages that changed since the deltaLink was issued
        initial_endpoint, initial_params = _extract_pagination_params(delta_link)
        using_stored_delta_link = True
        logger.info(
            "Teams Messages: using stored deltaLink for channel (true incremental)",
        )
    else:
        # Fresh delta query (initial sync)
        # Use delta endpoint - fetches all messages, returns deltaLink for next sync
        initial_endpoint = f"/v1.0/teams/{team_id}/channels/{channel_id}/messages/delta"
        initial_params: dict[str, Any] = {
            "$top": "50",
        }
        logger.info(
            "Teams Messages: fresh delta query for team=%s channel=%s",
            team_id,
            channel_id,
        )

    all_messages: list[dict[str, Any]] = []
    next_link: str | None = None
    new_delta_link: str | None = None
    max_pages = 100  # Safety limit (100 pages * 50 items = 5,000 messages max)

    for page_num in range(max_pages):  # noqa: B007
        # Determine endpoint and params for this request
        if next_link:
            endpoint, params = _extract_pagination_params(next_link)
        else:
            endpoint, params = initial_endpoint, initial_params

        try:
            resp = await nango.proxy_request(
                provider_config_key=provider,
                connection_id=connection_id,
                endpoint=endpoint,
                params=params,
            )
        except Exception as e:
            # Handle expired deltaLink (Microsoft returns 410 Gone or similar)
            if using_stored_delta_link and page_num == 0:
                error_msg = str(e).lower()
                if "410" in error_msg or "gone" in error_msg or "sync state" in error_msg:
                    logger.warning(
                        "Teams Messages: stored deltaLink expired, retrying with fresh delta query"
                    )
                    # Retry without deltaLink
                    return await fetch_messages(
                        nango=nango,
                        connection_id=connection_id,
                        is_initial=True,
                        last_synced_at=last_synced_at,
                        delta_link=None,  # Force fresh delta query
                        data_type=data_type,
                        provider_config_key=provider_config_key,
                        team_id=team_id,
                        channel_id=channel_id,
                    )
            raise

        if isinstance(resp, dict):
            # Check for error response (deltaLink might be expired)
            if "error" in resp and using_stored_delta_link and page_num == 0:
                error_code = resp.get("error", {}).get("code", "").lower()
                if error_code in ("syncstatenotfound", "resyncresquired", "invalidrequest"):
                    logger.warning(
                        "Teams Messages: deltaLink invalid (error=%s), retrying fresh",
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
                        team_id=team_id,
                        channel_id=channel_id,
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

                    # Enrich with team/channel context
                    item["teamId"] = team_id
                    item["channelId"] = channel_id
                    all_messages.append(item)

            # Check for next page or deltaLink
            next_link = resp.get("@odata.nextLink")
            if not next_link:
                # Capture deltaLink for next sync (only on last page)
                new_delta_link = resp.get("@odata.deltaLink")
                if new_delta_link:
                    logger.info(
                        "Teams Messages delta sync complete after %d pages, new deltaLink received",
                        page_num + 1,
                    )
                break
        else:
            break

    logger.info(
        "Teams Messages fetch complete: %d messages (initial=%s, used_delta_link=%s, team=%s, channel=%s)",
        len(all_messages),
        is_initial,
        using_stored_delta_link,
        team_id,
        channel_id,
    )

    return FetchMessagesResult(messages=all_messages, delta_link=new_delta_link)


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


async def fetch_chats(
    *,
    nango: NangoClient,
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
    _ = delta_link  # Delta not supported for /me/chats in v1.0 API

    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)

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

    all_chats: list[dict[str, Any]] = []
    next_link: str | None = None
    max_pages = 100  # Safety limit
    reached_cutoff = False

    for page_num in range(max_pages):  # noqa: B007
        # Determine endpoint and params for this request
        if next_link:
            endpoint, params = _extract_pagination_params(next_link)
        else:
            endpoint, params = initial_endpoint, initial_params

        resp = await nango.proxy_request(
            provider_config_key=provider,
            connection_id=connection_id,
            endpoint=endpoint,
            params=params,
        )

        if isinstance(resp, dict):
            # Extract chats from response
            if "value" in resp and isinstance(resp["value"], list):
                for chat in resp["value"]:
                    # Check if chat is older than cutoff based on last message
                    last_message = chat.get("lastMessagePreview", {})
                    last_message_time_str = (
                        last_message.get("createdDateTime") if last_message else None
                    )

                    # Also check lastUpdatedDateTime as fallback
                    if not last_message_time_str:
                        last_message_time_str = chat.get("lastUpdatedDateTime")

                    if last_message_time_str:
                        try:
                            # Parse the datetime string
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
                            pass  # Continue if date parsing fails

                    # Filter out 1:1 chats (oneOnOne)
                    if chat.get("chatType") == "oneOnOne":
                        continue

                    # Enrich chat name for display
                    chat_type = chat.get("chatType")
                    topic = chat.get("topic")
                    members = chat.get("members", [])

                    if not topic:
                        if chat_type == "group":
                            member_names = [
                                (m.get("displayName") or m.get("email") or "Unknown")
                                for m in members[:3]
                                if isinstance(m, dict)
                            ]
                            chat["displayName"] = (
                                ", ".join(member_names) if member_names else "Group Chat"
                            )
                            if len(members) > 3:
                                chat["displayName"] += f" +{len(members) - 3} more"
                        elif chat_type == "meeting":
                            chat["displayName"] = "Meeting Chat"
                        else:
                            chat["displayName"] = "Group Chat"
                    else:
                        chat["displayName"] = topic

                    # Normalize members data for storage
                    # Each member has: id, displayName, email, roles, userId, tenantId, visibleHistoryStartDateTime
                    if members:
                        normalized_members = []
                        for member in members:
                            normalized_members.append(
                                {
                                    "id": member.get("id"),
                                    "userId": member.get("userId"),
                                    "displayName": member.get("displayName"),
                                    "email": member.get("email"),
                                    "roles": member.get("roles", []),
                                    "tenantId": member.get("tenantId"),
                                    "visibleHistoryStartDateTime": member.get(
                                        "visibleHistoryStartDateTime"
                                    ),
                                }
                            )
                        chat["members"] = normalized_members

                    # Remove lastMessagePreview from stored data (we only needed it for filtering)
                    chat.pop("lastMessagePreview", None)

                    all_chats.append(chat)

            # Stop if we've reached the cutoff date
            if reached_cutoff:
                break

            # Check for next page
            next_link = resp.get("@odata.nextLink")
            if not next_link:
                break
        else:
            break

    logger.info(
        "Teams Chats fetch complete: %d chats after %d pages (phase=%s, reached_cutoff=%s, max_age_days=%d)",
        len(all_chats),
        page_num + 1,
        sync_phase,
        reached_cutoff,
        resolved_max_age_days,
    )

    # Return without delta_link since delta is not supported for chats listing
    return FetchChatsResult(chats=all_chats, delta_link=None)


async def fetch_chat_messages(
    *,
    nango: NangoClient,
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
    _ = delta_link  # Ignored - delta not supported for chat messages

    if not chat_id:
        raise ValueError("chat_id is required for fetching Teams chat messages")

    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)

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

    all_messages: list[dict[str, Any]] = []
    next_link: str | None = None
    max_pages = 100  # Safety limit (100 pages * 50 items = 5,000 messages max)
    reached_cutoff = False

    for page_num in range(max_pages):  # noqa: B007
        # Determine endpoint and params for this request
        if next_link:
            endpoint, params = _extract_pagination_params(next_link)
        else:
            endpoint, params = initial_endpoint, initial_params

        try:
            resp = await nango.proxy_request(
                provider_config_key=provider,
                connection_id=connection_id,
                endpoint=endpoint,
                params=params,
            )
        except Exception:
            logger.exception(
                "Teams Chat Messages: API error for chat=%s page=%d",
                chat_id,
                page_num,
            )
            raise

        if isinstance(resp, dict):
            # Extract messages from response
            if "value" in resp and isinstance(resp["value"], list):
                for item in resp["value"]:
                    # For incremental sync, check if message is older than cutoff
                    if cutoff_date:
                        created_str = item.get("createdDateTime")
                        if created_str:
                            try:
                                created_at = datetime.fromisoformat(
                                    created_str.replace("Z", "+00:00")
                                )
                                if created_at <= cutoff_date:
                                    # Message is older than cutoff - stop fetching
                                    reached_cutoff = True
                                    logger.debug(
                                        "Teams Chat Messages: reached cutoff at message %s (created %s)",
                                        item.get("id"),
                                        created_str,
                                    )
                                    break
                            except (ValueError, TypeError):
                                pass  # If we can't parse date, include the message

                    # Enrich with chat context
                    item["chatId"] = chat_id
                    all_messages.append(item)

            # Stop if we reached the cutoff
            if reached_cutoff:
                break

            # Check for next page
            next_link = resp.get("@odata.nextLink")
            if not next_link:
                break
        else:
            break

    logger.info(
        "Teams Chat Messages fetch complete: %d messages (initial=%s, chat=%s, reached_cutoff=%s)",
        len(all_messages),
        is_initial,
        chat_id,
        reached_cutoff,
    )

    # Note: delta_link is always None for chat messages (delta not supported)
    return FetchMessagesResult(messages=all_messages, delta_link=None)
