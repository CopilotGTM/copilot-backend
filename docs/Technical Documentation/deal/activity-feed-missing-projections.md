# CRM Activity Feed Missing Projections

## Goal

Inventory every confirmed source that should project into the new
`crm_activities` feed but does not yet do so. This is a planning document only;
implementation should pick one row at a time and add focused tests around that
single projection.

## Current State

The new activity feed source of truth is:

- `crm_activities`: compact feed-card projection rows.
- `crm_activity_links`: object membership for deal, contact, and company feeds.
- `crm_tasks`: canonical task source rows.
- `crm_notes`: canonical note source rows.

Current projection support is limited to tasks and notes:

- `ActivityFeedProjectionService.upsert_task()` writes `TASK` rows and links the
  primary deal plus optional contact/company metadata links.
- `ActivityFeedProjectionService.upsert_note()` writes `NOTE` rows and links the
  primary deal plus optional contact/company metadata links.
- Task and note writers call the projection service from internal APIs and from
  HubSpot/Salesforce task/note sync writers.
- Soft deletes for task/note source rows call
  `soft_delete_activity_projection()`.

The older deal timeline still has richer event coverage in
`deal_timeline_events`. Those events are not automatically visible in the new
`crm_activities` feed.

## Confirmed Already Covered

| Activity | Source table | Current projection path | Notes |
| --- | --- | --- | --- |
| Task created/updated/completed/deleted | `crm_tasks` | `ActivityFeedProjectionService.upsert_task()` / `soft_delete_activity_projection()` | Covered for manual/API tasks, agent-created deal tasks, HubSpot tasks, and Salesforce tasks. |
| Note created/updated/deleted | `crm_notes` | `ActivityFeedProjectionService.upsert_note()` / `soft_delete_activity_projection()` | Covered for deal notes and HubSpot/Salesforce notes. |

## Missing One-To-One Projection List

### 1. Deal Email Associations

**Source rows:** `emails`, `deal_email_associations`

**Current writers/readers:**

- `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py`
  creates `DealEmailAssociation`.
- `src/service/crm/timeline/deal_timeline_service.py` can create old
  `EMAIL` timeline rows.
- `src/service/crm/timeline/deal_timeline_sync_service.py` can backfill old
  `EMAIL` timeline rows.

**Missing activity projection:** create/update one `crm_activities` row per
`DealEmailAssociation`, with `activity_type = EMAIL`, `source_type = EMAIL`,
`source_record_id = association_id`, and `occurred_at = email_date` or the
provider email timestamp.

**Required links:**

- `DEAL` / `PRIMARY` link to `DealEmailAssociation.deal_id`.
- Optional `CONTACT` / `RELATED` links for matched stakeholder/contact emails
  when those emails can be resolved to `crm_contacts`.
- Optional `COMPANY` / `RELATED` link through the deal company or resolved
  participant company.

**Implementation trigger points:**

- Inline after `DealEmailAssociation` creation in the email matcher transaction.
- Batch/backfill path in timeline reconciliation or a new feed-projection
  reconciliation command.
- Status changes to `REMOVED` should soft-delete or unlink the activity from the
  deal feed instead of leaving stale feed rows.

**Priority:** P0. Emails are first-class existing timeline events and are absent
from the new activity feed.

### 2. Calendar Meeting Associations

**Source rows:** `calendar_events`, `calendar_event_deal_associations`,
`calendar_event_attendees`

**Current writers/readers:**

- `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
  creates `CalendarEventDealAssociation`.
- `src/service/meetings_tab_service.py` can manually associate meetings to deals.
- `src/service/crm/timeline/deal_timeline_service.py` can create old `MEETING`
  timeline rows.
- `src/service/crm/timeline/deal_timeline_sync_service.py` can backfill old
  `MEETING` timeline rows.

**Missing activity projection:** create/update one `crm_activities` row per
`CalendarEventDealAssociation`, with `activity_type = MEETING`,
`source_type = MEETING`, `source_record_id = association_id`, and
`occurred_at = calendar_events.start_at`.

**Required links:**

- `DEAL` / `PRIMARY` link to `CalendarEventDealAssociation.deal_id`.
- Optional `CONTACT` / `ATTENDEE` links for attendees resolved to
  `crm_contacts`.
- Optional `COMPANY` / `RELATED` link through the deal company.

**Implementation trigger points:**

- Inline after deal-meeting matcher creates an active association.
- Inline after Meetings tab manual association creates or reactivates an
  association.
- Calendar cancellation or association `REMOVED` should soft-delete or unlink
  the activity from the deal feed.
- Batch/backfill path for existing associations.

**Priority:** P0. Meetings are first-class existing timeline events and feed
cards already have display-payload validation for meeting-specific fields.

### 3. Deal Creation

**Source rows:** `crm_deals`, internal `crm_mutation_events`

**Current writers/readers:**

- Provider deal writers create `CRMDeal` rows.
- `src/service/crm/deals/write_service.py` creates internal CRM deals and records
  mutation events.
- `src/service/crm/timeline/deal_timeline_sync_service.py` builds old
  `DEAL_CREATED` timeline rows.

**Missing activity projection:** create one `crm_activities` row for deal
creation, with `activity_type = EVENT` or `OTHER`, `source_type = DEAL_CREATED`,
`source_record_id = deal_id`, and `occurred_at = source_created_at` or
`created_at`.

**Required links:**

- `DEAL` / `PRIMARY` link to the created deal.
- Optional `COMPANY` / `RELATED` link when `CRMDeal.company_id` is set.
- Optional `CONTACT` / `RELATED` link when `CRMDeal.primary_contact_id` is set.
- Optional `USER` / `OWNER` link when `CRMDeal.owner_id` is set.

**Implementation trigger points:**

- Internal deal creation path after the `CRMDeal` row exists.
- Provider event streams only when the provider event is explicitly a new deal
  creation, for example HubSpot `deal.creation` webhooks or Salesforce
  Opportunity CDC `CREATE`.
- Do not project from generic full/incremental provider sync by default. Those
  paths can insert old provider deals locally and would overfill the feed with
  historical creation cards.

**Priority:** P1. It is important for a complete feed, but less urgent than
communications.

### 4. Deal Stage Changes

**Source rows:** `crm_deal_stage_history`, internal `crm_mutation_events`

**Current writers/readers:**

- HubSpot and Salesforce deal sync write `CRMDealStageHistory`.
- `src/service/crm/deals/write_service.py` records stage history for internal
  updates.
- `src/service/crm/timeline/deal_timeline_sync_service.py` builds old
  `STAGE_CHANGE` timeline rows.

**Missing activity projection:** create one `crm_activities` row per
`CRMDealStageHistory` transition, with `activity_type = EVENT`,
`source_type = STAGE_CHANGE`, `source_record_id = history_id`, and
`occurred_at = entered_at`.

**Required links:**

- `DEAL` / `PRIMARY` link to `CRMDealStageHistory.deal_id`.
- Optional `COMPANY` / `RELATED` link through the current deal company.
- Optional `USER` / `OWNER` link through the current deal owner.

**Implementation trigger points:**

- Internal stage update path when a history row is created.
- Provider stage-change events only after the hook/stream path can identify the
  exact current transition. Do not project from broad provider stage-history
  import because full snapshots can replay old transitions.
- Batch/backfill path from existing stage-history rows only if product
  explicitly wants historical deal movement cards.

**Priority:** P1. Stage movement is central sales activity and already exists in
the old timeline.

### 5. Risk And Blocker Lifecycle

**Source rows:** `deal_risks_blockers`

**Current writers/readers:**

- `src/service/crm/risks/write_service.py` creates and updates risks/blockers.
- `src/service/crm/risks/lifecycle_service.py` resolves, dismisses, and reopens
  risks/blockers.
- `src/service/crm/timeline/deal_timeline_sync_service.py` builds old
  `RISK_FLAGGED`, `BLOCKER_ADDED`, `BLOCKER_RESOLVED`, and
  `BLOCKER_DISMISSED` rows.

**Missing activity projection:** create/update feed rows for lifecycle events,
not just the current risk/blocker state.

Recommended source identity:

- Opened/flagged: `risk_blocker_id`.
- Resolved/dismissed/reopened: either a lifecycle event identity from the
  risk/blocker audit trail, or a deterministic key such as
  `risk_blocker_id:resolved:<closed_at>`.

**Required links:**

- `DEAL` / `PRIMARY` link to `DealRiskBlocker.deal_id`.
- Optional `USER` / `OWNER` or `ASSIGNEE` link when owner fields resolve to a CRM
  user.
- Optional source link to meeting/email later if `source_type` and
  `source_record_id` can be resolved.

**Implementation trigger points:**

- Risk/blocker create.
- Resolve, dismiss, reopen, and delete/soft-delete lifecycle methods.
- AI extraction service that creates risks/blockers from meeting content.

**Priority:** P1. These are already old-timeline events and are user-visible
deal lifecycle signals.

### 6. Stakeholder / Participant Lifecycle

**Source rows:** `crm_deal_participants`

**Current writers/readers:**

- `src/service/crm/participants/stakeholder_write_service.py` creates, updates,
  and removes stakeholder-backed contact participants.
- `src/service/crm/participants/participant_write_service.py` creates, updates,
  and removes generic deal participants.
- `src/service/crm/timeline/deal_timeline_sync_service.py` builds old
  `STAKEHOLDER_ADDED` and `STAKEHOLDER_REMOVED` rows.

**Missing activity projection:** create feed rows for participant add/remove and
possibly role-change events, with `source_type = STAKEHOLDER_ADDED`,
`STAKEHOLDER_REMOVED`, or `STAKEHOLDER_UPDATED`.

**Required links:**

- `DEAL` / `PRIMARY` link to `CRMDealParticipant.deal_id`.
- `CONTACT` / `RELATED` link when `participant_type = contact` and
  `related_local_id` is set.
- `COMPANY` / `RELATED` link when `participant_type = company` and
  `related_local_id` is set.

**Implementation trigger points:**

- Stakeholder create/remove paths.
- Generic participant create/remove paths if product expects all participants,
  not only stakeholder contacts, in the feed.
- Role updates only if the UX wants “role changed” feed cards; otherwise leave
  role edits in audit/mutation history.

**Priority:** P1 for add/remove, P2 for role/edit updates.

### 7. Contact And Account Profile Mutations

**Source rows:** `crm_mutation_events`, `crm_contacts`, `crm_companies`

**Current writers/readers:**

- `src/service/crm/contacts/write_service.py` records contact mutation events.
- `src/service/crm/accounts/write_service.py` records account mutation events.
- Contact and account routers expose `/{id}/activity-feed`, but current feed
  rows only appear when tasks/notes link to those objects.

**Missing activity projection:** project relevant contact/account mutation events
into the object feed. Examples:

- `crm.contact.created`
- `crm.contact.email_added`
- `crm.contact.account_linked`
- `crm.account.created`
- `crm.account.domain_changed`
- `crm.account.owner_changed`
- `crm.account.lifecycle_stage_changed`

**Required links:**

- Contact mutation: `CONTACT` / `PRIMARY` link to the contact.
- Account mutation: `COMPANY` / `PRIMARY` link to the company.
- Optional related account/contact link for account-linking changes.

**Implementation trigger points:**

- In or after `CRMMutationEventService.record_event()` for contact/account
  entity types.
- Backfill from existing `crm_mutation_events` if historical object-feed
  completeness is required.

**Priority:** P2. Needed for contact/account feeds to feel complete, but should
be filtered to business-significant mutations to avoid noisy profile-edit spam.

### 8. Deal Field Mutations Beyond Stage

**Source rows:** `crm_mutation_events`, `crm_deals`

**Current writers/readers:**

- `src/service/crm/deals/write_service.py` records mutation events for deal
  create/update, owner changes, account/primary-contact relationship changes,
  lifecycle changes, and delete operations.
- The old timeline handles deal-created and stage-change separately, but not all
  internal deal mutation semantics.

**Missing activity projection:** project business-significant deal mutation
events that should appear in a deal feed. Examples:

- deal closed won/lost
- amount changed
- owner changed
- account changed
- primary contact changed
- deal deleted/restored if product wants audit-visible deletion events

**Required links:**

- `DEAL` / `PRIMARY` link to `CRMDealMutationEvent.deal_id`.
- Optional `COMPANY`, `CONTACT`, and `USER` links from changed-field values.

**Implementation trigger points:**

- In or after deal mutation-event recording.
- Backfill from existing `crm_mutation_events` for internal writes.
- Provider sync stage changes should stay sourced from `crm_deal_stage_history`;
  do not duplicate them from mutation events unless the source is explicitly an
  internal CRM write.

**Priority:** P2. Important, but should be designed with noise controls.

### 9. Meeting Intelligence Completion Events

**Source rows:** `calendar_event_deal_associations`

**Current writers/readers:**

- Meeting prep, summary, and follow-up statuses/data live on
  `CalendarEventDealAssociation`.
- Existing meeting timeline rows carry status metadata, but no separate new-feed
  activity is written when prep/summary/follow-up completes.

**Missing activity projection:** optional feed rows for generated meeting
intelligence, for example `MEETING_SUMMARY_CREATED` or
`MEETING_FOLLOWUP_CREATED`.

**Required links:**

- `DEAL` / `PRIMARY` link to the association deal.
- Optional `MEETING` source reference through `calendar_event_id` in
  `display_payload`.

**Implementation trigger points:**

- Meeting prep generation completion.
- Meeting summary generation completion.
- Meeting follow-up generation completion.

**Priority:** P3. This may be better represented as metadata on the meeting
activity card unless the UX explicitly wants separate feed entries.

### 10. Deal Assets From Email Attachments

**Source rows:** `deal_assets`, email attachment metadata

**Current writers/readers:**

- `src/workers/deal_worker/services/deal_asset_email_attachment_service.py`
  creates deal assets from email attachments.
- Deal asset APIs expose asset activity/status separately.

**Missing activity projection:** optional feed row when a new meaningful asset is
linked to a deal, with `source_type = DEAL_ASSET` or `EMAIL_ATTACHMENT`.

**Required links:**

- `DEAL` / `PRIMARY` link to the deal.
- Optional source reference to email association or email ID when available.

**Implementation trigger points:**

- After asset link creation.
- Deletion/unlink should soft-delete or unlink the feed card.

**Priority:** P3. Useful but likely secondary to communications and sales
lifecycle events.

## Provider Activity Types Not Yet Present As Source Tables

The activity enum already allows `CALL`, `EVENT`, `SMS`, `LINKEDIN_MESSAGE`,
`CHAT`, and `OTHER`, but this checkout does not currently have first-class
source tables/writers for HubSpot/Salesforce calls, CRM logged emails, CRM
meetings/events, SMS, LinkedIn messages, or CRM chat transcripts.

Do not write these directly into `crm_activities` as source-of-truth rows. Add
canonical source ingestion first, then add a projection method. Otherwise the
feed table would become both source and projection, which conflicts with the
current activity-feed boundary.

## Recommended Implementation Order

1. Email association projection.
2. Meeting association projection.
3. Stage-change projection.
4. Risk/blocker lifecycle projection.
5. Stakeholder add/remove projection.
6. Deal-created projection.
7. Contact/account mutation projection.
8. Other deal mutation projection.
9. Meeting intelligence generated-event projection only if UX requires separate
   cards.
10. Deal asset projection only if UX wants assets in the same feed.

## Per-Item Implementation Checklist

For each item above:

- Add one projection method to `ActivityFeedProjectionService`.
- Add tests in `tests/service/crm/test_activity_feed_projection_service.py`.
- Add writer-level tests that prove the source writer calls the projection
  method only on the intended inline or hook/stream path.
- Add a backfill/reconciliation path only after inline writes pass.
- Verify activity links always include `organisation_id` and never infer object
  ownership from external IDs alone.
- Decide whether source status changes should soft-delete the activity row or
  only delete specific `crm_activity_links`.

## Open Decisions Before Implementation

- Whether email/meeting activity IDs should reuse association IDs or use a new
  deterministic UUID namespace. Reusing association IDs is simplest and mirrors
  the old timeline `source_record_id`, but it means one association equals one
  feed card.
- Whether risk/blocker lifecycle changes should be separate feed rows or one
  mutable current-state row. Separate rows preserve chronology; one row reduces
  noise.
- Whether contact/account/deal mutation events should all project or only
  business-significant semantic events should project. The safer default is
  semantic events only.
- Whether meeting prep/summary/follow-up completion should appear as separate
  cards or as updated metadata on the existing meeting card.
