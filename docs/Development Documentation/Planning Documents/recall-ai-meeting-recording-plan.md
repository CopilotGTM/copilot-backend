# Recall.ai Meeting Recording Plan
## Status
PLANNING
## Goal
Use Recall.ai to join deal-related meetings, fetch the final transcript, and feed that transcript into the existing CopilotGTM meeting summary flow.
## Chosen Approach
We will use Recall option 3.
- Keep our own calendar sync.
- Keep our own deal-association logic.
- Use Recall only for bot join, recording lifecycle, and transcript delivery.
This fits the repo because we already have `calendar_events`, `calendar_event_deal_associations`,
`meetings`, `transcript_segments`, and org-scoped Celery worker patterns.
## Main Product Rule
We are not recording every meeting.
We only record meetings that are:
- in the organization
- not cancelled
- linked to at least one active deal
- carrying a usable meeting URL
Primary behavior:
- when a meeting is newly assigned to a deal, we immediately create/update the
  `meeting_recording_sessions` row and send it to Recall
- we do not wait for the hourly task for this primary path
## Core Decisions
1. Recording is organization-scoped, not user-scoped.
2. One meeting occurrence gets one bot per organization.
3. If three users sync the same event, we still create one Recall bot.
4. Deal association decides whether we record.
5. Recall decides how the bot joins and how transcript events arrive.
6. Existing `meetings` and `transcript_segments` remain the final transcript store.
7. Recall lifecycle state must live in a separate orchestration table.
## Architecture
```text
calendar sync / deal association update
  -> recording reconcile task
  -> meeting recording orchestrator
  -> provider adapter (Recall)
  -> Recall bot joins meeting
  -> Recall webhook arrives
  -> transcript fetch task
  -> meetings + transcript_segments
  -> existing summary / risk / stakeholder pipelines
```
## Provider Boundary
Create a small provider interface:
- `create_session(...)`
- `cancel_session(...)`
- `fetch_session(...)`
- `fetch_transcript(...)`
- `map_webhook(...)`
First implementation: `RecallMeetingRecordingProvider`
Future implementations can add native or other vendor adapters behind the same interface.
## New Table
Add a new org-scoped table:
- `meeting_recording_sessions`
This table is needed because `meetings` only tells us whether transcript data exists.
It does not tell us:
- whether a Recall bot was scheduled
- whether the join failed
- whether the meeting was cancelled before join
- which Recall bot maps to which event
- whether transcript delivery is pending or failed
## Step 1 Data Contract
This is the exact first-version contract for `meeting_recording_sessions`.
The row is created as soon as a meeting becomes deal-linked and eligible for recording.
It is not created only after Recall accepts the meeting.
Required columns:
- `session_id`: internal UUID primary key
- `organisation_id`: org boundary for every query
- `calendar_event_id`: FK to `calendar_events`
- `provider`: initial value `RECALL_AI`
- `dedupe_key`: stable value `meeting-recording:{organisation_id}:{event_id}:{provider}`
- `meeting_url`: join URL that will be sent to Recall
- `join_at`: planned provider join time
- `status`: internal lifecycle status
- `transcript_status`: internal transcript lifecycle status
- `provider_session_id`: Recall bot/session id, nullable until accepted
- `provider_status`: latest raw provider lifecycle string, nullable
- `sent_to_provider_at`: nullable; set only after Recall create-bot succeeds
- `last_webhook_at`: nullable; last successfully processed Recall webhook time
- `status_message`: nullable operational message for failures or suppression
- `retry_count`: integer retry counter for send/reconcile attempts
- `provider_payload`: latest raw provider payload snapshot for debugging
- `created_at`, `updated_at`
Internal `status` values:
- `PENDING_SEND`: row created, eligible, not yet sent to Recall
- `SEND_QUEUED`: send task queued but Recall has not yet accepted
- `SENT`: Recall create-bot accepted and `provider_session_id` is stored
- `JOINING`: Recall says bot is joining or waiting to join
- `IN_CALL`: Recall says bot is in the meeting / recording
- `COMPLETED`: provider lifecycle finished successfully
- `CANCELLED`: meeting no longer eligible or provider session cancelled
- `FAILED`: unrecoverable create/join/provider failure
Internal `transcript_status` values:
- `NOT_REQUESTED`: session exists but transcript is not yet expected
- `PENDING`: waiting for transcript-ready signal or transcript fetch completion
- `READY`: transcript fetched and materialized into `meetings` / `transcript_segments`
- `FAILED`: transcript should have existed but processing failed
- `NOT_AVAILABLE`: provider completed but no transcript is available
Rules for "not sent yet" versus "sent":
- "not sent yet" means `sent_to_provider_at IS NULL`
- "sent" means `sent_to_provider_at IS NOT NULL`
- the hourly fallback task must use `sent_to_provider_at IS NULL` as the primary unsent check
- event-based assignment creates the row first, then the send task moves it to `SEND_QUEUED`
- only a successful Recall create call sets `sent_to_provider_at` and moves status to `SENT`
Rules for lifecycle ownership:
- `status` is our normalized orchestration state
- `provider_status` keeps the latest raw Recall state without leaking Recall semantics into the app
- `provider_payload` stores the latest raw payload snapshot, not an append-only audit log
- transcript materialization remains in `meetings` and `transcript_segments`, not this table
## Unique Keys
- unique active `(organisation_id, calendar_event_id, provider)`
- unique `provider_session_id`
- unique `dedupe_key`
Recommended dedupe key:
`meeting-recording:{organisation_id}:{event_id}:{provider}`
## Existing Tables To Reuse
- `calendar_events`
- `calendar_event_deal_associations`
- `meetings`
- `transcript_segments`
For Recall transcripts, store provider-specific data in `Meeting.source_data`.
Do not bake Recall-only fields into the core transcript schema.
## Internal Event Model
Use Celery tasks as the internal event transport.
Do not introduce Kafka or another event bus for this slice.
Tasks to add:
- `meeting_recording.reconcile_event`
- `meeting_recording.reconcile_org`
- `meeting_recording.process_provider_webhook`
- `meeting_recording.fetch_transcript`
- `meeting_recording.reconcile_upcoming_all_orgs`
## Event Sources
1. Primary event-based path:
   when a meeting becomes linked to a deal, directly create/update the
   `meeting_recording_sessions` row and enqueue send-to-Recall
2. When a deal link is removed or cancelled:
   enqueue `meeting_recording.reconcile_event`
3. After event cancellation or meeting URL change:
   enqueue `meeting_recording.reconcile_event`
4. Temporary fallback task:
   every hour, scan upcoming meetings that are deal-linked but not yet sent to
   Recall, then create/update the session row and send them
5. Future cleanup:
   remove the hourly fallback once event-based assignment is proven stable
## Eligibility Rules
A meeting should have a recording session only when:
- it belongs to the org
- it is not cancelled
- it has at least one active deal association
- it has a supported meeting URL
- it is inside the scheduling horizon
Recommended defaults:
- lead time before join: `60-120s`
- scheduling horizon: `7 days`
- late join grace window: `15 minutes after start`
## Recall Create Flow
When a meeting becomes eligible:
1. build the dedupe key
2. create or update `meeting_recording_sessions` row
3. if not already sent, call Recall create bot API with:
   - `meeting_url`
   - `join_at`
   - `metadata`
   - transcript config
4. save Recall bot/session ID
Recommended Recall metadata:
- `organisation_id`
- `calendar_event_id`
- `meeting_recording_session_id`
- `deal_ids`
- `environment`
## Webhook Design
The Recall webhook endpoint must:
- verify signature
- return `2xx` quickly
- enqueue async processing
- never do heavy transcript work inline
Webhook processing must be idempotent.
Treat delivery as at-least-once.
Important rule:
- never trust Recall payload metadata alone
- always map webhook data to `meeting_recording_sessions` first
## Transcript Flow
When Recall reports transcript completion:
1. load the mapped `meeting_recording_sessions` row
2. fetch the final transcript from Recall
3. normalize transcript into `Meeting` and `TranscriptSegment`
4. update session transcript state
5. trigger the existing meeting-summary pipeline
This avoids building a second transcript system.
Main code areas:
- `src/service/meeting_bot/*`
- `src/service/integrations/meeting_recording/providers/*`
- `src/workers/deal_worker/tasks/meeting_bot_tasks.py`
- `src/api/routers/meeting_bot/recall_webhook_handler.py`
- `src/domain/models/organisation_tables/sync/meeting_recording.py`
## Security Rules
1. Always enforce org isolation through DB lookup.
2. Never update transcript data from a webhook without mapping it to the org first.
3. Keep Recall API keys and webhook verification secrets environment-scoped.
4. Do not hold DB sessions while calling Recall APIs or fetching transcripts.
## Explicit Failure States
Track cases like:
- `no_supported_meeting_url`
- `provider_join_failed`
- `provider_denied`
- `meeting_cancelled_before_join`
- `transcript_not_available`
- `transcript_failed`
## Observability
Add logs and metrics for session creation/dedupe/cancellation, webhook verification failures,
transcript completion latency, provider error rates, recordings per org, and transcript success rate.
Use correlation keys: `organisation_id`, `event_id`, `meeting_recording_session_id`,
`provider`, `provider_session_id`.
## Rollout
### Phase 1
- add `meeting_recording_sessions`
- add Recall provider client
- add webhook endpoint
- add transcript materialization
- add direct event-based send when a meeting is assigned to a deal

### Phase 2
- add hourly fallback task for upcoming deal-linked meetings not yet sent
- add cancellation handling
- validate dedupe and sent-state handling
### Phase 3
- add admin/debug visibility
- add replay tooling for failed webhook/transcript flows
- add dashboards and alerts
- add cost monitoring
### Phase 4
- remove the hourly fallback task after event-based flow is stable
- plug in a second provider or native bot if needed
## Biggest Risks
- Meeting platform lobby or waiting-room behavior may block join success.
- Sync-driven upstream input may schedule bots later than ideal.
- One meeting may map to multiple deals; transcript must still be captured once.
- Recall-specific logic must not leak into transcript models.
## Final Recommendation
Build Recall as a provider adapter behind a generic meeting recording orchestration layer.
Most important implementation rules:
- recording is driven by deal association state
- session lifecycle is stored in `meeting_recording_sessions`
- transcript output still lands in `meetings` and `transcript_segments`
- orchestration is event-driven with scheduled fallback
- provider logic stays isolated so Recall can be replaced later
