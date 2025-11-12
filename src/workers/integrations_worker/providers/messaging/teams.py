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
