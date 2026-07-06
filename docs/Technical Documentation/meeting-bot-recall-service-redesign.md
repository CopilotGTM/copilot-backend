# Recall meeting bot service redesign

This document describes the target structure for the Recall meeting bot backend.
The goal is to make the integration simple, explicit, and production-ready without
turning it into a new platform.

## Problem

Before this refactor, Recall was wired through a fuzzy set of responsibilities:

- The old dispatch service owned eligibility, session persistence,
  provider calls, cancellation, reschedule handling, fallback scanning, and
  reconciliation.
- `CRMMeetingRecordingControlService` reaches into private dispatch internals for
  cancellation.
- The Recall provider client lived under service integrations, which made it look
  like a user-owned integration. It is not. Recall is platform infrastructure
  used by meeting recording.
- Webhook handling mixes provider state updates, artifact download, transcript
  materialization, summary job creation, and notification triggering.
- Calendar cancellation is handled indirectly by sending the event through the
  same dispatch method that also schedules bots.

The production issue this structure creates is not only readability. It makes it
hard to answer questions like:

- Why was a bot scheduled?
- Why was a bot not cancelled?
- Is a cancellation blocked because the user integration is inactive?
- Did a webhook create a summary job, or did a background scan do it?
- Which service owns the external Recall side effect?

## Design principles

- Recall is not a `KnowledgeIntegration` and should not be modeled as a user
  integration.
- `meeting_recording_sessions` remains the durable source of truth for local
  recording lifecycle.
- Calendar sync owns calendar state. Meeting bot services react to calendar
  state.
- Cancellation must run for an existing recording session even if the calendar
  integration row is inactive or gone.
- Scheduling can still require the product's eligibility rules, but those rules
  should be explicit and isolated.
- Webhooks should update provider state first, then delegate follow-up work.
- Every external Recall mutation should be idempotent around
  `organisation_id + calendar_event_id + provider`.
- The smallest production-ready refactor is preferred over a broad rewrite.

## Target tree map

```text
src/service/meeting_bot/
|-- __init__.py
|-- lifecycle_service.py
|-- eligibility_service.py
|-- session_service.py
|-- recording_window.py
|-- webhook_service.py
|-- summary_trigger_service.py
|-- artifacts_service.py
|-- providers/
|   |-- __init__.py
|   |-- recall_client.py
|   `-- recall_webhook_verifier.py
`-- README.md

src/workers/deal_worker/tasks/
`-- meeting_bot_tasks.py

src/api/routers/meeting_bot/
`-- recall_webhook_handler.py

src/service/crm/access/
`-- meetings_access_service.py

src/domain/models/organisation_tables/sync/
`-- meeting_recording.py
```

## Service ownership

### `MeetingRecordingLifecycleService`

Owns meeting recording business flow.

Public methods:

```python
schedule_for_event(calendar_event_id: UUID, *, source: str) -> dict[str, str]
cancel_for_event(calendar_event_id: UUID, *, reason: str) -> dict[str, str]
sync_event_lifecycle(calendar_event_id: UUID, *, source: str) -> dict[str, str]
dispatch_unsent_upcoming() -> dict[str, int]
```

Responsibilities:

- Load the org-scoped `CalendarEvent`.
- Decide whether the event should schedule, reschedule, cancel, or skip.
- Call `MeetingRecordingEligibilityService` for scheduling eligibility.
- Call `MeetingRecordingSessionService` for all local state transitions.
- Call `RecallMeetingBotClient` for create, update, delete, leave, fetch, and list.
- Keep all Recall side effects behind idempotent session transitions.

Non-responsibilities:

- It does not parse raw Recall webhooks.
- It does not create meeting summary jobs.
- It does not know CRM visibility rules.
- It does not directly inspect `KnowledgeIntegration` except through eligibility
  rules used for automatic scheduling.

### `MeetingRecordingEligibilityService`

Owns scheduling rules only.

Responsibilities:

- Meeting must exist in the current organisation.
- Meeting must not be cancelled.
- Meeting must have a conference link.
- Meeting must be in the dispatch window.
- Automatic scheduling can require active calendar/user state if that remains a
  product rule.

Important rule:

Cancellation does not use this service. If a `meeting_recording_sessions` row
exists, cancellation is allowed to reconcile provider state.

### `MeetingRecordingSessionService`

Keeps the existing durable state boundary.

Responsibilities:

- Create or reuse a pending recording session.
- Atomically claim a send.
- Mark sent, failed, cancelled, completed, joining, in-call, and transcript-ready
  states.
- Lookup by event id and provider session id.
- Store provider payload snapshots needed for debugging.

This service should remain the only writer for `meeting_recording_sessions`.

### `RecallMeetingBotClient`

Pure Recall API adapter.

Move from:

```text
src/service/meeting_bot/providers/recall_client.py
```

To:

```text
src/service/meeting_bot/providers/recall_client.py
```

Responsibilities:

- Build Recall API URLs by region.
- Create, update, delete, leave, fetch, and list bots.
- Extract transcript and mixed-video artifacts from Recall payloads.
- Raise typed `RecallProviderError`.

Non-responsibilities:

- It does not know `CalendarEvent`.
- It does not know `MeetingRecordingSession`.
- It does not create jobs or write database state.

### `RecallWebhookService`

Owns provider callback state mapping.

Responsibilities:

- Extract `organisation_id` from bot metadata.
- Resolve local session by provider bot id inside that organisation.
- Map Recall events to local recording and transcript statuses.
- Store the last webhook payload and provider snapshot.
- Delegate artifact download to `MeetingRecordingArtifactService`.
- Delegate transcript-ready follow-up to `MeetingSummaryTriggerService`.

Non-responsibilities:

- It does not decide which future meetings should get bots.
- It does not create provider bots.
- It does not contain summary job SQL inline.

### `MeetingSummaryTriggerService`

Owns transcript-ready follow-up.

Responsibilities:

- Materialize the Recall transcript for one calendar event.
- Create or reuse the exact meeting summary jobs needed for that event.
- Enqueue only those durable job ids.
- Set `send_summary_notification=True` only on jobs created from the webhook
  transcript-ready path.

This directly protects against old-summary replays: notification work should be
scoped to the specific job created for the specific webhook event.

### `MeetingRecordingArtifactService`

Owns large media artifact handling.

Responsibilities:

- Download `video_mixed.done` assets.
- Store MP4 files under the org blob container.
- Return archived blob metadata for `provider_payload`.

This keeps webhook logic from owning storage streaming details.

## Event flow

### Scheduling flow

```text
calendar sync / meetings tab / deal matcher / agent tool
  -> deal.send_meeting_recording task
  -> MeetingRecordingLifecycleService.sync_event_lifecycle(event_id)
      -> load CalendarEvent
      -> if cancelled: cancel_for_event(event_id)
      -> else: schedule_for_event(event_id)
          -> eligibility check
          -> get/create session
          -> claim send
          -> RecallMeetingBotClient.create_or_update
          -> mark sent
```

### Cancellation flow

```text
calendar event becomes cancelled or all user states are deleted
  -> deal.send_meeting_recording task
  -> MeetingRecordingLifecycleService.sync_event_lifecycle(event_id)
      -> load CalendarEvent
      -> cancel_for_event(event_id, reason="calendar_cancelled")
          -> lookup existing session
          -> if no session: skip
          -> if provider bot id exists: delete scheduled bot
          -> if bot cannot be deleted and is in-call: leave call
          -> mark local session cancelled
```

Key behavior:

- Cancellation does not require an active calendar integration.
- Cancellation does not create a new recording session.
- Cancellation is idempotent if Recall returns 404 or the local row is already
  cancelled without a retryable error.

### Webhook flow

```text
Recall webhook
  -> FastAPI route verifies signature and dedupes webhook id
  -> deal.process_meeting_recording_webhook task
  -> RecallWebhookService.process(payload)
      -> resolve org from bot metadata
      -> resolve session by provider bot id
      -> update recording/transcript state
      -> on video_mixed.done: archive MP4
      -> on transcript.done: MeetingSummaryTriggerService.handle_transcript_ready(event_id)
          -> materialize transcript
          -> create/reuse summary jobs for this event
          -> enqueue only those job ids
```

## Current-to-target rename map

| Current | Target |
| --- | --- |
| `meeting_bot/dispatch_service.py` | Split into `lifecycle_service.py` and `eligibility_service.py` |
| `meeting_bot/session_service.py` | Keep as `session_service.py` |
| `meeting_bot/webhook_service.py` | Keep name, reduce to webhook state mapping |
| `meeting_bot/summary_notification_service.py` | Keep for notification delivery |
| `integrations/meeting_recording/providers/recall_provider.py` | Move to `meeting_bot/providers/recall_client.py` |
| `integrations/meeting_recording/providers/recall_webhook_verifier.py` | Move to `meeting_bot/providers/recall_webhook_verifier.py` |
| Inline transcript/summary logic in webhook service | Move to `summary_trigger_service.py` |
| Inline MP4 archival in webhook service | Move to `artifacts_service.py` |

## Worker task contract

Keep the current Celery task names to avoid deployment churn:

- `deal.send_meeting_recording`
- `deal.process_meeting_recording_webhook`
- `deal.send_unsent_meeting_recordings_org`
- `deal.trigger_send_unsent_meeting_recordings_all_orgs`

Only the implementation target changes:

```python
MeetingRecordingLifecycleService(organisation_id).sync_event_lifecycle(event_id)
RecallWebhookService(organisation_id).process(payload)
MeetingRecordingLifecycleService(organisation_id).dispatch_unsent_upcoming()
```

## Production requirements

### Idempotency

- Local uniqueness remains `organisation_id + calendar_event_id + provider`.
- Provider metadata must include `organisation_id`, `calendar_event_id`, and
  `dedupe_key`.
- Webhook processing resolves by provider bot id only inside the org recovered
  from metadata.
- Summary jobs are created with event/job guards and only pending durable jobs
  are enqueued.

### Failure handling

- Recall 404 during cancel is treated as already cleaned up.
- Recall delete failures that indicate an active/non-deletable bot should attempt
  `leave_call` only when appropriate.
- Provider errors are stored on `provider_payload.last_provider_error` or
  `provider_payload.last_cancel_error`.
- A failed provider call marks the session failed or cancelled with enough detail
  for support/debugging.

### Observability

Each lifecycle action should log structured fields:

- `event`
- `organisation_id`
- `calendar_event_id`
- `session_id`
- `provider_session_id`
- `source`
- `reason`
- `provider_code`
- `provider_detail`

Important log event names:

- `meeting_recording_schedule_requested`
- `meeting_recording_schedule_skipped`
- `meeting_recording_provider_create_succeeded`
- `meeting_recording_cancel_requested`
- `meeting_recording_provider_cancel_succeeded`
- `meeting_recording_provider_cancel_failed`
- `recall_webhook_processed`
- `recall_webhook_summary_jobs_enqueued`

### Security

- Webhook route must fail closed when `RECALL_WEBHOOK_SECRET` is missing.
- Signature verification must use the raw request body.
- Recall credentials remain environment/key-vault backed.
- Provider callbacks must not update sessions across org boundaries.

## Rollout plan

### Phase 1: Move provider boundary

- Move Recall provider and verifier under `src/service/meeting_bot/providers/`.
- Update imports only.
- Keep behavior unchanged.
- Run provider and webhook tests.

### Phase 2: Extract lifecycle service

- Introduce `MeetingRecordingLifecycleService`.
- Move public dispatch methods from `MeetingRecordingLifecycleService` into it.
- Keep `MeetingRecordingLifecycleService` as a temporary compatibility wrapper if
  needed.
- Update Celery task, CRM control service, meetings tab service, and agent tool
  to call the lifecycle service.

### Phase 3: Extract eligibility service

- Move scheduling-only rules into `MeetingRecordingEligibilityService`.
- Make cancellation bypass eligibility.
- Add tests proving inactive calendar integration does not block cancellation of
  an existing session.

### Phase 4: Extract webhook follow-up services

- Move MP4 archive logic into `MeetingRecordingArtifactService`.
- Move transcript materialization and job creation into
  `MeetingSummaryTriggerService`.
- Add tests proving `transcript.done` enqueues only jobs for the webhook event.

### Phase 5: Delete compatibility layer

- Remove the old dispatch compatibility wrapper once all call sites use
  `MeetingRecordingLifecycleService`.
- Remove the legacy meeting recording integration package if no imports remain.
- Update docs and run focused regression tests.

## Minimum test coverage

Focused tests should cover:

- Scheduling an eligible future meeting creates one provider bot.
- Replaying the same send does not create a duplicate bot.
- Rescheduled meeting updates or replaces the provider bot.
- Cancelled calendar event cancels an existing provider bot.
- Cancelled calendar event does not require active calendar integration.
- Manual remove rejects in-call/completed sessions.
- Webhook `transcript.done` materializes transcript and creates only event-scoped
  summary jobs.
- Webhook duplicate delivery does not create duplicate summary jobs.
- Webhook org metadata prevents cross-org session updates.

## Deliberate non-goals

- No new database table is needed for this refactor.
- No new dependency is needed.
- No change to Celery task names is needed.
- No Recall API webhook-management automation is included here.
- No broad rewrite of calendar sync is needed.

## Open product decision

The only product decision that should be confirmed before implementation:

Should automatic bot scheduling apply to all eligible synced calendar meetings,
or only to meetings that are linked to a deal / created from the meetings tab /
explicitly requested?

The service structure supports either. The recommended default is:

- automatic scheduling follows the existing product surface for now;
- cancellation always follows existing `meeting_recording_sessions` rows,
  independent of user integration state.
