# Stakeholder and communication hydration service restructure

## Status

Implemented on 2026-07-25. The domain moves, historical participant-resolution
fix, participant-owned discovery writes, unified visible job, post-commit
research dispatch, bounded history paging, metrics, documentation, and
regression coverage described below are present in this working tree.

Verification completed:

- focused cross-layer regression selection: 301 tests pass;
- changed-file Ruff checks pass;
- Python compilation checks pass;
- `git diff --check` passes;
- old production/test import-path searches return no matches;
- participant, communication, and deal domain services contain no worker
  imports.

This plan supersedes the file-placement parts of
`2026-07-24-stakeholder-hydration-wiring-hardening.md`. The business rules in
that plan remain valid unless this document explicitly replaces them.

Explicit replacements:

- Deal creation with one or more inline stakeholders starts one visible
  `stakeholder_sync` job for the whole deal. It does not start one job per
  stakeholder and does not also publish a duplicate silent hydration.
- Extracting participant resolution into one shared service is allowed. This is
  not a new workflow or queue; it is existing hydration logic reused by both
  changed-source and deal-replay modes.
- Visible hydration context uses the current `job_id`, `job_type`, and
  `emit_events` contract rather than introducing a second `visible_job_id`
  concept.

## Decision

Move reusable stakeholder and communication-mapping business logic out of
`src/workers`. Keep Celery tasks, queue publication, retries, visible progress,
scheduled claims, and recovery in the worker package.

Do not create a generic `src/service/crm/deal_mapping/` package. The deeper audit
shows that these services have clear domain owners:

- stakeholder normalization belongs to `crm/participants`;
- communication participant resolution and hydration belong to
  `crm/communications`;
- deal eligibility belongs to `crm/deals`;
- mailbox ownership belongs to `service/integrations/email`;
- durable job creation belongs to `crm/jobs`;
- Celery execution and recovery remain under `workers/deal_worker`.

This avoids replacing one miscellaneous worker folder with one miscellaneous
service folder.

## Why this restructure is justified

The repository boundary is already explicit:

```text
src/service  = business logic
src/workers  = Celery applications and execution boundaries
```

The current code violates that boundary in two directions:

1. Reusable CRM services such as `StakeholderSyncService` and
   `DealMappingEligibilityService` live below
   `src/workers/deal_worker/services/deal_mapper_services`.
2. `src/service/crm/discovery/communication_deal_association.py` imports the
   integration-email resolver from the worker package.

Moving files without fixing those imports would be cosmetic. The target
dependency direction for domain business logic is:

```text
API router / agent tool / Celery task
                 |
                 v
         src/service packages
                 |
                 v
       domain models and infrastructure
```

`crm/participants`, `crm/communications`, and `crm/deals` must not import
`src.workers`. `crm/jobs` is the repository's explicit application-orchestration
boundary: it may use the existing deal-worker client to publish a durable job
after committing its row. This narrow exception avoids introducing a new
application layer or messaging abstraction solely for this restructure.

## Current functional gap found during the audit

The reported scenario is valid:

1. A deal is created with an approved stakeholder.
2. Historical emails exist with that stakeholder, an internal user, and another
   customer participant.
3. Deal-scoped hydration finds the historical email.
4. No additional stakeholder is created.

The current deal-backfill path begins with only the contacts already approved on
the deal:

```text
backfill_deals
  -> load approved deal contact IDs
  -> load those CRMContact rows
  -> build identity_contacts from only those rows
  -> load their historical emails/events
  -> ask association service to discover participants
```

`CommunicationDealAssociationService` can discover only contact IDs already
present in `identity_contacts`. Therefore, a new email participant cannot be
discovered during historical deal replay unless source-driven hydration happened
to resolve that participant earlier.

The existing unit tests verify `_discover_participants` in isolation and verify
that deal backfill passes the approved anchor contacts. They do not cover the
end-to-end case where a historical communication contains an additional
unresolved participant.

This is the first behavior that must receive a failing characterization test
before implementation.

## Terminology and sources of truth

### Stakeholder

“Stakeholder” is product language for an active contact participant:

```text
CRMDealParticipant
  participant_type = CONTACT
  approval_status = APPROVED or PENDING
  is_deleted = false
  related_local_id -> CRMContact.contact_id
```

No separate stakeholder table or stakeholder mapper should become a new source
of truth.

### Existing stakeholder anchor

An existing approved contact participant used to locate historical
communications for a deal.

### Additional stakeholder candidate

An external person found in a communication that already matches a deal through
an existing approved stakeholder plus an internal deal participant.

### Communication association

The durable compatibility link:

- email: `DealEmailAssociation`;
- meeting: `CalendarEventDealAssociation`.

The activity feed is a projection of these associations. Activity visibility is
not matching evidence.

## Target folder structure

```text
src/
├── service/
│   ├── crm/
│   │   ├── participants/
│   │   │   ├── stakeholder_read_service.py
│   │   │   ├── stakeholder_write_service.py
│   │   │   └── stakeholder_sync_service.py
│   │   │
│   │   ├── communications/
│   │   │   ├── __init__.py
│   │   │   ├── participant_resolution_service.py
│   │   │   ├── hydration_service.py
│   │   │   └── deal_association_service.py
│   │   │
│   │   ├── deals/
│   │   │   ├── mapping_eligibility_service.py
│   │   │   └── ... existing deal services
│   │   │
│   │   └── jobs/
│   │       ├── deal_job_service.py
│   │       └── deal_job_sse_service.py
│   │
│   └── integrations/
│       └── email/
│           ├── mailbox_identity_resolver.py
│           └── ... existing email services
│
└── workers/
    └── deal_worker/
        ├── tasks/
        │   └── deal_mappers/
        │       ├── stakeholder_tasks.py
        │       ├── contact_communication_hydration_tasks.py
        │       └── visible_job_tracking.py
        │
        ├── services/
        │   └── deal_mapping_backfill_claim_service.py
        │
        └── utils/
            └── contact_communication_hydration_dispatch.py
```

No generic repository, interface, manager, registry, or base-service layer is
needed.

## Exact file moves

### Phase A: stakeholder ownership

| Current file | Target file | Reason |
| --- | --- | --- |
| `src/workers/deal_worker/services/deal_mapper_services/stakeholder_sync_service.py` | `src/service/crm/participants/stakeholder_sync_service.py` | Normalizes canonical participants and contacts; this is CRM business logic. |
| `src/workers/deal_worker/services/deal_mapper_services/deal_mapping_eligibility.py` | `src/service/crm/deals/mapping_eligibility_service.py` | Defines whether a deal can participate in CRM mapping. |
| `src/workers/deal_worker/services/deal_mapper_services/integration_email_resolver.py` | `src/service/integrations/email/mailbox_identity_resolver.py` | Resolves active user-owned integration mailboxes; it is not worker logic. |
| `src/workers/deal_worker/services/deal_mapper_services/deal_mapping_backfill_claim_service.py` | `src/workers/deal_worker/services/deal_mapping_backfill_claim_service.py` | Claims `DealJobs`, owns worker locks and recovery, and therefore remains worker orchestration. |

After imports are updated, delete the empty
`src/workers/deal_worker/services/deal_mapper_services/` directory. Do not leave
compatibility import wrappers; all consumers are internal and can move
atomically.

### Phase B: hydration ownership

| Current file | Target file | Reason |
| --- | --- | --- |
| `src/service/crm/discovery/contact_communication_hydration.py` | `src/service/crm/communications/hydration_service.py` | Orchestrates source-, contact-, and deal-scoped communication hydration. |
| `src/service/crm/discovery/communication_deal_association.py` | `src/service/crm/communications/deal_association_service.py` | Owns communication-to-deal matching and durable association writes. |
| Existing participant extraction/resolution code inside hydration | `src/service/crm/communications/participant_resolution_service.py` | The same resolution must run for source-driven and historical deal replay. |

Delete `src/service/crm/discovery/` only when no other files or imports remain.

## Service responsibilities

### `StakeholderWriteService`

Owns synchronous stakeholder membership writes:

- resolve or create `CRMContact`;
- create, reactivate, or reuse `CRMDealParticipant`;
- idempotently add communication-discovered contact participants with the
  supplied source and approval state;
- maintain approval and primary-contact state;
- write the stakeholder lifecycle activity;
- flush changes and return explicit write outcome metadata.

It must not publish Celery tasks.

### `StakeholderSyncService`

Owns normalization after CRM or manual participant changes:

- reject internal contact participants;
- normalize participant email;
- copy canonical contact identity onto the participant;
- report communication replay independently from research eligibility;
- return contact IDs that require research.

It must not import worker dispatch utilities. The Celery task dispatches research
for the returned IDs after the service transaction commits.

### `DealMappingEligibilityService`

Owns the reusable eligibility rule:

- deal exists in the organisation;
- deal is open and active;
- deal has an internal owner or active user participant;
- at least one internal participant maps to an application user.

It returns a decision and reason. It does not publish tasks.

### `MailboxIdentityResolver`

Returns active Gmail/Outlook mailbox identities grouped by application user. It
contains only database queries and normalization. CRM association services may
import it; it must not import CRM or worker orchestration.

### `CommunicationParticipantResolutionService`

Owns one reusable identity-resolution operation for a bounded set of emails and
events:

- extract normalized participant identities;
- apply current internal-user, inbound-engagement, role-mailbox, attendee and
  webinar guardrails;
- load existing CRM contacts for all eligible activity identities;
- create missing contacts only when existing creation guardrails permit it;
- return `identity_contacts`, created/reused contact IDs, skip counters, and the
  eligible contact identities for each individual email/event.

This service reuses the current hydration plan logic. It does not introduce a
new matching heuristic. Per-communication evidence is required so declined and
resource attendees filtered during resolution cannot re-enter matching when the
association service reads the raw provider row.

### `CommunicationHydrationService`

Owns three modes:

1. changed-source hydration;
2. explicit-contact history hydration;
3. deal-scoped history hydration.

For every mode it:

- loads communication history in deterministic bounded pages;
- calls `CommunicationParticipantResolutionService`;
- projects contact/account activity;
- calls `CommunicationDealAssociationService`;
- returns metrics and affected deal/contact IDs.

It does not publish Celery tasks or SSE events.

### `CommunicationDealAssociationService`

Owns:

- current-rule email and meeting matching;
- retry-safe `DealEmailAssociation` and
  `CalendarEventDealAssociation` writes;
- stale-association reconciliation;
- return additional-stakeholder candidates after a confident association;
- deal-owned activity projection;
- last-contact updates.

It does not resolve provider identities, write `CRMDealParticipant`, or publish
downstream tasks. `CommunicationHydrationService` passes its confident
additional-stakeholder candidates to `StakeholderWriteService` in the same
transaction.

### `DealJobService`

Remains the API/agent boundary for user-visible work:

- create one durable visible job;
- commit it before publication;
- publish the stable Celery task name;
- store enqueue status;
- keep failed publishes pending so reconnect/retry can enqueue them again;
- expose active jobs for reconnection.

Both inline deal creation and standalone stakeholder addition call the same
method after their business transaction commits.

For a standalone add, the job may retain the single participant ID. For deal
creation with multiple inline stakeholders, the deal-level job stores
`stakeholder_id = NULL` and records the affected participant IDs in metadata;
it must never choose one participant arbitrarily.

Primary-contact-only creation or update remains a direct, single
`deal_communication_hydration` job because there is no standalone stakeholder
action to represent. CRM imports and integration-driven participant changes use
the same stable worker tasks without creating user-visible jobs.

### Celery tasks

Tasks remain thin orchestration:

- parse UUID/string payloads;
- open bounded sessions through existing patterns;
- call services;
- publish progress and terminal SSE state;
- dispatch research, attachments and insights after commit;
- apply retry policy.

## Unified stakeholder runtime flow

```text
deal create with contact participant
             OR
standalone stakeholder add
             OR
participant membership API
              |
              v
StakeholderWriteService / ParticipantWriteService
              |
              v
canonical CRMContact + CRMDealParticipant written
              |
              v
stakeholder lifecycle activity written
              |
              v
request transaction commits
              |
              v
DealJobService creates one stakeholder_sync job for the deal action
              |
              v
stakeholder.sync_deal_stakeholders
              |
              v
DealMappingEligibilityService
              |
              v
StakeholderSyncService normalizes every active contact participant
              |
              v
worker dispatches returned research IDs
              |
              v
deal.hydrate_contact_communications(deal_id, same visible job)
              |
              v
CommunicationHydrationService.backfill_deals
              |
              v
CommunicationDealAssociationService writes associations
              |
              v
StakeholderWriteService adds confident additional stakeholders
              |
              v
shared participant policy reconciles stale associations
and globally rejects ambiguous meeting matches
              |
              v
associations/activity/participant transaction commits
              |
              v
post-commit attachment and insight publication
              |
              v
same visible job completes
```

One job represents the user action. The worker processes the whole deal, so the
job is not repeated once per inline stakeholder.

## Correct historical discovery flow

Deal replay must expand communication identities before association:

```text
load approved stakeholder anchors for the deal
  -> load historical emails/events containing an anchor
  -> resolve every eligible participant in those communications
  -> merge anchor and co-participant CRM contacts into identity_contacts
  -> test communication against current deal rule
  -> write email/meeting association
  -> return eligible customer-domain co-participant candidates
  -> add candidates through StakeholderWriteService
  -> replay newly approved contacts once, with discovery disabled
```

The communication still needs:

1. an existing approved deal contact; and
2. the deal owner or an active internal deal participant.

Resolving a co-participant does not itself make the communication eligible.

## Additional stakeholder rules

Preserve the current guardrails:

- discovery runs only after a confident communication-to-deal association;
- the additional contact must share a customer domain already established by
  the deal account or approved stakeholders;
- vendor, adviser and unrelated domains are not added;
- the first five communication-discovered participants are auto-approved;
- later discoveries remain pending;
- pending participants cannot anchor matching;
- discovery expands one hop only;
- a replay never recursively walks an unlimited email graph;
- a second replay reuses the contact and participant instead of duplicating
  either row.

Emails may associate with every eligible deal. Meetings still require one
unambiguous deal across all candidates. There is no rolling window or
deal-created-date cutoff.

## Example acceptance scenario

Given:

- open deal `D`;
- owner `nikhil@internal.test`;
- approved stakeholder `buyer@customer.test`;
- no stakeholder for `champion@customer.com`;
- a retained email containing all three addresses;
- an active mailbox integration for the internal owner.

When the stakeholder mapping job runs:

1. deal replay locates the retained email through `buyer@customer.test`;
2. participant resolution finds or creates the CRM contact for
   `champion@customer.com`;
3. the email matches `D` because it contains the approved buyer and internal
   owner;
4. one active `DealEmailAssociation` exists for `D`;
5. one deal activity card is projected for the email;
6. `champion@customer.com` becomes an `EMAIL_DISCOVERY` contact participant;
7. it is approved when the discovery cap allows it;
8. the new contact receives one bounded history replay;
9. repeating the job creates no duplicate contact, participant, association, or
   activity card.

## Implementation sequence

### Step 0: freeze behavior with tests

Before moving files:

- add one failing end-to-end service test for historical co-participant
  discovery;
- keep current source-driven participant discovery tests;
- record the current focused suite result so structural moves can be proven
  behavior-neutral apart from the explicit fix.

### Step 1: move stakeholder-owned services

- move stakeholder sync, deal eligibility, and mailbox identity resolver;
- move the worker claim service one level up;
- update production and test imports;
- remove worker imports from `StakeholderSyncService`;
- return research contact IDs in service results;
- keep Celery task names and payloads unchanged;
- delete the empty old package.

Verification:

- stakeholder sync and eligibility tests pass from their new service paths;
- `rg "src\\.workers\\.deal_worker\\.services\\.deal_mapper_services" src tests`
  returns nothing;
- no behavior changes yet.

### Step 2: converge stakeholder entrypoints

- preserve `StakeholderWriteService` as the canonical write boundary;
- expose explicit `created/reactivated/reused` outcome metadata;
- keep `replay_required` separate from returned research contact IDs;
- after commit, call one `DealJobService` method from:
  - deal creation with inline stakeholders;
  - standalone stakeholder API;
  - stakeholder agent tool;
- do not dispatch once per inline stakeholder;
- do not enqueue for an unchanged already-active stakeholder;
- keep non-user-visible participant/import entrypoints on the same stable Celery
  task without manufacturing frontend jobs.

Verification:

- one user action creates at most one visible job;
- both UI entrypoints return `processing_jobs`;
- duplicate stakeholder submission creates no replay;
- the task processes all active deal participants.

### Step 3: move hydration services

- create `src/service/crm/communications`;
- move hydration and association services;
- update task and test imports;
- move participant-plan/resolution logic into the shared resolution service;
- pass per-communication eligible identities into association;
- delete the empty `crm/discovery` package.

Verification:

- `src/service` contains no `src.workers` import for these flows;
- changed-source, contact and deal hydration tests pass before the behavior fix;
- stable Celery task names remain registered.

### Step 4: fix historical additional-stakeholder discovery

- run participant resolution over communications loaded by deal backfill;
- merge resolved co-participants into `identity_contacts`;
- keep matching anchored to existing approved deal contacts;
- write discovered membership only through `StakeholderWriteService`;
- preserve domain, cap, ambiguity and one-hop guardrails;
- add metrics separating:
  - identities considered;
  - existing contacts resolved;
  - contacts created;
  - participants added approved;
  - participants added pending;
  - participants skipped by domain;
  - unmatched and ambiguous communications.

Verification:

- the example acceptance scenario passes;
- source-driven discovery remains unchanged;
- ambiguous meetings and vendor-domain participants remain unassociated;
- retries remain idempotent.

### Step 5: visible completion and post-commit follow-ups

- keep the same visible job active from stakeholder sync through hydration;
- complete it only after association/activity commit;
- dispatch attachments and insights only for committed email associations;
- pass exact attachment-bearing email IDs from the current hydration input;
  thread context may help matching but must not widen provider work;
- mark exhausted hydration failure on the same visible job;
- keep background hydration silent unless explicitly given visible job context.

Verification:

- SSE never reports success before hydration commits;
- refresh recovery returns the active job;
- one terminal success or failure is published;
- an attachment outside the current hydration scope emits no extraction task;
- frontend requires no new endpoint or stream protocol.

### Step 6: delete obsolete structure and update documentation

- delete old import paths and empty packages;
- update diagrams and task maps that still describe split email/calendar
  mappers or three stakeholder jobs;
- preserve historical enum values only where old database rows require them;
- do not restore retired task names.

## Tests to move

| Current test | Target test |
| --- | --- |
| `tests/workers/deal_worker/services/test_stakeholder_sync_service.py` | `tests/service/crm/participants/test_stakeholder_sync_service.py` |
| `tests/workers/deal_worker/services/test_deal_mapping_eligibility.py` | `tests/service/crm/deals/test_mapping_eligibility_service.py` |
| `tests/workers/deal_worker/services/test_integration_email_resolver.py` | `tests/service/integrations/email/test_mailbox_identity_resolver.py` |
| `tests/service/crm/discovery/test_contact_communication_hydration.py` | `tests/service/crm/communications/test_hydration_service.py` |
| `tests/service/crm/discovery/test_communication_deal_association_service.py` | `tests/service/crm/communications/test_deal_association_service.py` |
| `tests/workers/deal_worker/services/test_deal_mapping_backfill_claim_service.py` | Keep in place; only update its production import. |

Task, router, job-service and agent tests remain in their current ownership
folders.

## Required new tests

1. Deal backfill resolves an existing co-participant contact and adds it.
2. Deal backfill creates an eligible missing co-participant contact and adds it.
3. Inbound-only history does not create a cold-outreach contact.
4. Vendor-domain co-participant is not added.
5. Sixth discovered participant is pending and cannot anchor another match.
6. Replaying the same email is idempotent.
7. Ambiguous meeting adds neither association nor stakeholder.
8. Declined and resource attendees cannot anchor association or discovery.
9. Deal creation and standalone add call the same job service.
10. Multiple inline stakeholders create one deal-level job with no arbitrary
    singular stakeholder ID.
11. Hydration completes the same visible stakeholder job.
12. Large retained histories are consumed in deterministic bounded pages
    without a date cutoff.

At least one test must exercise the complete service path rather than mocking
`_discover_participants`; otherwise the current identity-map gap can regress
undetected.

## Structural acceptance checks

The restructure is complete only when:

- reusable CRM mapping logic lives under `src/service`;
- participant, communication, and deal domain services have no import from
  `src.workers`; the documented `crm/jobs` orchestration exception is the only
  service-layer publisher in this flow;
- worker tasks contain orchestration rather than matching rules;
- old packages are deleted rather than retained as compatibility shells;
- Celery task names, queues and public API contracts remain stable;
- no database migration is required for the file move;
- the focused tests pass from their new locations;
- repository import checks find no old paths.

## Behavioral acceptance checks

The behavior is complete only when:

- adding a stakeholder during deal creation and adding one later converge after
  commit;
- one user action creates at most one visible deal job;
- the job processes all active stakeholders on the deal;
- historical emails and meetings are considered without a date window;
- eligible additional communication participants can become stakeholders;
- email and meeting associations are durable and retry-safe;
- a deal activity card never acts as matching evidence;
- job success is not emitted before hydration commits;
- rerunning the job is idempotent.

## Deployment and rollback

The file move does not change Celery task names, job types, API routes, database
tables, or SSE contracts. API and worker images may therefore roll using the
existing task-name contract.

Deploy producers and deal workers from the same commit when possible. Before
deployment:

- confirm worker task registration contains
  `stakeholder.sync_deal_stakeholders` and
  `deal.hydrate_contact_communications`;
- search active producers for retired mapper task names;
- run the focused service/task/router suites;
- inspect one staging job end to end, including database associations and
  activity links.

Rollback is a code rollback. Newly discovered participants and valid
communication associations are durable business data and should not be deleted
automatically during rollback.

## Explicit non-goals

- no generic event bus;
- no new worker or queue;
- no new job table;
- no new frontend stream;
- no rolling communication window;
- no restoration of split email/calendar mapper tasks;
- no refactor of deal summaries, risks, notes, assets or meeting insight
  services in this phase;
- no compatibility wrappers for old Python import paths.

Those domains can adopt the same ownership rule later, one bounded service group
at a time.
