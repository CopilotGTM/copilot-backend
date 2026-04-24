# Recall.ai Meeting Recording Execution Plan
## Goal
Implement the first production slice of Recall-based meeting recording with:
- direct event-based send when a meeting is assigned to a deal
- an hourly fallback task for upcoming deal-linked meetings not yet sent
- webhook-driven transcript ingestion into the existing meeting pipeline

## Step 1: Finalize data contract
- Define the exact responsibility of `meeting_recording_sessions`.
- Finalize the minimum columns, unique keys, and status values.
- Decide how to mark a meeting as "not sent", "sent", "joined", "done", or "failed".
- Confirm which fields stay provider-agnostic and which stay inside provider payload JSON.

## Step 2: Add persistence layer
- Create the new model and migration for `meeting_recording_sessions`.
- Add a small service responsible for create, update, lookup, and dedupe checks.
- Make sure org isolation is enforced in every query path.
- Keep this layer generic so Recall is only one provider, not a schema assumption.

## Step 3: Build the Recall provider adapter
- Add a Recall client responsible for create bot, cancel bot, fetch bot state, and fetch transcript.
- Normalize Recall responses into internal provider-agnostic objects.
- Add error classification so retries only happen for transient provider failures.
- Keep Recall request/response handling out of deal and transcript services.

## Step 4: Implement direct event-based send
- Hook into the flow where a meeting becomes newly associated to a deal.
- At that point, create or update the `meeting_recording_sessions` row immediately.
- If the row has not already been sent, enqueue the send-to-Recall task at once.
- This becomes the main path and should not wait for any scheduled fallback.

## Step 5: Implement hourly fallback task
- Add an org-level or all-org task that runs every hour for now.
- The task scans upcoming meetings that are deal-linked but not yet sent to Recall.
- For each eligible meeting, create or update the session row and send it if still unsent.
- Treat this as temporary operational safety, not the long-term primary design.

## Step 6: Add cancellation and reconciliation rules
- Handle cases where the meeting is cancelled, the deal link is removed, or the meeting URL changes.
- Reconcile existing session state before sending duplicate create requests.
- Cancel or suppress provider work when the meeting is no longer eligible.
- Keep dedupe centered on `organisation_id + calendar_event_id + provider`.

## Step 7: Add Recall webhook ingestion
- Add a webhook endpoint that verifies signatures and returns quickly.
- Resolve incoming webhook events to `meeting_recording_sessions` first.
- Enqueue async processing for lifecycle updates and transcript-ready events.
- Make webhook processing idempotent because Recall delivery is at-least-once.

## Step 8: Materialize transcript into existing tables
- When Recall signals transcript completion, fetch the final transcript from Recall.
- Normalize it into `Meeting` and `TranscriptSegment`.
- Update the recording session state to reflect transcript completion or failure.
- Trigger the existing meeting-summary pipeline after transcript materialization succeeds.

## Step 9: Add observability and operational visibility
- Add structured logs for send, dedupe, cancellation, webhook processing, and transcript completion.
- Add metrics for sent meetings, failed joins, transcript success rate, and fallback-task catches.
- Add enough admin/debug visibility to inspect one event end-to-end.
- Make it easy to answer why a given meeting was or was not sent to Recall.

## Step 10: Validate and harden
- Test the event-based path, the hourly fallback path, and dedupe behavior together.
- Test webhook idempotency and transcript materialization on repeated delivery.
- Validate that only deal-linked upcoming meetings are sent.
- Once the event-based path is stable in production, remove the hourly fallback task.

## Execution Order
Follow this order:
1. data contract and persistence
2. Recall adapter
3. event-based send
4. hourly fallback
5. webhook ingestion
6. transcript materialization
7. observability and hardening

## Success Criteria
- A newly deal-linked upcoming meeting is recorded in `meeting_recording_sessions` and sent to Recall immediately.
- An upcoming deal-linked meeting missed by the event path is caught by the hourly task.
- Recall webhook events update the correct org-scoped session row.
- Final transcript lands in existing meeting transcript tables and triggers downstream summary flow.
- Duplicate bots are not created for the same org meeting.
