# Recall Meeting Bot Service Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Recall meeting bot orchestration into clear provider, lifecycle, eligibility, session, webhook, artifact, and summary-trigger boundaries without changing the external API, Celery task names, or database schema.

**Architecture:** Keep `meeting_recording_sessions` as the durable provider-agnostic state table. Move Recall-specific HTTP/webhook code under `src/service/meeting_bot/providers/`, introduce `MeetingRecordingLifecycleService` as the only business orchestration boundary, and keep producer paths as thin enqueue or ownership-check layers. Cancellation must reconcile existing sessions independently of active user integration state.

**Tech Stack:** Python, FastAPI, Celery, SQLModel/SQLAlchemy, PostgreSQL, Redis task locks, Azure Blob Storage, Recall.ai API.

---

## Current Touchpoint Inventory

The implementation must account for every current Recall/meeting-bot touchpoint below.

### Core Services

- `src/service/meeting_bot/dispatch_service.py`
  Current all-in-one orchestration service. Split into lifecycle and eligibility while keeping a temporary compatibility wrapper until all callers migrate.
- `src/service/meeting_bot/session_service.py`
  Keep as the only writer for `meeting_recording_sessions`.
- `src/service/meeting_bot/webhook_service.py`
  Keep as webhook state mapper, then extract artifact and summary-trigger work.
- `src/service/meeting_bot/summary_notification_service.py`
  Keep notification delivery logic. Do not fold into webhook processing.
- `src/service/meeting_bot/recording_window.py`
  Keep shared scheduling window policy.
- `src/service/integrations/meeting_recording/providers/recall_provider.py`
  Move to `src/service/meeting_bot/providers/recall_client.py`.
- `src/service/integrations/meeting_recording/providers/recall_webhook_verifier.py`
  Move to `src/service/meeting_bot/providers/recall_webhook_verifier.py`.
- `src/service/integrations/transcripts/service.py`
  Imports Recall provider for signed transcript downloads.
- `src/service/meetings_tab_service.py`
  API-facing meeting creation, manual bot invite/cancel, playback URL resolution, and bot status projection.
- `src/service/crm/access/meetings_access_service.py`
  CRM meeting list, manual recording controls, and current private dispatch-service access.

### Producers and Runtime Entrypoints

- `src/service/integrations/calendar/sync_service.py`
  Queues `deal.send_meeting_recording` after calendar upsert/cancellation commits.
- `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
  Creates pending sessions for deal-linked meetings and queues `deal.send_meeting_recording`.
- `src/service/meetings_tab_service.py`
  Queues bot dispatch after provider-backed meeting creation and manually invokes dispatch.
- `src/agents/tools/intelligence/send_meeting_bot_invite.py`
  Proves user ownership, then invokes dispatch.
- `src/api/routers/meeting_bot/recall_webhook_handler.py`
  Verifies/dedupes webhook requests and queues `deal.process_meeting_recording_webhook`.
- `src/api/fastapi_app.py`
  Mounts `/users/meeting-bot/recall/webhook`.

### Worker and Scheduling Contracts

- `src/workers/deal_worker/tasks/meeting_bot_tasks.py`
  Defines `deal.process_meeting_recording_webhook`, `deal.send_meeting_recording`, `deal.send_unsent_meeting_recordings_org`, and `deal.trigger_send_unsent_meeting_recordings_all_orgs`.
- `src/workers/deal_worker/config.py`
  Registers the meeting-bot task names on the deal worker.
- `src/workers/deal_worker/scheduled_jobs.py`
  Schedules the nightly `meeting-bot-fallback` task at `0 20 * * *`.
- `src/workers/deal_worker/tasks/__init__.py`
  Exports meeting-bot tasks.

### API Schemas and Docs

- `src/api/schemas/meetings_tab_models.py`
  Exposes `MeetingRecordingResponse` and bot status fields.
- `src/api/schemas/crm_meeting_models.py`
  Exposes CRM meeting recording state.
- `docs/API Documentation/meetings-tab-api.md`
  Documents `/users/meetings/{event_id}/bot/invite`, delete, and recording playback.
- `docs/API Documentation/deals/crm-meetings-api.md`
  Documents CRM meeting recording controls.
- `docs/Technical Documentation/meeting-bot-recall-webhooks.md`
  Documents subscribed Recall webhook events.
- `docs/Technical Documentation/meeting-bot-recall-service-redesign.md`
  Describes the target service structure.

### Existing Test Coverage to Preserve and Extend

- `tests/service/meeting_bot/test_meeting_recording_dispatch_service.py`
- `tests/service/meeting_bot/test_meeting_recording_session_service.py`
- `tests/service/meeting_bot/test_meeting_recording_webhook_service.py`
- `tests/service/integrations/test_recall_provider.py`
- `tests/service/integrations/test_recall_webhook_verifier.py`
- `tests/service/integrations/test_transcript_service.py`
- `tests/service/integrations/test_calendar_sync_service_incremental.py`
- `tests/service/test_meetings_tab_service.py`
- `tests/service/crm/test_crm_meetings_access_service.py`
- `tests/api/routers/meeting_bot/test_recall_webhook_handler.py`
- `tests/api/routers/crm/test_crm_meetings_handler.py`
- `tests/agents/tools/intelligence/test_send_meeting_bot_invite.py`
- `tests/workers/deal_worker/tasks/test_meeting_bot_tasks.py`
- `tests/workers/deal_worker/test_cron_dispatch.py`
- `tests/workers/deal_worker/services/test_deal_meeting_matcher_recording_dispatch.py`
- `tests/scripts/test_cleanup_meeting_recordings_outside_window.py`

---

## Loophole Audit

The implementation is not complete unless these loopholes are explicitly closed.

1. Cancellation must not be blocked by inactive or deleted `KnowledgeIntegration` rows once a `meeting_recording_sessions` row exists.
2. Calendar recurring deletion tombstones must continue to flip local calendar state first, then queue meeting-bot cleanup through the existing single-event task.
3. `leave_call` must not be treated as the universal fallback after Recall delete failure; unstarted or terminal bots need provider status inspection or terminal handling.
4. Deal matcher pending-session creation must not bypass lifecycle idempotency or create duplicate bots.
5. Calendar sync, meetings tab, deal matcher, and agent tool can all enqueue or call bot work; duplicate triggers must converge on session-level idempotency.
6. Manual user cancel must still reject in-call/completed sessions, while calendar cancellation can reconcile provider cleanup for scheduled or failed sessions.
7. `transcript.done` must create/enqueue summary jobs only for the webhook event and only for durable pending jobs that need work.
8. Notification emails must stay tied to `send_summary_notification=True` on the specific summary job; no broad scan over old jobs.
9. Playback resolution must still prefer archived blob video and fall back to Recall signed URLs when no archive exists.
10. Provider metadata must keep `organisation_id`, `calendar_event_id`, and `dedupe_key`; webhook session lookup must stay org-scoped.
11. Celery task names, queues, and schedules must not change.
12. Public API routes and response contracts must not change during this refactor.
13. Provider move must update transcript service imports, tests, docs, and any compatibility exports.
14. The final state must leave no remaining business-code imports from `src.service.integrations.meeting_recording`.

---

## Target File Structure

Create:

- `src/service/meeting_bot/providers/__init__.py`
- `src/service/meeting_bot/providers/recall_client.py`
- `src/service/meeting_bot/providers/recall_webhook_verifier.py`
- `src/service/meeting_bot/eligibility_service.py`
- `src/service/meeting_bot/lifecycle_service.py`
- `src/service/meeting_bot/artifacts_service.py`
- `src/service/meeting_bot/summary_trigger_service.py`
- `src/service/meeting_bot/README.md`
- `tests/service/meeting_bot/test_recall_provider.py`
- `tests/service/meeting_bot/test_recall_webhook_verifier.py`
- `tests/service/meeting_bot/test_meeting_recording_eligibility_service.py`
- `tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py`
- `tests/service/meeting_bot/test_meeting_recording_artifacts_service.py`
- `tests/service/meeting_bot/test_meeting_summary_trigger_service.py`

Modify:

- `src/service/meeting_bot/__init__.py`
- `src/service/meeting_bot/dispatch_service.py`
- `src/service/meeting_bot/session_service.py`
- `src/service/meeting_bot/webhook_service.py`
- `src/service/meeting_bot/recording_window.py`
- `src/service/meetings_tab_service.py`
- `src/service/crm/access/meetings_access_service.py`
- `src/service/integrations/transcripts/service.py`
- `src/service/integrations/calendar/sync_service.py`
- `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
- `src/workers/deal_worker/tasks/meeting_bot_tasks.py`
- `src/agents/tools/intelligence/send_meeting_bot_invite.py`
- `docs/API Documentation/meetings-tab-api.md`
- `docs/API Documentation/deals/crm-meetings-api.md`
- `docs/Technical Documentation/meeting-bot-recall-webhooks.md`
- `docs/Technical Documentation/meeting-bot-recall-service-redesign.md`

Delete near the end, after compatibility is no longer needed:

- `src/service/integrations/meeting_recording/providers/recall_provider.py`
- `src/service/integrations/meeting_recording/providers/recall_webhook_verifier.py`
- `src/service/integrations/meeting_recording/providers/__init__.py`
- `src/service/integrations/meeting_recording/__init__.py`
- `tests/service/integrations/test_recall_provider.py`
- `tests/service/integrations/test_recall_webhook_verifier.py`

---

## Task 1: Move Recall Provider Into Meeting Bot Boundary

**Files:**
- Create: `src/service/meeting_bot/providers/__init__.py`
- Move: `src/service/integrations/meeting_recording/providers/recall_provider.py` to `src/service/meeting_bot/providers/recall_client.py`
- Move: `src/service/integrations/meeting_recording/providers/recall_webhook_verifier.py` to `src/service/meeting_bot/providers/recall_webhook_verifier.py`
- Move: `tests/service/integrations/test_recall_provider.py` to `tests/service/meeting_bot/test_recall_provider.py`
- Move: `tests/service/integrations/test_recall_webhook_verifier.py` to `tests/service/meeting_bot/test_recall_webhook_verifier.py`
- Modify: all imports found by `rg "service.integrations.meeting_recording|RecallMeetingRecordingProvider|verify_recall_webhook_signature" src tests`

- [ ] **Step 1: Move files with git mv**

Run:

```powershell
New-Item -ItemType Directory -Force src\service\meeting_bot\providers
git mv src\service\integrations\meeting_recording\providers\recall_provider.py src\service\meeting_bot\providers\recall_client.py
git mv src\service\integrations\meeting_recording\providers\recall_webhook_verifier.py src\service\meeting_bot\providers\recall_webhook_verifier.py
git mv tests\service\integrations\test_recall_provider.py tests\service\meeting_bot\test_recall_provider.py
git mv tests\service\integrations\test_recall_webhook_verifier.py tests\service\meeting_bot\test_recall_webhook_verifier.py
```

Expected: files are staged as renames.

- [ ] **Step 2: Add provider package export**

Write `src/service/meeting_bot/providers/__init__.py`:

```python
"""Provider adapters for meeting bot backends."""

from .recall_client import RecallMeetingRecordingProvider, RecallProviderError
from .recall_webhook_verifier import (
    RecallWebhookVerificationError,
    verify_recall_webhook_signature,
)

__all__ = [
    "RecallMeetingRecordingProvider",
    "RecallProviderError",
    "RecallWebhookVerificationError",
    "verify_recall_webhook_signature",
]
```

- [ ] **Step 3: Update imports**

Replace imports of:

```python
from src.service.integrations.meeting_recording.providers import (
    RecallMeetingRecordingProvider,
    RecallProviderError,
)
```

with:

```python
from src.service.meeting_bot.providers import (
    RecallMeetingRecordingProvider,
    RecallProviderError,
)
```

Replace verifier imports in `src/api/routers/meeting_bot/recall_webhook_handler.py` and tests with:

```python
from src.service.meeting_bot.providers import (
    RecallWebhookVerificationError,
    verify_recall_webhook_signature,
)
```

- [ ] **Step 4: Run provider/verifier tests**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_recall_provider.py tests/service/meeting_bot/test_recall_webhook_verifier.py -q --import-mode=importlib
```

Expected: all tests pass.

- [ ] **Step 5: Search for stale integration provider imports**

Run:

```powershell
rg "service\.integrations\.meeting_recording|integrations/meeting_recording" src tests docs
```

Expected: only compatibility shims remain if Task 1 keeps temporary exports.

- [ ] **Step 6: Commit**

Run:

```powershell
git add src tests
git commit -m "refactor: move Recall meeting bot provider boundary"
```

Expected: commit succeeds after hooks.

---

## Task 2: Add Scheduling Eligibility Service

**Files:**
- Create: `src/service/meeting_bot/eligibility_service.py`
- Create: `tests/service/meeting_bot/test_meeting_recording_eligibility_service.py`
- Modify: `src/service/meeting_bot/__init__.py`

- [ ] **Step 1: Write tests for scheduling-only rules**

Create `tests/service/meeting_bot/test_meeting_recording_eligibility_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.service.meeting_bot.eligibility_service import MeetingRecordingEligibilityService


def _event(**overrides):
    now = datetime.now(UTC)
    data = {
        "event_id": "event-1",
        "conference_link": "https://meet.example/call",
        "start_at": now + timedelta(hours=1),
        "status": "confirmed",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (None, "event_not_found"),
        (_event(conference_link=None), "missing_meeting_url"),
        (_event(status="cancelled"), "calendar_event_cancelled"),
        (_event(start_at=datetime.now(UTC) - timedelta(minutes=1)), "meeting_already_started"),
        (_event(start_at=datetime.now(UTC) + timedelta(days=4)), "meeting_outside_recording_window"),
    ],
)
def test_scheduling_ineligible_reasons(event, reason):
    service = MeetingRecordingEligibilityService()

    result = service.evaluate_for_scheduling(event, active_calendar_integration=True)

    assert result.allowed is False
    assert result.reason == reason


def test_scheduling_requires_active_calendar_integration_for_automatic_flow():
    service = MeetingRecordingEligibilityService()

    result = service.evaluate_for_scheduling(
        _event(),
        active_calendar_integration=False,
    )

    assert result.allowed is False
    assert result.reason == "calendar_integration_not_active"


def test_manual_reinvite_can_skip_calendar_integration_check():
    service = MeetingRecordingEligibilityService()

    result = service.evaluate_for_scheduling(
        _event(),
        active_calendar_integration=False,
        require_active_calendar_integration=False,
    )

    assert result.allowed is True
    assert result.reason is None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_meeting_recording_eligibility_service.py -q --import-mode=importlib
```

Expected: fail with `ModuleNotFoundError` for `eligibility_service`.

- [ ] **Step 3: Implement eligibility service**

Create `src/service/meeting_bot/eligibility_service.py`:

```python
"""Scheduling eligibility rules for meeting recording bots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.service.meeting_bot.recording_window import is_within_recording_dispatch_window


@dataclass(frozen=True)
class MeetingRecordingEligibilityResult:
    allowed: bool
    reason: str | None = None


class MeetingRecordingEligibilityService:
    """Evaluates whether a calendar event may schedule a recording bot."""

    def evaluate_for_scheduling(
        self,
        event: Any | None,
        *,
        active_calendar_integration: bool,
        require_active_calendar_integration: bool = True,
        now: datetime | None = None,
    ) -> MeetingRecordingEligibilityResult:
        if event is None:
            return MeetingRecordingEligibilityResult(False, "event_not_found")

        if require_active_calendar_integration and not active_calendar_integration:
            return MeetingRecordingEligibilityResult(False, "calendar_integration_not_active")

        if (getattr(event, "status", None) or "").lower() == "cancelled":
            return MeetingRecordingEligibilityResult(False, "calendar_event_cancelled")

        meeting_url = getattr(event, "conference_link", None)
        if not meeting_url:
            return MeetingRecordingEligibilityResult(False, "missing_meeting_url")

        join_at = getattr(event, "start_at", None)
        if join_at is None:
            return MeetingRecordingEligibilityResult(False, "missing_start_at")
        if join_at.tzinfo is None:
            join_at = join_at.replace(tzinfo=UTC)

        now_utc = now or datetime.now(UTC)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=UTC)

        if not is_within_recording_dispatch_window(join_at, now_utc):
            if join_at <= now_utc:
                return MeetingRecordingEligibilityResult(False, "meeting_already_started")
            return MeetingRecordingEligibilityResult(False, "meeting_outside_recording_window")

        return MeetingRecordingEligibilityResult(True)
```

- [ ] **Step 4: Export eligibility service**

Modify `src/service/meeting_bot/__init__.py`:

```python
from .eligibility_service import (
    MeetingRecordingEligibilityResult,
    MeetingRecordingEligibilityService,
)
```

and include both names in `__all__`.

- [ ] **Step 5: Run eligibility tests**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_meeting_recording_eligibility_service.py -q --import-mode=importlib
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

Run:

```powershell
git add src/service/meeting_bot tests/service/meeting_bot/test_meeting_recording_eligibility_service.py
git commit -m "refactor: isolate meeting bot scheduling eligibility"
```

Expected: commit succeeds.

---

## Task 3: Extract Lifecycle Service With Compatibility Wrapper

**Files:**
- Create: `src/service/meeting_bot/lifecycle_service.py`
- Create: `tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py`
- Modify: `src/service/meeting_bot/dispatch_service.py`
- Modify: `src/service/meeting_bot/__init__.py`

- [ ] **Step 1: Write compatibility tests around public lifecycle contract**

Create `tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py` with focused unit tests that monkeypatch DB/provider methods and do not require a live database:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.domain.models.organisation_tables.sync.meeting_recording import MeetingRecordingStatus
from src.service.meeting_bot.lifecycle_service import MeetingRecordingLifecycleService


@pytest.mark.asyncio
async def test_cancelled_calendar_event_uses_cancel_path(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(event_id=event_id, status="cancelled")

    async def fake_get_calendar_event(calendar_event_id):
        assert calendar_event_id == event_id
        return event

    async def fake_cancel(calendar_event_id, *, reason):
        assert calendar_event_id == event_id
        assert reason == "calendar_cancelled"
        return {"status": "cancelled", "calendar_event_id": str(event_id)}

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)
    monkeypatch.setattr(service, "cancel_for_event", fake_cancel)

    result = await service.sync_event_lifecycle(event_id)

    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_does_not_check_active_calendar_integration(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    provider_session_id = "recall-bot-1"
    service = MeetingRecordingLifecycleService(organisation_id)
    session_record = SimpleNamespace(
        session_id=session_id,
        status=MeetingRecordingStatus.SENT,
        provider_session_id=provider_session_id,
        provider_payload={},
    )
    provider_calls = []
    marked = []

    async def fake_get_by_event(calendar_event_id):
        assert calendar_event_id == event_id
        return session_record

    class Provider:
        async def cancel_session(self, value):
            provider_calls.append(value)

    async def fake_build_provider():
        return Provider()

    async def fake_mark_cancelled(value, **kwargs):
        marked.append((value, kwargs))
        return session_record

    monkeypatch.setattr(service._session_service, "get_by_event", fake_get_by_event)
    monkeypatch.setattr(service._session_service, "mark_cancelled", fake_mark_cancelled)
    monkeypatch.setattr(service, "_build_provider", fake_build_provider)

    result = await service.cancel_for_event(event_id, reason="calendar_cancelled")

    assert result["status"] == "cancelled"
    assert provider_calls == [provider_session_id]
    assert marked[0][0] == session_id
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py -q --import-mode=importlib
```

Expected: fail with `ModuleNotFoundError` for `lifecycle_service`.

- [ ] **Step 3: Create lifecycle service by moving dispatch behavior**

Create `src/service/meeting_bot/lifecycle_service.py` by moving the current implementation of `MeetingRecordingDispatchService` from `dispatch_service.py`, then rename the class to:

```python
class MeetingRecordingLifecycleService:
    """Owns meeting recording scheduling, rescheduling, and cancellation."""
```

Keep these public methods:

```python
async def sync_event_lifecycle(
    self,
    calendar_event_id: UUID,
    *,
    source: str = "single_event_task",
    allow_cancelled_reinvite: bool = False,
) -> dict[str, str]:
    event = await self._get_calendar_event(calendar_event_id)
    if event is None:
        return {
            "status": "skipped",
            "reason": "event_not_found",
            "calendar_event_id": str(calendar_event_id),
        }
    if (getattr(event, "status", None) or "").lower() == "cancelled":
        return await self.cancel_for_event(calendar_event_id, reason="calendar_cancelled")
    return await self.schedule_for_event(
        calendar_event_id,
        source=source,
        allow_cancelled_reinvite=allow_cancelled_reinvite,
        event=event,
    )
```

```python
async def schedule_for_event(
    self,
    calendar_event_id: UUID,
    *,
    source: str = "single_event_task",
    allow_cancelled_reinvite: bool = False,
    event: CalendarEvent | None = None,
) -> dict[str, str]:
    if event is None:
        event = await self._get_calendar_event(calendar_event_id)
    if event is None:
        return {
            "status": "skipped",
            "reason": "event_not_found",
            "calendar_event_id": str(calendar_event_id),
        }
    return await self._schedule_loaded_event(
        event,
        source=source,
        allow_cancelled_reinvite=allow_cancelled_reinvite,
    )
```

Then move the scheduling-only body of the current `dispatch_event()` into a new
private `_schedule_loaded_event()` method. Its first lines should start at the
current `meeting_url = getattr(event, "conference_link", None)` branch, because
cancelled-event routing now belongs to `sync_event_lifecycle()`.

```python
async def cancel_for_event(
    self,
    calendar_event_id: UUID,
    *,
    reason: str,
) -> dict[str, str]:
    return await self._cancel_existing_recording_for_calendar_cancellation(calendar_event_id)
```

Move the old `dispatch_event()` body into `schedule_for_event()` after removing the top-level cancelled-event branch.

- [ ] **Step 4: Replace old dispatch class with compatibility wrapper**

Replace `src/service/meeting_bot/dispatch_service.py` with:

```python
"""Compatibility wrapper for the meeting recording lifecycle service."""

from __future__ import annotations

from uuid import UUID

from src.service.meeting_bot.lifecycle_service import MeetingRecordingLifecycleService


class MeetingRecordingDispatchService(MeetingRecordingLifecycleService):
    """Backward-compatible name for callers not yet migrated."""

    async def dispatch_event(
        self,
        calendar_event_id: UUID,
        *,
        allow_cancelled_reinvite: bool = False,
    ) -> dict[str, str]:
        return await self.sync_event_lifecycle(
            calendar_event_id,
            allow_cancelled_reinvite=allow_cancelled_reinvite,
        )
```

- [ ] **Step 5: Export lifecycle service**

Modify `src/service/meeting_bot/__init__.py` to export:

```python
from .lifecycle_service import MeetingRecordingLifecycleService
```

and add `"MeetingRecordingLifecycleService"` to `__all__`.

- [ ] **Step 6: Run lifecycle and existing dispatch tests**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py tests/service/meeting_bot/test_meeting_recording_dispatch_service.py -q --import-mode=importlib
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

Run:

```powershell
git add src/service/meeting_bot tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py
git commit -m "refactor: introduce meeting recording lifecycle service"
```

Expected: commit succeeds.

---

## Task 4: Route Worker Tasks Through Lifecycle Service

**Files:**
- Modify: `src/workers/deal_worker/tasks/meeting_bot_tasks.py`
- Modify: `tests/workers/deal_worker/tasks/test_meeting_bot_tasks.py`

- [ ] **Step 1: Update task tests to patch lifecycle service**

In `tests/workers/deal_worker/tasks/test_meeting_bot_tasks.py`, update service patching/import expectations from `MeetingRecordingDispatchService` to `MeetingRecordingLifecycleService` while keeping task names unchanged:

```python
assert send_meeting_recording_task.name == "deal.send_meeting_recording"
assert process_meeting_recording_webhook_task.name == "deal.process_meeting_recording_webhook"
assert send_unsent_meeting_recordings_org_task.name == "deal.send_unsent_meeting_recordings_org"
assert trigger_send_unsent_meeting_recordings_all_orgs_task.name == "deal.trigger_send_unsent_meeting_recordings_all_orgs"
```

- [ ] **Step 2: Modify worker imports and calls**

In `src/workers/deal_worker/tasks/meeting_bot_tasks.py`, replace:

```python
from src.service.meeting_bot import (
    MeetingRecordingDispatchService,
    RecallMeetingRecordingWebhookService,
)
```

with:

```python
from src.service.meeting_bot import (
    MeetingRecordingLifecycleService,
    RecallMeetingRecordingWebhookService,
)
```

Replace:

```python
service = MeetingRecordingDispatchService(UUID(organisation_id))
result = await service.dispatch_event(UUID(calendar_event_id))
```

with:

```python
service = MeetingRecordingLifecycleService(UUID(organisation_id))
result = await service.sync_event_lifecycle(UUID(calendar_event_id))
```

Replace fallback scan construction with:

```python
service = MeetingRecordingLifecycleService(UUID(organisation_id))
result = await service.dispatch_unsent_upcoming()
```

- [ ] **Step 3: Run task contract tests**

Run:

```powershell
uv run pytest tests/workers/deal_worker/tasks/test_meeting_bot_tasks.py tests/workers/deal_worker/test_cron_dispatch.py -q --import-mode=importlib
```

Expected: task names, queue registration, and cron schedule remain unchanged.

- [ ] **Step 4: Commit**

Run:

```powershell
git add src/workers/deal_worker/tasks/meeting_bot_tasks.py tests/workers/deal_worker/tasks/test_meeting_bot_tasks.py
git commit -m "refactor: route meeting bot tasks through lifecycle service"
```

Expected: commit succeeds.

---

## Task 5: Migrate API and Agent Call Sites

**Files:**
- Modify: `src/service/meetings_tab_service.py`
- Modify: `src/service/crm/access/meetings_access_service.py`
- Modify: `src/agents/tools/intelligence/send_meeting_bot_invite.py`
- Modify: corresponding tests under `tests/service`, `tests/api`, and `tests/agents`

- [ ] **Step 1: Add tests that CRM cancel no longer reaches private dispatch fields**

In `tests/service/crm/test_crm_meetings_access_service.py`, add a test that patches `MeetingRecordingLifecycleService.cancel_for_event` and verifies `CRMMeetingRecordingControlService.cancel_bot()` uses it:

```python
@pytest.mark.asyncio
async def test_cancel_bot_delegates_to_lifecycle_service(monkeypatch):
    event_id = uuid4()
    organisation_id = uuid4()
    calls = []

    async def fake_cancel(self, calendar_event_id, *, reason):
        calls.append((calendar_event_id, reason))
        return {"status": "cancelled", "calendar_event_id": str(calendar_event_id)}

    monkeypatch.setattr(
        "src.service.crm.access.meetings_access_service.MeetingRecordingLifecycleService.cancel_for_event",
        fake_cancel,
    )

    service = CRMMeetingRecordingControlService(organisation_id)
    result = await service.cancel_bot(event_id)

    assert result["status"] == "cancelled"
    assert calls == [(event_id, "manual_user_cancel")]
```

- [ ] **Step 2: Update CRM control service**

In `src/service/crm/access/meetings_access_service.py`, replace the private dispatch-service cancellation implementation with:

```python
class CRMMeetingRecordingControlService:
    """Provider-facing recording controls after user-event ownership is proven."""

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id
        self._lifecycle_service = MeetingRecordingLifecycleService(organisation_id)

    async def invite_bot(self, event_id: UUID) -> dict[str, str]:
        return await self._lifecycle_service.schedule_for_event(
            event_id,
            source="crm_manual_invite",
            allow_cancelled_reinvite=True,
        )

    async def cancel_bot(self, event_id: UUID) -> dict[str, str]:
        return await self._lifecycle_service.cancel_for_event(
            event_id,
            reason="manual_user_cancel",
        )
```

Keep user visibility and start-time checks in the API/access layer before calling this service.

- [ ] **Step 3: Update meetings tab manual invite/cancel**

In `src/service/meetings_tab_service.py`, import `MeetingRecordingLifecycleService` and replace direct `MeetingRecordingDispatchService` usage:

```python
return await MeetingRecordingLifecycleService(self.organisation_id).schedule_for_event(
    event_id,
    source="meetings_tab_manual_invite",
    allow_cancelled_reinvite=True,
)
```

Keep `CRMMeetingRecordingControlService.cancel_bot()` for manual cancel if the CRM service is already routed through lifecycle.

- [ ] **Step 4: Update agent tool**

In `src/agents/tools/intelligence/send_meeting_bot_invite.py`, replace `MeetingRecordingDispatchService` with `MeetingRecordingLifecycleService`:

```python
lifecycle_service = MeetingRecordingLifecycleService(context.organisation_id)
dispatch_result = await lifecycle_service.schedule_for_event(
    event.event_id,
    source="agent_tool_manual_invite",
)
```

- [ ] **Step 5: Run call-site tests**

Run:

```powershell
uv run pytest tests/service/test_meetings_tab_service.py tests/service/crm/test_crm_meetings_access_service.py tests/api/routers/crm/test_crm_meetings_handler.py tests/agents/tools/intelligence/test_send_meeting_bot_invite.py -q --import-mode=importlib
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

Run:

```powershell
git add src/service/meetings_tab_service.py src/service/crm/access/meetings_access_service.py src/agents/tools/intelligence/send_meeting_bot_invite.py tests
git commit -m "refactor: migrate meeting bot API controls to lifecycle service"
```

Expected: commit succeeds.

---

## Task 6: Close Cancellation Loopholes

**Files:**
- Modify: `src/service/meeting_bot/lifecycle_service.py`
- Modify: `tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py`
- Modify: `tests/service/integrations/test_calendar_sync_service_incremental.py`

- [ ] **Step 1: Add tests for inactive integration cancellation**

Add to `tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py`:

```python
@pytest.mark.asyncio
async def test_calendar_cancel_does_not_call_active_integration_check(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingRecordingLifecycleService(organisation_id)
    event = SimpleNamespace(event_id=event_id, status="cancelled")

    async def fail_if_called(calendar_event_id):
        raise AssertionError("active integration check must not run for cancellation")

    async def fake_get_calendar_event(calendar_event_id):
        return event

    async def fake_cancel(calendar_event_id, *, reason):
        return {"status": "cancelled", "calendar_event_id": str(calendar_event_id)}

    monkeypatch.setattr(service, "_get_calendar_event", fake_get_calendar_event)
    monkeypatch.setattr(service, "_has_active_calendar_integration", fail_if_called)
    monkeypatch.setattr(service, "cancel_for_event", fake_cancel)

    result = await service.sync_event_lifecycle(event_id)

    assert result["status"] == "cancelled"
```

- [ ] **Step 2: Add tests for terminal/unstarted provider fallback**

```python
@pytest.mark.asyncio
async def test_cancel_terminal_provider_bot_does_not_leave_call(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    session_id = uuid4()
    provider_session_id = "recall-bot-terminal"
    service = MeetingRecordingLifecycleService(organisation_id)
    session_record = SimpleNamespace(
        session_id=session_id,
        status=MeetingRecordingStatus.SENT,
        provider_session_id=provider_session_id,
        provider_payload={},
    )

    class Provider:
        def __init__(self):
            self.leave_calls = []

        async def cancel_session(self, value):
            assert value == provider_session_id
            raise RecallProviderError(
                "cannot delete",
                status_code=405,
                code="cannot_delete_bot",
                detail="Bot is not deletable",
                response_body={"code": "cannot_delete_bot"},
            )

        async def fetch_session(self, value):
            assert value == provider_session_id
            return {"id": provider_session_id, "status": "done"}

        async def leave_call(self, value):
            self.leave_calls.append(value)

    provider = Provider()

    async def fake_get_by_event(calendar_event_id):
        assert calendar_event_id == event_id
        return session_record

    async def fake_build_provider():
        return provider

    async def fake_mark_cancelled(value, **kwargs):
        assert value == session_id
        return session_record

    monkeypatch.setattr(service._session_service, "get_by_event", fake_get_by_event)
    monkeypatch.setattr(service._session_service, "mark_cancelled", fake_mark_cancelled)
    monkeypatch.setattr(service, "_build_provider", fake_build_provider)

    result = await service.cancel_for_event(event_id, reason="calendar_cancelled")

    assert result["status"] == "cancelled"
    assert provider.leave_calls == []
```

- [ ] **Step 3: Implement provider status check before leave-call fallback**

In `MeetingRecordingLifecycleService`, add helper:

```python
async def _cancel_or_leave_provider_session(
    self,
    *,
    provider: RecallMeetingRecordingProvider,
    provider_session_id: str,
) -> None:
    try:
        await provider.cancel_session(provider_session_id)
        return
    except RecallProviderError as error:
        if error.status_code == 404:
            return
        if not self._is_not_deletable_error(error):
            raise

    try:
        bot_payload = await provider.fetch_session(provider_session_id)
    except RecallProviderError as fetch_error:
        if fetch_error.status_code == 404:
            return
        raise

    provider_status = str(bot_payload.get("status") or "").lower()
    if provider_status in {"done", "fatal", "completed", "cancelled"}:
        return

    await provider.leave_call(provider_session_id)
```

Use this helper in calendar cancellation and reschedule cleanup paths.

- [ ] **Step 4: Preserve recurring deletion enqueue tests**

Run:

```powershell
uv run pytest tests/service/integrations/test_calendar_sync_service_incremental.py -q --import-mode=importlib
```

Expected: tests covering cancelled/deleted event enqueue behavior still pass.

- [ ] **Step 5: Run lifecycle tests**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py tests/service/meeting_bot/test_meeting_recording_dispatch_service.py -q --import-mode=importlib
```

Expected: cancellation and compatibility tests pass.

- [ ] **Step 6: Commit**

Run:

```powershell
git add src/service/meeting_bot/lifecycle_service.py tests/service/meeting_bot/test_meeting_recording_lifecycle_service.py tests/service/integrations/test_calendar_sync_service_incremental.py
git commit -m "fix: make meeting bot cancellation independent and terminal-aware"
```

Expected: commit succeeds.

---

## Task 7: Extract Webhook Artifact Service

**Files:**
- Create: `src/service/meeting_bot/artifacts_service.py`
- Create: `tests/service/meeting_bot/test_meeting_recording_artifacts_service.py`
- Modify: `src/service/meeting_bot/webhook_service.py`

- [ ] **Step 1: Write artifact service tests**

Create `tests/service/meeting_bot/test_meeting_recording_artifacts_service.py`:

```python
from __future__ import annotations

from uuid import uuid4

import pytest

from src.service.meeting_bot.artifacts_service import MeetingRecordingArtifactService


def test_safe_artifact_id_strips_unsafe_characters():
    service = MeetingRecordingArtifactService(uuid4())

    assert service.safe_artifact_id("../bad id", "fallback") == "bad_id"
    assert service.safe_artifact_id("", "fallback") == "fallback"


@pytest.mark.asyncio
async def test_archive_video_returns_original_when_download_url_missing():
    service = MeetingRecordingArtifactService(uuid4())
    artifact = {"recording_id": "r1", "video_id": "v1"}

    result = await service.archive_video_artifact(uuid4(), artifact)

    assert result == artifact
```

- [ ] **Step 2: Implement artifact service**

Create `src/service/meeting_bot/artifacts_service.py` by moving `_archive_video_artifact()` and `_safe_artifact_id()` from webhook service:

```python
"""Artifact storage helpers for meeting recording provider media."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from src.infra.config.configs import recall_config
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage


class MeetingRecordingArtifactService:
    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id

    async def archive_video_artifact(
        self,
        event_id: UUID,
        video_artifact: dict[str, str],
    ) -> dict[str, Any]:
        download_url = video_artifact.get("download_url")
        if not download_url:
            return video_artifact

        recording_id = self.safe_artifact_id(video_artifact.get("recording_id"), "recording")
        video_id = self.safe_artifact_id(video_artifact.get("video_id"), "video")
        blob_path = f"meeting_recordings/{event_id}/{recording_id}/{video_id}.mp4"
        storage = AzureOrganisationBlobStorage(self.organisation_id)
        try:
            async with (
                httpx.AsyncClient(timeout=recall_config.RECALL_TIMEOUT_SECONDS) as client,
                client.stream("GET", download_url) as response,
            ):
                response.raise_for_status()
                content_type = response.headers.get("content-type") or "video/mp4"
                await storage.upload_stream(
                    blob_path,
                    response.aiter_bytes(),
                    content_type=content_type,
                )
                result: dict[str, Any] = {
                    **video_artifact,
                    "archived_blob_path": blob_path,
                    "archived_content_type": content_type,
                    "archived_at": datetime.now(UTC).isoformat(),
                }
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    result["archived_size_bytes"] = int(content_length)
                return result
        finally:
            await storage.close()

    @staticmethod
    def safe_artifact_id(value: Any, fallback: str) -> str:
        text = str(value or fallback)
        return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-") or fallback
```

- [ ] **Step 3: Delegate webhook archival to artifact service**

In `src/service/meeting_bot/webhook_service.py`, create `self.artifact_service = MeetingRecordingArtifactService(organisation_id)` in `__init__()` and replace:

```python
video_artifact = await self._archive_video_artifact(
    existing.calendar_event_id,
    video_artifact,
)
```

with:

```python
video_artifact = await self.artifact_service.archive_video_artifact(
    existing.calendar_event_id,
    video_artifact,
)
```

Remove the old private archive methods and now-unused imports.

- [ ] **Step 4: Run webhook and artifact tests**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_meeting_recording_artifacts_service.py tests/service/meeting_bot/test_meeting_recording_webhook_service.py -q --import-mode=importlib
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/service/meeting_bot/artifacts_service.py src/service/meeting_bot/webhook_service.py tests/service/meeting_bot
git commit -m "refactor: extract meeting recording artifact archival"
```

Expected: commit succeeds.

---

## Task 8: Extract Meeting Summary Trigger Service

**Files:**
- Create: `src/service/meeting_bot/summary_trigger_service.py`
- Create: `tests/service/meeting_bot/test_meeting_summary_trigger_service.py`
- Modify: `src/service/meeting_bot/webhook_service.py`

- [ ] **Step 1: Write summary trigger tests for event-scoped jobs**

Create `tests/service/meeting_bot/test_meeting_summary_trigger_service.py` with tests that use the existing helper patterns from `tests/service/meeting_bot/test_meeting_recording_webhook_service.py`:

```python
from __future__ import annotations

from uuid import uuid4

import pytest

from src.service.meeting_bot.summary_trigger_service import MeetingSummaryTriggerService


@pytest.mark.asyncio
async def test_handle_transcript_ready_returns_retryable_materialization_error(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    service = MeetingSummaryTriggerService(organisation_id)

    class Result:
        success = False
        should_retry = False
        error = "Recall transcript artifact is missing download_url"

    async def fake_materialize(value):
        assert value == event_id
        return Result()

    monkeypatch.setattr(service.transcript_service, "materialize_recall_transcript", fake_materialize)

    result = await service.handle_transcript_ready(event_id)

    assert result["transcript_materialized"] is False
    assert result["summary_jobs_created"] == 0
    assert result["summary_jobs_skipped"] == 0
    assert result["transcript_materialization_error"] == "Recall transcript artifact is missing download_url"
```

```python
@pytest.mark.asyncio
async def test_created_summary_job_is_scoped_to_transcript_event(monkeypatch):
    organisation_id = uuid4()
    event_id = uuid4()
    job_id = uuid4()
    service = MeetingSummaryTriggerService(organisation_id)
    created_jobs = []
    enqueued_jobs = []

    class Result:
        success = True
        should_retry = False
        error = None

    async def fake_materialize(value):
        assert value == event_id
        return Result()

    async def fake_create_jobs(value):
        assert value == event_id
        created_jobs.append(
            {
                "event_id": event_id,
                "job_metadata": {
                    "source": "recall_webhook",
                    "trigger": "transcript_done",
                    "send_summary_notification": True,
                    "event_id": str(event_id),
                },
            }
        )
        return {"created": 1, "skipped": 0, "job_ids_to_enqueue": [job_id]}

    async def fake_send_task(task_name, args, queue, routing_key):
        enqueued_jobs.append((task_name, args, queue, routing_key))

    monkeypatch.setattr(service.transcript_service, "materialize_recall_transcript", fake_materialize)
    monkeypatch.setattr(service, "_create_meeting_summary_jobs", fake_create_jobs)
    monkeypatch.setattr(
        "src.service.meeting_bot.summary_trigger_service.send_deal_task_async",
        fake_send_task,
    )

    result = await service.handle_transcript_ready(event_id)

    assert result["transcript_materialized"] is True
    assert result["summary_jobs_created"] == 1
    assert created_jobs[0]["event_id"] == event_id
    assert created_jobs[0]["job_metadata"]["source"] == "recall_webhook"
    assert created_jobs[0]["job_metadata"]["trigger"] == "transcript_done"
    assert created_jobs[0]["job_metadata"]["send_summary_notification"] is True
    assert enqueued_jobs == [
        (
            "meeting_summary_task.process",
            [str(organisation_id), str(job_id)],
            "deal_jobs",
            "deal_jobs",
        )
    ]
```

- [ ] **Step 2: Implement summary trigger service**

Move `_materialize_recall_transcript_and_enqueue_summaries()` and `_create_meeting_summary_jobs()` from `webhook_service.py` into `src/service/meeting_bot/summary_trigger_service.py`.

The new public method is:

```python
async def handle_transcript_ready(self, event_id: UUID) -> dict[str, Any]:
    return await self._materialize_recall_transcript_and_enqueue_summaries(event_id)
```

Keep the existing lock key:

```python
lock_key = f"task_lock:recall_transcript_materialize:{self.organisation_id}:{event_id}"
```

Keep summary job metadata:

```python
{
    "source": "recall_webhook",
    "trigger": "transcript_done",
    "send_summary_notification": True,
    "event_id": str(event_id),
}
```

- [ ] **Step 3: Delegate webhook transcript-ready handling**

In `src/service/meeting_bot/webhook_service.py`, create:

```python
self.summary_trigger_service = MeetingSummaryTriggerService(organisation_id)
```

Replace:

```python
await self._materialize_recall_transcript_and_enqueue_summaries(record.calendar_event_id)
```

with:

```python
await self.summary_trigger_service.handle_transcript_ready(record.calendar_event_id)
```

Remove now-unused imports from webhook service.

- [ ] **Step 4: Run summary trigger and webhook tests**

Run:

```powershell
uv run pytest tests/service/meeting_bot/test_meeting_summary_trigger_service.py tests/service/meeting_bot/test_meeting_recording_webhook_service.py tests/workers/deal_worker/tasks/meeting_insight_tasks/test_meeting_summary_task.py -q --import-mode=importlib
```

Expected: transcript-ready summary creation and notification flags remain event-scoped.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/service/meeting_bot/summary_trigger_service.py src/service/meeting_bot/webhook_service.py tests/service/meeting_bot tests/workers/deal_worker/tasks/meeting_insight_tasks/test_meeting_summary_task.py
git commit -m "refactor: isolate Recall transcript summary trigger"
```

Expected: commit succeeds.

---

## Task 9: Remove Compatibility Imports and Old Integration Package

**Files:**
- Modify: `src/service/meeting_bot/__init__.py`
- Delete: old `src/service/integrations/meeting_recording` package if no imports remain
- Modify: remaining tests/docs that reference old paths

- [ ] **Step 1: Migrate remaining dispatch imports**

Run:

```powershell
rg "MeetingRecordingDispatchService|service\.meeting_bot\.dispatch_service|service\.integrations\.meeting_recording" src tests docs
```

Expected before edits: only compatibility wrapper, old docs, and intentionally unmigrated test names.

Update remaining business code to use:

```python
from src.service.meeting_bot import MeetingRecordingLifecycleService
```

- [ ] **Step 2: Delete compatibility wrapper only after no call sites remain**

When `rg "MeetingRecordingDispatchService" src tests` shows only old dispatch tests, remove `src/service/meeting_bot/dispatch_service.py` and rename `tests/service/meeting_bot/test_meeting_recording_dispatch_service.py` to `tests/service/meeting_bot/test_meeting_recording_lifecycle_service_legacy_cases.py`.

Update imports in that test file:

```python
from src.service.meeting_bot.lifecycle_service import MeetingRecordingLifecycleService
```

Replace class name:

```python
class TestMeetingRecordingLifecycleService:
```

- [ ] **Step 3: Delete old integration provider package**

Run:

```powershell
rg "service\.integrations\.meeting_recording" src tests docs
```

Expected: no output.

Then delete:

```powershell
git rm src\service\integrations\meeting_recording\providers\__init__.py
git rm src\service\integrations\meeting_recording\__init__.py
```

- [ ] **Step 4: Run broad meeting-bot test slice**

Run:

```powershell
uv run pytest tests/service/meeting_bot tests/service/test_meetings_tab_service.py tests/service/crm/test_crm_meetings_access_service.py tests/api/routers/meeting_bot/test_recall_webhook_handler.py tests/agents/tools/intelligence/test_send_meeting_bot_invite.py -q --import-mode=importlib
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src tests docs
git commit -m "refactor: remove legacy meeting recording integration package"
```

Expected: commit succeeds.

---

## Task 10: Update Docs and Operational Notes

**Files:**
- Modify: `docs/Technical Documentation/meeting-bot-recall-service-redesign.md`
- Modify: `docs/Technical Documentation/meeting-bot-recall-webhooks.md`
- Modify: `docs/API Documentation/meetings-tab-api.md`
- Modify: `docs/API Documentation/deals/crm-meetings-api.md`
- Modify: `src/service/meeting_bot/README.md`

- [ ] **Step 1: Create service README**

Create `src/service/meeting_bot/README.md`:

```markdown
# Meeting Bot Services

`MeetingRecordingLifecycleService` owns scheduling, rescheduling, and cancellation.
`MeetingRecordingSessionService` owns `meeting_recording_sessions` writes.
`RecallWebhookService` maps provider callbacks to local state.
`MeetingSummaryTriggerService` handles transcript-ready summary jobs.
`MeetingRecordingArtifactService` archives provider media.
Provider-specific HTTP code lives under `providers/`.

Calendar sync, meetings APIs, deal matching, and agent tools should not call Recall
directly. They enqueue or delegate to the lifecycle service.
```

- [ ] **Step 2: Update redesign doc current state**

In `docs/Technical Documentation/meeting-bot-recall-service-redesign.md`, change target-language sections to implemented-language once tasks are complete and ensure the tree map matches actual files.

- [ ] **Step 3: Update webhook doc import path**

In `docs/Technical Documentation/meeting-bot-recall-webhooks.md`, update the service reference to:

```text
`src/service/meeting_bot/webhook_service.py`
```

and add:

```text
Transcript-ready follow-up is delegated to `src/service/meeting_bot/summary_trigger_service.py`.
Video archival is delegated to `src/service/meeting_bot/artifacts_service.py`.
```

- [ ] **Step 4: Confirm API docs do not promise changed routes**

Verify the documented endpoints remain:

```text
POST /users/meetings/{event_id}/bot/invite
DELETE /users/meetings/{event_id}/bot/invite
POST /users/crm/meetings/{event_id}/recording-bot
DELETE /users/crm/meetings/{event_id}/recording-bot
POST /users/meeting-bot/recall/webhook
```

- [ ] **Step 5: Commit**

Run:

```powershell
git add docs src/service/meeting_bot/README.md
git commit -m "docs: document meeting bot service boundaries"
```

Expected: commit succeeds.

---

## Task 11: Final Verification

**Files:**
- No new code files unless verification exposes a defect.

- [ ] **Step 1: Run focused meeting-bot regression suite**

Run:

```powershell
uv run pytest tests/service/meeting_bot tests/service/test_meetings_tab_service.py tests/service/crm/test_crm_meetings_access_service.py tests/service/integrations/test_calendar_sync_service_incremental.py tests/service/integrations/test_transcript_service.py tests/api/routers/meeting_bot/test_recall_webhook_handler.py tests/api/routers/crm/test_crm_meetings_handler.py tests/agents/tools/intelligence/test_send_meeting_bot_invite.py tests/workers/deal_worker/tasks/test_meeting_bot_tasks.py tests/workers/deal_worker/test_cron_dispatch.py tests/workers/deal_worker/services/test_deal_meeting_matcher_recording_dispatch.py -q --import-mode=importlib
```

Expected: all tests pass.

- [ ] **Step 2: Run lint and compile checks for changed Python files**

Run:

```powershell
uv run ruff check src/service/meeting_bot src/service/meetings_tab_service.py src/service/crm/access/meetings_access_service.py src/service/integrations/transcripts/service.py src/service/integrations/calendar/sync_service.py src/workers/deal_worker/tasks/meeting_bot_tasks.py src/agents/tools/intelligence/send_meeting_bot_invite.py tests/service/meeting_bot
```

Expected: no ruff errors.

Run:

```powershell
uv run python -m py_compile src/service/meeting_bot/lifecycle_service.py src/service/meeting_bot/eligibility_service.py src/service/meeting_bot/webhook_service.py src/service/meeting_bot/artifacts_service.py src/service/meeting_bot/summary_trigger_service.py src/service/meeting_bot/providers/recall_client.py src/service/meeting_bot/providers/recall_webhook_verifier.py
```

Expected: command exits 0.

- [ ] **Step 3: Run stale-import searches**

Run:

```powershell
rg "MeetingRecordingDispatchService|service\.integrations\.meeting_recording|src/service/integrations/meeting_recording" src tests docs
```

Expected: no output.

Run:

```powershell
rg "deal\.send_meeting_recording|deal\.process_meeting_recording_webhook|deal\.send_unsent_meeting_recordings_org|deal\.trigger_send_unsent_meeting_recordings_all_orgs" src tests docs
```

Expected: task names still appear in worker tasks, config, cron tests, docs, and producer tests.

- [ ] **Step 4: Verify git diff scope**

Run:

```powershell
git diff --stat
git diff --name-only
```

Expected: only meeting-bot/refactor docs, tests, and direct call-site files are changed.

- [ ] **Step 5: Final commit**

If verification caused final cleanup changes:

```powershell
git add src tests docs
git commit -m "test: verify meeting bot service refactor"
```

Expected: commit succeeds or there is nothing to commit.

---

## Acceptance Criteria

- Recall provider code lives under `src/service/meeting_bot/providers/`.
- Business code no longer imports from `src.service.integrations.meeting_recording`.
- `MeetingRecordingLifecycleService` is the only service that schedules, reschedules, or cancels provider bots.
- `MeetingRecordingSessionService` remains the only writer for `meeting_recording_sessions`.
- Calendar cancellation of an existing session does not require active `KnowledgeIntegration`.
- Terminal/unstarted Recall bots are not blindly sent `leave_call`.
- Webhook service delegates video archival and transcript-ready summary creation.
- `transcript.done` creates/enqueues only jobs for the webhook event.
- Notification delivery remains job-scoped through `send_summary_notification=True`.
- Meetings tab, CRM meetings, agent tool, calendar sync, and deal matcher behavior remain externally compatible.
- Celery task names, queue registration, and cron schedule are unchanged.
- Focused regression suite, ruff, py_compile, and stale-import searches pass.

---

## Execution Notes

- Do the work in dev/code only. Do not trigger prod pipeline or run prod Kubernetes commands during this refactor.
- Keep commits per task so rollback is simple if a loophole test exposes an architectural mistake.
- If a task gets too large, stop after tests are written and split the implementation before editing more files.
- Do not add a new database table or dependency for this refactor.
- Do not change API routes, response schema field names, Celery task names, or queue names.
