"""Metadata helpers for Google webhook subscription state on integrations.

The subscription services store all Google watch/channel state under one
connection metadata key so post-connect ensure, webhook validation, and renewal
agree on a single shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY = "google_webhook_subscriptions"


class GoogleSubscriptionKind(StrEnum):
    """Supported Google webhook kinds keyed in connection metadata."""

    GMAIL = "gmail"
    CALENDAR = "calendar"


@dataclass(frozen=True, slots=True)
class GoogleWebhookSubscriptionMetadata:
    """Canonical in-memory shape for one stored Google webhook entry."""

    kind: GoogleSubscriptionKind
    expires_at: datetime
    watch_history_id: str | None = None
    topic_name: str | None = None
    label_ids: list[str] | None = None
    label_filter_behavior: str | None = None
    channel_id: str | None = None
    resource_id: str | None = None
    resource_uri: str | None = None
    channel_token: str | None = None
    address: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def parse_subscription_kind(value: str | None) -> GoogleSubscriptionKind | None:
    """Parse a route/task string into a supported Google subscription kind."""
    if value is None:
        return None

    try:
        return GoogleSubscriptionKind(value)
    except ValueError:
        return None


def get_subscription_metadata(
    metadata: dict[str, Any] | None,
    kind: GoogleSubscriptionKind,
) -> GoogleWebhookSubscriptionMetadata | None:
    """Read one kind-specific subscription record from connection metadata.

    Invalid or incomplete metadata is treated as missing so renewal can create a
    fresh watch/channel instead of trusting a partially written JSON blob.
    """
    if not isinstance(metadata, dict):
        return None

    subscriptions = metadata.get(GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY)
    if not isinstance(subscriptions, dict):
        return None

    stored = subscriptions.get(kind.value)
    if not isinstance(stored, dict):
        return None

    expires_at = _parse_datetime(stored.get("expires_at"))
    if expires_at is None:
        return None

    if kind == GoogleSubscriptionKind.GMAIL:
        return _parse_gmail_metadata(kind, stored, expires_at)
    return _parse_calendar_metadata(kind, stored, expires_at)


def set_gmail_subscription_metadata(
    metadata: dict[str, Any] | None,
    *,
    watch_history_id: str,
    topic_name: str,
    label_ids: list[str],
    label_filter_behavior: str,
    expires_at: datetime,
) -> dict[str, Any]:
    """Write Gmail watch metadata while preserving unrelated metadata."""
    updated, subscriptions = _subscription_root(metadata)
    existing = _existing_subscription(subscriptions, GoogleSubscriptionKind.GMAIL)
    now = datetime.now(UTC).isoformat()

    subscriptions[GoogleSubscriptionKind.GMAIL.value] = {
        "watch_history_id": watch_history_id,
        "topic_name": topic_name,
        "label_ids": list(label_ids),
        "label_filter_behavior": label_filter_behavior,
        "expires_at": _format_datetime(expires_at),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    updated[GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY] = subscriptions
    return updated


def set_calendar_subscription_metadata(
    metadata: dict[str, Any] | None,
    *,
    channel_id: str,
    resource_id: str,
    resource_uri: str,
    channel_token: str,
    address: str,
    expires_at: datetime,
) -> dict[str, Any]:
    """Write Google Calendar channel metadata while preserving unrelated metadata."""
    updated, subscriptions = _subscription_root(metadata)
    existing = _existing_subscription(subscriptions, GoogleSubscriptionKind.CALENDAR)
    now = datetime.now(UTC).isoformat()

    subscriptions[GoogleSubscriptionKind.CALENDAR.value] = {
        "channel_id": channel_id,
        "resource_id": resource_id,
        "resource_uri": resource_uri,
        "channel_token": channel_token,
        "address": address,
        "expires_at": _format_datetime(expires_at),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    updated[GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY] = subscriptions
    return updated


def _parse_gmail_metadata(
    kind: GoogleSubscriptionKind,
    stored: dict[str, Any],
    expires_at: datetime,
) -> GoogleWebhookSubscriptionMetadata | None:
    """Parse Gmail watch metadata and reject incomplete stored entries."""
    watch_history_id = stored.get("watch_history_id")
    topic_name = stored.get("topic_name")
    label_ids = stored.get("label_ids")
    label_filter_behavior = stored.get("label_filter_behavior")
    if (
        not isinstance(watch_history_id, str)
        or not isinstance(topic_name, str)
        or not isinstance(label_ids, list)
        or not all(isinstance(label_id, str) for label_id in label_ids)
        or not isinstance(label_filter_behavior, str)
    ):
        return None

    return GoogleWebhookSubscriptionMetadata(
        kind=kind,
        expires_at=expires_at,
        watch_history_id=watch_history_id,
        topic_name=topic_name,
        label_ids=list(label_ids),
        label_filter_behavior=label_filter_behavior,
        created_at=_parse_datetime(stored.get("created_at")),
        updated_at=_parse_datetime(stored.get("updated_at")),
    )


def _parse_calendar_metadata(
    kind: GoogleSubscriptionKind,
    stored: dict[str, Any],
    expires_at: datetime,
) -> GoogleWebhookSubscriptionMetadata | None:
    """Parse Calendar channel metadata and reject incomplete stored entries."""
    channel_id = stored.get("channel_id")
    resource_id = stored.get("resource_id")
    resource_uri = stored.get("resource_uri")
    channel_token = stored.get("channel_token")
    address = stored.get("address")
    if not all(
        isinstance(value, str) and value
        for value in (channel_id, resource_id, resource_uri, channel_token, address)
    ):
        return None

    return GoogleWebhookSubscriptionMetadata(
        kind=kind,
        expires_at=expires_at,
        channel_id=channel_id,
        resource_id=resource_id,
        resource_uri=resource_uri,
        channel_token=channel_token,
        address=address,
        created_at=_parse_datetime(stored.get("created_at")),
        updated_at=_parse_datetime(stored.get("updated_at")),
    )


def _subscription_root(
    metadata: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(metadata or {})
    raw_subscriptions = updated.get(GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY)
    subscriptions = dict(raw_subscriptions) if isinstance(raw_subscriptions, dict) else {}
    return updated, subscriptions


def _existing_subscription(
    subscriptions: dict[str, Any],
    kind: GoogleSubscriptionKind,
) -> dict[str, Any]:
    existing = subscriptions.get(kind.value)
    return dict(existing) if isinstance(existing, dict) else {}


def _parse_datetime(value: Any) -> datetime | None:
    """Parse stored metadata timestamps and normalize them to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_datetime(value: datetime) -> str:
    """Serialize metadata timestamps in UTC for stable JSON storage."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
