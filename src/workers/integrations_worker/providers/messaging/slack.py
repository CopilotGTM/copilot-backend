"""Slack messaging fetcher via Slack Web API."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src.infra.utils.logger import get_logger
from src.service.integrations.api_error_utils import IntegrationPermissionError
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    call_with_token_retry,
)
from src.workers.integrations_worker.providers.messaging.teams import (
    FetchChatsResult,
    FetchMessagesResult,
)

logger = get_logger(__name__)

SLACK_API_BASE = "https://slack.com/api"


def _get_positive_int_env(var_name: str, default: int) -> int:
    raw = os.getenv(var_name, str(default))
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to default=%d", var_name, raw, default)
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


SLACK_MESSAGES_INITIAL_LOOKBACK_DAYS = _get_positive_int_env(
    "SLACK_MESSAGES_INITIAL_LOOKBACK_DAYS",
    90,
)
SLACK_MESSAGES_INCREMENTAL_BUFFER_MINUTES = _get_positive_int_env(
    "SLACK_MESSAGES_INCREMENTAL_BUFFER_MINUTES",
    1440,
)


def build_slack_channel_external_id(team_id: str, channel_id: str) -> str:
    """Namespace Slack conversation IDs by workspace so org-scoped rows do not collide."""
    return f"{team_id}:{channel_id}"


def split_slack_channel_external_id(external_channel_id: str) -> tuple[str | None, str]:
    """Split the stored canonical Slack channel ID into workspace and raw channel IDs."""
    if ":" not in external_channel_id:
        return None, external_channel_id
    team_id, channel_id = external_channel_id.split(":", 1)
    return team_id or None, channel_id


async def _slack_api_request(
    client: httpx.AsyncClient,
    method_name: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{SLACK_API_BASE}/{method_name}"
    response = await client.get(url, params=params)

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "1")
        try:
            wait_seconds = max(1.0, float(retry_after))
        except ValueError:
            wait_seconds = 1.0

        logger.warning(
            "Slack API rate limited for %s — retrying after %.1fs",
            method_name,
            wait_seconds,
        )
        await asyncio.sleep(wait_seconds)
        response = await client.get(url, params=params)

    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Slack {method_name} returned an invalid payload")
    if not payload.get("ok"):
        error_code = str(payload.get("error", "unknown_error"))
        if error_code == "missing_scope":
            raise IntegrationPermissionError(
                "Slack permissions were not granted. Please reconnect and allow Slack access.",
                status_code=response.status_code,
                provider="slack",
                endpoint=method_name,
                response_summary=error_code,
            )
        raise ValueError(f"Slack {method_name} failed: {error_code}")
    return payload


async def _paginate_slack_collection(
    client: httpx.AsyncClient,
    method_name: str,
    *,
    params: dict[str, Any] | None = None,
    list_key: str,
) -> list[dict[str, Any]]:
    all_items: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        request_params = dict(params or {})
        if cursor:
            request_params["cursor"] = cursor

        payload = await _slack_api_request(client, method_name, params=request_params)
        items = payload.get(list_key, [])
        if not isinstance(items, list):
            raise ValueError(f"Slack {method_name} payload missing list field {list_key}")

        all_items.extend(item for item in items if isinstance(item, dict))

        metadata = payload.get("response_metadata", {})
        if not isinstance(metadata, dict):
            break
        cursor = metadata.get("next_cursor") or None
        if not cursor:
            break

    return all_items


async def _paginate_slack_member_ids(client: httpx.AsyncClient, channel_id: str) -> list[str]:
    member_ids: list[str] = []
    cursor: str | None = None

    while True:
        params: dict[str, Any] = {"channel": channel_id, "limit": "200"}
        if cursor:
            params["cursor"] = cursor

        payload = await _slack_api_request(client, "conversations.members", params=params)
        members = payload.get("members", [])
        if isinstance(members, list):
            member_ids.extend(member for member in members if isinstance(member, str))

        metadata = payload.get("response_metadata", {})
        if not isinstance(metadata, dict):
            break
        cursor = metadata.get("next_cursor") or None
        if not cursor:
            break

    return member_ids


def _normalize_slack_user(member: dict[str, Any]) -> dict[str, Any]:
    profile = member.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}

    display_name = (
        profile.get("display_name")
        or profile.get("real_name")
        or member.get("real_name")
        or member.get("name")
    )

    role = "member"
    if member.get("is_owner"):
        role = "owner"
    elif member.get("is_admin"):
        role = "admin"
    elif member.get("is_restricted") or member.get("is_ultra_restricted"):
        role = "guest"
    elif member.get("is_bot"):
        role = "bot"

    return {
        "id": member.get("id"),
        "displayName": display_name,
        "name": member.get("name"),
        "email": profile.get("email"),
        "role": role,
        "joinedAt": None,
    }


async def _fetch_workspace_context(
    token: str,
) -> tuple[str, str | None, dict[str, dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        auth_payload = await _slack_api_request(client, "auth.test")
        team_id = str(auth_payload.get("team_id") or "")
        if not team_id:
            raise ValueError("Slack auth.test did not return a team_id")

        team_name: str | None = None
        team_payload = await _slack_api_request(client, "team.info")
        team = team_payload.get("team", {})
        if isinstance(team, dict):
            maybe_team_name = team.get("name")
            if isinstance(maybe_team_name, str):
                team_name = maybe_team_name

        users = await _paginate_slack_collection(
            client,
            "users.list",
            params={"limit": "200"},
            list_key="members",
        )

    user_map: dict[str, dict[str, Any]] = {}
    for user in users:
        user_id = user.get("id")
        if isinstance(user_id, str) and user_id:
            user_map[user_id] = _normalize_slack_user(user)

    return team_id, team_name, user_map


def _slack_oldest_timestamp(*, is_initial: bool, last_synced_at: datetime | None) -> str:
    if is_initial or last_synced_at is None:
        oldest = datetime.now(UTC) - timedelta(days=SLACK_MESSAGES_INITIAL_LOOKBACK_DAYS)
    else:
        oldest = last_synced_at.astimezone(UTC) - timedelta(
            minutes=SLACK_MESSAGES_INCREMENTAL_BUFFER_MINUTES
        )
    return f"{oldest.timestamp():.6f}"


def _normalize_slack_channel(
    conversation: dict[str, Any],
    *,
    team_id: str,
    team_name: str | None,
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_channel_id = str(conversation.get("id") or "")
    purpose = conversation.get("purpose", {})
    if not isinstance(purpose, dict):
        purpose = {}
    topic = conversation.get("topic", {})
    if not isinstance(topic, dict):
        topic = {}
    latest = conversation.get("latest", {})
    if not isinstance(latest, dict):
        latest = {}

    return {
        "id": build_slack_channel_external_id(team_id, raw_channel_id),
        "name": conversation.get("name") or "Unnamed",
        "is_private": bool(conversation.get("is_private")),
        "is_mpim": bool(conversation.get("is_mpim")),
        "is_archived": bool(conversation.get("is_archived")),
        "num_members": conversation.get("num_members") or len(members),
        "description": purpose.get("value") or topic.get("value"),
        "members": members,
        "teamId": team_id,
        "teamName": team_name,
        "slack_channel_id": raw_channel_id,
        "created": conversation.get("created"),
        "updated": conversation.get("updated"),
        "lastUpdatedDateTime": latest.get("ts"),
    }


async def fetch_channels(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch public and private Slack channels available to the connected user."""
    _ = connection_id
    _ = is_initial
    _ = last_synced_at
    _ = data_type
    _ = provider_config_key

    async def _do_fetch(token: str) -> list[dict[str, Any]]:
        team_id, team_name, user_map = await _fetch_workspace_context(token)
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            conversations = await _paginate_slack_collection(
                client,
                "conversations.list",
                params={
                    "types": "public_channel,private_channel",
                    "exclude_archived": "true",
                    "limit": "200",
                },
                list_key="channels",
            )

            normalized: list[dict[str, Any]] = []
            for conversation in conversations:
                raw_channel_id = conversation.get("id")
                if not isinstance(raw_channel_id, str) or not raw_channel_id:
                    continue

                member_ids = await _paginate_slack_member_ids(client, raw_channel_id)
                members = [user_map[mid] for mid in member_ids if mid in user_map]
                normalized.append(
                    _normalize_slack_channel(
                        conversation,
                        team_id=team_id,
                        team_name=team_name,
                        members=members,
                    )
                )

        return normalized

    return await call_with_token_retry(token_info, _do_fetch)


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
    """Fetch Slack multi-party DMs for the connected user."""
    _ = connection_id
    _ = is_initial
    _ = last_synced_at
    _ = delta_link
    _ = data_type
    _ = provider_config_key
    _ = max_age_days

    async def _do_fetch(token: str) -> FetchChatsResult:
        team_id, team_name, user_map = await _fetch_workspace_context(token)
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            conversations = await _paginate_slack_collection(
                client,
                "conversations.list",
                params={
                    "types": "mpim",
                    "exclude_archived": "true",
                    "limit": "200",
                },
                list_key="channels",
            )

            chats: list[dict[str, Any]] = []
            for conversation in conversations:
                raw_channel_id = conversation.get("id")
                if not isinstance(raw_channel_id, str) or not raw_channel_id:
                    continue

                member_ids = await _paginate_slack_member_ids(client, raw_channel_id)
                members = [user_map[mid] for mid in member_ids if mid in user_map]
                chats.append(
                    _normalize_slack_channel(
                        conversation,
                        team_id=team_id,
                        team_name=team_name,
                        members=members,
                    )
                )

        return FetchChatsResult(chats=chats, delta_link=None)

    return await call_with_token_retry(token_info, _do_fetch)


def _enrich_slack_message(
    message: dict[str, Any],
    *,
    external_channel_id: str,
    raw_channel_id: str,
    user_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    user_id = message.get("user")
    user_profile = user_map.get(user_id, {}) if isinstance(user_id, str) else {}
    enriched = dict(message)
    enriched["id"] = message.get("client_msg_id") or message.get("ts")
    enriched["channel"] = external_channel_id
    enriched["slack_channel_id"] = raw_channel_id
    if user_profile:
        enriched["user_profile"] = user_profile
    return enriched


async def _fetch_thread_replies(
    client: httpx.AsyncClient,
    *,
    raw_channel_id: str,
    external_channel_id: str,
    parent_message: dict[str, Any],
    oldest_ts: str,
    user_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    thread_ts = parent_message.get("thread_ts") or parent_message.get("ts")
    if not isinstance(thread_ts, str) or not thread_ts:
        return []

    reply_items = await _paginate_slack_collection(
        client,
        "conversations.replies",
        params={
            "channel": raw_channel_id,
            "ts": thread_ts,
            "oldest": oldest_ts,
            "inclusive": "true",
            "limit": "200",
        },
        list_key="messages",
    )

    replies: list[dict[str, Any]] = []
    for reply in reply_items:
        if reply.get("ts") == parent_message.get("ts"):
            continue
        replies.append(
            _enrich_slack_message(
                reply,
                external_channel_id=external_channel_id,
                raw_channel_id=raw_channel_id,
                user_map=user_map,
            )
        )
    return replies


async def fetch_thread_replies_for_known_threads(
    *,
    token_info: IntegrationTokenInfo,
    connection_id: str,
    channel_id: str | None,
    thread_oldest_map: dict[str, str] | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> FetchMessagesResult:
    """Fetch incremental replies for known Slack threads in a conversation."""
    _ = connection_id
    _ = data_type
    _ = provider_config_key

    if not channel_id:
        raise ValueError("channel_id is required for fetching Slack thread replies")

    if not thread_oldest_map:
        return FetchMessagesResult(messages=[], delta_link=None)

    _, raw_channel_id = split_slack_channel_external_id(channel_id)

    async def _do_fetch(token: str) -> FetchMessagesResult:
        _, _, user_map = await _fetch_workspace_context(token)
        headers = {"Authorization": f"Bearer {token}"}
        replies: list[dict[str, Any]] = []
        newest_ts: str | None = None
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            for thread_ts, oldest_ts in thread_oldest_map.items():
                thread_replies = await _fetch_thread_replies(
                    client,
                    raw_channel_id=raw_channel_id,
                    external_channel_id=channel_id,
                    parent_message={"ts": thread_ts, "thread_ts": thread_ts},
                    oldest_ts=oldest_ts,
                    user_map=user_map,
                )
                for reply in thread_replies:
                    reply_id = reply.get("client_msg_id") or reply.get("ts")
                    if not isinstance(reply_id, str) or reply_id in seen_ids:
                        continue
                    seen_ids.add(reply_id)
                    replies.append(reply)

                    reply_ts = reply.get("ts")
                    if (
                        isinstance(reply_ts, str)
                        and reply_ts
                        and (newest_ts is None or float(reply_ts) > float(newest_ts))
                    ):
                        newest_ts = reply_ts

        replies.sort(key=lambda msg: float(str(msg.get("ts") or "0")))
        return FetchMessagesResult(messages=replies, delta_link=newest_ts)

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
    """Fetch Slack messages from a specific conversation."""
    _ = connection_id
    _ = data_type
    _ = provider_config_key
    _ = team_id

    if not channel_id:
        raise ValueError("channel_id is required for fetching Slack messages")

    _, raw_channel_id = split_slack_channel_external_id(channel_id)
    oldest_ts = _slack_oldest_timestamp(is_initial=is_initial, last_synced_at=last_synced_at)

    async def _do_fetch(token: str) -> FetchMessagesResult:
        _, _, user_map = await _fetch_workspace_context(token)
        headers = {"Authorization": f"Bearer {token}"}
        messages: list[dict[str, Any]] = []
        newest_ts: str | None = delta_link

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            history = await _paginate_slack_collection(
                client,
                "conversations.history",
                params={
                    "channel": raw_channel_id,
                    "oldest": oldest_ts,
                    "inclusive": "true",
                    "limit": "200",
                },
                list_key="messages",
            )

            seen_ids: set[str] = set()
            for message in history:
                message_ts = message.get("ts")
                if (
                    isinstance(message_ts, str)
                    and message_ts
                    and (newest_ts is None or float(message_ts) > float(newest_ts))
                ):
                    newest_ts = message_ts

                enriched = _enrich_slack_message(
                    message,
                    external_channel_id=channel_id,
                    raw_channel_id=raw_channel_id,
                    user_map=user_map,
                )
                message_id = enriched.get("client_msg_id") or enriched.get("ts")
                if isinstance(message_id, str) and message_id not in seen_ids:
                    messages.append(enriched)
                    seen_ids.add(message_id)

                reply_count = message.get("reply_count")
                if isinstance(reply_count, int) and reply_count > 0:
                    replies = await _fetch_thread_replies(
                        client,
                        raw_channel_id=raw_channel_id,
                        external_channel_id=channel_id,
                        parent_message=message,
                        oldest_ts=oldest_ts,
                        user_map=user_map,
                    )
                    for reply in replies:
                        reply_id = reply.get("client_msg_id") or reply.get("ts")
                        if isinstance(reply_id, str) and reply_id not in seen_ids:
                            messages.append(reply)
                            seen_ids.add(reply_id)
                            reply_ts = reply.get("ts")
                            if isinstance(reply_ts, str) and (
                                newest_ts is None or float(reply_ts) > float(newest_ts)
                            ):
                                newest_ts = reply_ts

        messages.sort(key=lambda msg: float(str(msg.get("ts") or "0")))
        return FetchMessagesResult(messages=messages, delta_link=newest_ts)

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
    """Fetch Slack group chat messages from an MPIM conversation."""
    return await fetch_messages(
        token_info=token_info,
        connection_id=connection_id,
        is_initial=is_initial,
        last_synced_at=last_synced_at,
        delta_link=delta_link,
        data_type=data_type,
        provider_config_key=provider_config_key,
        channel_id=chat_id,
    )
