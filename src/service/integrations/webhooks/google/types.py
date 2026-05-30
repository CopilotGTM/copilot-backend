"""Typed Google webhook notification payloads.

The request service converts raw HTTP bodies and headers into these small
objects before ingestion. Keeping provider quirks here makes the ingestion
service read like business validation instead of envelope parsing.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GoogleGmailPubSubNotification:
    """Decoded Gmail notification delivered through Cloud Pub/Sub push."""

    message_id: str
    email_address: str
    history_id: str
    publish_time: str | None
    subscription: str | None
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> GoogleGmailPubSubNotification | None:
        """Parse a Pub/Sub push envelope into the Gmail-specific payload."""
        message = payload.get("message")
        if not isinstance(message, dict):
            return None

        message_id = message.get("messageId") or message.get("message_id")
        data = message.get("data")
        if not isinstance(message_id, str) or not isinstance(data, str):
            return None

        gmail_payload = _decode_pubsub_data(data)
        if gmail_payload is None:
            return None

        email_address = gmail_payload.get("emailAddress")
        history_id = gmail_payload.get("historyId")
        if not isinstance(email_address, str) or not isinstance(history_id, str):
            return None

        publish_time = message.get("publishTime")
        subscription = payload.get("subscription")
        return cls(
            message_id=message_id,
            email_address=email_address,
            history_id=history_id,
            publish_time=publish_time if isinstance(publish_time, str) else None,
            subscription=subscription if isinstance(subscription, str) else None,
            raw=payload,
        )


@dataclass(frozen=True, slots=True)
class GoogleCalendarChannelNotification:
    """Calendar event channel notification from `X-Goog-*` headers."""

    channel_id: str
    resource_id: str
    resource_state: str
    message_number: str
    resource_uri: str | None
    channel_token: str | None
    channel_expiration: str | None

    @classmethod
    def from_headers(
        cls,
        headers: Mapping[str, str],
    ) -> GoogleCalendarChannelNotification | None:
        """Parse Calendar notification headers case-insensitively."""
        lower_headers = {key.lower(): value for key, value in headers.items()}
        channel_id = lower_headers.get("x-goog-channel-id")
        resource_id = lower_headers.get("x-goog-resource-id")
        resource_state = lower_headers.get("x-goog-resource-state")
        message_number = lower_headers.get("x-goog-message-number")
        if not all(
            isinstance(value, str) and value
            for value in (channel_id, resource_id, resource_state, message_number)
        ):
            return None
        assert channel_id is not None
        assert resource_id is not None
        assert resource_state is not None
        assert message_number is not None

        return cls(
            channel_id=channel_id,
            resource_id=resource_id,
            resource_state=resource_state,
            message_number=message_number,
            resource_uri=lower_headers.get("x-goog-resource-uri"),
            channel_token=lower_headers.get("x-goog-channel-token"),
            channel_expiration=lower_headers.get("x-goog-channel-expiration"),
        )


def _decode_pubsub_data(data: str) -> dict[str, Any] | None:
    """Decode Pub/Sub's Base64URL JSON payload into a Gmail notification body."""
    try:
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    return payload if isinstance(payload, dict) else None
