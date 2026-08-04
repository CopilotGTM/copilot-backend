# Initial Email And Calendar Sync Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a newly connected user live, reconnect-safe email and calendar initial-sync progress, then immediately show the exact contacts and accounts created and the exact newly synced emails linked to CRM records.

**Architecture:** Preserve the parallel email/calendar Celery tasks already dispatched by `InitialSyncService`; do not add a second orchestrator or a Celery chord. Create one durable parent `IntegrationSyncHistory` row whose `sync_id` is the OAuth `attempt_id`, update it under a row lock as provider and hydration branches commit, and publish versioned snapshots to a tenant/run-scoped Redis Stream only after each database commit. An authenticated SSE endpoint replays the durable snapshot plus retained live snapshots, while the frontend resumes by the already-known OAuth attempt ID and ignores duplicate or older revisions.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy, PostgreSQL JSONB row state, Celery, Redis Streams, `sse-starlette`, Next.js/React, Zustand, Jest, pytest.

---

## Scope and decisions

This plan covers only post-OAuth email and calendar initial sync for direct Gmail/Google Calendar/Outlook integrations and the combined Google/Microsoft provider rows. CRM provider chains, messaging sync, webhook subscription setup, scheduled incremental sync, and provider-side file attachments remain outside this UI run.

Current code already provides the expensive parts:

- `InitialSyncService._trigger_provider_capability_syncs()` sends email and calendar tasks independently, so the provider reads already run in parallel.
- `sync_email_integration_task()` and `sync_calendar_integration_task()` commit provider rows before dispatching bounded contact-communication hydration.
- `ContactCommunicationHydrationResult` already returns `contacts_created`; `ContactWriteResult.created_account_id` already preserves whether account creation occurred but the hydration result currently discards it.
- `ActivityFeedProjectionService.sync_communication_participant_links()` already links email activity to contact and account participants.
- Redis Streams, `Last-Event-ID`, heartbeat, timeout/reconnect, and database-backed terminal replay already exist for deal jobs.

The user-visible run must not finish when provider tasks merely enqueue hydration. It becomes terminal only after every selected provider branch is terminal and every registered hydration chunk is terminal.

Use these user-facing metric definitions:

| UI label | Contract | Why |
|---|---|---|
| Emails synced | `messages_synced` returned by the email provider sync | Existing committed provider count |
| Calendar events synced | `events_synced` returned by the calendar provider sync | Existing committed provider count |
| Contacts created | New CRM contacts committed by hydration chunks | Existing creation flag, aggregated once |
| Accounts created | Unique `created_account_id` values committed by hydration chunks | Existing creation flag, newly propagated |
| Emails linked | Distinct changed email IDs from this run that committed at least one contact/account activity link | Avoids counting thread-context messages or one email once per participant |
| Time taken | `completed_at - started_at` from the durable parent run | This is the authoritative terminal instant/duration |

Call the last metric **Emails linked**, not **attachments**, because file attachments are a different downstream feature.

Parent status rules:

- `queued`: parent row committed, no provider branch started.
- `in_progress`: at least one branch started and at least one branch/chunk remains non-terminal.
- `success`: every requested branch and registered hydration chunk completed.
- `partial`: at least one requested branch completed, but another branch or chunk failed.
- `failed`: no requested branch completed successfully.

Provider failure must not erase successful counts from the other branch. A Redis publish failure must not alter durable job state. A browser disconnect must not cancel workers.

## State and event contract

Store this bounded document in the parent `IntegrationSyncHistory.sync_metadata` field:

```json
{
  "kind": "email_calendar_initial_sync",
  "revision": 4,
  "requested_capabilities": ["email", "calendar"],
  "branches": {
    "email": {
      "status": "hydrating",
      "attempt": 1,
      "records_synced": 842,
      "hydration_expected": 9,
      "hydration_completed": 6,
      "hydration_failed": 0,
      "error": null
    },
    "calendar": {
      "status": "completed",
      "attempt": 1,
      "records_synced": 31,
      "hydration_expected": 1,
      "hydration_completed": 1,
      "hydration_failed": 0,
      "error": null
    }
  },
  "hydration_chunks": {
    "email:0": "completed",
    "email:1": "completed",
    "calendar:0": "completed"
  },
  "metrics": {
    "emails_synced": 842,
    "calendar_events_synced": 31,
    "contacts_created": 126,
    "accounts_created": 47,
    "emails_linked": 519
  }
}
```

`hydration_chunks` is bounded by the existing 100-source chunk size. A retry checks its stable `<branch>:<chunk_index>` key; an already-completed key contributes no metrics twice.

Every SSE snapshot has this shape:

```json
{
  "sync_id": "oauth-attempt-uuid",
  "integration_id": "integration-uuid",
  "status": "in_progress",
  "revision": 4,
  "started_at": "2026-08-04T10:00:00Z",
  "completed_at": null,
  "duration_ms": null,
  "branches": {},
  "metrics": {}
}
```

The HTTP stream emits named SSE events:

- `ready`: stream authorization and setup succeeded.
- `snapshot`: the full durable/live snapshot above.
- `ping`: heartbeat only.
- `timeout`: closes this HTTP request but leaves the job running; the client reconnects.
- `complete`: follows the terminal snapshot and closes the client.
- `error`: authorization-safe transport failure; branch failures remain inside snapshots.

Each Redis `snapshot` entry carries `revision`. The frontend applies only snapshots whose revision is greater than its current revision. Redis entry IDs remain the `Last-Event-ID` resume cursor.

## File map

### Backend repository

| Path | Responsibility |
|---|---|
| `src/service/integrations/initial_sync_progress_service.py` | Parent-run state machine, row locking, idempotent metric aggregation, terminal evaluation, and post-commit Redis publication |
| `src/service/integrations/initial_sync_sse_service.py` | User authorization, durable snapshot recovery, Redis replay, heartbeat, and terminal completion |
| `src/infra/messaging/redis_channels.py` | Add the tenant/run-scoped initial-sync stream key helper |
| `src/service/integrations/copilot_callback_service.py` | Create the parent run after OAuth persistence and before task dispatch |
| `src/service/integrations/initial_sync_service.py` | Forward the optional run ID to the existing parallel email/calendar task dispatches and record dispatch failures independently |
| `src/workers/integrations_worker/tasks/email/integration_task.py` | Report email branch start, committed provider count, hydration registration, retry state, and terminal failure |
| `src/workers/integrations_worker/tasks/calendar/integration_task.py` | Report calendar branch start, committed provider count, hydration registration, retry state, and terminal failure |
| `src/workers/deal_worker/utils/contact_communication_hydration_dispatch.py` | Build stable tracked chunk keys, register all chunks before publishing them, and record broker publication failures |
| `src/workers/deal_worker/tasks/deal_mappers/contact_communication_hydration_tasks.py` | Report one tracked chunk's committed result or exhausted failure |
| `src/service/crm/communications/participant_resolution_service.py` | Preserve unique newly created account IDs already returned by `ContactWriteService` |
| `src/service/crm/communications/hydration_service.py` | Return `accounts_created` and exact changed-email link counts |
| `src/api/routers/integrations/copilot_oauth_routes.py` | Add authenticated status and SSE routes keyed by OAuth attempt ID |
| `src/workers/integrations_worker/tasks/initial_sync_recovery_tasks.py` | Terminalize abandoned parent runs after the bounded retry window |
| `src/workers/integrations_worker/tasks/__init__.py` | Register the recovery task |
| `src/workers/integrations_worker/config.py` | Route and limit the recovery task |
| `src/workers/integrations_worker/scheduled_jobs.py` | Run recovery every five minutes through the existing CronJob registry |

No database migration is required: `IntegrationSyncHistory` already has the parent identity, tenant/integration ownership, status, timestamps, processed/failed counts, JSONB metadata, and terminal error details.

### Frontend repository

| Path | Responsibility |
|---|---|
| `src/modules/integrations/services/oauth.service.ts` | Return the OAuth `attemptId` on successful or backend-reconciled completion |
| `src/modules/integrations/types/store.types.ts` | Type the connection result carrying the initial-sync ID |
| `src/modules/integrations/store/integrations.store.ts` | Return the run descriptor after refreshing connected integrations |
| `src/modules/integrations/hooks/use-initial-sync-stream.ts` | Fetch-based SSE reader with `Last-Event-ID`, revision filtering, timeout reconnect, and session recovery |
| `src/modules/integrations/components/initial-sync-progress-card.tsx` | Render email/calendar branch states, live counters, partial failures, and final duration |
| `src/app/(root)/(dashboard)/integrations/page.tsx` | Start/resume the compact progress card after OAuth without blocking navigation |

## Task 1: Build the durable parent-run state machine

**Files:**
- Create: `src/service/integrations/initial_sync_progress_service.py`
- Create: `tests/service/integrations/test_initial_sync_progress_service.py`

- [ ] **Step 1: Write failing tests for identity, row locking, terminal rules, and retry deduplication**

Cover these exact cases:

```python
async def test_start_uses_oauth_attempt_as_sync_id_and_records_requested_branches(): ...
async def test_branch_updates_lock_parent_row_by_org_and_sync_id(): ...
async def test_register_chunks_prevents_provider_completion_from_ending_run(): ...
async def test_completed_chunk_metrics_are_applied_only_once_on_redelivery(): ...
async def test_one_successful_and_one_failed_branch_finishes_partial(): ...
async def test_all_failed_branches_finish_failed(): ...
async def test_all_branches_and_chunks_complete_with_duration(): ...
async def test_publish_happens_after_commit_and_publish_failure_does_not_rollback(): ...
```

Assert that `start_run()` sets `sync_id == UUID(integration.connection_id)`, `organisation_id`, `integration_id`, `status=QUEUED`, and `sync_metadata.kind == "email_calendar_initial_sync"`.

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations/test_initial_sync_progress_service.py -q
```

Expected: collection/import failure because `initial_sync_progress_service.py` does not exist.

- [ ] **Step 3: Implement the minimum state service**

Use a string branch type restricted at the public boundary:

```python
InitialSyncBranch = Literal["email", "calendar"]
TERMINAL_BRANCH_STATUSES = {"completed", "failed"}
INITIAL_SYNC_KIND = "email_calendar_initial_sync"
```

Expose only the operations the real callers need:

```python
class InitialSyncProgressService:
    @staticmethod
    async def start_run(
        session: AsyncSession,
        *,
        integration: KnowledgeIntegration,
        requested_branches: list[InitialSyncBranch],
    ) -> UUID: ...

    @staticmethod
    async def mark_branch_started(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        attempt: int,
    ) -> None: ...

    @staticmethod
    async def register_hydration(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        *,
        records_synced: int,
        chunk_keys: list[str],
    ) -> None: ...

    @staticmethod
    async def complete_hydration_chunk(
        organisation_id: UUID,
        sync_id: UUID,
        branch: InitialSyncBranch,
        chunk_key: str,
        *,
        contacts_created: int,
        accounts_created: int,
        emails_linked: int,
    ) -> None: ...

    @staticmethod
    async def fail_hydration_chunk(..., error: str) -> None: ...

    @staticmethod
    async def fail_branch(..., error: str) -> None: ...
```

Every mutation must query with both tenant and run identity and lock the row:

```python
stmt = (
    select(IntegrationSyncHistory)
    .where(
        col(IntegrationSyncHistory.organisation_id) == organisation_id,
        col(IntegrationSyncHistory.sync_id) == sync_id,
    )
    .with_for_update()
)
```

Inside that lock: reject rows whose `sync_metadata.kind` differs, ignore duplicate terminal chunk completion, update counters, recompute parent status, increment revision, commit, then publish the serialized snapshot. Do not store provider message IDs, email addresses, subjects, attendee names, or OAuth tokens.

Set the parent columns as well as JSON metadata:

```python
record.records_processed = metrics["emails_synced"] + metrics["calendar_events_synced"]
record.records_failed = failed_branch_count + failed_chunk_count
record.completed_at = datetime_now_naive() if terminal else None
```

- [ ] **Step 4: Run focused tests and static checks**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations/test_initial_sync_progress_service.py -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/service/integrations/initial_sync_progress_service.py tests/service/integrations/test_initial_sync_progress_service.py
```

Expected: all focused tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit the state machine**

```powershell
git add src/service/integrations/initial_sync_progress_service.py tests/service/integrations/test_initial_sync_progress_service.py
git commit -m "feat(integrations): track initial sync progress"
```

## Task 2: Add authenticated, reconnect-safe SSE

**Files:**
- Create: `src/service/integrations/initial_sync_sse_service.py`
- Modify: `src/infra/messaging/redis_channels.py`
- Modify: `src/api/routers/integrations/copilot_oauth_routes.py`
- Create: `tests/service/integrations/test_initial_sync_sse_service.py`
- Modify: `tests/api/routers/integrations/test_copilot_oauth_routes.py`

- [ ] **Step 1: Write failing authorization and replay tests**

```python
async def test_connector_can_read_own_initial_sync_run(): ...
async def test_other_user_in_same_org_cannot_read_run(): ...
async def test_other_org_cannot_probe_run_uuid(): ...
async def test_active_stream_yields_durable_snapshot_before_redis_updates(): ...
async def test_older_redis_revision_is_safe_to_replay(): ...
async def test_completed_run_replays_snapshot_and_complete_without_redis(): ...
async def test_stream_resumes_after_last_event_id(): ...
async def test_stream_timeout_does_not_change_durable_status(): ...
```

Add route tests for authenticated `GET /users/integrations/copilot-oauth/initial-sync/{sync_id}` and `GET /users/integrations/copilot-oauth/initial-sync/{sync_id}/stream`, including 403/404 fail-closed behavior.

- [ ] **Step 2: Run tests and verify the missing service/routes fail**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations/test_initial_sync_sse_service.py tests/api/routers/integrations/test_copilot_oauth_routes.py -q
```

- [ ] **Step 3: Add a dedicated stream key without changing deal-job keys**

```python
def channel_for_initial_sync(organisation_id: UUID | str, sync_id: UUID | str) -> str:
    return f"initial_sync:{organisation_id}:{sync_id}"
```

Use `xadd_json_frame(..., maxlen=100)` and the existing stream TTL behavior. A snapshot is small and contains no customer communication content.

- [ ] **Step 4: Implement status authorization and stream replay**

Authorize by joining the parent history row to its `KnowledgeIntegration` and requiring:

```python
IntegrationSyncHistory.organisation_id == user_ids.organisation_id
KnowledgeIntegration.integration_id == IntegrationSyncHistory.integration_id
KnowledgeIntegration.created_by_user_id == user_ids.user_id
IntegrationSyncHistory.sync_metadata["kind"].as_string() == INITIAL_SYNC_KIND
```

On connect, emit `ready`, then the current durable `snapshot`. If terminal, emit `complete` and return without opening Redis. If active, replay retained Redis entries from `Last-Event-ID` or `0-0`; duplicates are safe because revisions are monotonic. Heartbeat every 25 seconds and close only the HTTP request after 300 seconds so the frontend reconnect loop can resume.

- [ ] **Step 5: Implement the FastAPI responses**

Use `EventSourceResponse`, pass `request.headers.get("last-event-id")`, stop promptly on `request.is_disconnected()`, and close the Redis client in `finally`.

- [ ] **Step 6: Verify backend SSE behavior**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations/test_initial_sync_sse_service.py tests/api/routers/integrations/test_copilot_oauth_routes.py -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/service/integrations/initial_sync_sse_service.py src/infra/messaging/redis_channels.py src/api/routers/integrations/copilot_oauth_routes.py
```

- [ ] **Step 7: Commit the SSE slice**

```powershell
git add src/service/integrations/initial_sync_sse_service.py src/infra/messaging/redis_channels.py src/api/routers/integrations/copilot_oauth_routes.py tests/service/integrations/test_initial_sync_sse_service.py tests/api/routers/integrations/test_copilot_oauth_routes.py
git commit -m "feat(integrations): stream initial sync status"
```

## Task 3: Create the run before preserving the existing parallel dispatch

**Files:**
- Modify: `src/service/integrations/copilot_callback_service.py`
- Modify: `src/service/integrations/initial_sync_service.py`
- Modify: `tests/service/integrations/test_copilot_callback_service.py`
- Modify: `tests/service/integrations/test_initial_sync_service.py`

- [ ] **Step 1: Write failing callback and fan-out tests**

```python
async def test_google_callback_commits_parent_run_before_email_calendar_dispatch(): ...
async def test_direct_gmail_callback_creates_email_only_run(): ...
async def test_non_email_calendar_callback_does_not_create_visible_run(): ...
def test_provider_capabilities_dispatch_email_and_calendar_with_same_sync_id(): ...
def test_email_dispatch_failure_marks_only_email_and_still_dispatches_calendar(): ...
def test_reactivation_uses_new_attempt_id_for_new_run(): ...
```

- [ ] **Step 2: Run the tests and verify they fail on the old call contract**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations/test_copilot_callback_service.py tests/service/integrations/test_initial_sync_service.py -q
```

- [ ] **Step 3: Make callback handoff asynchronous and durable**

After the integration and capability rows commit, derive requested branches from the actually enabled capabilities. Create the parent only when the list contains email or calendar, using `UUID(integration.connection_id)` as `sync_id`. Commit that row before publishing tasks.

Change the callback helper to return the optional run ID:

```python
async def _trigger_initial_sync(
    self,
    session: AsyncSession,
    integration: KnowledgeIntegration,
    *,
    enabled_capabilities: list[IntegrationType] | None = None,
) -> UUID | None: ...
```

The OAuth response does not need a second identifier: its existing `attempt_id` is the run ID. Keeping one identity also lets the popup-closed backend reconciliation path resume progress.

- [ ] **Step 4: Forward tracking context independently to both tasks**

```python
kwargs = {
    "organisation_id": org_id_str,
    "integration_id": int_id_str,
    "initial_sync_id": initial_sync_id,
}
```

Catch dispatch errors per capability, call `fail_branch()`, then continue the loop. Email and calendar must remain separate sends so the worker pool can execute them concurrently. Do not wait inside the OAuth callback.

- [ ] **Step 5: Run focused tests and commit**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations/test_copilot_callback_service.py tests/service/integrations/test_initial_sync_service.py -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/service/integrations/copilot_callback_service.py src/service/integrations/initial_sync_service.py
git add src/service/integrations/copilot_callback_service.py src/service/integrations/initial_sync_service.py tests/service/integrations/test_copilot_callback_service.py tests/service/integrations/test_initial_sync_service.py
git commit -m "feat(integrations): track OAuth initial sync fanout"
```

## Task 4: Track provider branches through hydration registration

**Files:**
- Modify: `src/workers/integrations_worker/tasks/email/integration_task.py`
- Modify: `src/workers/integrations_worker/tasks/calendar/integration_task.py`
- Modify: `tests/workers/integrations_worker/test_deal_matching_dispatch_suppressed.py`
- Modify: `tests/workers/integrations_worker/test_calendar_task_lock.py`
- Create: `tests/workers/integrations_worker/test_initial_sync_task_progress.py`

- [ ] **Step 1: Add failing task-boundary tests**

Cover both tasks:

```python
def test_tracked_task_marks_branch_started_with_retry_attempt(): ...
def test_tracked_task_registers_hydration_after_source_commit(): ...
def test_zero_changed_sources_completes_branch_without_hydration(): ...
def test_retryable_exception_does_not_terminalize_branch_before_retry_exhaustion(): ...
def test_handled_failure_and_exhausted_exception_fail_branch(): ...
def test_untracked_manual_and_webhook_calls_preserve_existing_behavior(): ...
```

- [ ] **Step 2: Run focused tests and observe failures**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/workers/integrations_worker/test_deal_matching_dispatch_suppressed.py tests/workers/integrations_worker/test_calendar_task_lock.py tests/workers/integrations_worker/test_initial_sync_task_progress.py -q
```

- [ ] **Step 3: Add optional task parameters without changing other callers**

```python
def sync_email_integration_task(..., initial_sync_id: str | None = None) -> dict[str, Any]: ...
def sync_calendar_integration_task(..., initial_sync_id: str | None = None) -> dict[str, Any]: ...
```

If present, mark the branch started with `self.request.retries + 1`. After provider persistence commits, pass the run and branch to the hydration dispatcher. If there are no changed IDs, register an empty chunk list; that transition completes the branch. Do not include subscription setup in the visible branch.

Before `raise self.retry(...)`, keep the branch active and publish the new attempt/error summary without marking it terminal. On handled failure, timeout without retry, cursor-contract failure, action-required failure, or exhausted retry, call `fail_branch()` with a sanitized message.

- [ ] **Step 4: Verify legacy paths are unchanged**

Manual refresh and webhook tasks omit `initial_sync_id`; they must create no parent rows, publish no initial-sync snapshots, and retain existing webhook receipt behavior.

- [ ] **Step 5: Run focused checks and commit**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/workers/integrations_worker/test_deal_matching_dispatch_suppressed.py tests/workers/integrations_worker/test_calendar_task_lock.py tests/workers/integrations_worker/test_initial_sync_task_progress.py -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/workers/integrations_worker/tasks/email/integration_task.py src/workers/integrations_worker/tasks/calendar/integration_task.py
git add src/workers/integrations_worker/tasks/email/integration_task.py src/workers/integrations_worker/tasks/calendar/integration_task.py tests/workers/integrations_worker
git commit -m "feat(integrations): report initial provider sync progress"
```

## Task 5: Produce exact contact, account, and email-link counts

**Files:**
- Modify: `src/service/crm/communications/participant_resolution_service.py`
- Modify: `src/service/crm/communications/hydration_service.py`
- Modify: `tests/service/crm/communications/test_hydration_service.py`

- [ ] **Step 1: Write failing metric tests**

```python
async def test_resolution_preserves_unique_created_account_ids(): ...
async def test_changed_email_with_multiple_contacts_counts_as_one_linked_email(): ...
async def test_related_thread_context_is_not_counted_as_changed_email_link(): ...
async def test_reused_contact_and_account_do_not_increment_created_counts(): ...
async def test_hydration_result_serializes_accounts_created_and_emails_linked(): ...
```

- [ ] **Step 2: Run the focused service tests**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/crm/communications/test_hydration_service.py -q
```

- [ ] **Step 3: Preserve account creation identities from the existing writer result**

Extend `CommunicationParticipantResolution` with:

```python
created_account_ids: frozenset[UUID]
```

Populate it from the already-returned batch result:

```python
created_account_ids=frozenset(
    result.created_account_id
    for result in resolved.values()
    if result.created_account_id is not None
)
```

No extra query and no new account-writing path are needed.

- [ ] **Step 4: Count only changed emails whose participant links commit**

Change `_project()` to return the projected activity count and the set of email IDs for which at least one contact participant was resolved and the activity/link write completed:

```python
async def _project(...) -> tuple[int, set[UUID]]: ...
```

For `hydrate_changed_sources()`, intersect that set with the caller's `changed_email_ids`. Do not count related thread messages loaded only for context. Add `accounts_created` and `emails_linked` to `ContactCommunicationHydrationResult` and `to_dict()`.

- [ ] **Step 5: Run focused tests and commit**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/crm/communications/test_hydration_service.py -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/service/crm/communications/participant_resolution_service.py src/service/crm/communications/hydration_service.py tests/service/crm/communications/test_hydration_service.py
git add src/service/crm/communications/participant_resolution_service.py src/service/crm/communications/hydration_service.py tests/service/crm/communications/test_hydration_service.py
git commit -m "feat(crm): count initial sync record creation"
```

## Task 6: Register hydration before publication and aggregate each chunk once

**Files:**
- Modify: `src/workers/deal_worker/utils/contact_communication_hydration_dispatch.py`
- Modify: `src/workers/deal_worker/tasks/deal_mappers/contact_communication_hydration_tasks.py`
- Modify: `tests/workers/deal_worker/tasks/deal_mappers/test_contact_communication_hydration_tasks.py`
- Create: `tests/workers/deal_worker/utils/test_contact_communication_hydration_dispatch.py`

- [ ] **Step 1: Write failing dispatch and worker tests**

```python
async def test_tracked_dispatch_registers_all_chunk_keys_before_first_send(): ...
async def test_tracked_dispatch_marks_only_failed_broker_publication_chunk(): ...
def test_hydration_success_reports_committed_metrics_once(): ...
def test_hydration_retry_does_not_report_failure_before_exhaustion(): ...
def test_hydration_redelivery_cannot_double_count_completed_chunk(): ...
def test_untracked_hydration_payload_is_unchanged(): ...
```

- [ ] **Step 2: Run tests and verify the new context is absent**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/workers/deal_worker/tasks/deal_mappers/test_contact_communication_hydration_tasks.py tests/workers/deal_worker/utils/test_contact_communication_hydration_dispatch.py -q
```

- [ ] **Step 3: Add optional initial-run context**

```python
initial_sync_id: UUID | None = None
initial_sync_branch: Literal["email", "calendar"] | None = None
```

Require both or neither. Build payloads first, derive stable keys such as `email:0`, call `register_hydration()` once with the complete key list and committed provider count, then publish each task with its key. This ordering prevents a fast child from finishing before the parent knows it exists.

- [ ] **Step 4: Report only committed terminal results**

After `org_session.commit()` and downstream dispatch attempts, call:

```python
await InitialSyncProgressService.complete_hydration_chunk(
    org_id,
    UUID(initial_sync_id),
    initial_sync_branch,
    initial_sync_chunk_key,
    contacts_created=result.contacts_created,
    accounts_created=result.accounts_created,
    emails_linked=result.emails_linked,
)
```

On retryable errors, retry first. Only the exhausted attempt marks the chunk failed. Follow-up research, attachment extraction, and deal-insight dispatch remain best-effort and do not delay the initial data-capture terminal result because contact/account/activity rows already committed.

- [ ] **Step 5: Verify and commit**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/workers/deal_worker/tasks/deal_mappers/test_contact_communication_hydration_tasks.py tests/workers/deal_worker/utils/test_contact_communication_hydration_dispatch.py -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/workers/deal_worker/utils/contact_communication_hydration_dispatch.py src/workers/deal_worker/tasks/deal_mappers/contact_communication_hydration_tasks.py
git add src/workers/deal_worker/utils/contact_communication_hydration_dispatch.py src/workers/deal_worker/tasks/deal_mappers/contact_communication_hydration_tasks.py tests/workers/deal_worker
git commit -m "feat(integrations): aggregate initial hydration results"
```

## Task 7: Close abandoned runs and add operational evidence

**Files:**
- Create: `src/workers/integrations_worker/tasks/initial_sync_recovery_tasks.py`
- Modify: `src/workers/integrations_worker/tasks/__init__.py`
- Modify: `src/workers/integrations_worker/config.py`
- Modify: `src/workers/integrations_worker/scheduled_jobs.py`
- Create: `tests/workers/integrations_worker/test_initial_sync_recovery_tasks.py`
- Modify: `tests/workers/integrations_worker/test_cron_dispatch.py`

- [ ] **Step 1: Write failing stale-run tests**

```python
async def test_recovery_ignores_recent_and_terminal_runs(): ...
async def test_recovery_marks_stale_open_chunks_failed_and_finalizes_parent(): ...
async def test_recovery_scopes_each_query_to_one_organisation(): ...
def test_initial_sync_recovery_is_registered_every_five_minutes(): ...
```

- [ ] **Step 2: Implement bounded recovery**

Register `integrations.recover_stale_initial_syncs` on `integrations_priority`, every five minutes. Use a two-hour stale threshold: provider attempts can consume roughly an hour across current 900/960-second task limits and retries, and hydration attempts can consume another hour across current 1200/1260-second limits. Recovery terminalizes the user-facing run; it does not invent another provider read. Existing scheduled incremental sync remains the data-recovery path.

Process at most 100 parent rows per execution and acquire the existing global task lock pattern so overlapping CronJobs do not duplicate work.

- [ ] **Step 3: Add structured logs and metrics at state transitions**

Log only IDs, branch, status, revision, counts, attempt, and elapsed duration. Emit counters for started/success/partial/failed/stale-terminalized and a duration histogram. Never log email addresses, subjects, participant names, tokens, or provider payloads.

- [ ] **Step 4: Verify scheduler and recovery**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/workers/integrations_worker/test_initial_sync_recovery_tasks.py tests/workers/integrations_worker/test_cron_dispatch.py -q
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/workers/integrations_worker/tasks/initial_sync_recovery_tasks.py src/workers/integrations_worker/scheduled_jobs.py src/workers/integrations_worker/config.py
```

- [ ] **Step 5: Commit recovery**

```powershell
git add src/workers/integrations_worker/tasks/initial_sync_recovery_tasks.py src/workers/integrations_worker/tasks/__init__.py src/workers/integrations_worker/config.py src/workers/integrations_worker/scheduled_jobs.py tests/workers/integrations_worker/test_initial_sync_recovery_tasks.py tests/workers/integrations_worker/test_cron_dispatch.py
git commit -m "feat(integrations): recover stale initial sync runs"
```

## Task 8: Return the attempt ID from the frontend OAuth flow

**Repository:** `D:\CopilotGTM\copilot-app-frontend` in a new frontend worktree created at execution time.

**Files:**
- Modify: `src/modules/integrations/services/oauth.service.ts`
- Modify: `src/modules/integrations/types/store.types.ts`
- Modify: `src/modules/integrations/store/integrations.store.ts`
- Modify: `__tests__/unit/modules/integrations/services/oauth.service.test.ts`
- Create: `__tests__/unit/modules/integrations/store/integrations.store.test.ts`

- [ ] **Step 1: Write failing return-contract tests**

```typescript
it("returns the OAuth attempt id after popup completion", async () => {});
it("returns the same attempt id after backend completion reconciliation", async () => {});
it("does not return an initial sync descriptor for admin-consent-only completion", async () => {});
```

- [ ] **Step 2: Run focused Jest tests**

```powershell
yarn test:jest __tests__/unit/modules/integrations/services/oauth.service.test.ts __tests__/unit/modules/integrations/store/integrations.store.test.ts --runInBand
```

- [ ] **Step 3: Return the identity the frontend already owns**

Extend the result without adding another backend lookup:

```typescript
export interface OAuthFlowResult {
  success: boolean;
  provider: string;
  attemptId?: string;
  error?: string;
}

export interface InitialSyncRunDescriptor {
  syncId: string;
  provider: string;
}
```

`initiateOAuthFlow()` already stores `attemptId` before navigating the popup. Preserve it in success and timeout/reconciliation results. Change `connectIntegration()` to return `Promise<InitialSyncRunDescriptor | null>` after refreshing integrations.

- [ ] **Step 4: Verify and commit in the frontend worktree**

```powershell
yarn test:jest __tests__/unit/modules/integrations/services/oauth.service.test.ts __tests__/unit/modules/integrations/store/integrations.store.test.ts --runInBand
yarn check-types
git add src/modules/integrations/services/oauth.service.ts src/modules/integrations/types/store.types.ts src/modules/integrations/store/integrations.store.ts __tests__/unit/modules/integrations
git commit -m "feat(integrations): expose initial sync run id"
```

## Task 9: Build the integration progress stream and compact UI

**Repository:** `D:\CopilotGTM\copilot-app-frontend` frontend worktree.

**Files:**
- Create: `src/modules/integrations/hooks/use-initial-sync-stream.ts`
- Create: `src/modules/integrations/components/initial-sync-progress-card.tsx`
- Modify: `src/app/(root)/(dashboard)/integrations/page.tsx`
- Create: `__tests__/unit/modules/integrations/hooks/use-initial-sync-stream.test.tsx`
- Create: `__tests__/unit/modules/integrations/components/initial-sync-progress-card.test.tsx`

- [ ] **Step 1: Write failing parser/reconnect tests**

```typescript
it("applies only snapshots with a newer revision", async () => {});
it("reconnects after timeout with Last-Event-ID", async () => {});
it("stops after complete and preserves the terminal snapshot", async () => {});
it("treats 401, 403, and 404 as terminal without an infinite reconnect loop", async () => {});
it("aborts the request on unmount without clearing a still-active run", async () => {});
```

- [ ] **Step 2: Implement the fetch-based stream hook**

Use `/api/proxy/integrations/copilot-oauth/initial-sync/${syncId}/stream`, matching the existing integrations proxy base, with `credentials: "include"`, `Accept: text/event-stream`, and the same bounded reconnect pattern as `use-deal-job-stream.ts`. Native `EventSource` cannot set `Last-Event-ID`; use `fetch()` and a `ReadableStream`.

Expose:

```typescript
interface UseInitialSyncStreamResult {
  snapshot: InitialSyncSnapshot | null;
  isStreaming: boolean;
  error: string | null;
  disconnect: () => void;
}
```

- [ ] **Step 3: Write failing UI tests**

```typescript
it("shows email and calendar as independent parallel rows", () => {});
it("updates contacts accounts and emails linked from snapshots", () => {});
it("shows partial completion without discarding successful counts", () => {});
it("shows authoritative duration only after terminal completion", () => {});
it("uses indeterminate activity instead of fake provider percentages", () => {});
```

- [ ] **Step 4: Build a compact non-blocking progress card**

Render:

- one row each for requested Email sync and Calendar sync, with queued/syncing/organizing/completed/failed state;
- live number tiles for contacts created, accounts created, and emails linked;
- provider record counts as secondary detail;
- an elapsed timer while active and backend `duration_ms` after terminal;
- a warning treatment for partial, an error treatment for failed, and a success treatment for complete;
- a close action that closes only the UI, never the backend run.

Use an `aria-live="polite"` status region, preserve keyboard focus, and do not animate counters in a way that hides their exact value.

- [ ] **Step 5: Wire OAuth completion and page refresh recovery**

After `startConnection()` returns a descriptor, store the latest active run ID in `sessionStorage` and render the progress card on the integrations page. On page mount, resume that ID. Clear it only after a terminal snapshot or a terminal 404 indicating the provider had no trackable email/calendar run. The current integration list refresh remains independent.

- [ ] **Step 6: Run frontend verification and commit**

```powershell
yarn test:jest __tests__/unit/modules/integrations/hooks/use-initial-sync-stream.test.tsx __tests__/unit/modules/integrations/components/initial-sync-progress-card.test.tsx --runInBand
yarn check-types
yarn lint
git add src/modules/integrations/hooks/use-initial-sync-stream.ts src/modules/integrations/components/initial-sync-progress-card.tsx 'src/app/(root)/(dashboard)/integrations/page.tsx' __tests__/unit/modules/integrations
git commit -m "feat(integrations): show live initial sync progress"
```

## Task 10: End-to-end verification, rollout, and rollback

**Files:**
- Modify if the repository keeps feature documentation: `docs/Technical Documentation/integrations/initial-sync-progress.md`
- Test only: backend and frontend files listed above

- [ ] **Step 1: Run the complete focused backend slice**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations/test_initial_sync_progress_service.py tests/service/integrations/test_initial_sync_sse_service.py tests/service/integrations/test_copilot_callback_service.py tests/service/integrations/test_initial_sync_service.py tests/service/crm/communications/test_hydration_service.py tests/workers/integrations_worker/test_deal_matching_dispatch_suppressed.py tests/workers/integrations_worker/test_calendar_task_lock.py tests/workers/integrations_worker/test_initial_sync_task_progress.py tests/workers/integrations_worker/test_initial_sync_recovery_tasks.py tests/workers/deal_worker/tasks/deal_mappers/test_contact_communication_hydration_tasks.py tests/workers/deal_worker/utils/test_contact_communication_hydration_dispatch.py tests/api/routers/integrations/test_copilot_oauth_routes.py -q
```

- [ ] **Step 2: Run backend quality gates**

```powershell
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m ruff check src/service/integrations src/workers/integrations_worker src/workers/deal_worker src/api/routers/integrations src/infra/messaging tests/service/integrations tests/workers/integrations_worker tests/workers/deal_worker tests/api/routers/integrations
D:\CopilotGTM\copilot-backend\.venv\Scripts\python.exe -m pytest tests/service/integrations tests/api/routers/integrations -q
git diff --check
```

- [ ] **Step 3: Run frontend quality gates**

```powershell
yarn test:jest __tests__/unit/modules/integrations --runInBand
yarn check-types
yarn lint
yarn build
git diff --check
```

- [ ] **Step 4: Verify staging with real small and large accounts**

For both Google and Microsoft:

1. Connect a new account and record OAuth attempt ID.
2. Confirm email and calendar rows enter active state independently and overlap in worker timestamps.
3. Reload the integrations page mid-run and confirm the same snapshot resumes.
4. Confirm Redis reconnect with `Last-Event-ID` does not duplicate counters.
5. Compare terminal `emails_synced` and `calendar_events_synced` with worker results.
6. Query contacts/accounts created with `created_via='contact_communication_hydration'` inside the run window and reconcile with terminal counts.
7. Reconcile `emails_linked` against distinct changed email IDs that have committed CRM activity participant links.
8. Force one provider permission failure and confirm the other branch finishes with parent `partial`.
9. Disconnect the browser during hydration and confirm the backend still reaches terminal state.
10. Let a test run cross the stale threshold in a controlled environment and confirm recovery terminalizes it once.

- [ ] **Step 5: Roll out behind a frontend visibility flag**

Deploy backend compatibility first; optional task parameters preserve every old caller. Enable the frontend card for internal users, then a small percentage, then all users after checking success/partial/failed rates, p50/p95 duration, stale recoveries, Redis publish errors, and SSE reconnect rate.

- [ ] **Step 6: Roll back safely**

Disable the frontend visibility flag first. Backend rollback is code-only because no migration was added; parent history rows remain valid audit data and are ignored by old code. Do not delete history rows or reset provider cursors. Existing provider tasks, scheduled incremental sync, and hydration continue through their old optional-parameter path.

## Acceptance criteria

- Email and calendar provider reads execute concurrently when both capabilities are enabled.
- The OAuth callback returns without waiting for provider work.
- The frontend shows both branch states from authenticated SSE and resumes after refresh/reconnect.
- The parent cannot complete before all registered hydration chunks are terminal.
- Terminal counts are committed counts, retry-idempotent, and do not double-count thread context or multiple participants on one email.
- A partial branch failure preserves and displays successful branch metrics.
- The terminal snapshot includes server-derived completion time and duration immediately after the final database commit.
- Redis loss affects live freshness only; PostgreSQL reconstructs current and terminal state.
- Manual refresh, webhooks, scheduled sync, CRM initial sync, messaging sync, and file-attachment extraction preserve their existing contracts.
- No PII, provider payload, token, subject, attendee name, or raw email address enters Redis events or progress logs.
