# Stakeholder add: task-level execution flow

Status: verified current implementation as of 2026-07-26.

This document describes what happens after a stakeholder is manually added to a
deal through the stakeholder API or an agent tool. It distinguishes synchronous
database work, Celery task dependencies, parallel branches, conditional work,
and user-visible completion boundaries.

It supersedes older task diagrams that show separate calendar and email matcher
tasks. Current matching is performed inside
`deal.hydrate_contact_communications`.

## Short answer

Contact research does **not** finish before stakeholder sync starts.

After the stakeholder transaction commits, the normal successful path publishes
two independent branches:

1. stakeholder normalization, followed by targeted communication hydration;
2. contact research.

The code publishes the stakeholder-processing job before the initial research
job, but Celery workers may execute both at the same time. Stakeholder sync does
not wait for research, and research completion does not trigger stakeholder
sync.

```text
stakeholder committed
        |
        +-------------------------------+
        |                               |
        v                               v
stakeholder sync                 contact research
        |                               |
        v                               v
targeted hydration               provider lookup
        |                               |
        |                         AI research summary
        |                               |
        |                         contact activity update
        |
        +--> discovered-contact research
        +--> attachment extraction --> post-upload
        +--> deal insight pipeline
```

The three hydration follow-up branches are published only after hydration
commits its contact, activity, stakeholder, and deal-association writes.

## Task graph

Legend:

- `A -> B`: B is published only after A reaches the required boundary.
- `A + B`: the tasks are independent and may run in parallel.
- `[condition]`: the task is not always created.

```text
API or agent stakeholder add
  -> synchronous stakeholder/contact/participant transaction
  -> commit
  -> publish stakeholder.sync_deal_stakeholders
  +  publish contact_research.process_job [eligible linked contact]

stakeholder.sync_deal_stakeholders
  -> verify deal mapping eligibility
  -> normalize existing participant-backed stakeholders
  -> commit normalization
  -> publish contact_research.process_job [missing research; deduplicated]
  -> publish deal.hydrate_contact_communications [rematching required]

deal.hydrate_contact_communications
  -> load approved stakeholder contacts
  -> load retained email and calendar history
  -> resolve communication participants into CRM contacts
  -> project contact and company activities
  -> associate qualifying emails and meetings with the deal
  -> discover same-domain additional stakeholders
  -> hydrate newly approved stakeholders once
  -> reconcile stale deal associations
  -> create/reuse deal-memory job rows for email-associated deals
  -> commit
  -> publish contact_research.process_job [newly discovered contacts]
  +  publish deal_asset.extract_email_attachments_deal [matched attachment emails]
  +  publish deal insight pipeline [at least one qualifying email]
  -> mark the visible stakeholder/hydration job complete

deal_asset.extract_email_attachments_deal
  -> download and store qualifying files
  -> commit PROCESSING DealAsset rows
  -> publish deal_asset.post_upload once per asset
  -> publish bounded extraction continuation [limit reached]

deal insight pipeline
  -> deal_memory.process_job
  -> group(
       risk_blocker.extract_deal_risks_blockers_task,
       stakeholder.analyze_deal_stakeholders_task
     )
  -> deal_tasks.sync_from_summary [meeting-origin pipeline only]
  -> deal_update.process_job [feature flag enabled]
```

## Phase 0: synchronous stakeholder creation

Primary API entry point:

```text
POST /{deal_id}/stakeholders
```

Primary agent entry points:

- `add_stakeholder`
- `create_stakeholder_if_missing`

`StakeholderWriteService.create_stakeholder_with_outcome` performs the
synchronous write:

1. Verify that the caller can control the deal.
2. Normalize email, name, and LinkedIn URL.
3. Resolve an existing contact by contact ID, email, or LinkedIn URL.
4. Reject contradictory identity fields.
5. For a LinkedIn-only stakeholder with no existing contact, resolve identity
   through the contact identity provider before the stakeholder can be created.
6. Reject internal-domain people.
7. Find or create the external CRM contact when business rules allow it.
8. Create, reactivate, or reuse the canonical `CRMDealParticipant`.
9. Mark the participant approved.
10. Set the first stakeholder as primary contact when applicable.
11. Upsert the stakeholder-added activity.
12. Commit the transaction.

The LinkedIn-only identity lookup in step 5 is synchronous identity resolution,
not the later `contact_research.process_job` Celery task.

If the stakeholder was merely reused without a membership change, the API does
not create a new stakeholder-processing job. The initial research dispatcher
may still return an existing active job or skip because research is current.

## Phase 1: post-commit dispatch

After a newly created or reactivated stakeholder commits, the entry point
normally does both of the following. The stakeholder-processing branch requires
the resulting stakeholder to have an email identity; a name-only or unresolved
LinkedIn-only participant cannot anchor communication replay.

### Branch A: visible stakeholder processing

`DealJobService.create_stakeholder_processing_jobs`:

1. Inserts one `STAKEHOLDER_SYNC` `DealJobs` row.
2. Commits the job before publishing so a fast worker can load it.
3. Publishes `stakeholder.sync_deal_stakeholders`.
4. Records whether the broker handoff was confirmed.

The task payload uses:

```text
change_type=MANUAL_REFRESH
trigger_rematching=true
emit_events=true
```

The durable metadata allows an unconfirmed broker handoff to be republished
at least once when active stakeholder jobs are read.

### Branch B: contact research

`dispatch_contact_research_for_contact` publishes
`contact_research.process_job` only when:

- the stakeholder is linked to an active CRM contact;
- the contact has a safe external email, LinkedIn URL, or cached provider data;
- current research is not already fresh, unless force refresh was requested;
- an equivalent active research job does not already exist.

The research dispatcher locks the contact during its deduplication check.
Repeated dispatches from stakeholder creation, stakeholder normalization, or
communication discovery therefore normally reuse or skip the same work.

### Parallelism

The API calls these dispatchers in order, but it does not construct a Celery
chain between them:

```text
publish stakeholder sync
publish contact research
return response
```

Once published, research and stakeholder processing run independently.

## Phase 2: `stakeholder.sync_deal_stakeholders`

Task configuration:

```text
queue: deal
acks_late: true
max retries: 3
soft limit: 300 seconds
hard limit: 360 seconds
```

### Eligibility gate

The task stops before normalization and hydration unless the deal:

- exists in the organisation;
- is open and not deleted;
- has an owner or active internal user participant;
- has at least one of those internal emails represented by a user row in the
  master user table.

The current master-user lookup checks organisation and normalized email only; it
does not add an active-status predicate.

### Normalization transaction

`StakeholderSyncService.sync_for_deal` does not search email or calendar
history. It:

1. Locks and loads the deal with existing participants.
2. Processes active `CONTACT` participants only.
3. Normalizes participant email addresses.
4. Rejects internal-domain contacts.
5. Ignores rejected or removed participants.
6. Links participants to existing CRM contacts by email.
7. Records whether the approved participant state changed.
8. Returns linked contacts that still need contact-owned research.
9. Commits the normalized participant state.

### Downstream publication

After normalization:

1. Missing-research contacts are offered to the research dispatcher.
2. If rematching is required, the task publishes one deal-scoped
   `deal.hydrate_contact_communications` payload.

For manual refresh, participant added/updated/removed, initial backfill, and
communication-remap changes, hydration is requested even when normalization
produces no row change. This is necessary because a participant removal can
invalidate an existing communication association.

Research publication occurs before hydration publication in the task body, but
the hydration task does not depend on research completion.

If this is a client-visible stakeholder job and hydration was published, the
stakeholder task deliberately leaves the job open. Hydration becomes the owner
of its terminal visible state.

## Phase 3: `deal.hydrate_contact_communications`

Task configuration:

```text
queue: deal
acks_late: true
max retries: 2
soft limit: 600 seconds
hard limit: 660 seconds
deal payload bound: 100 IDs
```

For a stakeholder mutation, hydration runs in deal-replay mode:

```text
backfill_deals([deal_id])
```

Inside one bounded hydration task:

1. Load approved, active contact participants for the requested deal.
2. Page through retained local email and calendar history for those contacts.
3. Resolve all communication participants into CRM contacts.
4. Upsert replay-safe email/meeting activity projections with contact and
   company links.
5. Match each communication against current deal eligibility:
   - at least one approved customer contact must participate;
   - the deal owner or another current internal participant must participate.
6. Create or reuse email/meeting-to-deal associations.
7. Collect additional same-customer-domain contacts from confident matches.
8. Auto-approve at most five communication-discovered stakeholders; retain
   additional candidates as pending.
9. Hydrate newly approved discovered contacts exactly once with discovery
   disabled, preventing stakeholder-of-stakeholder recursion.
10. Reconcile stale associations against the complete replay result.
11. Create or reuse a `DEAL_MEMORY_BUILD` job for every deal with a qualifying
    email association.
12. Commit all CRM writes.

Hydration reads already committed local email and calendar rows. It does not
fetch new mailbox or calendar history from Google or Microsoft.

### No-history behavior

Hydration still runs and completes when no retained emails or meetings are
found. In that case:

- no communication associations are created;
- no additional stakeholders are discovered;
- no attachment task is published;
- no email-triggered deal insight pipeline is published.

The independent contact-research branch may still be running or completed.

## Phase 4: post-hydration parallel fan-out

Only after the hydration transaction commits does the worker publish follow-up
work.

The publisher loops in a deterministic order, but the published Celery work is
not one parent chain. Once accepted by the broker, these branches can run in
parallel.

### Branch C: discovered-contact research

Every newly discovered approved contact is offered to
`contact_research.process_job`.

This uses the same contact-level lock, freshness checks, and active-job
deduplication as initial stakeholder research.

### Branch D: email attachment extraction

For each confidently email-associated deal, exact attachment-bearing email IDs
are chunked in batches of at most 100 and published to:

```text
deal_asset.extract_email_attachments_deal
```

The task:

1. Loads only the requested active deal-associated canonical emails.
2. Resolves a usable mailbox copy.
3. Lists and filters provider attachments.
4. Downloads supported attachments.
5. Computes content checksum and deduplicates.
6. Uploads the file to organisation blob storage.
7. Commits a `PROCESSING` `DealAsset`.
8. Publishes `deal_asset.post_upload` for each committed asset.
9. Publishes bounded continuation tasks if the per-run limit is reached.

`deal_asset.post_upload` computes or verifies the checksum, creates supported
thumbnails, commits `ACTIVE` or `FAILED`, and publishes the committed asset
status event.

Attachment work is conditional. A matched meeting alone does not produce an
email attachment task.

### Branch E: deal insight pipeline

For each deal with at least one qualifying email, hydration publishes a Celery
canvas:

```text
deal_memory.process_job
          |
          v
   +------+------+
   |             |
   v             v
risk/blocker   stakeholder
extraction     analysis
   |             |
   +------+------+
          |
          v
deal task sync [only when the pipeline has a source meeting event]
          |
          v
deal update [ENABLE_DEAL_UPDATE_AGENT_PIPELINE=true]
```

For the stakeholder-add/email-association path, no source meeting event is
supplied. Therefore `deal_tasks.sync_from_summary` is normally absent from this
specific pipeline. It is included when the same pipeline is started from a
meeting-summary path with a source event.

Risk/blocker extraction and stakeholder analysis wait for Deal Memory, then run
in parallel. The optional deal update waits for the insight group.

## Contact research internals

`contact_research.process_job` has its own lifecycle:

```text
PENDING
  -> IN_PROGRESS
  -> provider lookup or cached provider profile
  -> AI summary generation [usable provider profile]
  -> persist contact research
  -> write contact-enrichment completion activity
  -> COMPLETED
```

It retries retryable failures twice with exponential delay. It is contact-owned,
not deal-owned.

Research completion does not:

- trigger stakeholder sync;
- trigger communication hydration;
- block attachment extraction;
- block Deal Memory or stakeholder analysis.

Stakeholder reads resolve profile and research fields from the linked contact,
so a separate participant rewrite is not required after research completes.

## Completion boundaries

There are several different meanings of "finished":

| Boundary | What is guaranteed | What may still run |
|---|---|---|
| Stakeholder API response | Stakeholder transaction committed; on a successful response, stakeholder processing was published when eligible and research dispatch was attempted | Sync, research, hydration, attachments, insights |
| `STAKEHOLDER_SYNC` visible job completed | Normalization and hydration committed; attachment dispatch succeeded, while research and insight dispatch were attempted independently | Contact research, attachment extraction/post-upload, deal insight pipeline |
| Contact research job completed | Contact profile/research and completion activity persisted | Hydration, assets, deal insights |
| Attachment extraction completed | Assets were discovered/stored and post-upload was published | Asset post-upload, insights |
| `deal_asset.post_upload` completed | One asset reached a terminal processed/invalid state and its event was published | Other assets and insights |
| Deal insight canvas completed | Deal Memory, parallel insights, and configured later stages completed | Independent research/assets |

Consequently, a full briefing room and populated email/activity views do not
prove that research, asset post-upload, or the insight canvas has finished.

## Failure isolation and retries

- The stakeholder/contact/participant transaction commits before task
  publication.
- Stakeholder normalization and hydration are replay-safe and can be retried.
- Research dispatch failure is logged without rolling back normalized
  stakeholders.
- The visible stakeholder job remains open across the stakeholder-to-hydration
  handoff.
- Hydration commits before publishing downstream research, attachments, and
  insights.
- Attachment publication failure raises back into hydration so hydration can
  retry its idempotent work.
- Insight publication failures are logged independently and do not roll back
  committed communication associations.
- Contact research uses a contact lock plus active-job and freshness
  deduplication.
- Asset extraction and post-upload use stable identities and retry-safe state
  transitions.

## Independent two-hour backfill

The scheduled backfill is a safety net, not part of the immediate manual-add
chain:

```text
03 minutes past every second hour
  -> stakeholder.sync_all_deals_stakeholders_all_orgs
  -> one stakeholder.sync_all_deals_stakeholders_org per approved org
  -> claim at most 100 eligible unfinished deals per org
  -> one stakeholder.sync_deal_stakeholders per claimed deal
  -> normal targeted hydration and follow-ups
```

The claimer considers only open, non-deleted deals whose owner/internal
participant email maps to a user row in the master user table. The current
eligibility query does not additionally filter that user row by an active-status
column. Completed initial-backfill deals are skipped; active jobs are not
claimed again; stale jobs can be replaced.

## Source map

| Responsibility | Current source |
|---|---|
| API stakeholder add | `src/api/routers/crm/stakeholders_handler.py` |
| Agent stakeholder add | `src/agents/tools/crm/add_stakeholder.py` |
| Canonical participant/contact write | `src/service/crm/participants/stakeholder_write_service.py` |
| Visible stakeholder job creation | `src/service/crm/jobs/deal_job_service.py` |
| Contact research dispatch/deduplication | `src/workers/deal_worker/utils/contact_research_dispatch.py` |
| Contact research task | `src/workers/deal_worker/tasks/deal_insight_tasks/contact_research_tasks.py` |
| Stakeholder normalization task | `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py` |
| Stakeholder normalization service | `src/service/crm/participants/stakeholder_sync_service.py` |
| Hydration dispatch | `src/workers/deal_worker/utils/contact_communication_hydration_dispatch.py` |
| Hydration task and post-commit fan-out | `src/workers/deal_worker/tasks/deal_mappers/contact_communication_hydration_tasks.py` |
| Hydration business flow | `src/service/crm/communications/hydration_service.py` |
| Deal association and discovery evidence | `src/service/crm/communications/deal_association_service.py` |
| Additional stakeholder writer | `src/service/crm/participants/stakeholder_write_service.py` |
| Attachment extraction | `src/workers/deal_worker/tasks/deal_asset_email_attachment_tasks.py` |
| Attachment post-upload | `src/workers/deal_worker/tasks/deal_asset_post_upload_task.py` |
| Deal insight canvas | `src/workers/deal_worker/utils/pipeline_dispatch.py` |
| Scheduled backfill claim | `src/workers/deal_worker/services/deal_mapping_backfill_claim_service.py` |
