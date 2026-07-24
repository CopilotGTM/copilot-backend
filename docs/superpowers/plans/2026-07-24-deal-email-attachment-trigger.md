# Production Design: Deal Email Attachment Extraction

## Decision

Deal association remains inside communication hydration, but attachment extraction is
scoped to the exact email rows that caused the current association work.

The automatic path must not run a historical deal-wide scan whenever one new email arrives.
The existing broad scan remains available only for explicit repair and historical backfill.

No new mapper, service, queue, table, dependency, or scheduled organisation scan is
required.

---

## Why the current implementation is not final

The current trigger placement is correct, but the execution unit is too broad:

1. Changed-source hydration loads the complete retained provider thread.
2. Any attachment-bearing email in that thread can add its deal to
   `attachment_deal_ids`, including an old context email.
3. The per-deal task receives only `organisation_id` and `deal_id`.
4. `DealAssetEmailAttachmentService.process_deal()` consequently selects every
   attachment-bearing email currently associated with the deal.
5. Deduplication prevents duplicate `DealAsset` rows, but it happens only after the worker
   calls Gmail or Microsoft Graph to list attachment metadata.

This is database-idempotent but operationally wasteful. A new reply can cause provider calls
for old emails that have already been processed.

The current implementation also has two adjacent reliability gaps:

- `MAX_ATTACHMENTS_PER_DEAL` is an execution guard, not a persisted deal limit. Reaching it
  currently stops the loop without proving that all scoped work was completed.
- A failure to publish `deal_asset.post_upload` leaves the inserted asset in `PROCESSING`.
  Retrying extraction then hits the asset uniqueness constraint and does not redispatch
  post-upload work.

Production completion requires fixing these execution semantics, not only the trigger.

### Current implementation versus production target

| Concern | Current worktree | Production target |
|---|---|---|
| Trigger location | After association commit | Keep |
| Attachment flag gate | Deal-level `attachment_deal_ids` | Exact email UUIDs grouped by deal |
| Thread context | Can widen attachment trigger | Used for matching only |
| Task payload | Organisation + deal | Organisation + deal + bounded email UUIDs |
| Automatic provider query | Every attachment email on deal | Exact current canonical messages |
| Broad historical scan | Happens on normal trigger | Explicit repair/backfill only |
| Cross-mailbox identity | Provider-local attachment ID | Canonical message + filename + content SHA |
| Database insert | Conflict-safe | Keep, return winning asset identity/status |
| Execution cap | Silently stops | Scoped continuation, including mid-email |
| Extraction errors | Retry and end as Celery failure | Keep |
| Publish failure | Hydration retries visibly | Keep |
| Post-upload publish failure | Asset may remain `PROCESSING` | Status-aware redispatch and true task failure |

---

## Definition of "new email"

For this flow, "new" means **an email UUID explicitly included in the current hydration
input**, not every email loaded as relationship context and not necessarily a recent provider
timestamp.

This definition works for all entry points:

- **Incremental sync:** the changed email UUIDs returned by the committed provider sync.
- **Initial sync/migration:** the bounded email UUID batch currently being hydrated. These
  records may be historically dated but are new to this processing batch.
- **Deal/contact replay:** the exact historical email UUIDs selected by that explicit replay.
- **Participant expansion:** the exact emails selected for the newly discovered contact
  expansion.

Thread context may still be used to create contacts, understand engagement, and associate
activities. It must not silently widen the attachment extraction scope.

`changed_email_ids` currently includes both created and updated rows. That is acceptable:
an updated provider row may be the first version where `has_attachments` or usable provider
metadata is available. Database/source deduplication makes replay safe.

---

## Where the attachment task is triggered

There is one automatic producer boundary:

```text
dispatch_contact_communication_hydration_async(...)
  -> deal.hydrate_contact_communications
  -> ContactCommunicationHydrationService
  -> commit contact/activity/deal-association writes
  -> _dispatch_association_followups(...)
  -> deal_asset.extract_email_attachments_deal
```

The association service does not publish Celery work. It returns exact attachment email UUIDs
grouped by deal. The hydration worker is the only automatic **business-event entry producer**
for per-deal attachment extraction, and it publishes only after the organisation transaction
commits. Once extraction has started, the attachment task may publish a scoped continuation
of itself when it reaches the execution bound; that continuation cannot widen the original
email scope.

Concrete code ownership:

- Public hydration dispatcher:
  `src/workers/deal_worker/utils/contact_communication_hydration_dispatch.py`
- Hydration Celery task and post-commit attachment producer:
  `src/workers/deal_worker/tasks/deal_mappers/contact_communication_hydration_tasks.py`
- Per-deal attachment consumer:
  `src/workers/deal_worker/tasks/deal_asset_email_attachment_tasks.py`

### Existing events that enqueue hydration

| Business event | Current caller | Hydration input | Attachment scope |
|---|---|---|---|
| Gmail/Outlook sync succeeds | `src/workers/integrations_worker/tasks/email/integration_task.py` | `changed_email_ids`, trigger `email_sync` | Only those changed email UUIDs |
| Legacy incremental email sync succeeds | `src/workers/integrations_worker/tasks/deprecated/email/org_task.py` | `changed_email_ids`, trigger `deprecated_email_incremental_sync` | Only those changed email UUIDs |
| Calendar sync succeeds | `src/workers/integrations_worker/tasks/calendar/integration_task.py` | `changed_event_ids`, trigger `calendar_sync` | Empty unless event-driven participant expansion selects exact emails |
| Legacy calendar sync succeeds | `src/workers/integrations_worker/tasks/deprecated/calendar/org_task.py` | `changed_event_ids`, trigger `deprecated_calendar_incremental_sync` | Empty unless event-driven participant expansion selects exact emails |
| Deal created with contacts | `src/api/routers/crm/deals_handler.py` | `deal_ids`, trigger `deal_created` | Exact emails selected by that deal replay |
| Deal primary contact changes | `src/api/routers/crm/deals_handler.py` | `deal_ids`, trigger `deal_primary_contact_changed` | Exact emails selected by that deal replay |
| Deal participants change | `src/workers/deal_worker/tasks/deal_mappers/stakeholder_tasks.py` | `deal_ids`, trigger `deal_participants_changed` | Exact emails selected after participant sync |
| CRM import commits deals | `src/workers/deal_worker/tasks/crm_import_tasks.py` | `deal_ids`, trigger `crm_import_deals_committed` | Exact emails selected for imported deals |
| Lead converts to a deal | `src/api/routers/crm/leads/leads_handler.py` | `deal_ids`, trigger `lead_conversion_committed` | Exact emails selected for the converted deal |
| User manually creates a contact | `src/api/routers/crm/contacts_handler.py` | `contact_ids`, trigger `manual_contact_create` | Exact emails selected for that contact replay |
| CRM agent creates a contact | `src/agents/tools/crm/create_contact.py` | `contact_ids`, trigger `agent_contact_create` | Exact emails selected for that contact replay |

For every row above, hydration may create email/deal associations without publishing
attachment extraction. It publishes only when an exact in-scope email:

1. resolves to an eligible deal;
2. has canonical `has_attachments = true`; and
3. survives organisation, active-association, and replay-target checks.

### Primary live-email sequence

The normal new-email path is:

```text
Provider webhook/manual refresh/initial sync
  -> integrations.sync_email_integration
  -> EmailDeltaSyncService commits email rows
  -> integration_task passes changed_email_ids to hydration dispatcher
  -> deal.hydrate_contact_communications
  -> association result returns {deal_id: exact changed email UUIDs}
  -> hydration commits
  -> deal_asset.extract_email_attachments_deal(
       organisation_id,
       deal_id,
       email_ids,
     )
```

This is the path that ensures an old email loaded only as thread context does not trigger
provider work.

### Repair triggers

Repair is separate from automatic email processing:

```text
Association repair:
  deal.hydrate_contact_communications(deal_ids/contact_ids)
    -> recreate/reactivate missing communication associations
    -> scoped attachment extraction

Asset-only repair after associations exist:
  deal_asset.extract_email_attachments_all_orgs
  -> deal_asset.extract_email_attachments_org
  -> broad organisation/deal scan
```

The attachment repair tasks can recover missing assets only when an active
`DealEmailAssociation` already exists. They cannot recover a deal/contact mutation whose
hydration publication was lost, because there is no association for them to discover. That
case requires targeted communication-hydration replay for the affected deal/contact, followed
by normal scoped extraction.

These repair paths are invoked deliberately by an operator or rollout/migration command. They
are not called by each email and are not silently scheduled as a cron job.

### Trigger delivery guarantees

- Email/calendar sync, stakeholder sync, and CRM import run inside retryable Celery tasks,
  so a hydration publication exception is visible to the parent task.
- Parent retry is not a delivery guarantee after commit. Email/calendar retries may advance
  provider cursors and return no changed IDs; CRM-import retries may advance to the next
  durable batch. Without retaining the exact committed IDs, the retry may not reconstruct the
  lost hydration payload.
- Deal/contact/lead API routes and the CRM agent currently commit first, then treat hydration
  publication as best-effort and log an exhausted failure so the successful user mutation is
  not turned into an HTTP error.
- `_dispatch_association_followups` uses bounded broker retries and then fails the hydration
  task, which safely replays because association writes already committed and are idempotent.

The reliable boundary begins after the hydration task has been accepted by the broker.
Before that point, every post-commit producer is bounded best-effort unless it durably retains
the exact replay IDs.

Without a transactional outbox or a durable hydration-replay record, a process crash after
commit but before hydration publication cannot be recovered automatically. Targeted
communication hydration is the operational recovery; broad attachment repair applies only
after associations exist. If guaranteed delivery for every producer becomes mandatory, a
durable outbox/replay mechanism is required and is a separate infrastructure decision.

---

## End-to-end flow

```text
Committed email sync or explicit replay
  -> hydrate the exact email UUID batch
  -> load thread context for contact/activity/deal reasoning
  -> confidently associate messages with eligible deals
  -> for attachment signaling, keep only exact in-scope email UUIDs
  -> group those UUIDs by deal
  -> commit contacts, activities, associations, and memory jobs
  -> publish the existing per-deal task with:
       organisation_id
       deal_id
       email_ids
  -> worker canonicalizes scoped UUIDs to internet_message_id
  -> worker loads every usable active-provider copy per canonical message
  -> worker downloads through the first successful copy
  -> worker lists, filters, downloads, uploads, and inserts assets
  -> worker guarantees post-upload work is either published or visibly failed
```

The association transaction never waits on the broker, Gmail, Microsoft Graph, Azure Blob
Storage, checksum generation, or thumbnail generation.

---

## Component contracts

### 1. Association result

Replace the deal-only attachment signal with an exact mapping:

```python
attachment_email_ids_by_deal: dict[UUID, set[UUID]]
```

Rules:

- Add an email UUID only after the message has a valid deal association.
- The email must pass the current association target restrictions.
- The email must be inside the caller-provided attachment scope.
- `has_attachments` must be boolean `True` or canonical string `"true"`, matching the
  string-backed database predicate.
- Ambiguous, unmatched, target-mismatched, and associations that remain inactive emit
  nothing.
- A previously removed/cancelled association that is validly reactivated by the current
  matching rules does emit attachment work.
- Multiple local copies of one provider message are allowed in the result; the extraction
  query canonicalizes them by `internet_message_id`.
- `email_deal_ids` remains unchanged for memory/insight dispatch.

The association method receives an explicit required scope:

```python
attachment_scope_email_ids: set[UUID]
```

Callers must never infer this scope from the complete `emails` argument because that list may
contain thread context.

### 2. Hydration result

Propagate the exact mapping through `ContactCommunicationHydrationResult`.

Changed-source hydration passes only the originally loaded changed email UUIDs as the
attachment scope. Backfill/expansion calls pass the exact email rows selected by that replay.

The mapping remains internal worker orchestration state. The public metrics payload reports
counts, not potentially large UUID collections.

### 3. Worker dispatch

The existing task stays:

```text
deal_asset.extract_email_attachments_deal
```

Its automatic payload becomes:

```python
{
    "organisation_id": str,
    "deal_id": str,
    "email_ids": list[str],
}
```

Dispatch rules:

- Group and deduplicate UUIDs by deal.
- Sort IDs for deterministic tests/logging.
- Chunk large replay sets at `100` email UUIDs per task. A normal changed-source hydration
  batch is already capped at 100.
- Never publish with an empty list.
- Use the existing bounded Kombu publisher retry policy.
- If publishing still fails, attempt independent insight dispatches, then re-raise so the
  existing hydration retry safely republishes after the already-committed association.
- Do not log raw email addresses, subjects, provider message IDs, or attachment names.

Research and deal insights remain independent. Attachment failure must be visible, but it
must not prevent sibling follow-up attempts.

### 4. Per-deal task input

Extend the existing task without renaming or rerouting it:

```python
def extract_email_attachments_deal_task(
    self: Task,
    organisation_id: str,
    deal_id: str,
    email_ids: list[str] | None = None,
) -> dict[str, Any]:
```

Semantics:

- A non-empty `email_ids` list is the normal automatic path.
- `None` explicitly means the legacy broad deal scan and is reserved for manual repair or
  deliberate historical backfill.
- An empty list is invalid and raises `ValueError`; it must never accidentally mean
  "scan the whole deal."
- UUID parsing and the maximum payload count are validated at the task boundary.
- `errors > 0` enters the existing exponential Celery retry path.
- Exhausted retries raise so Celery records `FAILURE`, not `SUCCESS` with a failed-looking
  return value.

### 5. Extraction service query

Extend the existing service:

```python
async def process_deal(
    self,
    deal_id: UUID,
    email_ids: list[UUID] | None = None,
) -> dict[str, Any]:
```

For scoped execution:

1. Select seed `Email` rows by organisation and `email_id`.
2. Require `is_deleted = false` and canonical `has_attachments = "true"`.
3. Extract their non-null `internet_message_id` values.
4. Join active `DealEmailAssociation` rows for the requested deal and organisation.
5. Select every non-deleted local email copy with an active supported integration for each
   canonical message.
6. Order copies deterministically, independent of which mailbox UUID happened to enter the
   scope. Prefer the integration recorded by an existing legacy asset, then stable
   integration/email UUID order.
7. Attempt copies in that order and stop after the first copy that completes provider
   processing without a retryable access/download error. If one token, message, metadata, or
   binary-download path fails, continue the canonical message through the next connected
   mailbox. Report a retryable extraction error only when every active copy fails.

The broad `email_ids=None` path keeps the existing active-association scan for repair.

All tenant predicates remain in SQL even though an organisation-scoped session is used. Task
payloads are an external trust boundary.

No new database index is required:

- `emails.email_id` is the primary key.
- `(organisation_id, internet_message_id)` is indexed.
- deal email associations are indexed by deal, organisation/status, and
  `internet_message_id`.

### 5a. Multi-mailbox access rule

An attachment belongs to the canonical email/deal relationship, not to the integration used
to fetch it.

For example, if three organisation users have connected mailboxes and all three contain the
same RFC message:

- there is one `DealEmailAssociation` for the deal and `internet_message_id`;
- there may be three local `Email` rows and three provider-local message/attachment IDs;
- the worker builds one ordered access-candidate list;
- it uses the first mailbox copy that can actually list/download the attachment;
- it stops after success, so three integrations do not cause three downloads or three assets;
- if the first copy returns an unusable token, access denial, or missing message, the next
  active copy is attempted;
- no rule requires the selected integration to belong to the deal owner, sender, recipient,
  or the user who initiated hydration. Organisation tenancy and possession of the canonical
  message are the access boundary.

Provider identifiers remain integration-specific implementation details. They are retained
as provenance, while the content-stable identity prevents the chosen mailbox from changing
the logical asset.

If a provider copy fails after some attachments were already created, the next copy may
re-read those files while completing the message. Content identity and conflict-safe inserts
still prevent duplicate assets. This extra provider work exists only on the failure path; the
normal three-integration case performs one successful provider pass.

The first implementation may walk all active copies because the normal cardinality is small
and this directly satisfies the access rule. If organisation-wide copies make this scan a
measured bottleneck, add an integration-health preference/cap with retry rotation; do not add
that complexity speculatively.

### 6. Provider and storage execution

Retain the fresh-session phases:

1. Read candidate rows and release the database session.
2. Call provider APIs and Azure Blob Storage without holding a database connection.
3. Insert/finalize database state in a fresh short transaction.

Normal scoped execution therefore calls providers only for the current email batch and stops
after one mailbox copy succeeds. A retry may list the same scoped message again; this is
intentional and bounded.

Existing policies remain:

- Gmail and Microsoft providers only.
- Provider junk filtering.
- Existing MIME allowlist.
- Existing 25 MB per-attachment download limit.
- Organisation-scoped blob storage.
- Deterministic organisation/deal/source storage paths; new assets use the content-stable
  source identity described below.

Unsupported, junk, and oversized attachments are policy skips, not retryable task errors.
Provider, blob, and database failures are retryable errors.

### 7. Idempotency

Provider attachment IDs are mailbox-local. The same RFC message stored in two Gmail/Outlook
mailboxes may expose different attachment IDs, so provider attachment ID cannot be the
authoritative cross-mailbox identity.

For newly created assets, derive the logical source identity after download from:

```text
deal
  + canonical internet_message_id
  + normalized filename
  + attachment content SHA-256
```

The provider attachment ID remains provenance in `asset_metadata`, not the dedup authority.
Two mailbox copies of the same message/file therefore produce the same `source_record_id`.
The deterministic blob path for new assets uses the same stable identity.
Persist the already-computed content SHA-256 in `DealAsset.checksum`; post-upload processing
may reuse it instead of reading the blob solely to recompute the same checksum.

The existing unique constraint `(deal_id, source_type, source_record_id, filename)` protects
concurrent inserts because the new source ID and normalized filename are stable across
provider copies. No schema migration is required.

Backward compatibility for assets created with the legacy provider-ID source key is explicit:

- Before download, recognize a legacy source ID from the same provider copy.
- After download, recognize an existing active asset by canonical message metadata,
  normalized filename, and stored checksum.
- Prefer the provider copy recorded in any existing legacy asset when that integration
  remains usable, regardless of asset status. This preserves its provider-local source ID.
- Existing `ACTIVE` legacy rows may have `checksum=None` because the current post-upload
  service can finalize after checksum failure. If cross-copy comparison is required, read and
  hash the existing deterministic blob once, persist that checksum, and compare it with the
  newly downloaded content.
- If the existing legacy blob cannot be read or verified, fail the scoped item visibly for
  retry/operator repair. Do not create a second asset from an unverifiable filename/size
  heuristic.
- Do not rewrite existing active asset IDs or blob paths during this change.

Blob upload occurs before the conflict-safe insert. Because the path is deterministic, a
retry overwrites the same source path instead of creating unbounded orphan paths.

### 8. Execution limit and continuation

The existing value `50` is an execution bound, not a permanent business limit.

The service must report:

```python
{
    "limit_reached": bool,
    "remaining_email_ids": list[str],
}
```

Rules:

- Stop at the attachment bound, including in the middle of an email.
- When stopped mid-email, include that same email UUID in `remaining_email_ids`; the
  continuation relists it and skips the source IDs already completed.
- Also return every scoped email not yet started.
- The task publishes one continuation using the same task name and the remaining UUIDs.
- Continuations use the same bounded publisher retry policy.
- A continuation publish failure fails the current task.
- Broad repair mode continues only when the processing loop actually stopped at the creation
  bound while it still had an unvisited attachment or email. Merely selecting historical,
  unsupported, or already-active assets is not proof of remaining work and cannot create a
  continuation.

This removes silent truncation while preserving a bounded worker runtime.

### 9. Post-upload completion

An inserted attachment is not complete until `deal_asset.post_upload` has been durably
accepted by the broker or the failure is visible.

Required behavior:

- Replace the current set-only existing-source lookup with a mapping containing
  `asset_id`, `status`, source identity, checksum, and storage path. Otherwise the early
  source-ID skip prevents recovery code from ever running.
- An existing `ACTIVE` asset is a successful idempotent no-op.
- An existing `PROCESSING`/`FAILED` asset with a usable blob returns its `asset_id` for
  post-upload recovery instead of being skipped.
- `_create_deal_asset` returns the identity/status of either the inserted row or the row that
  won the conflict.
- Post-upload publication uses bounded publisher retries.
- Publication exhaustion increments extraction errors and fails the attachment task.
- On extraction retry, the existing non-active asset is found and post-upload publication is
  attempted again without re-inserting it.
- `deal_asset.post_upload` treats an already `ACTIVE` asset as success.
- If its service reports failure, the task raises into its own configured Celery retry path;
  retry exhaustion is recorded as task `FAILURE`.
- New assets cannot become `ACTIVE` without a checksum. Extraction already persists the
  downloaded content hash; legacy assets missing one use the compatibility recovery above.

This does not provide transactional queue delivery. It does ensure a transient broker failure
is replayable from existing database state rather than silently stranding an asset.

---

## Recovery modes

The system has two deliberately different modes:

| Mode | Input | Provider scope | Purpose |
|---|---|---|---|
| Automatic | Exact `email_ids` | Current hydration batch only | Normal production path |
| Repair/backfill | `email_ids=None` | All active associated deal emails | Operator or migration recovery |

The all-organisations and organisation-level tasks remain recovery tools. They are not invoked
for every email and are not silently enabled as a cron schedule.

Before rollout, run the repair path once if existing production associations may have missing
assets. New automatic tasks then stay scoped.

---

## Observability

Use structured logs at these boundaries:

- `deal_attachment_dispatch_requested`
- `deal_attachment_dispatch_failed`
- `deal_attachment_extraction_started`
- `deal_attachment_extraction_completed`
- `deal_attachment_extraction_failed`
- `deal_attachment_continuation_dispatched`
- `deal_attachment_post_upload_dispatch_failed`

Include only:

- organisation ID
- deal ID
- task ID/correlation context
- scoped email count
- provider-message count
- attachments discovered
- assets created
- assets deduplicated
- policy skips
- errors
- continuation count

Do not add a second metrics framework. Existing Celery task state plus structured logs are the
initial production signal.

---

## Minimal implementation delta

Production behavior requires changes in these existing boundaries only:

| File | Required change |
|---|---|
| `communication_deal_association.py` | Return exact attachment email UUIDs grouped by deal and require an explicit attachment scope |
| `contact_communication_hydration.py` | Keep changed emails separate from thread context for attachment scope and propagate grouped UUIDs |
| `contact_communication_hydration_tasks.py` | Chunk/group scoped UUIDs and publish them after commit |
| `deal_asset_email_attachment_tasks.py` | Validate scoped payloads, call scoped service execution, continue bounded work, and preserve true failure state |
| `deal_asset_email_attachment_service.py` | Add the exact-email query, explicit remaining-work result, and replayable post-upload dispatch |
| `deal_asset_post_upload_task.py` | Treat real processing failure as retryable while accepting already-active assets as success |
| `deal_asset_post_upload_service.py` | Distinguish successful idempotent no-op from actual processing failure |

Everything else is tests and documentation. The provider download helpers, blob-storage
client, deal-asset model, migrations, worker routing, task names, queue topology, and
association tables remain unchanged.

This is intentionally more than the trigger-only diff, because the trigger-only diff cannot
prove current-email scoping, bounded completion, or asset finalization. It is still smaller
than introducing an outbox, a second service, or a new workflow engine.

---

## Implementation sequence

### Phase 1: Exact association signal

1. Add `attachment_email_ids_by_deal`.
2. Require `attachment_scope_email_ids`.
3. Prove an old attachment in the same thread does not emit a live signal.
4. Prove a changed attachment email does emit a signal for existing and newly created
   associations.
5. Prove backfill and participant expansion pass their exact selected email scope.

### Phase 2: Scoped task and query

1. Add optional `email_ids` to the existing task/service.
2. Validate and chunk task input.
3. Canonicalize scoped UUIDs through `internet_message_id`.
4. Enforce tenant, active association, non-deleted email, and active integration predicates.
5. Prove no provider method is called for an associated historical email outside the scope.
6. Preserve `email_ids=None` broad repair behavior.

### Phase 3: Bounded completion

1. Make the execution cap return explicit remaining work.
2. Publish scoped continuations rather than silently stopping.
3. Make post-upload publication replayable for an existing non-active asset.
4. Make post-upload task failures use real Celery retries.

### Phase 4: Rollout

1. Deploy the backward-compatible consumer/task signature first.
2. Deploy the exact-scope producer second.
3. Run one explicit historical repair if required.
4. Monitor failure state, continuation volume, provider errors, and assets stuck in
   `PROCESSING`.

---

## Required tests

### Association/hydration

- Changed attachment email maps to exactly one deal and is emitted.
- Old attachment email loaded only as thread context is not emitted.
- Updated in-scope email with attachments is emitted.
- No-attachment, ambiguous, unmatched, target-mismatched, and still-inactive emails are not
  emitted.
- A valid reactivation of a removed/cancelled association is emitted.
- Duplicate local copies collapse to one canonical provider message during extraction, and
  different mailbox-local attachment IDs resolve to one content-stable asset identity.
- Deal/contact replay propagates only its selected email scope.

### Dispatch/task

- One deal and up to 100 email UUIDs produce one scoped task.
- More than 100 UUIDs produce deterministic chunks.
- Empty scope produces no dispatch.
- Empty task payload is rejected rather than causing a broad scan.
- Publisher exhaustion attempts insights and then fails hydration for retry.
- Service errors retry and exhaust as true task failure.

### Extraction query/provider

- Scoped mode cannot select another organisation's email.
- Scoped mode cannot select a historical email outside the UUID scope.
- A scoped duplicate may use another active integration copy with the same
  `internet_message_id`.
- Three active mailbox copies produce one ordered candidate group and one successful
  attachment download.
- A copy-local token/access/message failure falls through to the next active integration.
- Failure of every active copy becomes one retryable message extraction error.
- Candidate selection has no deal-owner or initiating-user integration restriction.
- Deleted emails, inactive associations, inactive integrations, and no-attachment rows are
  excluded.
- Broad mode retains historical repair behavior.
- Provider clients are not called when SQL returns no scoped candidates.

### Idempotency/continuation

- A repeated scoped task creates no duplicate asset.
- A concurrent conflict returns the existing asset identity/status.
- A legacy active asset without a checksum is verified from its existing blob before
  cross-copy repair; verification failure does not create a duplicate.
- Reaching the execution limit mid-email republishes that email plus every unstarted email.
- Unsupported-only work does not create an infinite continuation.
- Post-upload publish failure causes extraction failure and is retried.
- Retrying an existing `PROCESSING` asset republishes post-upload.
- An already `ACTIVE` asset is a successful no-op.

---

## Acceptance criteria

The implementation is production-ready only when all are true:

- A new/current email with attachments is extracted after a valid deal association commits.
- A historical email outside the current scope is not queried from the provider during the
  automatic path.
- Thread context can influence matching without widening attachment work.
- Explicit replay/backfill processes its exact selected batch.
- Broad deal scans occur only through an explicit repair contract.
- All task payloads are tenant-scoped, bounded, validated, and retry-safe.
- Repeated delivery does not create duplicate database assets or unbounded blob paths.
- Execution limits never silently abandon scoped work.
- Attachment extraction and post-upload failures become visible Celery failures after
  retries.
- Research and deal insights remain behaviorally independent.
- No new mapper, service, queue, table, dependency, or cron scan is introduced.

---

## Non-goals

- Downloading attachments inside the association transaction.
- Treating thread context as automatic attachment scope.
- Extracting meeting, CRM note, chat, or manual-upload files.
- Changing provider authentication, MIME policy, or junk filtering.
- Moving or rewriting existing blob paths; only newly extracted assets use the stable path
  key.
- Deleting assets when an email association is later removed.
- Claiming exactly-once queue publication without an outbox.
- Adding a schema migration solely for this implementation.
