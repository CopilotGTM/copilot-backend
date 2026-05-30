# Microsoft Graph Webhook Incremental Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Microsoft Graph webhook support for Outlook Calendar and Outlook Email so verified Microsoft notifications schedule the existing incremental sync tasks.

**Architecture:** Microsoft Graph webhooks are a lightweight trigger only. The public route validates Microsoft challenge and notification requests, the ingestion service verifies stored subscription state and debounces bursts, and Celery runs the existing `integrations.sync_calendar_integration` or `integrations.sync_email_integration` tasks. Subscription creation and renewal are stored in `KnowledgeIntegration.connection_metadata` so webhook routing stays tenant-safe.

**Tech Stack:** FastAPI, Celery, Redis, SQLModel, Microsoft Graph `/subscriptions`, existing custom OAuth token resolution, existing integrations worker queues.

**Implementation amendment:** Scheduled webhook renewal now uses integration-level fan-out. The scheduled task is `integrations.renew_microsoft_graph_subscriptions_all`; it scans active approved organisations for Outlook Calendar/Email integrations and dispatches `integrations.ensure_microsoft_graph_subscription` with `organisation_id` and `integration_id` for each integration. The earlier org-level renewal task shape in this historical plan is superseded and should not be implemented.

---

## Scope

Build v1 for:

- `IntegrationType.OUTLOOK_CALENDAR`
- `IntegrationType.OUTLOOK_EMAIL`

Do not implement Teams webhooks in this plan.

Keep scheduled polling. Webhooks lower latency, while scheduled sync remains the correctness backstop for missed notifications, expired subscriptions, and provider delivery gaps.

## Existing convergence points

Use these existing tasks. Do not build a new Microsoft sync pipeline.

- Calendar task: `integrations.sync_calendar_integration`
- Email task: `integrations.sync_email_integration`
- Calendar cursor: `KnowledgeIntegration.connection_metadata["calendar_delta_link"]`
- Email cursors: `KnowledgeIntegration.connection_metadata["email_folder_delta_links"]`
- Celery priority queue: `integrations_priority`

## Files

- Create: `src/api/routers/integrations/microsoft_webhook_routes.py`
- Modify: `src/api/routers/integrations/__init__.py`
- Create: `src/service/integrations/webhooks/microsoft/__init__.py`
- Create: `src/service/integrations/webhooks/microsoft/types.py`
- Create: `src/service/integrations/webhooks/microsoft/metadata.py`
- Create: `src/service/integrations/webhooks/microsoft/ingestion_service.py`
- Create: `src/service/integrations/webhooks/microsoft/subscription_service.py`
- Modify: `src/workers/integrations_worker/providers/_microsoft_graph.py`
- Create: `src/workers/integrations_worker/tasks/microsoft_graph_subscription_tasks.py`
- Modify: `src/workers/integrations_worker/tasks/__init__.py`
- Modify: `src/workers/integrations_worker/config.py`
- Modify: `src/workers/integrations_worker/scheduled_jobs.py`
- Modify: `src/service/integrations/initial_sync_service.py`
- Create: `tests/api/routers/integrations/test_microsoft_webhook_routes.py`
- Create: `tests/service/integrations/webhooks/microsoft/test_ingestion_service.py`
- Create: `tests/service/integrations/webhooks/microsoft/test_subscription_service.py`
- Create: `tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py`
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`

## Metadata contract

Store webhook subscriptions inside each Microsoft `KnowledgeIntegration` row:

```python
MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY = "microsoft_graph_subscriptions"

{
    "microsoft_graph_subscriptions": {
        "calendar": {
            "subscription_id": "graph-subscription-id",
            "client_state": "secret-created-by-us",
            "resource": "/me/events",
            "change_type": "created,updated,deleted",
            "expires_at": "2026-06-03T12:00:00Z",
            "notification_url": "https://api.example.com/users/integrations/microsoft/webhook?organisation_id=...&integration_id=...&kind=calendar",
            "lifecycle_notification_url": "https://api.example.com/users/integrations/microsoft/webhook/lifecycle?organisation_id=...&integration_id=...&kind=calendar",
            "created_at": "2026-05-28T12:00:00Z",
            "updated_at": "2026-05-28T12:00:00Z"
        },
        "email": {
            "subscription_id": "graph-subscription-id",
            "client_state": "secret-created-by-us",
            "resource": "/me/messages",
            "change_type": "created,updated,deleted",
            "expires_at": "2026-06-03T12:00:00Z",
            "notification_url": "https://api.example.com/users/integrations/microsoft/webhook?organisation_id=...&integration_id=...&kind=email",
            "lifecycle_notification_url": "https://api.example.com/users/integrations/microsoft/webhook/lifecycle?organisation_id=...&integration_id=...&kind=email",
            "created_at": "2026-05-28T12:00:00Z",
            "updated_at": "2026-05-28T12:00:00Z"
        }
    }
}
```

Use `/me/messages` for email v1. It may notify for folders we do not monitor, but the existing email sync still only processes monitored folders. This keeps v1 simple and avoids creating one subscription per folder.

## Task 1: Add typed Microsoft webhook helpers

**Files:**
- Create: `src/service/integrations/webhooks/microsoft/__init__.py`
- Create: `src/service/integrations/webhooks/microsoft/types.py`
- Create: `src/service/integrations/webhooks/microsoft/metadata.py`
- Test: `tests/service/integrations/webhooks/microsoft/test_subscription_service.py`

- [ ] **Step 1: Write metadata helper tests**

Add these tests to `tests/service/integrations/webhooks/microsoft/test_subscription_service.py`:

```python
from datetime import UTC, datetime

from src.service.integrations.webhooks.microsoft.metadata import (
    MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY,
    MicrosoftSubscriptionKind,
    get_subscription_metadata,
    set_subscription_metadata,
)


def test_set_and_get_subscription_metadata_round_trips_existing_metadata() -> None:
    metadata = {"existing": {"value": 1}}

    updated = set_subscription_metadata(
        metadata,
        kind=MicrosoftSubscriptionKind.CALENDAR,
        subscription_id="sub-calendar",
        client_state="secret",
        resource="/me/events",
        change_type="created,updated,deleted",
        expires_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )

    assert updated["existing"] == {"value": 1}
    stored = updated[MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY]["calendar"]
    assert stored["subscription_id"] == "sub-calendar"
    assert stored["client_state"] == "secret"
    assert stored["resource"] == "/me/events"
    assert stored["expires_at"] == "2026-06-03T12:00:00+00:00"

    parsed = get_subscription_metadata(updated, MicrosoftSubscriptionKind.CALENDAR)
    assert parsed is not None
    assert parsed.subscription_id == "sub-calendar"
    assert parsed.kind == MicrosoftSubscriptionKind.CALENDAR
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/microsoft/test_subscription_service.py::test_set_and_get_subscription_metadata_round_trips_existing_metadata -v
```

Expected: FAIL with import error for missing Microsoft webhook metadata module.

- [ ] **Step 3: Add metadata helpers**

Create `src/service/integrations/webhooks/microsoft/__init__.py`:

```python
"""Microsoft Graph webhook subscription and ingestion support."""
```

Create `src/service/integrations/webhooks/microsoft/metadata.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY = "microsoft_graph_subscriptions"


class MicrosoftSubscriptionKind(StrEnum):
    CALENDAR = "calendar"
    EMAIL = "email"


@dataclass(frozen=True, slots=True)
class MicrosoftGraphSubscriptionMetadata:
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
    if value == MicrosoftSubscriptionKind.CALENDAR.value:
        return MicrosoftSubscriptionKind.CALENDAR
    if value == MicrosoftSubscriptionKind.EMAIL.value:
        return MicrosoftSubscriptionKind.EMAIL
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def get_subscription_metadata(
    metadata: dict[str, Any] | None,
    kind: MicrosoftSubscriptionKind,
) -> MicrosoftGraphSubscriptionMetadata | None:
    subscriptions = (metadata or {}).get(MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY)
    if not isinstance(subscriptions, dict):
        return None

    raw = subscriptions.get(kind.value)
    if not isinstance(raw, dict):
        return None

    expires_at = _parse_datetime(raw.get("expires_at"))
    if expires_at is None:
        return None

    subscription_id = raw.get("subscription_id")
    client_state = raw.get("client_state")
    resource = raw.get("resource")
    change_type = raw.get("change_type")
    notification_url = raw.get("notification_url")
    lifecycle_notification_url = raw.get("lifecycle_notification_url")
    if not all(
        isinstance(value, str) and value
        for value in (
            subscription_id,
            client_state,
            resource,
            change_type,
            notification_url,
            lifecycle_notification_url,
        )
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
        created_at=_parse_datetime(raw.get("created_at")),
        updated_at=_parse_datetime(raw.get("updated_at")),
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
    now = datetime.now(UTC)
    updated = dict(metadata or {})
    subscriptions = dict(updated.get(MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY) or {})
    existing = dict(subscriptions.get(kind.value) or {})
    created_at = existing.get("created_at") or now.isoformat()
    subscriptions[kind.value] = {
        "subscription_id": subscription_id,
        "client_state": client_state,
        "resource": resource,
        "change_type": change_type,
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "notification_url": notification_url,
        "lifecycle_notification_url": lifecycle_notification_url,
        "created_at": created_at,
        "updated_at": now.isoformat(),
    }
    updated[MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY] = subscriptions
    return updated


def clear_subscription_metadata(
    metadata: dict[str, Any] | None,
    kind: MicrosoftSubscriptionKind,
) -> dict[str, Any]:
    updated = dict(metadata or {})
    subscriptions = dict(updated.get(MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY) or {})
    subscriptions.pop(kind.value, None)
    updated[MICROSOFT_GRAPH_SUBSCRIPTIONS_KEY] = subscriptions
    return updated
```

- [ ] **Step 4: Add webhook notification type parser**

Create `src/service/integrations/webhooks/microsoft/types.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MicrosoftGraphWebhookNotification:
    subscription_id: str
    client_state: str
    change_type: str
    resource: str
    notification_id: str | None
    tenant_id: str | None
    resource_data: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
    ) -> "MicrosoftGraphWebhookNotification | None":
        subscription_id = payload.get("subscriptionId")
        client_state = payload.get("clientState")
        change_type = payload.get("changeType")
        resource = payload.get("resource")
        if not all(
            isinstance(value, str) and value
            for value in (subscription_id, client_state, change_type, resource)
        ):
            return None

        notification_id = payload.get("id")
        tenant_id = payload.get("tenantId")
        resource_data = payload.get("resourceData")
        return cls(
            subscription_id=subscription_id,
            client_state=client_state,
            change_type=change_type,
            resource=resource,
            notification_id=notification_id if isinstance(notification_id, str) else None,
            tenant_id=tenant_id if isinstance(tenant_id, str) else None,
            resource_data=resource_data if isinstance(resource_data, dict) else {},
            raw=payload,
        )

    def dedupe_token(self) -> str:
        if self.notification_id:
            return self.notification_id
        blob = "|".join(
            (
                self.subscription_id,
                self.change_type,
                self.resource,
                str(self.resource_data.get("id") or ""),
            )
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Run test to verify it passes**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/microsoft/test_subscription_service.py::test_set_and_get_subscription_metadata_round_trips_existing_metadata -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/service/integrations/webhooks/microsoft tests/service/integrations/webhooks/microsoft/test_subscription_service.py
git commit -m "feat: add microsoft graph webhook metadata helpers"
```

## Task 2: Add Microsoft webhook route

**Files:**
- Create: `src/api/routers/integrations/microsoft_webhook_routes.py`
- Modify: `src/api/routers/integrations/__init__.py`
- Test: `tests/api/routers/integrations/test_microsoft_webhook_routes.py`

- [ ] **Step 1: Write failing route tests**

Add `tests/api/routers/integrations/test_microsoft_webhook_routes.py`:

```python
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.api.fastapi_app import create_app
from src.api.routers.integrations.microsoft_webhook_routes import (
    get_microsoft_webhook_ingestion_service,
)


def test_microsoft_webhook_validation_token_returns_plain_text() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/users/integrations/microsoft/webhook?validationToken=abc%20123",
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "abc 123"


def test_microsoft_webhook_enqueues_verified_payload(monkeypatch) -> None:
    app = create_app()
    ingestion_service = AsyncMock()
    app.dependency_overrides[get_microsoft_webhook_ingestion_service] = (
        lambda: ingestion_service
    )
    client = TestClient(app)

    payload = {
        "value": [
            {
                "subscriptionId": "sub-email",
                "clientState": "secret",
                "changeType": "updated",
                "resource": "Users/user@tenant/Messages/message-id",
                "resourceData": {"id": "message-id"},
            }
        ]
    }

    response = client.post(
        "/users/integrations/microsoft/webhook"
        "?organisation_id=11111111-1111-1111-1111-111111111111"
        "&integration_id=22222222-2222-2222-2222-222222222222"
        "&kind=email",
        json=payload,
    )

    assert response.status_code == 202
    ingestion_service.ingest.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/api/routers/integrations/test_microsoft_webhook_routes.py -v
```

Expected: FAIL with missing `microsoft_webhook_routes` import.

- [ ] **Step 3: Implement route**

Create `src/api/routers/integrations/microsoft_webhook_routes.py`:

```python
from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.microsoft.ingestion_service import (
    MicrosoftWebhookIngestionService,
)
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    parse_subscription_kind,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/microsoft", tags=["integrations", "microsoft-webhooks"])

MICROSOFT_WEBHOOK_MAX_BODY_BYTES = 256 * 1024


async def get_microsoft_webhook_ingestion_service() -> MicrosoftWebhookIngestionService:
    return MicrosoftWebhookIngestionService()


def _parse_notifications(raw_body: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Microsoft webhook JSON payload",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Microsoft webhook payload",
        )

    value = payload.get("value")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Microsoft webhook notification collection",
        )
    return value


def _check_declared_size(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if not content_length:
        return
    try:
        declared_content_length = int(content_length)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Content-Length header",
        ) from exc
    if declared_content_length > MICROSOFT_WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Microsoft webhook body is too large",
        )


@router.post("/webhook")
async def handle_microsoft_webhook(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    organisation_id: UUID | None = Query(default=None),
    integration_id: UUID | None = Query(default=None),
    kind: str | None = Query(default=None),
    ingestion_service: MicrosoftWebhookIngestionService = Depends(
        get_microsoft_webhook_ingestion_service
    ),
) -> Response:
    if validation_token is not None:
        return PlainTextResponse(unquote(validation_token), status_code=status.HTTP_200_OK)

    subscription_kind = parse_subscription_kind(kind)
    if organisation_id is None or integration_id is None or subscription_kind is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Microsoft webhook routing parameters",
        )

    _check_declared_size(request)
    raw_body = await request.body()
    if len(raw_body) > MICROSOFT_WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Microsoft webhook body is too large",
        )

    await ingestion_service.ingest(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=subscription_kind,
        notification_payloads=_parse_notifications(raw_body),
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/webhook/lifecycle")
async def handle_microsoft_webhook_lifecycle(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    organisation_id: UUID | None = Query(default=None),
    integration_id: UUID | None = Query(default=None),
    kind: str | None = Query(default=None),
    ingestion_service: MicrosoftWebhookIngestionService = Depends(
        get_microsoft_webhook_ingestion_service
    ),
) -> Response:
    if validation_token is not None:
        return PlainTextResponse(unquote(validation_token), status_code=status.HTTP_200_OK)

    subscription_kind = parse_subscription_kind(kind)
    if organisation_id is None or integration_id is None or subscription_kind is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Microsoft lifecycle routing parameters",
        )

    _check_declared_size(request)
    raw_body = await request.body()
    if len(raw_body) > MICROSOFT_WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Microsoft lifecycle body is too large",
        )

    await ingestion_service.ingest_lifecycle(
        organisation_id=organisation_id,
        integration_id=integration_id,
        kind=subscription_kind,
        notification_payloads=_parse_notifications(raw_body),
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)
```

- [ ] **Step 4: Register route**

Modify `src/api/routers/integrations/__init__.py`:

```python
from .microsoft_webhook_routes import router as microsoft_webhook_router

integrations_router.include_router(microsoft_webhook_router)
```

Keep the existing HubSpot router registration unchanged.

- [ ] **Step 5: Run route tests**

Run:

```bash
uv run pytest tests/api/routers/integrations/test_microsoft_webhook_routes.py -v
```

Expected: FAIL because `MicrosoftWebhookIngestionService` is not implemented yet.

- [ ] **Step 6: Commit after Task 3 passes**

Do not commit this task yet if imports still fail. Commit after Task 3 creates the ingestion service.

## Task 3: Add webhook ingestion service that schedules existing sync tasks

**Files:**
- Create: `src/service/integrations/webhooks/microsoft/ingestion_service.py`
- Test: `tests/service/integrations/webhooks/microsoft/test_ingestion_service.py`

- [ ] **Step 1: Write failing ingestion tests**

Add `tests/service/integrations/webhooks/microsoft/test_ingestion_service.py`:

```python
from datetime import UTC, datetime
from uuid import UUID

import pytest

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.microsoft.ingestion_service import (
    MicrosoftWebhookIngestionService,
)
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    set_subscription_metadata,
)


ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
INTEGRATION_ID = UUID("22222222-2222-2222-2222-222222222222")


def _integration(kind: MicrosoftSubscriptionKind) -> KnowledgeIntegration:
    integration_type = (
        IntegrationType.OUTLOOK_CALENDAR
        if kind == MicrosoftSubscriptionKind.CALENDAR
        else IntegrationType.OUTLOOK_EMAIL
    )
    metadata = set_subscription_metadata(
        {},
        kind=kind,
        subscription_id=f"sub-{kind.value}",
        client_state="secret",
        resource="/me/events" if kind == MicrosoftSubscriptionKind.CALENDAR else "/me/messages",
        change_type="created,updated,deleted",
        expires_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
        notification_url="https://api.example.test/users/integrations/microsoft/webhook",
        lifecycle_notification_url=(
            "https://api.example.test/users/integrations/microsoft/webhook/lifecycle"
        ),
    )
    return KnowledgeIntegration(
        integration_id=INTEGRATION_ID,
        organisation_id=ORG_ID,
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=integration_type,
        name="Outlook",
        credentials={},
        connection_id="conn",
        is_active=True,
        connection_metadata=metadata,
    )


@pytest.mark.asyncio
async def test_ingest_calendar_notification_enqueues_calendar_sync(monkeypatch) -> None:
    sent_tasks: list[dict] = []

    async def fake_send_task_async(**kwargs):
        sent_tasks.append(kwargs)
        return "task-id"

    async def fake_load_integration(*args, **kwargs):
        return _integration(MicrosoftSubscriptionKind.CALENDAR)

    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.ingestion_service.send_integrations_task_async",
        fake_send_task_async,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.ingestion_service._load_integration",
        fake_load_integration,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.ingestion_service._claim_dedupe",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.ingestion_service._claim_sync_pending",
        lambda *args, **kwargs: True,
    )

    result = await MicrosoftWebhookIngestionService().ingest(
        organisation_id=ORG_ID,
        integration_id=INTEGRATION_ID,
        kind=MicrosoftSubscriptionKind.CALENDAR,
        notification_payloads=[
            {
                "subscriptionId": "sub-calendar",
                "clientState": "secret",
                "changeType": "updated",
                "resource": "Users/user@tenant/Events/event-id",
                "resourceData": {"id": "event-id"},
            }
        ],
    )

    assert result.queued_count == 1
    assert sent_tasks[0]["task_name"] == "integrations.sync_calendar_integration"
    assert sent_tasks[0]["kwargs"] == {
        "organisation_id": str(ORG_ID),
        "integration_id": str(INTEGRATION_ID),
    }
    assert sent_tasks[0]["queue"] == "integrations_priority"


@pytest.mark.asyncio
async def test_ingest_rejects_client_state_mismatch(monkeypatch) -> None:
    async def fake_load_integration(*args, **kwargs):
        return _integration(MicrosoftSubscriptionKind.EMAIL)

    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.ingestion_service._load_integration",
        fake_load_integration,
    )

    result = await MicrosoftWebhookIngestionService().ingest(
        organisation_id=ORG_ID,
        integration_id=INTEGRATION_ID,
        kind=MicrosoftSubscriptionKind.EMAIL,
        notification_payloads=[
            {
                "subscriptionId": "sub-email",
                "clientState": "wrong",
                "changeType": "updated",
                "resource": "Users/user@tenant/Messages/message-id",
            }
        ],
    )

    assert result.queued_count == 0
    assert result.invalid_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/microsoft/test_ingestion_service.py -v
```

Expected: FAIL with missing ingestion service.

- [ ] **Step 3: Implement ingestion service**

Create `src/service/integrations/webhooks/microsoft/ingestion_service.py`:

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
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftSubscriptionKind,
    get_subscription_metadata,
)
from src.service.integrations.webhooks.microsoft.types import (
    MicrosoftGraphWebhookNotification,
)
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)

MICROSOFT_WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60
MICROSOFT_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS = 45
MICROSOFT_WEBHOOK_SYNC_COUNTDOWN_SECONDS = 15

_TASK_BY_KIND = {
    MicrosoftSubscriptionKind.CALENDAR: "integrations.sync_calendar_integration",
    MicrosoftSubscriptionKind.EMAIL: "integrations.sync_email_integration",
}

_INTEGRATION_TYPE_BY_KIND = {
    MicrosoftSubscriptionKind.CALENDAR: IntegrationType.OUTLOOK_CALENDAR,
    MicrosoftSubscriptionKind.EMAIL: IntegrationType.OUTLOOK_EMAIL,
}


@dataclass(frozen=True, slots=True)
class MicrosoftWebhookIngestionResult:
    parsed_count: int
    valid_count: int
    queued_count: int
    duplicate_count: int
    pending_count: int
    invalid_count: int


class MicrosoftWebhookIngestionService:
    async def ingest(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        notification_payloads: list[dict],
    ) -> MicrosoftWebhookIngestionResult:
        integration = await _load_integration(
            organisation_id=organisation_id,
            integration_id=integration_id,
            kind=kind,
        )
        if integration is None:
            logger.warning(
                "Microsoft webhook integration not found",
                extra={
                    "event": "microsoft_webhook_integration_missing",
                    "organisation_id": str(organisation_id),
                    "integration_id": str(integration_id),
                    "kind": kind.value,
                },
            )
            return MicrosoftWebhookIngestionResult(
                parsed_count=len(notification_payloads),
                valid_count=0,
                queued_count=0,
                duplicate_count=0,
                pending_count=0,
                invalid_count=len(notification_payloads),
            )

        subscription = get_subscription_metadata(integration.connection_metadata, kind)
        if subscription is None:
            return MicrosoftWebhookIngestionResult(
                parsed_count=len(notification_payloads),
                valid_count=0,
                queued_count=0,
                duplicate_count=0,
                pending_count=0,
                invalid_count=len(notification_payloads),
            )

        valid_count = 0
        queued_count = 0
        duplicate_count = 0
        pending_count = 0
        invalid_count = 0

        for payload in notification_payloads:
            notification = MicrosoftGraphWebhookNotification.from_payload(payload)
            if notification is None:
                invalid_count += 1
                continue

            if (
                notification.subscription_id != subscription.subscription_id
                or notification.client_state != subscription.client_state
            ):
                invalid_count += 1
                logger.warning(
                    "Rejected Microsoft webhook notification",
                    extra={
                        "event": "microsoft_webhook_notification_invalid",
                        "organisation_id": str(organisation_id),
                        "integration_id": str(integration_id),
                        "kind": kind.value,
                        "subscription_id": notification.subscription_id,
                    },
                )
                continue

            valid_count += 1
            if not _claim_dedupe(notification):
                duplicate_count += 1
                continue

            if not _claim_sync_pending(organisation_id, integration_id, kind):
                pending_count += 1
                continue

            await _enqueue_sync(organisation_id, integration_id, kind)
            queued_count += 1

        return MicrosoftWebhookIngestionResult(
            parsed_count=len(notification_payloads),
            valid_count=valid_count,
            queued_count=queued_count,
            duplicate_count=duplicate_count,
            pending_count=pending_count,
            invalid_count=invalid_count,
        )

    async def ingest_lifecycle(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        notification_payloads: list[dict],
    ) -> MicrosoftWebhookIngestionResult:
        valid_payloads = [
            payload
            for payload in notification_payloads
            if isinstance(payload.get("subscriptionId"), str)
        ]
        if valid_payloads:
            await _enqueue_sync(organisation_id, integration_id, kind, countdown=0)
            return MicrosoftWebhookIngestionResult(
                parsed_count=len(notification_payloads),
                valid_count=len(valid_payloads),
                queued_count=1,
                duplicate_count=0,
                pending_count=0,
                invalid_count=len(notification_payloads) - len(valid_payloads),
            )
        return MicrosoftWebhookIngestionResult(
            parsed_count=len(notification_payloads),
            valid_count=0,
            queued_count=0,
            duplicate_count=0,
            pending_count=0,
            invalid_count=len(notification_payloads),
        )


async def _load_integration(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    kind: MicrosoftSubscriptionKind,
) -> KnowledgeIntegration | None:
    async for session in acquire_org_async_session(organisation_id):
        statement = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.integration_id) == integration_id,
            col(KnowledgeIntegration.integration_type) == _INTEGRATION_TYPE_BY_KIND[kind],
            col(KnowledgeIntegration.is_active).is_(True),
        )
        result = await session.execute(statement)
        return result.scalar_one_or_none()
    return None


def _dedupe_key(notification: MicrosoftGraphWebhookNotification) -> str:
    return (
        "webhook:microsoft:dedupe:"
        f"{notification.subscription_id}:{notification.dedupe_token()}"
    )


def _sync_pending_key(
    organisation_id: UUID,
    integration_id: UUID,
    kind: MicrosoftSubscriptionKind,
) -> str:
    return f"webhook:microsoft:sync:{organisation_id}:{integration_id}:{kind.value}"


def _claim_dedupe(notification: MicrosoftGraphWebhookNotification) -> bool:
    try:
        redis = get_redis_client()
        return bool(
            redis.set(
                _dedupe_key(notification),
                "1",
                nx=True,
                ex=MICROSOFT_WEBHOOK_DEDUPE_TTL_SECONDS,
            )
        )
    except Exception:
        logger.debug("Microsoft webhook dedupe failed open", exc_info=True)
        return True


def _claim_sync_pending(
    organisation_id: UUID,
    integration_id: UUID,
    kind: MicrosoftSubscriptionKind,
) -> bool:
    try:
        redis = get_redis_client()
        return bool(
            redis.set(
                _sync_pending_key(organisation_id, integration_id, kind),
                "1",
                nx=True,
                ex=MICROSOFT_WEBHOOK_SYNC_DEBOUNCE_TTL_SECONDS,
            )
        )
    except Exception:
        logger.debug("Microsoft webhook debounce failed open", exc_info=True)
        return True


async def _enqueue_sync(
    organisation_id: UUID,
    integration_id: UUID,
    kind: MicrosoftSubscriptionKind,
    *,
    countdown: int = MICROSOFT_WEBHOOK_SYNC_COUNTDOWN_SECONDS,
) -> str:
    return await send_integrations_task_async(
        task_name=_TASK_BY_KIND[kind],
        kwargs={
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
        },
        queue="integrations_priority",
        routing_key="integrations_priority",
        countdown=countdown,
    )
```

- [ ] **Step 4: Run ingestion and route tests**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/microsoft/test_ingestion_service.py tests/api/routers/integrations/test_microsoft_webhook_routes.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Tasks 2 and 3**

```bash
git add src/api/routers/integrations src/service/integrations/webhooks/microsoft tests/api/routers/integrations/test_microsoft_webhook_routes.py tests/service/integrations/webhooks/microsoft/test_ingestion_service.py
git commit -m "feat: route microsoft graph webhooks to incremental sync"
```

## Task 4: Add Microsoft Graph subscription service

**Files:**
- Modify: `src/workers/integrations_worker/providers/_microsoft_graph.py`
- Create: `src/service/integrations/webhooks/microsoft/subscription_service.py`
- Test: `tests/service/integrations/webhooks/microsoft/test_subscription_service.py`

- [ ] **Step 1: Add failing subscription creation test**

Append to `tests/service/integrations/webhooks/microsoft/test_subscription_service.py`:

```python
from uuid import UUID

import pytest

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.microsoft.metadata import MicrosoftSubscriptionKind
from src.service.integrations.webhooks.microsoft.subscription_service import (
    MicrosoftGraphSubscriptionService,
)


@pytest.mark.asyncio
async def test_ensure_subscription_creates_calendar_subscription(monkeypatch) -> None:
    captured_payloads: list[dict] = []

    async def fake_graph_post_json(client, endpoint, *, json_body, params=None):
        captured_payloads.append(json_body)
        return {
            "id": "sub-calendar",
            "expirationDateTime": "2026-06-03T12:00:00Z",
        }

    async def fake_get_access_token(token_info):
        return "access-token"

    monkeypatch.setenv("MICROSOFT_GRAPH_WEBHOOK_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.graph_api_post_json",
        fake_graph_post_json,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.microsoft.subscription_service.get_access_token",
        fake_get_access_token,
    )

    integration = KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.OUTLOOK_CALENDAR,
        name="Outlook Calendar",
        credentials={},
        connection_id="conn",
        is_active=True,
        connection_metadata={},
    )

    metadata = await MicrosoftGraphSubscriptionService().ensure_subscription_metadata(
        organisation_id=integration.organisation_id,
        integration=integration,
        kind=MicrosoftSubscriptionKind.CALENDAR,
    )

    assert captured_payloads[0]["resource"] == "/me/events"
    assert captured_payloads[0]["changeType"] == "created,updated,deleted"
    assert captured_payloads[0]["notificationUrl"].startswith(
        "https://api.example.test/users/integrations/microsoft/webhook?"
    )
    assert metadata["microsoft_graph_subscriptions"]["calendar"]["subscription_id"] == (
        "sub-calendar"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/microsoft/test_subscription_service.py::test_ensure_subscription_creates_calendar_subscription -v
```

Expected: FAIL with missing subscription service.

- [ ] **Step 3: Add PATCH helper for renewal**

Modify `src/workers/integrations_worker/providers/_microsoft_graph.py` by adding `graph_api_patch_json` and exporting it:

```python
async def graph_api_patch_json(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    json_body: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """PATCH JSON to Microsoft Graph with shared 429 retry/error handling."""
    url = f"{GRAPH_BASE_URL}{endpoint}"

    async def _do() -> dict[str, Any]:
        response = await client.patch(url, params=params, json=json_body)
        if response.status_code == 403:
            response_text = response.text.lower()
            if (
                "insufficient privileges" in response_text
                or "required application permissions" in response_text
                or "insufficient permissions" in response_text
            ):
                raise IntegrationPermissionError(
                    "Required Microsoft permissions were not granted. Please reconnect this integration.",
                    status_code=403,
                    provider="microsoft",
                    endpoint=endpoint,
                    response_summary=response.text,
                )
        response.raise_for_status()
        return response.json()

    return await _request_with_rate_limit_retry(_do)
```

Add `"graph_api_patch_json"` to `__all__`.

- [ ] **Step 4: Implement subscription service**

Create `src/service/integrations/webhooks/microsoft/subscription_service.py`:

```python
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.orm import attributes

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo, get_access_token
from src.service.integrations.webhooks.microsoft.metadata import (
    MicrosoftGraphSubscriptionMetadata,
    MicrosoftSubscriptionKind,
    get_subscription_metadata,
    set_subscription_metadata,
)
from src.workers.integrations_worker.providers._microsoft_graph import (
    graph_api_patch_json,
    graph_api_post_json,
)

MICROSOFT_GRAPH_SUBSCRIPTION_CHANGE_TYPE = "created,updated,deleted"
MICROSOFT_GRAPH_RENEWAL_BUFFER = timedelta(hours=24)

_RESOURCE_BY_KIND = {
    MicrosoftSubscriptionKind.CALENDAR: "/me/events",
    MicrosoftSubscriptionKind.EMAIL: "/me/messages",
}

_INTEGRATION_TYPE_BY_KIND = {
    MicrosoftSubscriptionKind.CALENDAR: IntegrationType.OUTLOOK_CALENDAR,
    MicrosoftSubscriptionKind.EMAIL: IntegrationType.OUTLOOK_EMAIL,
}


class MicrosoftGraphSubscriptionService:
    async def ensure_subscription_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        kind: MicrosoftSubscriptionKind,
    ) -> dict:
        self._validate_kind(integration, kind)
        existing = get_subscription_metadata(integration.connection_metadata, kind)
        if existing and existing.expires_at > datetime.now(UTC) + MICROSOFT_GRAPH_RENEWAL_BUFFER:
            return dict(integration.connection_metadata or {})

        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        access_token = await get_access_token(token_info)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            response = await graph_api_post_json(
                client,
                "/v1.0/subscriptions",
                json_body=self._build_subscription_payload(
                    organisation_id=organisation_id,
                    integration_id=integration.integration_id,
                    kind=kind,
                    client_state=secrets.token_urlsafe(32),
                ),
            )

        subscription_id = response.get("id")
        expires_at_raw = response.get("expirationDateTime")
        if not isinstance(subscription_id, str) or not isinstance(expires_at_raw, str):
            raise RuntimeError("Microsoft Graph subscription response missing id or expiration")

        expires_at = _parse_graph_datetime(expires_at_raw)
        payload = self._build_subscription_payload(
            organisation_id=organisation_id,
            integration_id=integration.integration_id,
            kind=kind,
            client_state=response.get("clientState") or secrets.token_urlsafe(32),
        )
        return set_subscription_metadata(
            integration.connection_metadata,
            kind=kind,
            subscription_id=subscription_id,
            client_state=payload["clientState"],
            resource=payload["resource"],
            change_type=payload["changeType"],
            expires_at=expires_at,
            notification_url=payload["notificationUrl"],
            lifecycle_notification_url=payload["lifecycleNotificationUrl"],
        )

    async def renew_subscription_metadata(
        self,
        *,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        kind: MicrosoftSubscriptionKind,
    ) -> dict:
        self._validate_kind(integration, kind)
        existing = get_subscription_metadata(integration.connection_metadata, kind)
        if existing is None:
            return await self.ensure_subscription_metadata(
                organisation_id=organisation_id,
                integration=integration,
                kind=kind,
            )
        if existing.expires_at > datetime.now(UTC) + MICROSOFT_GRAPH_RENEWAL_BUFFER:
            return dict(integration.connection_metadata or {})

        token_info = IntegrationTokenInfo.from_integration(integration, organisation_id)
        access_token = await get_access_token(token_info)
        new_expiration = _requested_expiration()
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        ) as client:
            response = await graph_api_patch_json(
                client,
                f"/v1.0/subscriptions/{existing.subscription_id}",
                json_body={"expirationDateTime": _format_graph_datetime(new_expiration)},
            )

        expires_at = _parse_graph_datetime(
            response.get("expirationDateTime") or _format_graph_datetime(new_expiration)
        )
        return set_subscription_metadata(
            integration.connection_metadata,
            kind=kind,
            subscription_id=existing.subscription_id,
            client_state=existing.client_state,
            resource=existing.resource,
            change_type=existing.change_type,
            expires_at=expires_at,
            notification_url=existing.notification_url,
            lifecycle_notification_url=existing.lifecycle_notification_url,
        )

    def _build_subscription_payload(
        self,
        *,
        organisation_id: UUID,
        integration_id: UUID,
        kind: MicrosoftSubscriptionKind,
        client_state: str,
    ) -> dict[str, str]:
        base_url = os.environ["MICROSOFT_GRAPH_WEBHOOK_BASE_URL"].rstrip("/")
        query = urlencode(
            {
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
                "kind": kind.value,
            }
        )
        return {
            "changeType": MICROSOFT_GRAPH_SUBSCRIPTION_CHANGE_TYPE,
            "notificationUrl": (
                f"{base_url}/users/integrations/microsoft/webhook?{query}"
            ),
            "lifecycleNotificationUrl": (
                f"{base_url}/users/integrations/microsoft/webhook/lifecycle?{query}"
            ),
            "resource": _RESOURCE_BY_KIND[kind],
            "expirationDateTime": _format_graph_datetime(_requested_expiration()),
            "clientState": client_state,
        }

    def _validate_kind(
        self,
        integration: KnowledgeIntegration,
        kind: MicrosoftSubscriptionKind,
    ) -> None:
        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        if integration_type != _INTEGRATION_TYPE_BY_KIND[kind]:
            raise ValueError(
                f"Cannot create {kind.value} subscription for {integration_type.value}"
            )


def persist_subscription_metadata(
    integration: KnowledgeIntegration,
    metadata: dict,
) -> None:
    integration.connection_metadata = metadata
    attributes.flag_modified(integration, "connection_metadata")


def _requested_expiration() -> datetime:
    return datetime.now(UTC) + timedelta(days=6)


def _format_graph_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_graph_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
```

- [ ] **Step 5: Run subscription service tests**

Run:

```bash
uv run pytest tests/service/integrations/webhooks/microsoft/test_subscription_service.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/workers/integrations_worker/providers/_microsoft_graph.py src/service/integrations/webhooks/microsoft/subscription_service.py tests/service/integrations/webhooks/microsoft/test_subscription_service.py
git commit -m "feat: manage microsoft graph subscriptions"
```

## Task 5: Add Celery tasks for subscription ensure and renewal

**Files:**
- Create: `src/workers/integrations_worker/tasks/microsoft_graph_subscription_tasks.py`
- Modify: `src/workers/integrations_worker/tasks/__init__.py`
- Modify: `src/workers/integrations_worker/config.py`
- Modify: `src/workers/integrations_worker/scheduled_jobs.py`
- Test: `tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py`

- [ ] **Step 1: Write failing worker task tests**

Create `tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py`:

```python
from uuid import UUID

import pytest

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.microsoft.metadata import MicrosoftSubscriptionKind
from src.workers.integrations_worker.tasks.microsoft_graph_subscription_tasks import (
    _kind_for_integration_type,
)


def test_kind_for_integration_type_maps_only_outlook_v1_types() -> None:
    assert (
        _kind_for_integration_type(IntegrationType.OUTLOOK_CALENDAR)
        == MicrosoftSubscriptionKind.CALENDAR
    )
    assert (
        _kind_for_integration_type(IntegrationType.OUTLOOK_EMAIL)
        == MicrosoftSubscriptionKind.EMAIL
    )
    assert _kind_for_integration_type(IntegrationType.TEAMS) is None


@pytest.mark.asyncio
async def test_ensure_task_updates_subscription_metadata(monkeypatch) -> None:
    from src.workers.integrations_worker.tasks import microsoft_graph_subscription_tasks as tasks

    integration = KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.OUTLOOK_EMAIL,
        name="Outlook Email",
        credentials={},
        connection_id="conn",
        is_active=True,
        connection_metadata={},
    )

    class FakeResult:
        def scalar_one_or_none(self):
            return integration

    class FakeSession:
        async def execute(self, statement):
            return FakeResult()

        def add(self, instance):
            assert instance is integration

        async def commit(self):
            integration.connection_metadata["committed"] = True

    async def fake_session_generator(org_id):
        yield FakeSession()

    async def fake_ensure(self, *, organisation_id, integration, kind):
        return {"microsoft_graph_subscriptions": {"email": {"subscription_id": "sub"}}}

    monkeypatch.setattr(tasks, "acquire_org_async_session", fake_session_generator)
    monkeypatch.setattr(
        tasks.MicrosoftGraphSubscriptionService,
        "ensure_subscription_metadata",
        fake_ensure,
    )

    result = await tasks._ensure_microsoft_graph_subscription(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
    )

    assert result["status"] == "success"
    assert integration.connection_metadata["committed"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py -v
```

Expected: FAIL with missing task module.

- [ ] **Step 3: Implement task module**

Create `src/workers/integrations_worker/tasks/microsoft_graph_subscription_tasks.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task
from sqlalchemy import select
from sqlmodel import col

from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.integrations.webhooks.microsoft.metadata import MicrosoftSubscriptionKind
from src.service.integrations.webhooks.microsoft.subscription_service import (
    MicrosoftGraphSubscriptionService,
    persist_subscription_metadata,
)
from src.workers.integrations_worker.celery_app import app
from src.workers.integrations_worker.config import TASK_LIMITS

logger = get_logger(__name__)

ENSURE_TASK = "integrations.ensure_microsoft_graph_subscription"
RENEW_ALL_ORGS_TASK = "integrations.renew_microsoft_graph_subscriptions_all_orgs"
RENEW_ORG_TASK = "integrations.renew_microsoft_graph_subscriptions_org"

MICROSOFT_WEBHOOK_INTEGRATION_TYPES = (
    IntegrationType.OUTLOOK_CALENDAR,
    IntegrationType.OUTLOOK_EMAIL,
)


def _kind_for_integration_type(
    integration_type: IntegrationType,
) -> MicrosoftSubscriptionKind | None:
    if integration_type == IntegrationType.OUTLOOK_CALENDAR:
        return MicrosoftSubscriptionKind.CALENDAR
    if integration_type == IntegrationType.OUTLOOK_EMAIL:
        return MicrosoftSubscriptionKind.EMAIL
    return None


@app.task(
    name=ENSURE_TASK,
    bind=True,
    max_retries=TASK_LIMITS[ENSURE_TASK]["retries"],
    default_retry_delay=TASK_LIMITS[ENSURE_TASK].get("retry_delay", 60),
    acks_late=True,
    soft_time_limit=TASK_LIMITS[ENSURE_TASK]["soft"],
    time_limit=TASK_LIMITS[ENSURE_TASK]["hard"],
)
def ensure_microsoft_graph_subscription_task(
    self: Task,
    organisation_id: str,
    integration_id: str,
) -> dict[str, Any]:
    try:
        return run_async(
            _ensure_microsoft_graph_subscription(
                organisation_id=UUID(organisation_id),
                integration_id=UUID(integration_id),
            )
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        raise


async def _ensure_microsoft_graph_subscription(
    *,
    organisation_id: UUID,
    integration_id: UUID,
) -> dict[str, Any]:
    async for session in acquire_org_async_session(organisation_id):
        statement = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.integration_id) == integration_id,
            col(KnowledgeIntegration.integration_type).in_(MICROSOFT_WEBHOOK_INTEGRATION_TYPES),
            col(KnowledgeIntegration.is_active).is_(True),
            col(KnowledgeIntegration.connection_id).isnot(None),
        )
        result = await session.execute(statement)
        integration = result.scalar_one_or_none()
        if integration is None:
            return {
                "status": "skipped",
                "reason": "integration_not_found",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }

        integration_type = (
            IntegrationType(integration.integration_type)
            if isinstance(integration.integration_type, str)
            else integration.integration_type
        )
        kind = _kind_for_integration_type(integration_type)
        if kind is None:
            return {
                "status": "skipped",
                "reason": "unsupported_integration_type",
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id),
            }

        metadata = await MicrosoftGraphSubscriptionService().ensure_subscription_metadata(
            organisation_id=organisation_id,
            integration=integration,
            kind=kind,
        )
        persist_subscription_metadata(integration, metadata)
        session.add(integration)
        await session.commit()
        return {
            "status": "success",
            "organisation_id": str(organisation_id),
            "integration_id": str(integration_id),
            "kind": kind.value,
        }
    return {
        "status": "failed",
        "reason": "session_unavailable",
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
    }


@app.task(
    name=RENEW_ALL_ORGS_TASK,
    bind=True,
    max_retries=TASK_LIMITS[RENEW_ALL_ORGS_TASK]["retries"],
    acks_late=True,
    soft_time_limit=TASK_LIMITS[RENEW_ALL_ORGS_TASK]["soft"],
    time_limit=TASK_LIMITS[RENEW_ALL_ORGS_TASK]["hard"],
)
def renew_microsoft_graph_subscriptions_all_orgs_task(self: Task) -> dict[str, Any]:
    return run_async(_renew_microsoft_graph_subscriptions_all_orgs())


async def _renew_microsoft_graph_subscriptions_all_orgs() -> dict[str, Any]:
    organisation_ids: list[UUID] = []
    async for session in acquire_master_async_session():
        statement = select(col(Organisation.organisation_id)).where(
            col(Organisation.is_active).is_(True),
            col(Organisation.status) == OrganisationStatus.APPROVED,
        )
        result = await session.execute(statement)
        organisation_ids = list(result.scalars().all())

    for organisation_id in organisation_ids:
        app.send_task(
            RENEW_ORG_TASK,
            kwargs={"organisation_id": str(organisation_id)},
            queue="integrations",
            routing_key="integrations",
        )

    return {
        "status": "success",
        "orgs_dispatched": len(organisation_ids),
        "completed_at": datetime.now(UTC).isoformat(),
    }


@app.task(
    name=RENEW_ORG_TASK,
    bind=True,
    max_retries=TASK_LIMITS[RENEW_ORG_TASK]["retries"],
    default_retry_delay=TASK_LIMITS[RENEW_ORG_TASK].get("retry_delay", 120),
    acks_late=True,
    soft_time_limit=TASK_LIMITS[RENEW_ORG_TASK]["soft"],
    time_limit=TASK_LIMITS[RENEW_ORG_TASK]["hard"],
)
def renew_microsoft_graph_subscriptions_org_task(
    self: Task,
    organisation_id: str,
) -> dict[str, Any]:
    try:
        return run_async(
            _renew_microsoft_graph_subscriptions_org(organisation_id=UUID(organisation_id))
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        raise


async def _renew_microsoft_graph_subscriptions_org(
    *,
    organisation_id: UUID,
) -> dict[str, Any]:
    renewed = 0
    skipped = 0
    async for session in acquire_org_async_session(organisation_id):
        statement = select(KnowledgeIntegration).where(
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.integration_type).in_(MICROSOFT_WEBHOOK_INTEGRATION_TYPES),
            col(KnowledgeIntegration.is_active).is_(True),
            col(KnowledgeIntegration.connection_id).isnot(None),
        )
        result = await session.execute(statement)
        integrations = result.scalars().all()

        for integration in integrations:
            integration_type = (
                IntegrationType(integration.integration_type)
                if isinstance(integration.integration_type, str)
                else integration.integration_type
            )
            kind = _kind_for_integration_type(integration_type)
            if kind is None:
                skipped += 1
                continue
            metadata = await MicrosoftGraphSubscriptionService().renew_subscription_metadata(
                organisation_id=organisation_id,
                integration=integration,
                kind=kind,
            )
            persist_subscription_metadata(integration, metadata)
            session.add(integration)
            renewed += 1

        await session.commit()

    return {
        "status": "success",
        "organisation_id": str(organisation_id),
        "renewed": renewed,
        "skipped": skipped,
    }
```

- [ ] **Step 4: Register tasks**

Modify `src/workers/integrations_worker/tasks/__init__.py` to import and export:

```python
from src.workers.integrations_worker.tasks.microsoft_graph_subscription_tasks import (
    ensure_microsoft_graph_subscription_task,
    renew_microsoft_graph_subscriptions_all_orgs_task,
    renew_microsoft_graph_subscriptions_org_task,
)
```

Add the three task names to `__all__`.

- [ ] **Step 5: Add task limits and routes**

Modify `src/workers/integrations_worker/config.py` and add to `TASK_LIMITS`:

```python
"integrations.ensure_microsoft_graph_subscription": {
    "soft": 120,
    "hard": 180,
    "retries": 3,
    "retry_delay": 60,
},
"integrations.renew_microsoft_graph_subscriptions_all_orgs": {
    "soft": 120,
    "hard": 180,
    "retries": 0,
},
"integrations.renew_microsoft_graph_subscriptions_org": {
    "soft": 600,
    "hard": 660,
    "retries": 2,
    "retry_delay": 120,
},
```

Add to `task_routes`:

```python
"integrations.ensure_microsoft_graph_subscription": {
    "queue": "integrations_priority",
    "routing_key": "integrations_priority",
},
"integrations.renew_microsoft_graph_subscriptions_all_orgs": {
    "queue": "integrations",
    "routing_key": "integrations",
},
"integrations.renew_microsoft_graph_subscriptions_org": {
    "queue": "integrations",
    "routing_key": "integrations",
},
```

- [ ] **Step 6: Add scheduled renewal**

Modify `src/workers/integrations_worker/scheduled_jobs.py` to include renewal every 12 hours:

```python
{
    "name": "renew-microsoft-graph-subscriptions",
    "task_name": "integrations.renew_microsoft_graph_subscriptions_all_orgs",
    "schedule": "0 */12 * * *",
}
```

Use the existing schedule entry shape in that file. If the file uses constants or a dataclass, add the equivalent entry without changing existing jobs.

- [ ] **Step 7: Run worker task tests**

Run:

```bash
uv run pytest tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/workers/integrations_worker/tasks src/workers/integrations_worker/config.py src/workers/integrations_worker/scheduled_jobs.py tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py
git commit -m "feat: renew microsoft graph webhook subscriptions"
```

## Task 6: Trigger subscription ensure after Outlook connect

**Files:**
- Modify: `src/service/integrations/initial_sync_service.py`
- Test: existing initial sync service tests, or add `tests/service/integrations/test_initial_sync_service.py` if no focused coverage exists

- [ ] **Step 1: Write failing initial-sync test**

Add or extend the focused initial sync service test:

```python
from uuid import UUID

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.initial_sync_service import InitialSyncService


def test_outlook_calendar_connect_triggers_sync_and_subscription(monkeypatch) -> None:
    sent_tasks: list[dict] = []

    def fake_send_integrations_task(*, task_name, kwargs, queue="integrations", routing_key=None):
        sent_tasks.append(
            {
                "task_name": task_name,
                "kwargs": kwargs,
                "queue": queue,
                "routing_key": routing_key,
            }
        )
        return "task-id"

    monkeypatch.setattr(
        "src.service.integrations.initial_sync_service.send_integrations_task",
        fake_send_integrations_task,
    )

    integration = KnowledgeIntegration(
        integration_id=UUID("22222222-2222-2222-2222-222222222222"),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        created_by_user_id=UUID("33333333-3333-3333-3333-333333333333"),
        integration_type=IntegrationType.OUTLOOK_CALENDAR,
        name="Outlook Calendar",
        credentials={},
        connection_id="conn",
    )

    InitialSyncService().trigger_initial_sync(
        organisation_id=integration.organisation_id,
        integration=integration,
    )

    assert [task["task_name"] for task in sent_tasks] == [
        "integrations.sync_calendar_integration",
        "integrations.ensure_microsoft_graph_subscription",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run the focused test file:

```bash
uv run pytest tests/service/integrations/test_initial_sync_service.py -v
```

Expected: FAIL because subscription ensure is not dispatched.

- [ ] **Step 3: Update initial sync service**

Modify `src/service/integrations/initial_sync_service.py`:

```python
MICROSOFT_WEBHOOK_INTEGRATION_TYPES = {
    IntegrationType.OUTLOOK_CALENDAR,
    IntegrationType.OUTLOOK_EMAIL,
}
```

Update the calendar branch:

```python
if integration_type in self.CALENDAR_INTEGRATION_TYPES:
    self._trigger_calendar_sync(org_id_str, int_id_str)
    if integration_type in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
        self._trigger_microsoft_graph_subscription_ensure(org_id_str, int_id_str)
    return
```

Update the email branch:

```python
if integration_type in self.EMAIL_INTEGRATION_TYPES:
    self._trigger_email_sync(org_id_str, int_id_str)
    if integration_type in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
        self._trigger_microsoft_graph_subscription_ensure(org_id_str, int_id_str)
    return
```

Add method:

```python
def _trigger_microsoft_graph_subscription_ensure(
    self,
    org_id_str: str,
    int_id_str: str,
) -> None:
    send_integrations_task(
        task_name="integrations.ensure_microsoft_graph_subscription",
        kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
        queue="integrations_priority",
        routing_key="integrations_priority",
    )
    logger.info("Triggered Microsoft Graph subscription ensure: integration=%s", int_id_str)
```

- [ ] **Step 4: Run initial sync test**

Run:

```bash
uv run pytest tests/service/integrations/test_initial_sync_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/service/integrations/initial_sync_service.py tests/service/integrations/test_initial_sync_service.py
git commit -m "feat: ensure microsoft webhooks after outlook connect"
```

## Task 7: Documentation and final verification

**Files:**
- Modify: `docs/Technical Documentation/integrations/sync-by-provider.md`

- [ ] **Step 1: Update provider sync docs**

Add this section under the Outlook Calendar and Outlook Email details in `docs/Technical Documentation/integrations/sync-by-provider.md`:

```markdown
### Microsoft Graph Webhooks

Outlook Calendar and Outlook Email use Microsoft Graph change notifications as
low-latency triggers. Webhook payloads are not persisted directly. After the
backend validates the stored `clientState` and subscription id, it schedules
the existing single-integration incremental sync task:

- Outlook Calendar: `integrations.sync_calendar_integration`
- Outlook Email: `integrations.sync_email_integration`

The sync task remains the only writer for provider data. Calendar continues to
use `connection_metadata.calendar_delta_link`; email continues to use
`connection_metadata.email_folder_delta_links`.

Subscriptions are stored in
`connection_metadata.microsoft_graph_subscriptions` and renewed by
`integrations.renew_microsoft_graph_subscriptions_all_orgs`.

Scheduled polling stays enabled as a correctness backstop for missed
notifications, expired subscriptions, and Microsoft delivery gaps.
```

- [ ] **Step 2: Run targeted tests**

Run:

```bash
uv run pytest tests/api/routers/integrations/test_microsoft_webhook_routes.py tests/service/integrations/webhooks/microsoft/test_ingestion_service.py tests/service/integrations/webhooks/microsoft/test_subscription_service.py tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py tests/service/integrations/test_initial_sync_service.py -v
```

Expected: PASS.

- [ ] **Step 3: Run lint for touched code**

Run:

```bash
uv run ruff check src/api/routers/integrations/microsoft_webhook_routes.py src/service/integrations/webhooks/microsoft src/workers/integrations_worker/tasks/microsoft_graph_subscription_tasks.py tests/api/routers/integrations/test_microsoft_webhook_routes.py tests/service/integrations/webhooks/microsoft tests/workers/integrations_worker/test_microsoft_graph_subscription_tasks.py
```

Expected: PASS.

- [ ] **Step 4: Run type check for touched source**

Run:

```bash
uv run mypy src/api/routers/integrations/microsoft_webhook_routes.py src/service/integrations/webhooks/microsoft src/workers/integrations_worker/tasks/microsoft_graph_subscription_tasks.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/Technical\ Documentation/integrations/sync-by-provider.md
git commit -m "docs: document microsoft graph webhook sync triggers"
```

## Rollout notes

Required runtime env:

```bash
MICROSOFT_GRAPH_WEBHOOK_BASE_URL=https://<public-api-host>
```

Operational checks after deploy:

- Confirm the public validation request returns `200 text/plain` for `validationToken`.
- Connect or reconnect an Outlook Calendar integration and confirm `microsoft_graph_subscriptions.calendar` is stored.
- Connect or reconnect an Outlook Email integration and confirm `microsoft_graph_subscriptions.email` is stored.
- Trigger a Microsoft calendar or email update and confirm one `integrations.sync_*_integration` task is enqueued.
- Confirm scheduled polling still runs as before.

## Self-review

Spec coverage:

- Outlook Calendar webhook intake: Task 2 and Task 3.
- Outlook Email webhook intake: Task 2 and Task 3.
- Trigger existing incremental sync, not a new writer: Task 3.
- Subscription creation and metadata: Task 4.
- Renewal: Task 5.
- Post-connect enablement: Task 6.
- Documentation: Task 7.

Placeholder scan:

- No deferred implementation placeholders are intentionally left in this plan.

Type consistency:

- `MicrosoftSubscriptionKind.CALENDAR` maps to `IntegrationType.OUTLOOK_CALENDAR`.
- `MicrosoftSubscriptionKind.EMAIL` maps to `IntegrationType.OUTLOOK_EMAIL`.
- Both webhook and lifecycle routes use the same query routing fields: `organisation_id`, `integration_id`, and `kind`.
