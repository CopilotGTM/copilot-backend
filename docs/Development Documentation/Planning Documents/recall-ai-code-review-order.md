# Recall.ai Code Review Order

## Purpose
- Use this document to review the Recall.ai integration in the same order the code executes.
- Start from configuration, then move to persistence, outbound dispatch, and finally inbound webhook handling.
- This is the fastest way to understand the current end-to-end Recall meeting bot flow.

## Current Ownership
- `src/service/meeting_bot/` owns internal meeting-bot lifecycle code.
- `src/service/integrations/meeting_recording/providers/` owns Recall-specific provider code.
- `src/api/routers/meeting_bot/` owns the webhook HTTP ingress.
- `src/workers/deal_worker/tasks/meeting_bot_tasks.py` is the Celery entry point.

## 1. Configuration First
- File: `src/infra/config/configs.py`
- Review the `RecallConfig` class first.
- This file defines the operational inputs:
- `RECALL_REGION`
- `RECALL_TIMEOUT_SECONDS`
- `RECALL_BOT_NAME`
- `RECALL_API_KEY`
- `RECALL_WEBHOOK_SECRET`
- It also shows the Key Vault secret names:
- `recall-api-key`
- `recall-webhook-secret`
- This is the source of truth for local, dev, and prod runtime settings.

## 2. Deployment Values Next
- Files:
- `charts/application/values.yaml`
- `charts/application/values-dev.yaml`
- `charts/application/values-prod.yaml`
- Review these after config.
- They show what actually gets injected into API, workers, and cronjobs.
- This is important because the chart uses the shared app secret for all workloads.

## 3. Data Model and Persistence
- Files:
- `src/domain/models/organisation_tables/sync/meeting_recording.py`
- `src/service/meeting_bot/session_service.py`
- Review these before task code.
- They define the org-scoped session row, dedupe shape, and send lifecycle.
- Focus on:
- `provider`
- `status`
- `transcript_status`
- `provider_session_id`
- `sent_to_provider_at`

## 4. Direct Event-Based Trigger
- File: `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
- This is the main business trigger.
- When a new `calendar_event_deal_association` is inserted, this file creates the pending recording row.
- After the DB commit, it dispatches the Recall send task.
- Review `_create_associations()`, `_enqueue_meeting_recording()`, and `_dispatch_pending_recordings()`.

## 5. Worker Entry Point
- File: `src/workers/deal_worker/tasks/meeting_bot_tasks.py`
- Review this after the matcher.
- This file is the Celery boundary.
- It contains:
- direct single-event send task
- org-level fallback scan task
- all-org fallback dispatcher task
- webhook processing task

## 6. Orchestration Layer
- File: `src/service/meeting_bot/dispatch_service.py`
- This is the core runtime flow after the worker task starts.
- It loads the calendar event, validates eligibility, creates or updates the session row, and sends to Recall.
- It also powers the temporary fallback scan for unsent upcoming meetings.

## 7. Provider Integration
- Files:
- `src/service/integrations/meeting_recording/providers/recall_provider.py`
- `src/service/integrations/meeting_recording/providers/recall_webhook_verifier.py`
- Review these before the API layer.
- The provider file handles outbound Recall API calls.
- The verifier file handles inbound Recall webhook signature verification.
- These files are intentionally isolated so Recall-specific behavior does not leak into deal logic.

## 8. API Entry Point For Webhooks
- Files:
- `src/api/fastapi_app.py`
- `src/api/routers/meeting_bot/__init__.py`
- `src/api/routers/meeting_bot/recall_webhook_handler.py`
- Review these after the provider files.
- This is the HTTP entry point at:
- `POST /users/meeting-bot/recall/webhook`
- It verifies the Recall signature, dedupes webhook deliveries, and pushes async processing into the worker layer.

## 9. Webhook Processing Layer
- File: `src/service/meeting_bot/webhook_service.py`
- Review this after the API entry point.
- This file maps Recall `bot.*` and `transcript.*` events into our normalized session lifecycle.
- It also fetches transcript artifact metadata when `transcript.done` arrives.

## Recommended Review Sequence
1. `configs.py`
2. `values.yaml`, `values-dev.yaml`, `values-prod.yaml`
3. `meeting_recording.py`
4. `session_service.py`
5. `deal_meeting_matcher.py`
6. `meeting_bot_tasks.py`
7. `dispatch_service.py`
8. `recall_provider.py`
9. `recall_webhook_verifier.py`
10. `fastapi_app.py`
11. `meeting_bot/__init__.py`
12. `meeting_bot/recall_webhook_handler.py`
13. `webhook_service.py`

## Notes
- `bots` is now only for chat-bot style integrations like Teams and Slack.
- Recall meeting recording webhook handling now lives in the dedicated `meeting_bot` router.
- Internal meeting-bot lifecycle code now lives in `src/service/meeting_bot/`.
- Only Recall-specific provider code remains under `src/service/integrations/meeting_recording/providers/`.
- The remaining major implementation area after this review order is transcript materialization into `meetings` and `transcript_segments`.
