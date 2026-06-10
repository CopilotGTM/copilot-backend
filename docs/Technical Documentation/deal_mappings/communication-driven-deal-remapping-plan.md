# Communication-Driven Deal Remapping Plan

> **Purpose**: Define how email and calendar sync should trigger targeted deal
> remapping after broad organization-level calendar/email matchers were removed.
> This plan keeps the system deal-level, bounded, and event-driven.

## Final Decision

Do **not** restore automatic broad org-level matchers after email or calendar
sync:

```text
deal.match_calendar_to_deals_org
deal.match_email_to_deals_org
```

Instead, every successful email/calendar sync or webhook ingestion should run a
bounded candidate resolver:

```text
email/calendar sync or webhook
  -> collect changed emails/events
      -> resolve candidate open deals from participants
          -> create/reuse deal-level stakeholder remap job
              -> stakeholder.sync_deal_stakeholders
                  -> deal.match_calendar_to_deals
                  -> deal.match_email_to_deals
```

The existing deal-level stakeholder task remains the entry point for remapping.
It already creates durable downstream calendar and email rematch jobs before
dispatching the single-deal matchers.

## Why This Is Needed

The initial-backfill plan intentionally stopped integration sync from dispatching
broad org matchers. That protects large organizations from repeatedly scanning
thousands of deals after every sync.

However, email and calendar sync still need a way to remap deals when new
communications arrive. The correct replacement is not "scan every deal again".
The replacement is "inspect the communications that changed, infer a small set
of likely deals, and trigger only those deal-level jobs".

This matters because:

- initial email sync can ingest a large mailbox
- webhooks can arrive frequently and in bursts
- most emails/meetings are not deal-related
- completed initial backfill must not block future communication-triggered
  remaps
- deal mapping should remain observable through `DealJobs`

## Key Loopholes And Fixes

### Loophole: Sync Results Only Return Counts

Email and calendar sync results currently expose aggregate counts such as
`messages_synced` and `records_processed`. Counts are not enough to know which
communications should trigger remapping.

Fix:

- email sync returns bounded `changed_email_ids`
- calendar sync returns bounded `changed_event_ids`
- if a branch cannot return IDs immediately, use a fallback query:

```text
integration_id == current integration
synced_at >= sync_started_at
order newest first
limit candidate inspection cap
```

The fallback is acceptable only as a compatibility bridge. The preferred contract
is explicit changed IDs from the sync service.

### Loophole: Only-Internal Communications Create False Positives

If an email or meeting contains only company/internal participants, it is usually
not a customer deal communication.

Fix:

- skip any changed email/event with no external participant
- do not dispatch a remap job for internal-only records

### Loophole: Synced User Alone Fans Out Too Broadly

Triggering every deal owned by the synced mailbox user can create too many jobs,
especially during initial mailbox sync.

Fix:

- do not use synced-user-only matching in v1
- require one internal anchor and one external anchor
- internal anchor narrows to the deal team
- external anchor proves the communication is customer/account related

### Loophole: Domain Matching Can Over-Match

Company domains are useful but can match too many deals for the same account.

Fix:

- use domain matching only when an internal deal-team anchor also matches
- ignore free domains such as Gmail, Outlook, Yahoo, iCloud, Proton, and similar
  personal mail domains
- prefer exact contact email matches over domain-only matches

### Loophole: Completed Initial Backfill Could Block Future Updates

Initial backfill is one-time. Communication sync is recurring. A completed
initial-backfill job must not suppress future remapping.

Fix:

- communication-driven jobs use `mapping_purpose = "event_rematch"`
- coalesce only `PENDING` and `IN_PROGRESS` event-rematch jobs
- do not coalesce against `COMPLETED` event-rematch jobs
- do not let `mapping_purpose = "initial_backfill"` block this flow

### Loophole: Mapping Logic Could Leak Into Task Files

Task files should stay thin triggers. Candidate resolution and job creation need
to live in services.

Fix:

- add a service such as `CommunicationDealMappingTriggerService`
- org/integration/webhook tasks call one service method after sync persistence
- the service owns participant extraction, candidate ranking, caps, job
  coalescing, and dispatch

## Service Contract

Create a service with a single public method similar to:

```python
async def trigger_for_changed_communications(
    *,
    organisation_id: UUID,
    integration_id: UUID,
    source_kind: Literal["email", "calendar"],
    trigger_source: Literal[
        "initial_sync",
        "incremental_sync",
        "manual_sync",
        "webhook",
    ],
    changed_record_ids: list[UUID],
    sync_started_at: datetime | None = None,
) -> CommunicationDealMappingTriggerResult:
    ...
```

Result shape:

```python
class CommunicationDealMappingTriggerResult(BaseModel):
    inspected_records: int
    skipped_internal_only: int
    candidate_deals: int
    eligible_deals: int
    jobs_created: int
    jobs_reused: int
    jobs_dispatched: int
    capped: bool
```

Task files should log and return the result summary, but should not implement
candidate logic directly.

## Candidate Resolution Rules

### Step 1: Load Changed Communications

For email:

- load by `Email.email_id`
- require the row belongs to the current organization and integration/folder
  lineage
- extract:
  - `from_email`
  - `to_emails`
  - `cc_emails`
  - `bcc_emails`

For calendar:

- load by `CalendarEvent.event_id`
- require the event belongs to the current organization
- require a matching `CalendarEventUserState.integration_id`
- extract:
  - `organiser_email`
  - attendee emails from `CalendarAttendee`
  - integration email as an additional internal candidate

### Step 2: Split Internal And External Participants

Internal participant sources:

- same-organization app users from `public.users.email`
- active connected `KnowledgeIntegration.integration_email`
- deal owner email
- deal `CRMDealParticipant` with `participant_type == USER`

External participant sources:

- any communication participant not classified as internal
- exact CRM contact emails
- non-free email domains from external participants

Normalize emails before comparison:

```text
lowercase
strip whitespace
drop empty values
```

### Step 3: Skip Non-Deal Communications

Skip the communication if:

- no external participant exists
- all participants are internal
- every external email has a free domain and no exact CRM contact match exists

### Step 4: Find Candidate Deals

A deal is a candidate only when all are true:

1. deal belongs to the organization
2. deal is not deleted
3. deal status is `OPEN`
4. existing deal-mapping eligibility passes
5. at least one internal anchor matches:
   - `CRMDeal.owner_email`, or
   - non-deleted `CRMDealParticipant.related_email` where
     `participant_type == USER`
6. at least one external anchor matches:
   - exact `CRMDeal.primary_contact_email`
   - exact CONTACT participant email
   - exact CRM contact email related to the deal
   - non-free external domain matching deal/contact/company domain

Do not trigger a deal only because the synced integration owner owns the deal.

## Candidate Ranking

Rank candidate deals before dispatch:

| Rank | Signal | Reason |
| --- | --- | --- |
| 1 | Existing association for changed email/event | The communication was already linked before and changed again. |
| 2 | Exact external contact email + internal deal user | Strongest new-match signal. |
| 3 | Exact primary contact email + internal deal user | Strong deal-level signal. |
| 4 | Non-free external domain + internal deal user | Useful but broader. |

When the cap is reached, dispatch higher-ranked deals first.

## Caps And Dispatch Rules

Default v1 caps:

| Trigger | Records inspected | Deal jobs dispatched |
| --- | ---: | ---: |
| Initial integration sync | 500 newest changed communications | 50 deals |
| Incremental/manual integration sync | 500 newest changed communications | 50 deals |
| Webhook burst | 100 newest changed communications | 10 deals |

Dispatch rules:

1. Dedupe by `deal_id`.
2. Create or reuse one `STAKEHOLDER_SYNC` `DealJobs` row per candidate deal.
3. Use `job_metadata.mapping_purpose = "event_rematch"`.
4. Reuse only active jobs:
   - `PENDING`
   - `IN_PROGRESS`
5. Do not reuse `COMPLETED` jobs.
6. Dispatch:

```json
{
  "organisation_id": "<org_uuid>",
  "deal_id": "<deal_uuid>",
  "change_type": "COMMUNICATION_SYNC_REMAP",
  "trigger_rematching": true,
  "job_id": "<stakeholder_job_uuid>"
}
```

Recommended metadata:

```json
{
  "source": "communication_sync_remap",
  "mapping_purpose": "event_rematch",
  "source_kind": "email",
  "trigger_source": "initial_sync",
  "integration_id": "<integration_uuid>",
  "candidate_reason": "exact_external_contact_and_internal_user",
  "changed_record_sample": ["<email_or_event_uuid>"]
}
```

## Where To Wire It

Call the trigger service only after the sync has successfully persisted records
and cursor state.

Wire these paths:

- email org sync task, after each successful integration sync
- email integration sync task, after successful manual integration sync
- calendar org sync task, after each successful integration sync
- calendar integration sync task, after successful manual integration sync
- email/calendar webhook ingestion paths, after changed rows are persisted

Do not call the trigger service when:

- sync failed
- sync timed out
- no changed communications were persisted
- the integration is inactive or disconnected
- the sync skipped because required scopes are missing

## Initial Sync Behavior

Initial email/calendar sync can produce many records. It must still be eligible
to trigger communication-driven remapping, but only through the caps above.

Important:

```text
The one-time initial-backfill rule does not apply to communication sync.
```

Initial deal backfill answers: "Has this deal been mapped at least once?"

Communication sync answers: "Did new or changed communications arrive that are
likely relevant to a small set of deals?"

Those are different ledgers and must not block each other.

## Webhook Behavior

Webhooks should use the same candidate resolver as sync tasks.

Provider webhooks may deliver only a notification/cursor, not a complete local
email/event row. In that case:

1. webhook task fetches or syncs the changed provider data
2. local rows are persisted
3. changed row IDs are passed to the trigger service
4. trigger service dispatches bounded deal-level jobs

Webhook bursts should be coalesced by active `DealJobs`, not by dropping future
completed remaps.

## Test Plan

Add focused tests for the service:

1. email with only internal participants dispatches zero jobs
2. calendar event with only internal attendees dispatches zero jobs
3. email with app user + exact CRM contact dispatches one matching open deal
4. calendar event with internal attendee + external contact dispatches one
   matching open deal
5. external domain match requires an internal deal-team anchor
6. free-domain external address does not create domain-only candidates
7. closed/deleted/ineligible deals are skipped
8. duplicate communications for one deal create one job
9. existing `PENDING`/`IN_PROGRESS` event-rematch job is reused
10. existing `COMPLETED` event-rematch job does not block a new trigger
11. initial-backfill jobs do not block communication-triggered jobs
12. caps are enforced and result reports `capped = true`

Add task-level tests:

1. email org task calls trigger service after successful integration sync
2. email integration task calls trigger service after successful manual sync
3. calendar org task calls trigger service after successful integration sync
4. calendar integration task calls trigger service after successful manual sync
5. failed sync does not call trigger service
6. skipped/missing-scope sync does not call trigger service

## Acceptance Criteria

Implementation is complete only when all are true:

1. Broad org email/calendar matchers are not automatically dispatched by sync.
2. Sync results expose bounded changed email/event IDs or use the documented
   fallback.
3. Candidate resolution requires both internal and external anchors.
4. Internal-only communications are skipped.
5. Domain-only matching ignores free domains and requires an internal deal-team
   anchor.
6. Candidate deals pass existing eligibility before dispatch.
7. Communication-triggered jobs use `mapping_purpose = "event_rematch"`.
8. Active event-rematch jobs coalesce; completed jobs do not suppress future
   sync-triggered remaps.
9. Dispatch stays deal-level through `stakeholder.sync_deal_stakeholders`.
10. Initial sync, incremental sync, manual sync, and webhooks share the same
    candidate resolver.
11. Caps are enforced and visible in logs/results.

## Rollout Notes

- Start with the default caps; do not raise them until queue depth and task
  runtime are observed in production.
- Track:
  - changed communications inspected
  - internal-only skips
  - candidate deals
  - eligible deals
  - jobs created/reused/dispatched
  - capped runs
- If too many candidates are produced, tighten ranking before increasing caps.
- If too few candidates are produced, add exact CRM contact/company association
  lookups before allowing synced-user-only fanout.

