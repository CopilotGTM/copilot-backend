"""Microsoft Teams fetcher via Microsoft Graph API through Nango proxy.

Fetches Teams channels and messages.

Microsoft Graph API documentation:
- List channels: https://learn.microsoft.com/en-us/graph/api/channel-list
- List messages: https://learn.microsoft.com/en-us/graph/api/channel-list-messages
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.service.integrations.nango_client import NangoClient


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

    # Step 1: Get all teams user is part of
    teams_endpoint = "/v1.0/me/joinedTeams"
    teams_resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=teams_endpoint,
    )

    teams = []
    if (
        isinstance(teams_resp, dict)
        and "value" in teams_resp
        and isinstance(teams_resp["value"], list)
    ):
        teams = teams_resp["value"]

    # Step 2: For each team, fetch all channels
    all_channels = []
    for team in teams:
        team_id = team.get("id")
        team_name = team.get("displayName", "Unknown Team")

        if not team_id:
            continue

        channels_endpoint = f"/v1.0/teams/{team_id}/channels"
        channels_params = {
            "$select": "id,displayName,description,email,webUrl,membershipType,createdDateTime",
        }

        channels_resp = await nango.proxy_request(
            provider_config_key=provider,
            connection_id=connection_id,
            endpoint=channels_endpoint,
            params=channels_params,
        )

        channels = []
        if (
            isinstance(channels_resp, dict)
            and "value" in channels_resp
            and isinstance(channels_resp["value"], list)
        ):
            channels = channels_resp["value"]

        # Enrich each channel with team context
        for channel in channels:
            channel["teamId"] = team_id
            channel["teamName"] = team_name
            all_channels.append(channel)

    return all_channels


async def fetch_messages(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
    team_id: str | None = None,
    channel_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Teams messages from a specific channel.

    Uses Microsoft Graph API: GET /v1.0/teams/{teamId}/channels/{channelId}/messages

    Args:
        team_id: Required - Team ID containing the channel
        channel_id: Required - Channel ID to fetch messages from

    Returns:
        List of message dictionaries with structure:
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
    """
    _ = data_type

    if not team_id or not channel_id:
        raise ValueError("Both team_id and channel_id are required for fetching Teams messages")

    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)

    now = datetime.now(UTC)

    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    # Determine time range for message sync
    if is_initial or not last_synced_at:
        # Initial sync: last 90 days
        time_min = now - timedelta(days=90)
    else:
        # Incremental: from last sync minus 1 day buffer
        time_min = last_synced_at - timedelta(days=1)

    time_min = _ensure_utc(time_min)

    # Microsoft Graph endpoint for channel messages
    endpoint = f"/v1.0/teams/{team_id}/channels/{channel_id}/messages"

    # Note: Teams API doesn't support direct time filtering like Outlook
    # We'll fetch recent messages and filter in the sync service
    params = {
        "$top": "50",  # Get last 50 messages per channel
        "$orderby": "createdDateTime desc",
        "$expand": "replies",  # Include threaded replies
    }

    resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
        params=params,
    )

    items = []
    if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
        items = resp["value"]

    # Filter messages by time_min
    filtered_items = []
    for item in items:
        created_str = item.get("createdDateTime")
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if created_dt >= time_min:
                    filtered_items.append(item)
            except (ValueError, AttributeError):
                # If date parsing fails, include the message
                filtered_items.append(item)
        else:
            filtered_items.append(item)

    # Enrich messages with team/channel context
    for msg in filtered_items:
        msg["teamId"] = team_id
        msg["channelId"] = channel_id

    return filtered_items


async def fetch_chats(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Teams chats (group and meeting chats only, excludes 1:1).

    Uses Microsoft Graph API: GET /v1.0/me/chats

    Note: Excludes oneOnOne chats (1:1 direct messages) - only syncs group and meeting chats.

    Returns:
        List of chat dictionaries with structure:
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
    _ = is_initial
    _ = last_synced_at

    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)

    # Fetch all chats user participates in
    chats_endpoint = "/v1.0/me/chats"
    chats_params = {
        "$select": "id,topic,chatType,createdDateTime,lastUpdatedDateTime,webUrl,tenantId,onlineMeetingInfo",
        "$expand": "members",  # Include member list
    }

    resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=chats_endpoint,
        params=chats_params,
    )

    all_chats = []
    if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
        all_chats = resp["value"]

    # Filter out 1:1 chats (oneOnOne) - only keep group and meeting chats
    chats = [chat for chat in all_chats if chat.get("chatType") != "oneOnOne"]

    # Enrich chat names for better display
    for chat in chats:
        chat_type = chat.get("chatType")
        topic = chat.get("topic")

        # Generate display name if topic is None
        if not topic:
            members = chat.get("members", [])
            if chat_type == "group":
                # Use member names
                member_names = [m.get("displayName", "Unknown") for m in members[:3]]
                chat["displayName"] = ", ".join(member_names)
                if len(members) > 3:
                    chat["displayName"] += f" +{len(members) - 3} more"
            elif chat_type == "meeting":
                chat["displayName"] = "Meeting Chat"
            else:
                chat["displayName"] = "Group Chat"
        else:
            chat["displayName"] = topic

    return chats


async def fetch_chat_messages(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
    chat_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Teams chat messages from a specific chat.

    Uses Microsoft Graph API: GET /v1.0/chats/{chatId}/messages

    Args:
        chat_id: Required - Chat ID to fetch messages from

    Returns:
        List of message dictionaries (same structure as channel messages)
    """
    _ = data_type

    if not chat_id:
        raise ValueError("chat_id is required for fetching Teams chat messages")

    provider = provider_config_key or to_nango_connect_integration_key(IntegrationType.TEAMS)

    now = datetime.now(UTC)

    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    # Determine time range for message sync
    if is_initial or not last_synced_at:
        # Initial sync: last 90 days
        time_min = now - timedelta(days=90)
    else:
        # Incremental: from last sync minus 1 day buffer
        time_min = last_synced_at - timedelta(days=1)

    time_min = _ensure_utc(time_min)

    # Microsoft Graph endpoint for chat messages
    endpoint = f"/v1.0/chats/{chat_id}/messages"

    params = {
        "$top": "50",  # Get last 50 messages per chat
        "$orderby": "createdDateTime desc",
    }

    resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
        params=params,
    )

    items = []
    if isinstance(resp, dict) and "value" in resp and isinstance(resp["value"], list):
        items = resp["value"]

    # Filter messages by time_min
    filtered_items = []
    for item in items:
        created_str = item.get("createdDateTime")
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if created_dt >= time_min:
                    filtered_items.append(item)
            except (ValueError, AttributeError):
                # If date parsing fails, include the message
                filtered_items.append(item)
        else:
            filtered_items.append(item)

    # Enrich messages with chat context
    for msg in filtered_items:
        msg["chatId"] = chat_id
        # Note: chat messages don't have teamId/channelId

    return filtered_items
