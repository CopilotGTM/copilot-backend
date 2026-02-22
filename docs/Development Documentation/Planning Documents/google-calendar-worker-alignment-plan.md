# Google Calendar Alignment Plan (Integrations Worker v2)

## Status
- Updated: 2026-02-12
- Owner: Integrations Worker
- Scope: Align Google Calendar sync with the current three-tier architecture, incremental standards, and observability model used by Teams/Outlook/Email.
- Execution: Implemented in branch `feat/google-calendar-worker-alignment` (no conservative rollout).

## 1. Plan Review Summary (Final Approved Direction)
Approved and implemented decisions:
1. Use Google `syncToken` incremental mode (no `updatedMin` strategy toggle).
2. Keep bounded first sync window (`90d` past, `365d` future), then token-based increments.
3. Store provider cursor under `connection_metadata.calendar_delta_link`.
4. Keep Outlook backward compatibility by reading/writing legacy `outlook_delta_link`.
5. No conservative rollout/feature flag path.

## 2. Current-State Audit

### 2.1 Worker Architecture (Current)
- Tier 1: `src/workers/integrations_worker/tasks/calendar/all_orgs_task.py`
- Tier 2: `src/workers/integrations_worker/tasks/calendar/org_task.py`
- Tier 3: `src/workers/integrations_worker/tasks/calendar/integration_task.py`
- Beat config: `src/workers/integrations_worker/config.py`
  - UTC 12-23: every 15 minutes
  - UTC 00-11: hourly

### 2.2 Provider Behavior (Current)
- Google: `src/workers/integrations_worker/providers/calendar/google_calendar.py`
  - Initial: `timeMin=now-90d`, `timeMax=now+365d`, `orderBy=startTime`
  - Incremental: `updatedMin=last_synced_at-1h`, `showDeleted=true`
  - Pagination: yes (`nextPageToken`)
  - Cursor persistence: none
- Outlook: `src/workers/integrations_worker/providers/calendar/outlook_calendar.py`
  - Uses Graph delta API
  - Cursor in `connection_metadata["outlook_delta_link"]`
  - 410-style fallback to fresh query

### 2.3 Sync State + History (Current)
- Data tables: `calendar_events`, `calendar_event_attendees`, `calendar_event_user_states`
- History table: `integration_sync_history`
- Integration status: `knowledge_integrations.last_synced_at`, `last_sync_status`, `last_sync_error`
- Incremental change detection: `CalendarSyncService.sync_calendar_events_incremental`

### 2.4 Local Runtime Finding
- Local `docker compose ps` currently shows no `integrations-worker` and no `integrations-beat` container running.
- Meaning: scheduled calendar tasks are configured but not executing locally right now.

## 3. Critical Constraint (Must Be Respected)
Google Calendar `syncToken` is supported, but incremental requests with `syncToken` cannot include:
- `timeMin`
- `timeMax`
- `updatedMin`
- `orderBy`
(and a few other parameters)

Reference:
- Google Events list docs: https://developers.google.com/workspace/calendar/api/v3/reference/events/list
- Google sync guide: https://developers.google.com/workspace/calendar/api/guides/sync

Implication:
- We cannot blindly switch current windowed logic (`90d past / 365d future`) to `syncToken` without a strategy decision.

## 4. Strategy Decision (Approved)
- Selected strategy: **Token-based Google incremental (Option B style)**.
- Rollout mode: **direct adoption** (no feature flag / no conservative mode).
- First sync behavior: bounded bootstrap window only when `last_synced_at` is null.
- Incremental behavior: use stored `syncToken`; on invalid/expired token, auto-fallback to
  bounded full sync and store fresh token.

## 5. Target Cursor Metadata Contract
Store one calendar cursor per integration row:

```json
{
  "calendar_delta_link": "..."
}
```

Backward compatibility rules:
- Continue reading old Outlook key: `outlook_delta_link`.
- During migration window, write both `calendar_delta_link` and `outlook_delta_link` for Outlook.
- For Google rows, `calendar_delta_link` stores `syncToken`.

## 6. Revised Phased Plan

## Phase 0: Worktree + Runtime Setup (No Functional Changes)
- Create dedicated worktree/branch: `feat/google-calendar-worker-alignment`
- Copy `.env` to worktree.
- Start integrations runtime:
  - `docker compose --profile integrations up -d integrations-worker integrations-beat`
- Verify scheduler/worker health before coding.

Deliverable:
- Isolated runnable workspace for integration tasks.

## Phase 1: Metadata Helper + Compatibility Layer
Files:
- `src/workers/integrations_worker/tasks/calendar/org_task.py`
- `src/workers/integrations_worker/tasks/calendar/integration_task.py`
- New helper: `src/service/integrations/calendar_cursor_metadata.py`

Work:
- Add get/set cursor helpers for outlook/google.
- Add compatibility reads for old Outlook key.
- No provider behavior change yet.

Deliverable:
- Stable cursor contract and backward compatibility.

## Phase 2: Google Incremental Strategy Implementation
File:
- `src/workers/integrations_worker/providers/calendar/google_calendar.py`

Work:
- Add token-capable fetch mode.
- Use token-safe params only for sync-token requests.
- Capture and return `nextSyncToken`.
- On 410/invalid token -> fallback full sync path.
- Enforce first-sync bootstrap when `is_initial=True` even if stale token exists.

Deliverable:
- Google provider supports token-based incremental safely.

## Phase 3: Task Wiring to Persist Google Token
Files:
- `src/workers/integrations_worker/tasks/calendar/org_task.py`
- `src/workers/integrations_worker/tasks/calendar/integration_task.py`

Work:
- Pass strategy mode and existing cursor into provider fetch.
- Persist returned Google token after successful fetch+persist.
- Preserve existing `last_synced_at` and status updates.

Deliverable:
- Tier 2 and Tier 3 are cursor-aware for Google and Outlook.

## Phase 4: Sync History/Telemetry Normalization
Files:
- `src/service/integrations/calendar_sync_service.py`
- (optional) task-level metadata block

Work:
- Ensure metadata consistency keys in `integration_sync_history`:
  - `provider`, `created`, `updated`, `skipped`, `action_counts`
- Keep cancelled/deleted observability.

Deliverable:
- Comparable telemetry across providers.

Status:
- Deferred from this iteration (no behavior regression introduced).

## Phase 5: Tests
Files:
- `tests/workers/integrations_worker/providers/calendar/test_google_calendar.py`
- `tests/workers/integrations_worker/tasks/calendar/*`
- Optional helper tests for cursor metadata.

Work:
- Fix stale connect key expectation (`google-calendar`).
- Add tests:
  - updated_min mode
  - sync_token mode
  - token invalid fallback
  - cursor persistence and compatibility reads

Deliverable:
- Regression coverage for both incremental modes.

## Phase 6: Docs Refresh
Update to remove stale references and reflect current architecture:
- `docs/Technical Documentation/deal/teams-channels-auto-sync.md`
- `docs/Technical Documentation/integration/sync_tasks/calendar-sync-flow.md`
- `docs/Technical Documentation/integration/sync_tasks/scheduled-sync-architecture.md`
- `docs/Technical Documentation/deal/task-orchestration-map.md`

Add:
- Google token constraints and selected incremental strategy.

Deliverable:
- Architecture docs match actual code paths.

## Phase 7: Rollout Verification
Checks:
- Beat dispatch frequency and Tier-2 fanout.
- Google + Outlook sync history records.
- No duplicate regressions in `calendar_events`.
- Token invalidation path verified.

Deliverable:
- Release-ready proof for production rollout.

## 7. Acceptance Criteria
1. Calendar worker behavior unchanged for Outlook and remains backward-compatible.
2. Google uses `syncToken` incremental mode with token-safe request params.
3. Token mode persists cursor and recovers from invalid token safely.
4. Cursor metadata contract is unified (`calendar_delta_link`) and documented.
5. Local integrations worker + beat validation completed.
6. Stale docs/tasks references are corrected.

## 8. Risks and Mitigations
- Risk: Google token invalidation creates gaps.
  - Mitigation: deterministic fallback full sync and token refresh.
- Risk: token mode increases first-sync data volume.
  - Mitigation: bounded bootstrap window and reuse of incremental cursor thereafter.
- Risk: breaking old Outlook metadata reads.
  - Mitigation: dual-read, dual-write transition.
- Risk: documentation drift.
  - Mitigation: docs update mandatory in same PR.

## 9. Out of Scope (This Iteration)
- Multi-calendar sync beyond `primary` calendar.
- API contract changes for frontend integrations endpoints.
- Database schema migrations (JSON metadata only in this phase).

## 10. Execution Order
1. Phase 0
2. Phase 1
3. Phase 2
4. Phase 3
5. Phase 5
6. Phase 6
7. Phase 7

Note:
- Implementation executed after approval, with tests and docs updated in same branch.
