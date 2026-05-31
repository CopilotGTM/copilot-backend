# Google Gmail and Calendar Webhook Incremental Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add production-grade Google Gmail and Google Calendar webhook support so provider notifications schedule the existing single-integration incremental sync tasks.

**Architecture:** Google webhooks are lightweight triggers only. Gmail uses Gmail `users.watch` to publish mailbox changes into a Google Cloud Pub/Sub topic, and Pub/Sub push calls our public webhook. Google Calendar uses Calendar API event channel `watch` to call our public webhook directly. Both paths validate stored channel/watch metadata, dedupe and debounce provider retries, and enqueue existing `integrations.sync_email_integration` or `integrations.sync_calendar_integration` tasks; scheduled/manual sync remains the correctness backstop for dropped notifications and expired provider cursors.

**Tech Stack:** FastAPI, Celery, Redis, SQLModel, Google Gmail API, Google Calendar API, Google Cloud Pub/Sub push, existing custom OAuth token resolution, existing integrations worker queues.

---

## Sources Checked

- Gmail push notifications: Gmail uses Cloud Pub/Sub, requires granting publish rights to `gmail-api-push@system.gserviceaccount.com`, returns `historyId` and `expiration` from `watch`, sends Pub/Sub `message.data` as Base64URL JSON with `emailAddress` and `historyId`, requires successful HTTP response to acknowledge, and recommends daily watch renewal because watches must be renewed at least every 7 days.
  - https://developers.google.com/workspace/gmail/api/guides/push
  - https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/watch
- Calendar push notifications: Calendar `watch` returns `id`, `resourceId`, `resourceUri`, `token`, and `expiration`; notifications arrive as `X-Goog-*` headers without useful body data; `sync` notifications can arrive before the `watch` response; channels must be replaced near expiration; notifications are not 100% reliable.
  - https://developers.google.com/workspace/calendar/api/guides/push

## Scope

Build v1 for:

- `IntegrationType.GMAIL`
- `IntegrationType.GOOGLE_CALENDAR`

Do not implement Google Drive, Google Meet Events API, secondary-calendar fanout, Gmail pull subscriptions, or provider data writers in this plan.

Keep existing sync tasks as the only writer path:

- Gmail task: `integrations.sync_email_integration`
- Google Calendar task: `integrations.sync_calendar_integration`
- Gmail cursor: existing email folder `historyId` metadata used by the Gmail provider path
- Google Calendar cursor: `KnowledgeIntegration.connection_metadata["calendar_delta_link"]`
- Celery queue for webhook-triggered sync: `integrations_priority`

## Production Decisions

1. Gmail and Calendar use separate subscription mechanics.
   - Gmail does not call our webhook directly from Gmail. Gmail publishes to Cloud Pub/Sub; Pub/Sub push calls our webhook.
   - Calendar calls our webhook directly with `X-Goog-*` headers.
2. Webhook payloads never write emails or events.
   - They only wake the existing incremental sync task.
3. Provider routing must be tenant-scoped.
   - Every integration lookup filters by both `organisation_id` and `integration_id`.
   - Gmail additionally validates decoded `emailAddress` against `KnowledgeIntegration.integration_email` if the field exists, otherwise against existing connection metadata email if present.
4. Gmail Pub/Sub push authentication is mandatory.
   - Configure the Pub/Sub push subscription with an OIDC token.
   - Validate the `Authorization: Bearer ...` token before trusting query params, `message.data`, or decoded Gmail `emailAddress`.
   - Enforce expected audience and expected Pub/Sub push service account email. Gmail `emailAddress` is an ownership check, not request authentication.
   - If OIDC cannot be configured in an environment, use a stored signed secret per push subscription instead; do not deploy a public Gmail webhook that is protected only by route/query params.
5. Calendar channel token is a verification and routing secret, not a bearer credential.
   - Generate an opaque token or signed value and store it in metadata.
   - Do not put OAuth tokens or sensitive provider credentials in the Calendar channel token.
6. Scheduled polling/backfill remains available.
   - Gmail docs explicitly warn notifications can be delayed or dropped.
   - Calendar docs say notifications are not 100% reliable.
7. The implementation mirrors the completed Microsoft structure.
   - Thin FastAPI route.
   - Request service for HTTP parsing and guardrails.
   - Ingestion service for metadata validation, dedupe, debounce, and sync enqueue.
   - Subscription service for Google API calls.
   - Subscription task service for fresh-session worker orchestration.
   - Celery adapter for retry and task registration.

## File Structure

Create:

- `src/api/routers/integrations/google_webhook_routes.py`
- `src/service/integrations/webhooks/google/__init__.py`
- `src/service/integrations/webhooks/google/metadata.py`
- `src/service/integrations/webhooks/google/types.py`
- `src/service/integrations/webhooks/google/pubsub_auth.py`
- `src/service/integrations/webhooks/google/request_service.py`
- `src/service/integrations/webhooks/google/ingestion_service.py`
- `src/service/integrations/webhooks/google/subscription_service.py`
- `src/service/integrations/webhooks/google/subscription_task_service.py`
- `src/workers/integrations_worker/tasks/google_subscription_tasks.py`
- `tests/api/routers/integrations/test_google_webhook_routes.py`
- `tests/service/integrations/webhooks/google/test_metadata.py`
- `tests/service/integrations/webhooks/google/test_request_service.py`
- `tests/service/integrations/webhooks/google/test_pubsub_auth.py`
- `tests/service/integrations/webhooks/google/test_ingestion_service.py`
- `tests/service/integrations/webhooks/google/test_subscription_service.py`
- `tests/service/integrations/webhooks/google/test_subscription_task_service.py`
- `tests/workers/integrations_worker/test_google_subscription_tasks.py`
- `docs/Technical Documentation/integrations/google-webhook-flow.md`

Modify:

- `src/api/routers/integrations/__init__.py`
- `src/workers/integrations_worker/providers/_google_api.py`
- `src/workers/integrations_worker/tasks/__init__.py`
- `src/workers/integrations_worker/config.py`
- `src/workers/integrations_worker/scheduled_jobs.py`
- `src/service/integrations/initial_sync_service.py`
- `docs/Technical Documentation/integrations/sync-by-provider.md`

## Metadata Contract

Store provider subscription state in the Google `KnowledgeIntegration.connection_metadata` row:

```python
GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY = "google_webhook_subscriptions"

{
    "google_webhook_subscriptions": {
        "gmail": {
            "watch_history_id": "9876543210",
            "topic_name": "projects/copilot-prod/topics/gmail-push",
            "label_ids": ["INBOX"],
            "label_filter_behavior": "INCLUDE",
            "expires_at": "2026-06-06T12:00:00+00:00",
            "created_at": "2026-05-30T12:00:00+00:00",
            "updated_at": "2026-05-30T12:00:00+00:00"
        },
        "calendar": {
            "channel_id": "uuid-we-generated",
            "resource_id": "google-resource-id",
            "resource_uri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "channel_token": "opaque-secret",
            "address": "https://api.example.com/users/integrations/google/calendar/webhook?organisation_id=11111111-1111-1111-1111-111111111111&integration_id=22222222-2222-2222-2222-222222222222",
            "expires_at": "2026-06-06T12:00:00+00:00",
            "created_at": "2026-05-30T12:00:00+00:00",
            "updated_at": "2026-05-30T12:00:00+00:00"
        }
    }
}
```

## Runtime Flow

```text
Google connect
  -> InitialSyncService
  -> integrations.sync_email_integration or integrations.sync_calendar_integration
  -> persist Gmail historyId or Google Calendar syncToken
  -> integrations.ensure_google_subscription
  -> GoogleSubscriptionTaskService.ensure_subscription
  -> GoogleSubscriptionService.ensure_subscription_metadata
  -> Gmail users.watch or Calendar events.watch
  -> persist KnowledgeIntegration.connection_metadata

Gmail mailbox update
  -> Gmail publishes to Cloud Pub/Sub topic
  -> Pub/Sub push calls /users/integrations/google/gmail/webhook
  -> Google Pub/Sub OIDC token is validated
  -> GoogleWebhookRequestService.handle_gmail_pubsub
  -> GoogleWebhookIngestionService.ingest_gmail
  -> integrations.sync_email_integration

Google Calendar event update
  -> Calendar API calls /users/integrations/google/calendar/webhook
  -> GoogleWebhookRequestService.handle_calendar_channel
  -> GoogleWebhookIngestionService.ingest_calendar
  -> integrations.sync_calendar_integration

Scheduled renewal
  -> integrations.renew_google_subscriptions_all
  -> GoogleSubscriptionTaskService.dispatch_all_subscription_ensures
  -> integrations.ensure_google_subscription per eligible Google integration
```

## Task 1: Add Google Webhook Metadata Helpers

**Files:**
- Create: `src/service/integrations/webhooks/google/__init__.py`
- Create: `src/service/integrations/webhooks/google/metadata.py`
- Test: `tests/service/integrations/webhooks/google/test_metadata.py`

- [ ] **Step 1: Write failing metadata tests**

Create `tests/service/integrations/webhooks/google/test_metadata.py`:

```python
from datetime import UTC, datetime

from src.service.integrations.webhooks.google.metadata import (
    GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY,
    GoogleSubscriptionKind,
    get_subscription_metadata,
    set_calendar_subscription_metadata,
    set_gmail_subscription_metadata,
)


def test_set_and_get_gmail_subscription_metadata_preserves_existing_values() -> None:
    updated = set_gmail_subscription_metadata(
        {"existing": {"value": 1}},
        watch_history_id="123",
        topic_name="projects/proj/topics/gmail",
        label_ids=["INBOX"],
        label_filter_behavior="INCLUDE",
        expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )

    assert updated["existing"] == {"value": 1}
    stored = updated[GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY]["gmail"]
    assert stored["watch_history_id"] == "123"
    assert stored["topic_name"] == "projects/proj/topics/gmail"

    parsed = get_subscription_metadata(updated, GoogleSubscriptionKind.GMAIL)
    assert parsed is not None
    assert parsed.kind == GoogleSubscriptionKind.GMAIL
    assert parsed.expires_at == datetime(2026, 6, 6, 12, 0, tzinfo=UTC)


def test_set_and_get_calendar_subscription_metadata_preserves_channel_fields() -> None:
    updated = set_calendar_subscription_metadata(
        {},
        channel_id="channel-1",
        resource_id="resource-1",
        resource_uri="https://www.googleapis.com/calendar/v3/calendars/primary/events",
        channel_token="secret-token",
        address="https://api.example.test/users/integrations/google/calendar/webhook",
        expires_at=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )

    parsed = get_subscription_metadata(updated, GoogleSubscriptionKind.CALENDAR)
    assert parsed is not None
    assert parsed.kind == GoogleSubscriptionKind.CALENDAR
    assert parsed.channel_id == "channel-1"
    assert parsed.resource_id == "resource-1"
    assert parsed.channel_token == "secret-token"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_metadata.py -v
```

Expected: FAIL with missing `src.service.integrations.webhooks.google`.

- [ ] **Step 3: Implement metadata helpers**

Create `src/service/integrations/webhooks/google/__init__.py`:

```python
"""Google Gmail and Calendar webhook subscription support."""
```

Create `src/service/integrations/webhooks/google/metadata.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY = "google_webhook_subscriptions"


class GoogleSubscriptionKind(StrEnum):
    GMAIL = "gmail"
    CALENDAR = "calendar"


@dataclass(frozen=True, slots=True)
class GoogleWebhookSubscriptionMetadata:
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
    if value == GoogleSubscriptionKind.GMAIL.value:
        return GoogleSubscriptionKind.GMAIL
    if value == GoogleSubscriptionKind.CALENDAR.value:
        return GoogleSubscriptionKind.CALENDAR
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def get_subscription_metadata(
    metadata: dict[str, Any] | None,
    kind: GoogleSubscriptionKind,
) -> GoogleWebhookSubscriptionMetadata | None:
    subscriptions = (metadata or {}).get(GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY)
    if not isinstance(subscriptions, dict):
        return None
    raw = subscriptions.get(kind.value)
    if not isinstance(raw, dict):
        return None
    expires_at = _parse_datetime(raw.get("expires_at"))
    if expires_at is None:
        return None

    if kind == GoogleSubscriptionKind.GMAIL:
        watch_history_id = raw.get("watch_history_id")
        topic_name = raw.get("topic_name")
        if not isinstance(watch_history_id, str) or not isinstance(topic_name, str):
            return None
        label_ids = raw.get("label_ids")
        return GoogleWebhookSubscriptionMetadata(
            kind=kind,
            expires_at=expires_at,
            watch_history_id=watch_history_id,
            topic_name=topic_name,
            label_ids=label_ids if isinstance(label_ids, list) else [],
            label_filter_behavior=raw.get("label_filter_behavior")
            if isinstance(raw.get("label_filter_behavior"), str)
            else None,
            created_at=_parse_datetime(raw.get("created_at")),
            updated_at=_parse_datetime(raw.get("updated_at")),
        )

    channel_id = raw.get("channel_id")
    resource_id = raw.get("resource_id")
    resource_uri = raw.get("resource_uri")
    channel_token = raw.get("channel_token")
    address = raw.get("address")
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
        created_at=_parse_datetime(raw.get("created_at")),
        updated_at=_parse_datetime(raw.get("updated_at")),
    )


def _subscription_root(metadata: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(metadata or {})
    subscriptions = dict(updated.get(GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY) or {})
    return updated, subscriptions


def set_gmail_subscription_metadata(
    metadata: dict[str, Any] | None,
    *,
    watch_history_id: str,
    topic_name: str,
    label_ids: list[str],
    label_filter_behavior: str,
    expires_at: datetime,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    updated, subscriptions = _subscription_root(metadata)
    existing = dict(subscriptions.get(GoogleSubscriptionKind.GMAIL.value) or {})
    subscriptions[GoogleSubscriptionKind.GMAIL.value] = {
        "watch_history_id": watch_history_id,
        "topic_name": topic_name,
        "label_ids": label_ids,
        "label_filter_behavior": label_filter_behavior,
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "created_at": existing.get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
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
    now = datetime.now(UTC)
    updated, subscriptions = _subscription_root(metadata)
    existing = dict(subscriptions.get(GoogleSubscriptionKind.CALENDAR.value) or {})
    subscriptions[GoogleSubscriptionKind.CALENDAR.value] = {
        "channel_id": channel_id,
        "resource_id": resource_id,
        "resource_uri": resource_uri,
        "channel_token": channel_token,
        "address": address,
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "created_at": existing.get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
    }
    updated[GOOGLE_WEBHOOK_SUBSCRIPTIONS_KEY] = subscriptions
    return updated
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_metadata.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/webhooks/google tests/service/integrations/webhooks/google/test_metadata.py
git commit -m "feat: add google webhook metadata helpers"
```

## Task 2: Add Google Webhook Payload Types

**Files:**
- Create: `src/service/integrations/webhooks/google/types.py`
- Test: `tests/service/integrations/webhooks/google/test_request_service.py`

- [ ] **Step 1: Write failing payload parser tests**

Create `tests/service/integrations/webhooks/google/test_request_service.py`:

```python
import base64
import json

from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)


def test_gmail_pubsub_notification_decodes_message_data() -> None:
    data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": "seller@example.com", "historyId": "987"}).encode()
    ).decode()

    notification = GoogleGmailPubSubNotification.from_payload(
        {"message": {"messageId": "pubsub-1", "data": data}}
    )

    assert notification is not None
    assert notification.message_id == "pubsub-1"
    assert notification.email_address == "seller@example.com"
    assert notification.history_id == "987"


def test_calendar_channel_notification_reads_required_headers() -> None:
    notification = GoogleCalendarChannelNotification.from_headers(
        {
            "x-goog-channel-id": "channel-1",
            "x-goog-resource-id": "resource-1",
            "x-goog-resource-state": "exists",
            "x-goog-message-number": "2",
            "x-goog-channel-token": "secret",
        }
    )

    assert notification is not None
    assert notification.channel_id == "channel-1"
    assert notification.resource_state == "exists"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_request_service.py -v
```

Expected: FAIL with missing `types.py`.

- [ ] **Step 3: Implement type parsers**

Create `src/service/integrations/webhooks/google/types.py`:

```python
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class GoogleGmailPubSubNotification:
    message_id: str
    email_address: str
    history_id: str
    publish_time: str | None
    subscription: str | None
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GoogleGmailPubSubNotification | None":
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        message_id = message.get("messageId") or message.get("message_id")
        data = message.get("data")
        if not isinstance(message_id, str) or not isinstance(data, str):
            return None
        try:
            decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            gmail_payload = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
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
    ) -> "GoogleCalendarChannelNotification | None":
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
        return cls(
            channel_id=channel_id,
            resource_id=resource_id,
            resource_state=resource_state,
            message_number=message_number,
            resource_uri=lower_headers.get("x-goog-resource-uri"),
            channel_token=lower_headers.get("x-goog-channel-token"),
            channel_expiration=lower_headers.get("x-goog-channel-expiration"),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_request_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/webhooks/google/types.py tests/service/integrations/webhooks/google/test_request_service.py
git commit -m "feat: parse google webhook notifications"
```

## Task 3: Add Thin Google Webhook Routes and Request Service

**Files:**
- Create: `src/api/routers/integrations/google_webhook_routes.py`
- Modify: `src/api/routers/integrations/__init__.py`
- Create: `src/service/integrations/webhooks/google/ingestion_service.py` with a minimal import-safe result/service stub; Task 4 replaces the stub with real ingestion behavior.
- Create: `src/service/integrations/webhooks/google/pubsub_auth.py`
- Create: `src/service/integrations/webhooks/google/request_service.py`
- Test: `tests/api/routers/integrations/test_google_webhook_routes.py`
- Test: `tests/service/integrations/webhooks/google/test_pubsub_auth.py`
- Test: `tests/service/integrations/webhooks/google/test_request_service.py`

- [ ] **Step 1: Write failing route tests**

Create `tests/api/routers/integrations/test_google_webhook_routes.py`:

```python
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.api.fastapi_app import create_app
from src.api.routers.integrations.google_webhook_routes import (
    get_google_webhook_request_service,
)


def test_gmail_webhook_delegates_to_request_service() -> None:
    app = create_app()
    service = AsyncMock()
    app.dependency_overrides[get_google_webhook_request_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        "/users/integrations/google/gmail/webhook"
        "?organisation_id=11111111-1111-1111-1111-111111111111"
        "&integration_id=22222222-2222-2222-2222-222222222222",
        json={"message": {"messageId": "1", "data": "e30="}},
    )

    assert response.status_code == 204
    service.handle_gmail_pubsub.assert_awaited_once()


def test_calendar_webhook_delegates_to_request_service() -> None:
    app = create_app()
    service = AsyncMock()
    app.dependency_overrides[get_google_webhook_request_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        "/users/integrations/google/calendar/webhook"
        "?organisation_id=11111111-1111-1111-1111-111111111111"
        "&integration_id=22222222-2222-2222-2222-222222222222",
        headers={
            "X-Goog-Channel-ID": "channel-1",
            "X-Goog-Resource-ID": "resource-1",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Message-Number": "2",
            "X-Goog-Channel-Token": "secret",
        },
    )

    assert response.status_code == 204
    service.handle_calendar_channel.assert_awaited_once()
```

Extend `tests/service/integrations/webhooks/google/test_request_service.py` with
Gmail auth guardrails:

- `test_gmail_pubsub_requires_valid_oidc_before_ingestion`: pass a fake
  `pubsub_auth_validator` whose `validate` returns `True`, a fake ingestion
  service, and assert Gmail parsing reaches `ingest_gmail`.
- `test_gmail_pubsub_rejects_invalid_oidc_before_payload_trust`: pass a fake
  validator whose `validate` returns `False`, send an otherwise well-shaped
  Pub/Sub body, assert `HTTPException.status_code == 401`, and assert the
  ingestion service was not called.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/api/routers/integrations/test_google_webhook_routes.py tests/service/integrations/webhooks/google/test_pubsub_auth.py -v
```

Expected: FAIL with missing `google_webhook_routes` or `pubsub_auth`.

- [ ] **Step 3: Implement thin route**

Create `src/api/routers/integrations/google_webhook_routes.py`:

```python
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from src.service.integrations.webhooks.google.request_service import (
    GOOGLE_WEBHOOK_MAX_BODY_BYTES,
    GoogleWebhookRequestService,
)

router = APIRouter(prefix="/google", tags=["integrations", "google-webhooks"])


async def get_google_webhook_request_service() -> GoogleWebhookRequestService:
    return GoogleWebhookRequestService()


@router.post("/gmail/webhook")
async def handle_google_gmail_webhook(
    request: Request,
    organisation_id: UUID | None = Query(default=None),
    integration_id: UUID | None = Query(default=None),
    webhook_service: GoogleWebhookRequestService = Depends(get_google_webhook_request_service),
) -> Response:
    await webhook_service.handle_gmail_pubsub(
        request=request,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/calendar/webhook")
async def handle_google_calendar_webhook(
    request: Request,
    organisation_id: UUID | None = Query(default=None),
    integration_id: UUID | None = Query(default=None),
    webhook_service: GoogleWebhookRequestService = Depends(get_google_webhook_request_service),
) -> Response:
    await webhook_service.handle_calendar_channel(
        request=request,
        organisation_id=organisation_id,
        integration_id=integration_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "GOOGLE_WEBHOOK_MAX_BODY_BYTES",
    "get_google_webhook_request_service",
    "router",
]
```

Modify `src/api/routers/integrations/__init__.py`:

```python
from .google_webhook_routes import router as google_webhook_router

integrations_router.include_router(google_webhook_router)

__all__ = [
    "integrations_router",
    "copilot_oauth_router",
    "bot_integration_router",
    "integration_router",
    "hubspot_webhook_router",
    "microsoft_webhook_router",
    "google_webhook_router",
]
```

- [ ] **Step 4: Implement request service**

Create a minimal `src/service/integrations/webhooks/google/ingestion_service.py` before
`request_service.py` imports it. Task 4 expands this file with the real DB,
Redis, dedupe, and enqueue behavior:

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)


@dataclass(slots=True, frozen=True)
class GoogleWebhookIngestionResult:
    valid_count: int
    queued_count: int
    duplicate_count: int
    pending_count: int
    invalid_count: int


class GoogleWebhookIngestionService:
    async def ingest_gmail(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        notification: GoogleGmailPubSubNotification,
    ) -> GoogleWebhookIngestionResult:
        raise NotImplementedError("Google Gmail webhook ingestion is implemented in Task 4")

    async def ingest_calendar(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        notification: GoogleCalendarChannelNotification,
    ) -> GoogleWebhookIngestionResult:
        raise NotImplementedError("Google Calendar webhook ingestion is implemented in Task 4")
```

Create `tests/service/integrations/webhooks/google/test_pubsub_auth.py`:

```python
import pytest

from src.service.integrations.webhooks.google.pubsub_auth import (
    GooglePubSubPushAuthValidator,
)


@pytest.mark.asyncio
async def test_pubsub_auth_rejects_missing_bearer_token() -> None:
    validator = GooglePubSubPushAuthValidator(
        audience="https://api.example.com/users/integrations/google/gmail/webhook",
        expected_service_account_email="gmail-push@project.iam.gserviceaccount.com",
    )

    result = await validator.validate({})

    assert result is False
```

Create `src/service/integrations/webhooks/google/pubsub_auth.py`:

```python
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class GooglePubSubPushAuthValidator:
    def __init__(
        self,
        *,
        audience: str,
        expected_service_account_email: str,
    ) -> None:
        self._audience = audience
        self._expected_service_account_email = expected_service_account_email

    async def validate(self, headers: Mapping[str, str]) -> bool:
        authorization = headers.get("Authorization") or headers.get("authorization") or ""
        if not authorization.startswith("Bearer "):
            return False
        token = authorization.removeprefix("Bearer ").strip()
        if not token or not self._audience or not self._expected_service_account_email:
            return False

        try:
            payload = await asyncio.to_thread(self._verify_token, token)
        except Exception:
            logger.warning("Google Pub/Sub push token verification failed", exc_info=True)
            return False

        return payload.get("email") == self._expected_service_account_email

    def _verify_token(self, token: str) -> dict[str, Any]:
        request = google_requests.Request()
        return dict(
            cast(
                "Mapping[str, Any]",
                id_token.verify_oauth2_token(token, request, self._audience),
            )
        )
```

Create `src/service/integrations/webhooks/google/request_service.py`:

```python
from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status

from src.service.integrations.webhooks.google.ingestion_service import (
    GoogleWebhookIngestionResult,
    GoogleWebhookIngestionService,
)
from src.service.integrations.webhooks.google.pubsub_auth import GooglePubSubPushAuthValidator
from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)

GOOGLE_WEBHOOK_MAX_BODY_BYTES = 256 * 1024


class GoogleWebhookRequestService:
    def __init__(
        self,
        ingestion_service: GoogleWebhookIngestionService | None = None,
        pubsub_auth_validator: GooglePubSubPushAuthValidator | None = None,
    ) -> None:
        self._ingestion_service = ingestion_service or GoogleWebhookIngestionService()
        self._pubsub_auth_validator = pubsub_auth_validator or GooglePubSubPushAuthValidator(
            audience=os.environ.get("GOOGLE_PUBSUB_PUSH_AUDIENCE", ""),
            expected_service_account_email=os.environ.get(
                "GOOGLE_PUBSUB_PUSH_SERVICE_ACCOUNT_EMAIL",
                "",
            ),
        )

    async def handle_gmail_pubsub(
        self,
        *,
        request: Request,
        organisation_id: UUID | None,
        integration_id: UUID | None,
    ) -> GoogleWebhookIngestionResult:
        await self._validate_pubsub_push_auth(request)
        org_id, int_id = self._require_routing_params(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        payload = self._parse_json(await self._read_body(request))
        notification = GoogleGmailPubSubNotification.from_payload(payload)
        if notification is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Gmail Pub/Sub webhook payload",
            )
        return await self._ingestion_service.ingest_gmail(
            organisation_id=org_id,
            integration_id=int_id,
            notification=notification,
        )

    async def handle_calendar_channel(
        self,
        *,
        request: Request,
        organisation_id: UUID | None,
        integration_id: UUID | None,
    ) -> GoogleWebhookIngestionResult:
        org_id, int_id = self._require_routing_params(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        notification = GoogleCalendarChannelNotification.from_headers(request.headers)
        if notification is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Google Calendar webhook headers",
            )
        return await self._ingestion_service.ingest_calendar(
            organisation_id=org_id,
            integration_id=int_id,
            notification=notification,
        )

    async def _validate_pubsub_push_auth(self, request: Request) -> None:
        if await self._pubsub_auth_validator.validate(request.headers):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Gmail Pub/Sub push authentication",
        )

    async def _read_body(self, request: Request) -> bytes:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > GOOGLE_WEBHOOK_MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Google webhook body is too large",
            )
        raw_body = await request.body()
        if len(raw_body) > GOOGLE_WEBHOOK_MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Google webhook body is too large",
            )
        return raw_body

    def _parse_json(self, raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Google webhook JSON payload",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Google webhook JSON payload",
            )
        return payload

    def _require_routing_params(
        self,
        *,
        organisation_id: UUID | None,
        integration_id: UUID | None,
    ) -> tuple[UUID, UUID]:
        if organisation_id is None or integration_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Google webhook routing parameters",
            )
        return organisation_id, integration_id
```

- [ ] **Step 5: Run route and request tests**

Run:

```bash
uv run pytest tests/api/routers/integrations/test_google_webhook_routes.py tests/service/integrations/webhooks/google/test_pubsub_auth.py tests/service/integrations/webhooks/google/test_request_service.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/api/routers/integrations src/service/integrations/webhooks/google/ingestion_service.py src/service/integrations/webhooks/google/pubsub_auth.py src/service/integrations/webhooks/google/request_service.py tests/api/routers/integrations/test_google_webhook_routes.py tests/service/integrations/webhooks/google/test_pubsub_auth.py tests/service/integrations/webhooks/google/test_request_service.py
git commit -m "feat: add google webhook routes"
```

## Task 4: Add Google Webhook Ingestion Service

**Files:**
- Modify: `src/service/integrations/webhooks/google/ingestion_service.py`
- Test: `tests/service/integrations/webhooks/google/test_ingestion_service.py`

- [ ] **Step 1: Write failing ingestion tests**

Create `tests/service/integrations/webhooks/google/test_ingestion_service.py` with four concrete tests. Use the fixtures from `tests/service/integrations/webhooks/microsoft/test_ingestion_service.py`: monkeypatch `acquire_org_async_session`, `get_redis_client`, and `send_integrations_task_async`.

Required assertions:

- `test_gmail_ingestion_rejects_wrong_integration_type`: return `invalid_count == 1` and `queued_count == 0` when the loaded integration is not `IntegrationType.GMAIL`.
- `test_gmail_ingestion_dedupes_pubsub_message_id_and_enqueues_email_sync`: first call returns `queued_count == 1` and records `integrations.sync_email_integration`; second call with same Pub/Sub `message_id` returns `duplicate_count == 1`.
- `test_calendar_ingestion_verifies_channel_id_resource_id_and_token`: matching headers enqueue `integrations.sync_calendar_integration`; mismatched `X-Goog-Channel-Token` returns `invalid_count == 1`.
- `test_calendar_sync_notification_is_acknowledged_without_sync_enqueue`: `resource_state == "sync"` returns `valid_count == 1` and `queued_count == 0`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_ingestion_service.py -v
```

Expected: FAIL because the Task 3 stub raises `NotImplementedError`.

- [ ] **Step 3: Implement ingestion service**

Replace the Task 3 import-safe stub in `src/service/integrations/webhooks/google/ingestion_service.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_org_async_session
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.google.metadata import (
    GoogleSubscriptionKind,
    get_subscription_metadata,
)
from src.service.integrations.webhooks.google.types import (
    GoogleCalendarChannelNotification,
    GoogleGmailPubSubNotification,
)
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)

GOOGLE_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60
GOOGLE_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS = 45
GOOGLE_WEBHOOK_SYNC_COUNTDOWN_SECONDS = 15


@dataclass(slots=True, frozen=True)
class GoogleWebhookIngestionResult:
    valid_count: int
    queued_count: int
    duplicate_count: int
    pending_count: int
    invalid_count: int


class GoogleWebhookIngestionService:
    async def ingest_gmail(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        notification: GoogleGmailPubSubNotification,
    ) -> GoogleWebhookIngestionResult:
        integration = await self._load_integration(
            organisation_id=organisation_id,
            integration_id=integration_id,
            integration_type=IntegrationType.GMAIL,
        )
        metadata = get_subscription_metadata(
            getattr(integration, "connection_metadata", None) if integration else None,
            GoogleSubscriptionKind.GMAIL,
        )
        if integration is None or metadata is None:
            return GoogleWebhookIngestionResult(0, 0, 0, 0, 1)

        if not self._matches_gmail_email(integration, notification.email_address):
            return GoogleWebhookIngestionResult(0, 0, 0, 0, 1)

        if not self._claim_dedupe(f"gmail:{notification.message_id}"):
            return GoogleWebhookIngestionResult(1, 0, 1, 0, 0)
        if not self._claim_sync_pending(organisation_id, integration_id, GoogleSubscriptionKind.GMAIL):
            return GoogleWebhookIngestionResult(1, 0, 0, 1, 0)

        await self._enqueue_sync(
            task_name="integrations.sync_email_integration",
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        return GoogleWebhookIngestionResult(1, 1, 0, 0, 0)

    async def ingest_calendar(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        notification: GoogleCalendarChannelNotification,
    ) -> GoogleWebhookIngestionResult:
        integration = await self._load_integration(
            organisation_id=organisation_id,
            integration_id=integration_id,
            integration_type=IntegrationType.GOOGLE_CALENDAR,
        )
        metadata = get_subscription_metadata(
            getattr(integration, "connection_metadata", None) if integration else None,
            GoogleSubscriptionKind.CALENDAR,
        )
        if integration is None or metadata is None:
            return GoogleWebhookIngestionResult(0, 0, 0, 0, 1)
        if (
            notification.channel_id != metadata.channel_id
            or notification.resource_id != metadata.resource_id
            or notification.channel_token != metadata.channel_token
        ):
            return GoogleWebhookIngestionResult(0, 0, 0, 0, 1)
        if notification.resource_state == "sync":
            return GoogleWebhookIngestionResult(1, 0, 0, 0, 0)

        dedupe_key = f"calendar:{notification.channel_id}:{notification.message_number}"
        if not self._claim_dedupe(dedupe_key):
            return GoogleWebhookIngestionResult(1, 0, 1, 0, 0)
        if not self._claim_sync_pending(
            organisation_id,
            integration_id,
            GoogleSubscriptionKind.CALENDAR,
        ):
            return GoogleWebhookIngestionResult(1, 0, 0, 1, 0)

        await self._enqueue_sync(
            task_name="integrations.sync_calendar_integration",
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        return GoogleWebhookIngestionResult(1, 1, 0, 0, 0)

    async def _load_integration(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        integration_type: IntegrationType,
    ) -> KnowledgeIntegration | None:
        async for session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type) == integration_type.value,
                col(KnowledgeIntegration.is_active).is_(True),
            )
            result = await session.execute(stmt)
            return result.scalars().first()
        return None

    def _matches_gmail_email(
        self,
        integration: KnowledgeIntegration,
        email_address: str,
    ) -> bool:
        candidates = [
            getattr(integration, "integration_email", None),
            (integration.connection_metadata or {}).get("email"),
            (integration.connection_metadata or {}).get("emailAddress"),
            (integration.connection_metadata or {}).get("google_email"),
        ]
        normalized = email_address.lower()
        known = [value.lower() for value in candidates if isinstance(value, str) and value]
        return not known or normalized in known

    def _claim_dedupe(self, token: str) -> bool:
        try:
            return bool(
                get_redis_client().set(
                    f"webhook:google:dedupe:{token}",
                    "1",
                    nx=True,
                    ex=GOOGLE_WEBHOOK_DEDUPE_TTL_SECONDS,
                )
            )
        except Exception:
            logger.debug("Google webhook dedupe claim failed", exc_info=True)
            return True

    def _claim_sync_pending(
        self,
        organisation_id: UUID,
        integration_id: UUID,
        kind: GoogleSubscriptionKind,
    ) -> bool:
        try:
            return bool(
                get_redis_client().set(
                    f"webhook:google:sync:{organisation_id}:{integration_id}:{kind.value}",
                    "1",
                    nx=True,
                    ex=GOOGLE_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS,
                )
            )
        except Exception:
            logger.debug("Google webhook sync pending claim failed", exc_info=True)
            return True

    async def _enqueue_sync(
        self,
        *,
        task_name: str,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> str:
        return await send_integrations_task_async(
            task_name=task_name,
            kwargs={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            },
            queue="integrations_priority",
            routing_key="integrations_priority",
            countdown=GOOGLE_WEBHOOK_SYNC_COUNTDOWN_SECONDS,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_ingestion_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/webhooks/google/ingestion_service.py tests/service/integrations/webhooks/google/test_ingestion_service.py
git commit -m "feat: ingest google webhook notifications"
```

## Task 5: Add Google API POST Helpers

**Files:**
- Modify: `src/workers/integrations_worker/providers/_google_api.py`
- Test: `tests/workers/integrations_worker/providers/test_google_api_token.py`

- [ ] **Step 1: Write failing tests for POST and stop calls**

Extend `tests/workers/integrations_worker/providers/test_google_api_token.py` with:

- `test_gmail_api_post_json_posts_to_gmail_base_url`: use an `httpx.MockTransport`, call `gmail_api_post_json(client, "/gmail/v1/users/me/watch", json_body={"topicName": "projects/proj/topics/gmail"})`, and assert the request URL starts with `https://gmail.googleapis.com/gmail/v1/users/me/watch`.
- `test_calendar_api_post_json_posts_to_calendar_base_url`: use an `httpx.MockTransport`, call `calendar_api_post_json(client, "/calendar/v3/calendars/primary/events/watch", json_body={"id": "channel-1"})`, and assert the request URL starts with `https://www.googleapis.com/calendar/v3/calendars/primary/events/watch`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/test_google_api_token.py -v
```

Expected: FAIL because only GET helper wrappers exist.

- [ ] **Step 3: Add POST helpers**

Modify `src/workers/integrations_worker/providers/_google_api.py`:

```python
async def _google_api_post_json(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    *,
    json_body: dict[str, Any],
    integration_type: IntegrationType | None = None,
) -> dict[str, Any]:
    url = f"{base_url}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.post(url, json=json_body)
        if response.status_code == 403:
            response_text = response.text.lower()
            if (
                "insufficientpermissions" in response_text
                or "insufficient permissions" in response_text
                or "insufficient authentication scopes" in response_text
                or "access_token_scope_insufficient" in response_text
            ):
                raise IntegrationPermissionError(
                    _get_google_permission_message(integration_type),
                    status_code=403,
                    provider="google",
                    endpoint=endpoint,
                    response_summary=response.text,
                )
        response.raise_for_status()
        return response.json() if response.content else {}

    return await _request_with_rate_limit_retry(_do)


async def gmail_api_post_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
) -> dict[str, Any]:
    return await _google_api_post_json(
        client,
        GMAIL_BASE_URL,
        endpoint,
        json_body=json_body,
        integration_type=IntegrationType.GMAIL,
    )


async def calendar_api_post_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
) -> dict[str, Any]:
    return await _google_api_post_json(
        client,
        GOOGLE_CALENDAR_BASE_URL,
        endpoint,
        json_body=json_body,
        integration_type=IntegrationType.GOOGLE_CALENDAR,
    )
```

Add both helper names to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/workers/integrations_worker/providers/test_google_api_token.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/workers/integrations_worker/providers/_google_api.py tests/workers/integrations_worker/providers/test_google_api_token.py
git commit -m "feat: add google api post helpers"
```

## Task 6: Add Google Subscription Service

**Files:**
- Create: `src/service/integrations/webhooks/google/subscription_service.py`
- Test: `tests/service/integrations/webhooks/google/test_subscription_service.py`

- [ ] **Step 1: Write failing subscription service tests**

Create `tests/service/integrations/webhooks/google/test_subscription_service.py` with these tests:

- `test_gmail_subscription_uses_users_watch_and_stores_expiration`: monkeypatch `GOOGLE_GMAIL_PUBSUB_TOPIC`, `get_access_token`, and `gmail_api_post_json`; assert the returned metadata contains `watch_history_id` and an UTC `expires_at`.
- `test_calendar_subscription_uses_events_watch_and_stores_resource_id`: monkeypatch `GOOGLE_WEBHOOK_BASE_URL`, `get_access_token`, and `calendar_api_post_json`; assert returned metadata contains `channel_id`, `resource_id`, `resource_uri`, `channel_token`, and callback `address`.
- `test_existing_unexpired_subscription_is_reused`: seed metadata with `expires_at` more than two days in the future and assert neither Google API helper is called.
- `test_calendar_payload_uses_public_base_url_and_routing_params`: assert the payload address contains `/users/integrations/google/calendar/webhook`, `organisation_id=11111111-1111-1111-1111-111111111111`, and `integration_id=22222222-2222-2222-2222-222222222222`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_subscription_service.py -v
```

Expected: FAIL with missing subscription service.

- [ ] **Step 3: Implement subscription service**

Create `src/service/integrations/webhooks/google/subscription_service.py`:

```python
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from sqlalchemy.orm import attributes

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.providers.token_resolution import (
    IntegrationTokenInfo,
    get_access_token,
)
from src.service.integrations.webhooks.google.metadata import (
    GoogleSubscriptionKind,
    get_subscription_metadata,
    set_calendar_subscription_metadata,
    set_gmail_subscription_metadata,
)
from src.workers.integrations_worker.providers._google_api import (
    calendar_api_post_json,
    gmail_api_post_json,
)

GOOGLE_GMAIL_RENEWAL_BUFFER = timedelta(days=1)
GOOGLE_CALENDAR_RENEWAL_BUFFER = timedelta(days=1)
GOOGLE_CALENDAR_REQUESTED_CHANNEL_LIFETIME = timedelta(days=6)

_INTEGRATION_TYPE_BY_KIND = {
    GoogleSubscriptionKind.GMAIL: IntegrationType.GMAIL,
    GoogleSubscriptionKind.CALENDAR: IntegrationType.GOOGLE_CALENDAR,
}


class GoogleSubscriptionService:
    async def ensure_subscription_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        kind: GoogleSubscriptionKind,
    ) -> dict:
        self._validate_kind(integration, kind)
        existing = get_subscription_metadata(integration.connection_metadata, kind)
        buffer = (
            GOOGLE_GMAIL_RENEWAL_BUFFER
            if kind == GoogleSubscriptionKind.GMAIL
            else GOOGLE_CALENDAR_RENEWAL_BUFFER
        )
        if existing and existing.expires_at > datetime.now(UTC) + buffer:
            return dict(integration.connection_metadata or {})
        if kind == GoogleSubscriptionKind.GMAIL:
            return await self._create_gmail_watch_metadata(
                organisation_id=organisation_id,
                integration=integration,
            )
        return await self._create_calendar_channel_metadata(
            organisation_id=organisation_id,
            integration=integration,
        )

    async def _create_gmail_watch_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
    ) -> dict:
        topic_name = os.environ.get("GOOGLE_GMAIL_PUBSUB_TOPIC")
        if not topic_name:
            raise RuntimeError("GOOGLE_GMAIL_PUBSUB_TOPIC is required")
        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        access_token = await get_access_token(token_info)
        payload = {
            "topicName": topic_name,
            "labelIds": ["INBOX"],
            "labelFilterBehavior": "INCLUDE",
        }
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            response = await gmail_api_post_json(
                client,
                "/gmail/v1/users/me/watch",
                json_body=payload,
            )
        history_id = response.get("historyId")
        expiration = response.get("expiration")
        if not isinstance(history_id, str) or not isinstance(expiration, str):
            raise RuntimeError("Gmail watch response missing historyId or expiration")
        return set_gmail_subscription_metadata(
            integration.connection_metadata,
            watch_history_id=history_id,
            topic_name=topic_name,
            label_ids=["INBOX"],
            label_filter_behavior="INCLUDE",
            expires_at=_parse_epoch_millis(expiration),
        )

    async def _create_calendar_channel_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
    ) -> dict:
        channel_id = str(uuid4())
        channel_token = secrets.token_urlsafe(32)
        address = self._calendar_callback_url(
            organisation_id=organisation_id,
            integration_id=integration.integration_id,
        )
        expiration = datetime.now(UTC) + GOOGLE_CALENDAR_REQUESTED_CHANNEL_LIFETIME
        payload = {
            "id": channel_id,
            "type": "web_hook",
            "address": address,
            "token": channel_token,
            "expiration": str(int(expiration.timestamp() * 1000)),
        }
        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        access_token = await get_access_token(token_info)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            response = await calendar_api_post_json(
                client,
                "/calendar/v3/calendars/primary/events/watch",
                json_body=payload,
            )
        resource_id = response.get("resourceId")
        resource_uri = response.get("resourceUri")
        response_expiration = response.get("expiration") or payload["expiration"]
        if not isinstance(resource_id, str) or not isinstance(resource_uri, str):
            raise RuntimeError("Calendar watch response missing resourceId or resourceUri")
        return set_calendar_subscription_metadata(
            integration.connection_metadata,
            channel_id=channel_id,
            resource_id=resource_id,
            resource_uri=resource_uri,
            channel_token=channel_token,
            address=address,
            expires_at=_parse_epoch_millis(str(response_expiration)),
        )

    def _calendar_callback_url(self, *, organisation_id: UUID, integration_id: UUID) -> str:
        raw_base_url = os.environ.get("GOOGLE_WEBHOOK_BASE_URL")
        if not raw_base_url:
            raise RuntimeError("GOOGLE_WEBHOOK_BASE_URL is required")
        query = urlencode(
            {
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }
        )
        return f"{raw_base_url.rstrip('/')}/users/integrations/google/calendar/webhook?{query}"

    def _validate_kind(
        self,
        integration: KnowledgeIntegration,
        kind: GoogleSubscriptionKind,
    ) -> None:
        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        if integration_type != _INTEGRATION_TYPE_BY_KIND[kind]:
            raise ValueError(f"Cannot create {kind.value} subscription for {integration_type.value}")


def persist_subscription_metadata(integration: KnowledgeIntegration, metadata: dict) -> None:
    integration.connection_metadata = metadata
    attributes.flag_modified(integration, "connection_metadata")


def _parse_epoch_millis(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_subscription_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/webhooks/google/subscription_service.py tests/service/integrations/webhooks/google/test_subscription_service.py
git commit -m "feat: manage google webhook subscriptions"
```

## Task 7: Add Google Subscription Task Service

**Files:**
- Create: `src/service/integrations/webhooks/google/subscription_task_service.py`
- Test: `tests/service/integrations/webhooks/google/test_subscription_task_service.py`

- [ ] **Step 1: Write failing task service tests**

Create tests covering the same service contract as `tests/service/integrations/webhooks/microsoft/test_subscription_task_service.py`:

- `test_ensure_subscription_loads_detached_snapshot_and_persists_after_api_call`: fake org sessions, return a connected Gmail integration, fake `GoogleSubscriptionService.ensure_subscription_metadata`, and assert the persisted row is updated after the fake API call.
- `test_dispatch_all_subscription_ensures_fans_out_by_integration`: fake master/org sessions and assert the dispatcher receives one callback per active Gmail/Google Calendar integration.
- `test_kind_for_integration_type_maps_only_google_email_and_calendar`: assert Gmail maps to `GoogleSubscriptionKind.GMAIL`, Google Calendar maps to `GoogleSubscriptionKind.CALENDAR`, and Outlook integrations return `None`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_subscription_task_service.py -v
```

Expected: FAIL with missing task service.

- [ ] **Step 3: Implement task service**

Create `src/service/integrations/webhooks/google/subscription_task_service.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.service.integrations.webhooks.google.metadata import GoogleSubscriptionKind
from src.service.integrations.webhooks.google.subscription_service import (
    GoogleSubscriptionService,
    persist_subscription_metadata,
)

GOOGLE_WEBHOOK_INTEGRATION_TYPES = (
    IntegrationType.GMAIL,
    IntegrationType.GOOGLE_CALENDAR,
)


def kind_for_integration_type(integration_type: IntegrationType) -> GoogleSubscriptionKind | None:
    if integration_type == IntegrationType.GMAIL:
        return GoogleSubscriptionKind.GMAIL
    if integration_type == IntegrationType.GOOGLE_CALENDAR:
        return GoogleSubscriptionKind.CALENDAR
    return None


class GoogleSubscriptionTaskService:
    def __init__(self, subscription_service: GoogleSubscriptionService | None = None) -> None:
        self._subscription_service = subscription_service or GoogleSubscriptionService()

    async def ensure_subscription(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> dict[str, Any]:
        integration_snapshot = await self._load_integration_snapshot(
            organisation_id=organisation_id,
            integration_id=integration_id,
        )
        if integration_snapshot is None:
            return {
                "status": "skipped",
                "reason": "integration_not_found",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }
        kind = self._kind_for_integration(integration_snapshot)
        if kind is None:
            return {
                "status": "skipped",
                "reason": "unsupported_integration_type",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }
        metadata = await self._subscription_service.ensure_subscription_metadata(
            organisation_id=organisation_id,
            integration=integration_snapshot,
            kind=kind,
        )
        persisted = await self._persist_metadata(
            organisation_id=organisation_id,
            integration_id=integration_id,
            metadata=metadata,
        )
        if not persisted:
            return {
                "status": "skipped",
                "reason": "integration_not_found_at_persist",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }
        return {
            "status": "success",
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "kind": kind.value,
        }

    async def dispatch_all_subscription_ensures(
        self,
        *,
        dispatch_subscription_ensure: Callable[[UUID, UUID], None],
    ) -> dict[str, Any]:
        organisation_ids: list[UUID] = []
        async for session in acquire_master_async_session():
            statement = select(col(Organisation.organisation_id)).where(
                col(Organisation.is_active).is_(True),
                col(Organisation.status) == OrganisationStatus.APPROVED,
            )
            result = await session.execute(statement)
            organisation_ids = list(result.scalars().all())

        integrations_dispatched = 0
        for organisation_id in organisation_ids:
            integration_ids = await self._load_org_integration_ids(organisation_id)
            for integration_id in integration_ids:
                dispatch_subscription_ensure(organisation_id, integration_id)
                integrations_dispatched += 1

        return {
            "status": "success",
            "orgs_checked": len(organisation_ids),
            "integrations_dispatched": integrations_dispatched,
            "completed_at": datetime.now(UTC).isoformat(),
        }

    async def _load_integration_snapshot(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> KnowledgeIntegration | None:
        async for session in acquire_org_async_session(organisation_id):
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(GOOGLE_WEBHOOK_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            integration = result.scalar_one_or_none()
            if integration is None:
                return None
            return self._snapshot_integration(integration)
        return None

    async def _load_org_integration_ids(self, organisation_id: UUID) -> list[UUID]:
        async for session in acquire_org_async_session(organisation_id):
            statement = select(col(KnowledgeIntegration.integration_id)).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_type).in_(GOOGLE_WEBHOOK_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            return list(result.scalars().all())
        return []

    async def _persist_metadata(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        metadata: dict[str, Any],
    ) -> bool:
        async for session in acquire_org_async_session(organisation_id):
            statement = select(KnowledgeIntegration).where(
                col(KnowledgeIntegration.organisation_id) == organisation_id,
                col(KnowledgeIntegration.integration_id) == integration_id,
                col(KnowledgeIntegration.integration_type).in_(GOOGLE_WEBHOOK_INTEGRATION_TYPES),
                col(KnowledgeIntegration.is_active).is_(True),
                col(KnowledgeIntegration.connection_id).isnot(None),
            )
            result = await session.execute(statement)
            integration = result.scalar_one_or_none()
            if integration is None:
                return False
            persist_subscription_metadata(integration, metadata)
            session.add(integration)
            await session.commit()
            return True
        return False

    def _kind_for_integration(self, integration: KnowledgeIntegration) -> GoogleSubscriptionKind | None:
        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        return kind_for_integration_type(integration_type)

    def _snapshot_integration(self, integration: KnowledgeIntegration) -> KnowledgeIntegration:
        return KnowledgeIntegration.model_validate(
            {
                "integration_id": integration.integration_id,
                "organisation_id": integration.organisation_id,
                "created_by_user_id": integration.created_by_user_id,
                "integration_type": integration.integration_type,
                "name": integration.name,
                "credentials": dict(integration.credentials or {}),
                "connection_id": integration.connection_id,
                "access_token_expires_at": integration.access_token_expires_at,
                "scopes": list(integration.scopes or []),
                "is_active": integration.is_active,
                "connection_metadata": dict(integration.connection_metadata or {}),
            }
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/google/test_subscription_task_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/webhooks/google/subscription_task_service.py tests/service/integrations/webhooks/google/test_subscription_task_service.py
git commit -m "feat: orchestrate google webhook subscription tasks"
```

## Task 8: Add Google Celery Subscription Tasks

**Files:**
- Create: `src/workers/integrations_worker/tasks/google_subscription_tasks.py`
- Modify: `src/workers/integrations_worker/tasks/__init__.py`
- Modify: `src/workers/integrations_worker/config.py`
- Modify: `src/workers/integrations_worker/scheduled_jobs.py`
- Test: `tests/workers/integrations_worker/test_google_subscription_tasks.py`

- [ ] **Step 1: Write failing Celery task tests**

Create `tests/workers/integrations_worker/test_google_subscription_tasks.py` covering:

- `test_ensure_google_subscription_task_returns_success`: monkeypatch `GoogleSubscriptionTaskService.ensure_subscription` to return `{"status": "success"}` and assert task output preserves that status.
- `test_renew_google_subscriptions_all_dispatches_priority_ensure`: monkeypatch `GoogleSubscriptionTaskService.dispatch_all_subscription_ensures`, call the dispatcher callback with one org/integration id pair, and assert `ensure_google_subscription_task.apply_async` uses `queue="integrations_priority"`.
- `test_google_subscription_renewal_is_registered_as_scheduled_job`: import `get_scheduled_job("renew-google-subscriptions")` and assert it returns a `ScheduledJob` with `task_name == "integrations.renew_google_subscriptions_all"` and a nonzero `max_lag_seconds`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_google_subscription_tasks.py -v
```

Expected: FAIL with missing task module.

- [ ] **Step 3: Implement Celery task adapter**

Create `src/workers/integrations_worker/tasks/google_subscription_tasks.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.google.subscription_task_service import (
    GoogleSubscriptionTaskService,
    kind_for_integration_type,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

ENSURE_TASK = "integrations.ensure_google_subscription"
RENEW_ALL_TASK = "integrations.renew_google_subscriptions_all"

_ENSURE_LIMITS = TASK_LIMITS[ENSURE_TASK]
_RENEW_ALL_LIMITS = TASK_LIMITS[RENEW_ALL_TASK]
_kind_for_integration_type = kind_for_integration_type


@app.task(
    name=ENSURE_TASK,
    bind=True,
    max_retries=_ENSURE_LIMITS["retries"],
    default_retry_delay=_ENSURE_LIMITS.get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=_ENSURE_LIMITS["soft"],
    time_limit=_ENSURE_LIMITS["hard"],
)
def ensure_google_subscription_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    try:
        result = run_async(
            GoogleSubscriptionTaskService().ensure_subscription(
                organisation_id=UUID(organisation_id),
                integration_id=UUID(integration_id),
            )
        )
        result["started_at"] = started_at
        result["completed_at"] = datetime.now(UTC).isoformat()
        return result
    except SoftTimeLimitExceeded:
        return {
            "status": "timed_out",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        logger.error("Google subscription ensure failed", exc_info=True)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "integration_id": integration_id,
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


@app.task(
    name=RENEW_ALL_TASK,
    bind=False,
    max_retries=_RENEW_ALL_LIMITS["retries"],
    acks_late=True,
    soft_time_limit=_RENEW_ALL_LIMITS["soft"],
    time_limit=_RENEW_ALL_LIMITS["hard"],
)
def renew_google_subscriptions_all_task() -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()

    def dispatch_subscription_ensure(organisation_id: UUID, integration_id: UUID) -> None:
        ensure_google_subscription_task.apply_async(
            kwargs={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            },
            queue="integrations_priority",
            routing_key="integrations_priority",
        )

    try:
        result = run_async(
            GoogleSubscriptionTaskService().dispatch_all_subscription_ensures(
                dispatch_subscription_ensure=dispatch_subscription_ensure,
            )
        )
        result["started_at"] = started_at
        result["completed_at"] = datetime.now(UTC).isoformat()
        return result
    except Exception as exc:
        logger.error("Google subscription renewal dispatch failed", exc_info=True)
        return {
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


ensure_google_subscription = ensure_google_subscription_task
renew_google_subscriptions_all = renew_google_subscriptions_all_task
```

- [ ] **Step 4: Register task imports**

Modify `src/workers/integrations_worker/tasks/__init__.py`:

```python
from src.workers.integrations_worker.tasks.google_subscription_tasks import (
    ensure_google_subscription_task,
    renew_google_subscriptions_all_task,
)
```

Add both task names to `__all__`.

- [ ] **Step 5: Add task limits and routes**

Modify `src/workers/integrations_worker/config.py`:

```python
"integrations.ensure_google_subscription": {
    "soft": 120,
    "hard": 180,
    "retries": 3,
    "retry_delay": 60,
},
"integrations.renew_google_subscriptions_all": {
    "soft": 120,
    "hard": 180,
    "retries": 0,
},
```

Add to task routes:

```python
"integrations.ensure_google_subscription": {
    "queue": "integrations_priority",
    "routing_key": "integrations_priority",
},
"integrations.renew_google_subscriptions_all": {
    "queue": "integrations",
    "routing_key": "integrations",
},
```

- [ ] **Step 6: Add scheduled renewal**

Modify `src/workers/integrations_worker/scheduled_jobs.py` to schedule daily renewal:

```python
"renew-google-subscriptions": ScheduledJob(
    name="renew-google-subscriptions",
    task_name="integrations.renew_google_subscriptions_all",
    schedule="0 2 * * *",
    max_lag_seconds=60 * 60 * 30,
),
```

Use the existing `ScheduledJob(...)` shape in that file. `cron_dispatch` reads
attributes such as `job.task_name`, `job.queue`, and `job.kwargs`, so do not add
this as a raw dict.

- [ ] **Step 7: Run tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_google_subscription_tasks.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/workers/integrations_worker/tasks src/workers/integrations_worker/config.py src/workers/integrations_worker/scheduled_jobs.py tests/workers/integrations_worker/test_google_subscription_tasks.py
git commit -m "feat: renew google webhook subscriptions"
```

## Task 9: Trigger Google Subscription Ensure After Connect

**Files:**
- Modify: `src/service/integrations/initial_sync_service.py`
- Test: `tests/service/integrations/test_initial_sync_service.py`

- [ ] **Step 1: Write failing initial sync tests**

Add tests named `test_gmail_connect_triggers_sync_and_google_subscription` and `test_google_calendar_connect_triggers_sync_and_google_subscription`. Each should monkeypatch `send_integrations_task`, create a `KnowledgeIntegration` with the relevant `IntegrationType`, call `InitialSyncService().trigger_initial_sync(organisation_id, integration)`, and assert the task dispatch order below.

Each test should assert task dispatch order:

```python
[
    "integrations.sync_email_integration",
    "integrations.ensure_google_subscription",
]
```

for Gmail, and:

```python
[
    "integrations.sync_calendar_integration",
    "integrations.ensure_google_subscription",
]
```

for Google Calendar.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/service/integrations/test_initial_sync_service.py -v
```

Expected: FAIL because Google subscription ensure is not dispatched.

- [ ] **Step 3: Update initial sync service**

Modify `src/service/integrations/initial_sync_service.py`:

```python
GOOGLE_WEBHOOK_INTEGRATION_TYPES = {
    IntegrationType.GMAIL,
    IntegrationType.GOOGLE_CALENDAR,
}
```

In the calendar branch:

```python
if integration_type in self.CALENDAR_INTEGRATION_TYPES:
    self._trigger_calendar_sync(org_id_str, int_id_str)
    if integration_type in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
        self._trigger_microsoft_graph_subscription_ensure(org_id_str, int_id_str)
    if integration_type in GOOGLE_WEBHOOK_INTEGRATION_TYPES:
        self._trigger_google_subscription_ensure(org_id_str, int_id_str)
    return
```

In the email branch:

```python
if integration_type in self.EMAIL_INTEGRATION_TYPES:
    self._trigger_email_sync(org_id_str, int_id_str)
    if integration_type in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
        self._trigger_microsoft_graph_subscription_ensure(org_id_str, int_id_str)
    if integration_type in GOOGLE_WEBHOOK_INTEGRATION_TYPES:
        self._trigger_google_subscription_ensure(org_id_str, int_id_str)
    return
```

Add method:

```python
def _trigger_google_subscription_ensure(
    self,
    org_id_str: str,
    int_id_str: str,
) -> None:
    send_integrations_task(
        task_name="integrations.ensure_google_subscription",
        kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
        queue="integrations_priority",
        routing_key="integrations_priority",
    )
    logger.info("Triggered Google subscription ensure: integration=%s", int_id_str)
```

- [ ] **Step 4: Run tests**

Run:

```bash
uv run pytest tests/service/integrations/test_initial_sync_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/initial_sync_service.py tests/service/integrations/test_initial_sync_service.py
git commit -m "feat: ensure google webhooks after connect"
```

## Task 10: Documentation and Final Verification

**Files:**
- Create: `docs/Technical Documentation/integrations/google-webhook-flow.md`
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`

- [ ] **Step 1: Add Google webhook flow doc**

Create `docs/Technical Documentation/integrations/google-webhook-flow.md`:

```markdown
# Google Webhook Flow

This document defines service boundaries for Gmail and Google Calendar webhook
triggers.

## Principle

Google webhooks are triggers only. Webhook payloads and headers must not write
email or calendar data directly. After request validation and stored subscription
verification, the backend schedules the existing incremental sync task.

## Gmail

Gmail publishes mailbox updates to Cloud Pub/Sub. Pub/Sub push calls:

```text
/users/integrations/google/gmail/webhook?organisation_id=11111111-1111-1111-1111-111111111111&integration_id=22222222-2222-2222-2222-222222222222
```

The handler first validates the Pub/Sub push OIDC bearer token, then decodes
`message.data`, validates the org-scoped integration, dedupes `messageId`, and
enqueues `integrations.sync_email_integration`.

## Google Calendar

Calendar event channels call:

```text
/users/integrations/google/calendar/webhook?organisation_id=11111111-1111-1111-1111-111111111111&integration_id=22222222-2222-2222-2222-222222222222
```

The handler verifies `X-Goog-Channel-ID`, `X-Goog-Resource-ID`, and
`X-Goog-Channel-Token` against stored metadata. `sync` notifications are
acknowledged without enqueueing a data sync. `exists` notifications enqueue
`integrations.sync_calendar_integration`.

## Reliability

Scheduled/manual sync remains the correctness backstop because provider
notifications can be delayed, dropped, retried, or delivered during subscription
renewal overlap windows.
```

- [ ] **Step 2: Update provider sync docs**

Add a Google section to `docs/Technical Documentation/integrations/sync-by-provider.md`:

```markdown
### Google Webhooks

Gmail and Google Calendar use provider notifications only as low-latency
triggers. The webhook paths validate stored subscription metadata and schedule
the existing single-integration sync tasks:

- Gmail: `integrations.sync_email_integration`
- Google Calendar: `integrations.sync_calendar_integration`

Gmail watches are renewed by `integrations.renew_google_subscriptions_all` via
Gmail `users.watch` and Cloud Pub/Sub push delivery. Google Calendar channels
are replaced near expiration by the same renewal task using Calendar
`events.watch`.

The sync task remains the only writer for provider data. Gmail continues to use
Gmail history IDs in the email sync provider path. Google Calendar continues to
use `connection_metadata.calendar_delta_link`.
```

- [ ] **Step 3: Run targeted tests**

Run:

```bash
uv run pytest tests/api/routers/integrations/test_google_webhook_routes.py tests/service/integrations/webhooks/google tests/workers/integrations_worker/test_google_subscription_tasks.py tests/service/integrations/test_initial_sync_service.py tests/workers/integrations_worker/providers/test_google_api_token.py -v
```

Expected: PASS.

- [ ] **Step 4: Run lint on touched code**

Run:

```bash
uv run ruff check src/api/routers/integrations/google_webhook_routes.py src/service/integrations/webhooks/google src/workers/integrations_worker/tasks/google_subscription_tasks.py src/workers/integrations_worker/providers/_google_api.py tests/api/routers/integrations/test_google_webhook_routes.py tests/service/integrations/webhooks/google tests/workers/integrations_worker/test_google_subscription_tasks.py
```

Expected: PASS.

- [ ] **Step 5: Run type checks on touched source**

Run:

```bash
uv run mypy src/api/routers/integrations/google_webhook_routes.py src/service/integrations/webhooks/google src/workers/integrations_worker/tasks/google_subscription_tasks.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/Technical\ Documentation/integrations/google-webhook-flow.md docs/Technical\ Documentation/integrations/sync-by-provider.md
git commit -m "docs: document google webhook sync triggers"
```

## Runtime Configuration

Required runtime env:

```bash
GOOGLE_WEBHOOK_BASE_URL=https://<public-api-host>
GOOGLE_GMAIL_PUBSUB_TOPIC=projects/<project-id>/topics/<topic-name>
GOOGLE_PUBSUB_PUSH_AUDIENCE=https://<public-api-host>/users/integrations/google/gmail/webhook
GOOGLE_PUBSUB_PUSH_SERVICE_ACCOUNT_EMAIL=<pubsub-push-service-account>@<project-id>.iam.gserviceaccount.com
```

Required Google Cloud setup:

- Create a Pub/Sub topic for Gmail notifications.
- Grant `gmail-api-push@system.gserviceaccount.com` publish permission on that topic.
- Create a dedicated service account for Pub/Sub push authentication, grant it
  only the permissions required by Pub/Sub to mint push OIDC tokens, and set it
  as the push subscription's OIDC service account.
- Create a Pub/Sub push subscription pointing to:

```text
https://<public-api-host>/users/integrations/google/gmail/webhook?organisation_id=11111111-1111-1111-1111-111111111111&integration_id=22222222-2222-2222-2222-222222222222
```

The push subscription must send an OIDC token whose audience matches
`GOOGLE_PUBSUB_PUSH_AUDIENCE`. The API must reject missing or invalid bearer
tokens before decoding `message.data` or using `organisation_id`,
`integration_id`, or Gmail `emailAddress`.

Important: one static Pub/Sub push subscription cannot include per-user `organisation_id` and `integration_id` if all Gmail watches publish to the same topic. Therefore v1 must choose one of these two production-safe options before implementation:

1. **Recommended v1:** one Pub/Sub push subscription per Gmail integration, each with the routing query params in the push endpoint.
2. Shared topic/subscription plus server-side lookup by decoded `emailAddress`, with strict uniqueness checks per active organisation integration.

Use option 1 unless Google Cloud Pub/Sub subscription count/cost limits make it unacceptable for our expected tenant volume. If using option 2, update Task 3 and Task 4 to make Gmail `organisation_id` and `integration_id` optional on the route and resolve the integration by decoded `emailAddress` plus active Gmail integration ownership.

## Rollout Checks

- Connect or reconnect a Gmail integration and confirm `google_webhook_subscriptions.gmail` is stored with `watch_history_id` and `expires_at`.
- Connect or reconnect a Google Calendar integration and confirm `google_webhook_subscriptions.calendar` is stored with `channel_id`, `resource_id`, and `channel_token`.
- Send a Gmail test email to the connected inbox and confirm one `integrations.sync_email_integration` task is enqueued.
- Create/update/cancel a primary-calendar event and confirm one `integrations.sync_calendar_integration` task is enqueued.
- Confirm duplicate Pub/Sub `messageId` and repeated Calendar `X-Goog-Message-Number` do not create duplicate sync fanout.
- Confirm daily renewal dispatches `integrations.ensure_google_subscription` for active Gmail and Google Calendar integrations only.
- Confirm manual scheduled sync still works when notification delivery is disabled.

## Self-Review

Spec coverage:

- Keeps Microsoft implementation structure: route, request service, ingestion service, subscription service, subscription task service, Celery adapter.
- Handles Gmail production mechanics via Cloud Pub/Sub and Gmail `users.watch`.
- Handles Calendar production mechanics via Calendar API channel `watch`.
- Keeps provider data writes in existing sync tasks only.
- Enforces tenant-scoped lookups and stored metadata validation.
- Includes renewal, post-connect ensure, docs, tests, lint, and type checks.

Placeholder scan:

- No deferred implementation placeholders remain.
- The Pub/Sub routing decision is called out as a required explicit production choice because Gmail's provider shape differs from Microsoft and Calendar.

Type consistency:

- `GoogleSubscriptionKind.GMAIL` maps to `IntegrationType.GMAIL`.
- `GoogleSubscriptionKind.CALENDAR` maps to `IntegrationType.GOOGLE_CALENDAR`.
- Both sync enqueue paths pass `organisation_id` and `integration_id` to existing Tier 3 sync tasks.
