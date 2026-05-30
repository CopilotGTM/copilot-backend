"""Metadata helpers for Microsoft Graph subscription state on integrations.

Connection metadata stores webhook subscription details per integration and
subscription kind (`calendar`/`email`). Helpers here keep that structure
consistent so worker tasks, webhooks, and renewal logic share one contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY = "microsoft_graph_subscriptions"


class MicrosoftSubscriptionKind(str, Enum):
    """Supported Graph webhook kinds keyed in connection metadata."""

    CALENDAR = "calendar"
    EMAIL = "email"


@dataclass(frozen=True)
class MicrosoftGraphSubscriptionMetadata:
    """Canonical in-memory shape for one stored subscription entry."""

    kind: MicrosoftSubscriptionKind
    subscription_id: str
    client_state: str
    resource: str
    change_type: str
    expires_at: datetime
    notification_url: str
    lifecycle_notification_url: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


def parse_subscription_kind(value: str | None) -> MicrosoftSubscriptionKind | None:
    """Parse a user/webhook string into a supported subscription kind."""
    if value is None:
        return None

    try:
        return MicrosoftSubscriptionKind(value)
    except ValueError:
        return None


def get_subscription_metadata(
    metadata: dict[str, Any] | None,
    kind: MicrosoftSubscriptionKind,
) -> MicrosoftGraphSubscriptionMetadata | None:
    """Read one kind-specific subscription record from connection metadata."""
    if not isinstance(metadata, dict):
        return None

    subscriptions = metadata.get(MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY)
    if not isinstance(subscriptions, dict):
        return None

    stored = subscriptions.get(kind.value)
    if not isinstance(stored, dict):
        return None

    subscription_id = stored.get("subscription_id")
    client_state = stored.get("client_state")
    resource = stored.get("resource")
    change_type = stored.get("change_type")
    notification_url = stored.get("notification_url")
    lifecycle_notification_url = stored.get("lifecycle_notification_url")
    expires_at = _parse_datetime(stored.get("expires_at"))
    created_at = _parse_datetime(stored.get("created_at"))
    updated_at = _parse_datetime(stored.get("updated_at"))

    if (
        not isinstance(subscription_id, str)
        or not isinstance(client_state, str)
        or not isinstance(resource, str)
        or not isinstance(change_type, str)
        or expires_at is None
        or not isinstance(notification_url, str)
        or not isinstance(lifecycle_notification_url, str)
    ):
        return None

    return MicrosoftGraphSubscriptionMetadata(
        kind=kind,
        subscription_id=subscription_id,
        client_state=client_state,
        resource=resource,
        change_type=change_type,
        expires_at=expires_at,
        notification_url=notification_url,
        lifecycle_notification_url=lifecycle_notification_url,
        created_at=created_at,
        updated_at=updated_at,
    )


def set_subscription_metadata(
    metadata: dict[str, Any] | None,
    *,
    kind: MicrosoftSubscriptionKind,
    subscription_id: str,
    client_state: str,
    resource: str,
    change_type: str,
    expires_at: datetime,
    notification_url: str,
    lifecycle_notification_url: str,
) -> dict[str, Any]:
    """Write subscription metadata while preserving existing timestamps.

    `created_at`/`updated_at` are managed by callers so we preserve prior values
    during partial updates and renewal writes.
    """
    updated = dict(metadata or {})
    raw_subscriptions = updated.get(MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY)
    subscriptions = dict(raw_subscriptions) if isinstance(raw_subscriptions, dict) else {}
    existing = subscriptions.get(kind.value)
    existing_subscription = existing if isinstance(existing, dict) else {}

    stored: dict[str, Any] = {
        "subscription_id": subscription_id,
        "client_state": client_state,
        "resource": resource,
        "change_type": change_type,
        "expires_at": expires_at.isoformat(),
        "notification_url": notification_url,
        "lifecycle_notification_url": lifecycle_notification_url,
    }

    if existing_subscription.get("created_at") is not None:
        stored["created_at"] = existing_subscription["created_at"]
    if existing_subscription.get("updated_at") is not None:
        stored["updated_at"] = existing_subscription["updated_at"]

    subscriptions[kind.value] = stored
    updated[MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY] = subscriptions
    return updated


def clear_subscription_metadata(
    metadata: dict[str, Any] | None,
    kind: MicrosoftSubscriptionKind,
) -> dict[str, Any]:
    """Remove one subscription kind and clean up empty metadata containers."""
    updated = dict(metadata or {})
    subscriptions = dict(updated.get(MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY) or {})
    subscriptions.pop(kind.value, None)

    if subscriptions:
        updated[MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY] = subscriptions
    else:
        updated.pop(MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY, None)

    return updated


def _parse_datetime(value: Any) -> datetime | None:
    """Parse datetime values stored in metadata JSON fields."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
