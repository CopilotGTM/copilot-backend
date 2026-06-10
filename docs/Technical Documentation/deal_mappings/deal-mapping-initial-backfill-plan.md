# Deal Mapping Initial Backfill And Event-Based Remapping Plan

> **Purpose**: This document defines the production deal-mapping strategy for
> stakeholder, calendar, and email mapping. The goal is to map every eligible
> deal once during initial backfill, avoid repeatedly reprocessing unchanged
> deals, and rely on event-based triggers for future updates.

## Final V1 Decision

Do **not** use chunk workers for the current initial-backfill implementation.
Do not keep a stakeholder chunk task as a public v1 code path.

The current v1 will use bounded per-deal task dispatch:

```text
scheduled deal-mapping
  -> all-org coordinator
      -> org coordinator claims a bounded batch of deals
          -> dispatch one stakeholder task per claimed deal
              -> dispatch one calendar task for that deal
              -> dispatch one email task for that deal
```

Reasoning:

- This is easier to explain and operate right now.
- Each deal gets its own Celery task, retry boundary, logs, and `DealJobs` row.
- We still avoid the old failure mode by claiming only a bounded number of deals
  per org run.
- Chunking can be revisited later only with a fresh design and implementation.
  It should not remain as dormant v1 stakeholder task code.

Default for v1:

- Claim **100 deals per organization run**.
- Do not claim 1000 deals per org run yet.
- Consider raising to 500 only after observing worker queue depth, task runtime,
  and Redis/Celery behavior in production.

## Current Task Map

The deal worker has three mapping families:

| Process | All-org task | Org task | Deal task | Basic goal |
| --- | --- | --- | --- | --- |
| Stakeholder sync | `stakeholder.sync_all_deals_stakeholders_all_orgs` | `stakeholder.sync_all_deals_stakeholders_org` | `stakeholder.sync_deal_stakeholders` | Sync CRM CONTACT participants into `DealStakeholder` rows. |
| Calendar matching | `deal.match_calendar_to_deals_all_orgs` | `deal.match_calendar_to_deals_org` | `deal.match_calendar_to_deals` | Link calendar meetings to deals when internal team and customer participants overlap. |
| Email matching | `deal.match_email_to_deals_all_orgs` | `deal.match_email_to_deals_org` | `deal.match_email_to_deals` | Link emails to deals when internal team and customer participants overlap. |

Only the following path should be used for scheduled initial backfill:

```text
stakeholder.sync_all_deals_stakeholders_all_orgs
  -> stakeholder.sync_all_deals_stakeholders_org
      -> stakeholder.sync_deal_stakeholders per claimed deal
          -> deal.match_calendar_to_deals per deal
          -> deal.match_email_to_deals per deal
```

Do not call single-deal stakeholder sync in an unbounded loop. It must only be
dispatched for a bounded claimed batch.

## Current Implementation Status

Completed:

1. Shared deal-mapping eligibility uses app users from `public.users`.
2. Deal-level stakeholder, calendar, and email tasks re-check eligibility.
3. Stakeholder org task has durable initial-backfill claim logic.
4. Stakeholder org task dispatches one single-deal stakeholder task per claimed
   deal.
5. Stakeholder chunk task code/export/tests have been removed from v1.
6. Stakeholder task creates/reuses calendar and email `DealJobs` before
   dispatching downstream rematch tasks.
7. Integration sync completion no longer dispatches broad org-level calendar or
   email deal matchers automatically.
8. Stakeholder initial-backfill claim uses a DB advisory lock scoped to the
   organization claim operation.

Not completed for the revised v1:

- None for the revised v1 implementation plan.

## Eligibility Rule

A deal is eligible for mapping when all checks below pass:

1. The deal belongs to the current organization.
2. The deal is not deleted.
3. The deal status is `OPEN`.
4. At least one same-organization app user from `public.users.email` appears on
   the deal as:
   - `CRMDeal.owner_email`, or
   - a non-deleted `CRMDealParticipant.related_email` where
     `participant_type == USER`.

Important correction:

```text
Do not require every internal CRM user on the deal to exist in public.users.
Any one matching app user is enough.
```

The following do not make a deal eligible:

- CRM user ID matches without an email match in `public.users`
- `created_by_user_id` or `updated_by_user_id`
- CONTACT participants
- company/account participants
- internal-domain heuristics

Use normalized email comparison: lowercase, stripped email strings.

## Durable Job Semantics

Initial backfill must use existing `DealJobs` as the durable replay ledger.
Do not add a new table for v1.

The one-time mapping marker is stored in `DealJobs.job_metadata`:

```json
{
  "mapping_purpose": "initial_backfill"
}
```

Use one `DealJobs` row per deal per mapping stage:

| Stage | Job type | Worker |
| --- | --- | --- |
| Stakeholder sync | `STAKEHOLDER_SYNC` | `stakeholder.sync_deal_stakeholders` |
| Calendar matching | `DEAL_CALENDAR_REMATCH` | `deal.match_calendar_to_deals` |
| Email matching | `DEAL_EMAIL_REMATCH` | `deal.match_email_to_deals` |

Initial backfill job metadata:

```json
{
  "source": "deal_mapping_initial_backfill",
  "mapping_purpose": "initial_backfill",
  "claim_batch_id": "<uuid>",
  "deal_id": "<deal_uuid>"
}
```

Claim rules:

1. Find eligible open deals.
2. Exclude active initial-backfill stakeholder jobs:
   - `status IN (PENDING, IN_PROGRESS)`
   - `job_type == STAKEHOLDER_SYNC`
   - `job_metadata.mapping_purpose == "initial_backfill"`
3. Exclude completed initial-backfill stakeholder jobs:
   - `status == COMPLETED`
   - `job_type == STAKEHOLDER_SYNC`
   - `job_metadata.mapping_purpose == "initial_backfill"`
4. Do not permanently exclude failed initial-backfill jobs.
5. Claim at most `100` deals by creating `STAKEHOLDER_SYNC` `DealJobs` rows.
6. Dispatch one `stakeholder.sync_deal_stakeholders` task per claimed deal.

This avoids the bad case where a deal is skipped forever just because a job row
was created but the worker crashed before doing real work.

## Production Loopholes And Fixes

### Loophole: Per-Deal Dispatch Can Create Too Many Celery Tasks

If we dispatch every eligible deal at once, a large org can create tens of
thousands of tasks.

Fix:

- Claim only 100 deals per org run.
- Let repeated scheduled runs drain the backlog gradually.
- Increase to 500 only after observing production metrics.

### Loophole: Stakeholder Completion Can Hide Downstream Calendar/Email Failure

If stakeholder sync dispatches calendar/email tasks without durable downstream
jobs, downstream failure is harder to track. This is fixed for both
initial-backfill and event-triggered stakeholder rematching paths.

Fix:

- When stakeholder sync succeeds, create/reuse:
  - `DEAL_CALENDAR_REMATCH`
  - `DEAL_EMAIL_REMATCH`
- Pass those job IDs to:
  - `deal.match_calendar_to_deals`
  - `deal.match_email_to_deals`

Both single-deal calendar/email tasks already accept `job_id` and use
`VisibleDealJobTracker`.

### Loophole: Broad Org Matchers Can Still Run After Integration Sync

The integration worker must not dispatch broad org matchers when calendar or
email sync persists data:

- `deal.match_calendar_to_deals_org`
- `deal.match_email_to_deals_org`

Fix:

- Stop integration workers from dispatching broad org matchers automatically.
- Keep public org/all-org task definitions temporarily for manual recovery or
  old callers.
- Build true event-based remapping later after calendar/email sync exposes
  changed event/message identifiers or an equivalent candidate-deal resolver.

### Loophole: Duplicate Claiming Can Happen If Locks Expire Or Runs Overlap

Redis task locks reduce duplicate runs but are not a durable database guarantee.

Fix:

- Use a DB-level advisory lock during each initial-backfill claim operation.
- Scope the advisory lock by organisation ID and job type.
- Keep the Redis task lock as the first cheap guard.

### Loophole: Failed Jobs Could Be Skipped Forever

Failed initial-backfill jobs must not become permanent exclusion markers.

Fix:

- Exclude only active and completed jobs from future claims.
- Let failed jobs be claimed again by a later run.
- Store failure details in `result_data` and `error_message`.

### Loophole: Email Matching Has Required Side Effects

Email matching is not only association creation. It can trigger:

- deal memory/insight pipeline fanout
- email attachment extraction

Fix:

- Preserve those side effects in `deal.match_email_to_deals`.
- Do not move initial backfill to a path that skips the single-deal email task
  behavior.

## Step-By-Step Implementation

### Step 1: Fix Eligibility To "Any App User On Deal" - Done

The shared eligibility service must:

- load same-org `public.users.email`
- load candidate open, non-deleted CRM deals
- collect owner and USER participant emails
- mark the deal eligible if any collected email appears in public users

### Step 2: Keep Deal-Level Guards - Done

These deal-level tasks must re-check eligibility before work:

- `stakeholder.sync_deal_stakeholders`
- `deal.match_calendar_to_deals`
- `deal.match_email_to_deals`

If a deal is ineligible, return skipped status and complete the visible job
instead of failing it.

### Step 3: Convert Stakeholder Org Task Into Bounded Per-Deal Dispatcher - Done

Keep the public task name unchanged:

```text
stakeholder.sync_all_deals_stakeholders_org
```

Required behavior:

1. Acquire the org-level Redis lock.
2. Acquire a DB advisory lock for the org/job-type claim.
3. Load eligible open deals.
4. Exclude active/completed stakeholder initial-backfill jobs.
5. Claim at most `100` deals by creating `STAKEHOLDER_SYNC` `DealJobs` rows.
6. Commit the claimed jobs before dispatch.
7. Dispatch one `stakeholder.sync_deal_stakeholders` task per claimed deal:

```json
{
  "organisation_id": "<org_uuid>",
  "deal_id": "<deal_uuid>",
  "change_type": "INITIAL_BACKFILL",
  "trigger_rematching": true,
  "job_id": "<stakeholder_job_uuid>"
}
```

Return:

- `eligible_deals`
- `already_completed`
- `already_active`
- `claimable_deals`
- `claimed_deals`
- `deal_tasks_dispatched`
- `claim_batch_id`

Do not return `chunks_dispatched` or `chunk_tasks` from this path.

### Step 4: Create Calendar/Email Jobs In The Single-Deal Stakeholder Task - Done

When `stakeholder.sync_deal_stakeholders` succeeds and rematching is required:

1. Create/reuse a `DEAL_CALENDAR_REMATCH` job for the same deal.
2. Create/reuse a `DEAL_EMAIL_REMATCH` job for the same deal.
3. Dispatch:

```text
deal.match_calendar_to_deals(organisation_id, deal_id, calendar_job_id)
deal.match_email_to_deals(organisation_id, deal_id, email_job_id)
```

Do not dispatch calendar/email tasks without a durable job ID in the
stakeholder-triggered rematching path.

### Step 5: Stop Automatic Broad Org Calendar/Email Matching - Done

The recurring system must not automatically scan every eligible deal just
because calendar/email sync completed.

Integration worker dispatch:

- Calendar sync does not automatically dispatch `deal.match_calendar_to_deals_org`.
- Email sync does not automatically dispatch `deal.match_email_to_deals_org`.

Keep public task definitions temporarily:

- `deal.match_calendar_to_deals_org`
- `deal.match_calendar_to_deals_all_orgs`
- `deal.match_email_to_deals_org`
- `deal.match_email_to_deals_all_orgs`

### Step 6: Future Event-Based Remapping

After a deal has completed initial backfill, periodic scheduling should not
reprocess it just because time passed.

Future remapping should be triggered by specific events, such as:

- CRM deal owner changed
- CRM USER participant added/removed
- CRM CONTACT participant added/removed
- CRM contact email changed
- deal reopened
- app user added or user email changed
- manual refresh requested
- calendar/email sync found new relevant data

Implementation note:

- Calendar/email sync currently exposes aggregate counts in the visible task
  return contract.
- True event-based matching needs changed event/message identifiers or an
  equivalent candidate-deal resolver.
- Treat that as a follow-up step after the durable initial-backfill chain is
  stable.

### Step 7: Add Dry-Run Reporting Before Rollout

Add a read-only diagnostic report before enabling this in production:

- total open deals
- eligible deals
- completed initial-backfill jobs by stage
- active initial-backfill jobs by stage
- failed initial-backfill jobs by stage
- claimable deals by stage
- skipped deals by reason

Do not auto-remove existing associations in this change.

## Expected Final Flow

```text
Scheduled deal-mapping
  -> stakeholder.sync_all_deals_stakeholders_all_orgs
      -> stakeholder.sync_all_deals_stakeholders_org
          -> load eligible open deals
          -> exclude active/completed stakeholder initial_backfill jobs
          -> claim next 100 stakeholder jobs in DealJobs
          -> dispatch one stakeholder deal task per claimed deal
              -> mark stakeholder job IN_PROGRESS
              -> re-check eligibility
              -> sync stakeholders
              -> mark stakeholder job COMPLETED / FAILED / skipped-completed
              -> create/reuse calendar + email initial_backfill jobs
              -> dispatch single-deal calendar/email tasks with job IDs

Future CRM/user/calendar/email/manual event
  -> targeted deal-level path
      -> re-check eligibility
      -> process or skipped-complete
```

## Acceptance Criteria

Implementation is complete only when all of these are true:

1. Eligibility uses "any app user on the deal", not "all internal users".
2. Deal-level tasks cannot process an ineligible deal.
3. Initial stakeholder backfill creates durable `STAKEHOLDER_SYNC` `DealJobs`.
4. Org stakeholder coordinator claims at most `100` new eligible deals per run.
5. Org stakeholder coordinator dispatches one single-deal stakeholder task per
   claimed deal.
6. Stakeholder chunk task is not exported and not available as a v1 task.
7. Completed initial-backfill jobs prevent repeated scheduled processing.
8. Active initial-backfill jobs prevent duplicate in-flight processing.
9. Failed initial-backfill jobs can be retried by a later run.
10. Stakeholder task creates durable calendar/email jobs before dispatching
   calendar/email tasks for initial-backfill and event-triggered rematching.
11. Calendar/email single-deal tasks receive `job_id` and mark job state.
12. Email matching preserves deal-memory and attachment side effects.
13. Integration calendar/email sync does not automatically dispatch broad org
    matchers.
14. Future updates are event-driven, not periodic full reprocessing.

## Test Commands

Run focused tests first:

```bash
uv run pytest tests/workers/deal_worker/services/test_deal_mapping_eligibility.py -v
uv run pytest tests/workers/deal_worker/tasks/deal_mappers/test_stakeholder_tasks.py -v
uv run pytest tests/workers/deal_worker/tasks/deal_mappers/test_calendar_matching_tasks.py -v
uv run pytest tests/workers/deal_worker/tasks/deal_mappers/test_email_matching_tasks.py -v
```

Also add focused tests for integration sync dispatch behavior:

```bash
uv run pytest tests/workers/integrations_worker/tasks/calendar -v
uv run pytest tests/workers/integrations_worker/tasks/email -v
```

Run broader checks before PR:

```bash
uv run ruff check src/ tests/
uv run mypy src/
```

## Rollout Notes

- Roll out behind existing task names.
- Watch claim counts, completed counts, active counts, failed counts, and retry
  counts by job type.
- The expected large-org behavior is gradual draining:

```text
Run 1 claims 100 stakeholder jobs
Run 2 claims next 100 stakeholder jobs
Run 3 claims next 100 stakeholder jobs
...
```

- Calendar/email downstream work should be visible as separate
  `DEAL_CALENDAR_REMATCH` and `DEAL_EMAIL_REMATCH` job completion counts.
- Once all eligible deals have completed all three initial-backfill stages,
  scheduled runs should claim `0` deals.
- Existing old associations are not cleaned in this change.
- Chunk workers can be reconsidered later as a new optimization, but they are
  not part of v1 and should not remain as dormant stakeholder task code.
