# Deal Mapping Git Change Overview

## Purpose

This change moves deal mapping away from broad organization-wide rematching after
email/calendar sync and toward bounded, durable, per-deal jobs.

The new model has two paths:

1. Initial backfill claims a small batch of eligible open deals and maps each
   deal once.
2. New email/calendar communications trigger targeted remapping only for deals
   likely affected by those communications.

Both paths use `DealJobs` as the replay and visibility ledger.

## High-Level Flow

```text
Initial scheduled backfill
  -> stakeholder.sync_all_deals_stakeholders_org
      -> DealMappingBackfillClaimService claims up to 100 eligible deals
          -> create PENDING STAKEHOLDER_SYNC DealJobs
          -> dispatch stakeholder.sync_deal_stakeholders per claimed deal
              -> re-check deal eligibility
              -> sync stakeholders for one deal
              -> if rematching is needed:
                  -> DealMappingDownstreamJobService creates/reuses:
                      -> DEAL_CALENDAR_REMATCH DealJob
                      -> DEAL_EMAIL_REMATCH DealJob
                  -> dispatch deal.match_calendar_to_deals
                  -> dispatch deal.match_email_to_deals

Email/calendar sync or webhook
  -> sync changed messages/events
      -> CommunicationDealMappingTriggerService inspects changed records
          -> skip internal-only communications
          -> resolve candidate open deals
          -> create/reuse event-rematch STAKEHOLDER_SYNC DealJobs
          -> dispatch stakeholder.sync_deal_stakeholders
              -> same downstream calendar/email rematch path as above
```

## Core Services

### `DealMappingEligibilityService`

Centralizes whether a deal can enter stakeholder, calendar, or email mapping.

A deal is eligible only when:

- it belongs to the current organization
- it is open
- it is not deleted
- it has at least one internal CRM user email that also exists in same-org
  `public.users.email`

Internal user emails come from:

- `CRMDeal.owner_email`
- active `CRMDealParticipant.related_email` where `participant_type == USER`

Important rule: any one app user on the deal is enough. CRM-only users do not
block mapping if another internal user is a real app user.

The deal-level stakeholder, calendar, and email tasks all call this service
before doing work. Ineligible jobs are completed as `skipped` instead of running
matchers.

### `DealMappingBackfillClaimService`

Claims initial backfill work for one organization.

Logic:

- takes an organization-scoped advisory transaction lock
- loads eligible deal IDs from `DealMappingEligibilityService`
- ignores active or completed initial-backfill stakeholder jobs
- allows failed initial-backfill jobs to be claimed again
- creates up to `INITIAL_BACKFILL_CLAIM_LIMIT = 100` new `STAKEHOLDER_SYNC`
  jobs
- returns lightweight task payloads for the org task to dispatch

Initial backfill metadata:

```json
{
  "source": "deal_mapping_initial_backfill",
  "mapping_purpose": "initial_backfill",
  "claim_batch_id": "<uuid>",
  "deal_id": "<deal_uuid>"
}
```

### `DealMappingDownstreamJobService`

Creates calendar/email rematch jobs after stakeholder sync determines rematching
is needed.

For each deal it ensures there is one:

- `DEAL_CALENDAR_REMATCH`
- `DEAL_EMAIL_REMATCH`

Reuse rules differ by purpose:

- `INITIAL_BACKFILL` reuses `PENDING`, `IN_PROGRESS`, and `COMPLETED` downstream
  jobs so one-time backfill does not repeat completed stages.
- event rematches reuse only `PENDING` and `IN_PROGRESS` jobs so completed past
  remaps do not block future communication updates.

### `CommunicationDealMappingTriggerService`

Runs after successful email/calendar sync or webhook-triggered sync.

Inputs:

- organization ID
- integration ID
- source kind: `email` or `calendar`
- trigger source: `initial_sync`, `incremental_sync`, `manual_sync`, or
  `webhook`
- changed record IDs when available
- sync start time as fallback

Record loading:

- email records are loaded by org, integration, and either changed email IDs or
  `synced_at >= sync_started_at`
- calendar records are loaded by org, integration user state, and either changed
  event IDs or `synced_at >= sync_started_at`
- existing email/event deal associations are included because they are the
  strongest candidate signal

Participant classification:

- normalize all participant emails
- internal means exact app/integration email or configured organization domain
- free domains such as Gmail, Outlook, Yahoo, iCloud, and Proton are not treated
  as account domains
- records with no external participant are skipped

Candidate deal resolution:

- batch sync can load open deals anchored to internal users
- webhook sync uses a narrower lookup:
  - existing communication associations
  - exact external contact email matches
  - no broad internal-user fanout
  - no domain-only matching

Candidate ranking:

1. existing communication association
2. exact external contact plus internal user
3. exact primary contact plus internal user
4. external company domain plus internal user

Dispatch caps:

- normal sync inspects up to 500 records and dispatches up to 50 deals
- webhook sync inspects up to 100 records and dispatches up to 10 deals

Created stakeholder job metadata:

```json
{
  "source": "communication_sync_remap",
  "mapping_purpose": "event_rematch",
  "source_kind": "email|calendar",
  "trigger_source": "manual_sync|incremental_sync|webhook",
  "integration_id": "<integration_uuid>",
  "candidate_reason": "<reason>",
  "changed_record_sample": ["<record_uuid>"],
  "created_from_change_type": "COMMUNICATION_SYNC_REMAP"
}
```

## Entry Point Changes

### Integration Sync Tasks

Email and calendar integration/org sync tasks now call
`CommunicationDealMappingTriggerService` after successful persistence when
records changed.

The old broad dispatches were removed:

```text
deal.match_calendar_to_deals_org
deal.match_email_to_deals_org
```

Sync results now include `communication_deal_mapping` summaries when the trigger
runs.

### Webhook Ingestion

Google and Microsoft webhook ingestion now pass:

```json
{"trigger_source": "webhook"}
```

That lets downstream sync use webhook-specific caps and targeted candidate
lookup.

### Stakeholder Org Task

`stakeholder.sync_all_deals_stakeholders_org` no longer calls
`StakeholderSyncService.sync_for_organisation()` and no longer dispatches broad
calendar/email org matchers.

It now:

- claims a bounded initial-backfill batch
- dispatches one `stakeholder.sync_deal_stakeholders` task per claimed deal
- passes the durable `DealJobs.job_id` into each task

### Stakeholder Deal Task

`stakeholder.sync_deal_stakeholders` now:

- checks eligibility before syncing
- completes ineligible jobs as skipped
- creates/reuses downstream calendar/email `DealJobs`
- dispatches downstream tasks only when a new downstream job needs execution

### Calendar And Email Deal Tasks

`deal.match_calendar_to_deals` and `deal.match_email_to_deals` now re-check
eligibility before matching. Ineligible deals are marked skipped through visible
job tracking.

### Meeting Bot Session Service

First-session creation now handles a duplicate insert race:

- try to insert the pending session
- if a unique constraint loses to another worker, rollback
- re-read and return the winning row
- re-raise only if the winning row cannot be found

This prevents concurrent webhook-triggered workers from failing on the same
calendar event session.

### Helm Values

The `stakeholder-research-retry` cron schedule was removed from
`charts/application/values.yaml`.

## Durable Job Semantics

All automated jobs use:

```python
AUTOMATED_PIPELINE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
```

Job purposes are intentionally separate:

| Purpose | Meaning | Completed jobs reused? |
| --- | --- | --- |
| `initial_backfill` | one-time mapping for eligible existing open deals | yes |
| `event_rematch` | recurring remap caused by new communications | no |

This prevents completed backfill from suppressing future sync/webhook remaps.

## Safety Properties

- Every query uses `organisation_id` as the tenant boundary.
- External/provider IDs are never trusted alone.
- Org-wide email/calendar rematch dispatch after sync is removed.
- Backfill is bounded to 100 claimed deals per org run.
- Communication remaps are capped by trigger type.
- Webhooks cannot fan out through every deal owned by an internal user.
- Internal-only communications do not dispatch deal mapping.
- Active pending/in-progress jobs are reused to avoid duplicate concurrent work.
- Failed initial-backfill jobs can be retried.

## Test Coverage Added Or Updated

The staged tests cover:

- eligibility decisions for open/deleted/closed deals and app-user membership
- initial-backfill claim limits, active/completed suppression, and lock failure
- downstream calendar/email job creation and reuse rules
- communication-triggered remap dispatch, caps, webhook narrowing, and completed
  event-rematch behavior
- stakeholder/calendar/email task eligibility skips
- stakeholder org task bounded per-deal dispatch
- removal of broad integration-sync deal matcher dispatch
- webhook `trigger_source="webhook"` propagation
- meeting recording session recovery after duplicate insert races

