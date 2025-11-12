"""Outlook Email fetcher via Microsoft Graph API through Nango proxy.

Fetches email folders and messages from Outlook/Office 365 mailboxes.

Microsoft Graph API documentation:
- List folders: https://learn.microsoft.com/en-us/graph/api/user-list-mailfolders
- List messages: https://learn.microsoft.com/en-us/graph/api/user-list-messages
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.service.integrations.nango_client import NangoClient


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
    Fetch Outlook email folders via Nango proxy.

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

    resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
        params=params,
    )

    items = []
    if isinstance(resp, dict):
        if "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]
        elif "items" in resp and isinstance(resp["items"], list):
            items = resp["items"]

    return items


async def fetch_messages(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
    folder_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Outlook email messages via Nango proxy.

    Uses Microsoft Graph API: GET /v1.0/me/messages or /v1.0/me/mailFolders/{folderId}/messages

    Args:
        folder_id: Optional folder ID to fetch messages from specific folder

    Returns:
        List of message dictionaries with structure:
        {
            "id": "AAMkAGI...",
            "conversationId": "AAQkAGI...",
            "subject": "Meeting notes",
            "bodyPreview": "Here are the notes...",
            "body": {"contentType": "html", "content": "<html>..."},
            "from": {"emailAddress": {"name": "John Doe", "address": "john@example.com"}},
            "toRecipients": [...],
            "ccRecipients": [...],
            "bccRecipients": [...],
            "replyTo": [...],
            "sentDateTime": "2025-11-12T10:30:00Z",
            "receivedDateTime": "2025-11-12T10:30:05Z",
            "hasAttachments": true,
            "attachments": [...],
            "importance": "normal",
            "isRead": false,
            "isDraft": false,
            "flag": {...},
            "categories": ["Red category"],
            "inferenceClassification": "focused"
        }
    """
    _ = data_type  # intentionally unused

    provider = provider_config_key or to_nango_connect_integration_key(
        IntegrationType.OUTLOOK_EMAIL
    )

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

    # Build endpoint - either all messages or folder-specific
    endpoint = f"/v1.0/me/mailFolders/{folder_id}/messages" if folder_id else "/v1.0/me/messages"

    # Microsoft Graph query parameters
    params = {
        "$filter": f"receivedDateTime ge {time_min.isoformat()}",
        "$orderby": "receivedDateTime desc",
        "$top": "999",  # Max items per request
        "$select": "id,conversationId,subject,bodyPreview,body,from,toRecipients,ccRecipients,bccRecipients,replyTo,sentDateTime,receivedDateTime,hasAttachments,importance,isRead,isDraft,flag,categories,inferenceClassification,internetMessageId,parentFolderId",
    }

    resp = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
        params=params,
    )

    items = []
    if isinstance(resp, dict):
        if "value" in resp and isinstance(resp["value"], list):
            items = resp["value"]
        elif "items" in resp and isinstance(resp["items"], list):
            items = resp["items"]

    # Note: Pagination (@odata.nextLink) can be added in future iterations
    # Currently returning first page up to 999 items per sync

    return items
