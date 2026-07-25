# Deal assets service restructure

## Status

Implemented on 2026-07-25. The target paths and runtime rules in this document
are present in this working tree.

Verification completed:

- stakeholder and communication cross-layer selection: 301 tests pass;
- asset, provider, task, API, schedule, and chart selection: 150 tests pass;
- focused Ruff and Python compilation checks pass;
- comment/docstring structural checks pass for the changed production flow;
- old production/test import searches return no matches;
- `git diff --check` and trailing-whitespace checks pass.

This plan owns service placement and dependency direction. The detailed
attachment matching, multi-mailbox, deduplication, continuation, and recovery
rules remain defined by:

- `docs/Technical Documentation/deal/email-attachment-assets.md`;
- `docs/superpowers/plans/2026-07-24-deal-email-attachment-trigger.md`.

Do not combine this restructure with unrelated deal worker cleanup.

## Decision

Move reusable deal-asset business logic out of worker packages and into one CRM
asset domain package. Keep Celery registration, scheduling, locks, retries,
queue publication, and task-level event publication in the deal worker.

The target boundary is:

```text
API router / hydration task / operator repair task
                     |
                     v
           src/service/crm/assets
                     |
                     +--> src/service/integrations/email
                     |
                     v
          domain models and infrastructure

Celery task names, queues, retries, and any scheduling ownership remain in
`src/workers`; this flow has no enabled periodic schedule.
```

This is the next bounded restructure because the only two asset pipeline
services are still under `src/workers/deal_worker/services`, while the API asset
service already lives under `src/service/crm`.

## Why a direct file move is insufficient

The current asset pipeline has three ownership problems:

1. `DealAssetEmailAttachmentService` is CRM asset business logic but lives
   below the deal worker.
2. It imports the attachment downloader and provider token helpers through
   `src.workers.integrations_worker`.
3. It publishes `deal_asset.post_upload` itself, so moving it unchanged would
   make the CRM service layer depend on a Celery worker.

`DealAssetPostUploadService` also publishes the asset SSE update internally.
That couples persistence and media processing to task delivery concerns.

The restructure is complete only when the moved CRM asset services and
attachment provider have no imports from `src.workers`.

## Business behavior that must remain unchanged

The move must preserve these rules:

- attachments are considered only after an active email-to-deal association
  exists;
- the low-latency path starts after hydration commits its associations;
- the unscheduled all-organisation path remains an explicit operator
  repair/backfill path;
- only Gmail/Google and Microsoft email integrations are eligible;
- only active organisation-owned integrations may provide attachment bytes;
- provider calls and Azure operations run without a database session held;
- junk, unsupported, too-small, and oversized files remain filtered;
- attachment identity is deal-scoped and retry-safe;
- one email copied into multiple connected mailboxes must not create duplicate
  deal assets;
- a newly created file asset remains `PROCESSING` until post-upload work
  completes;
- post-upload computes the checksum, generates supported thumbnails, and marks
  the asset `ACTIVE` only when a checksum is usable;
- the task publishes the existing asset status event after the service commit;
- a retry must not create another `DealAsset`, blob, activity, or frontend card;
- manual uploads and email-derived uploads use the same post-upload task;
- links do not enter the file post-upload pipeline.

No rolling time window or deal-created-date restriction is introduced.

## Target folder structure

```text
src/
├── service/
│   ├── crm/
│   │   └── assets/
│   │       ├── __init__.py
│   │       ├── deal_assets_service.py
│   │       ├── email_attachment_service.py
│   │       └── post_upload_service.py
│   │
│   └── integrations/
│       └── email/
│           ├── attachment_provider.py
│           ├── mailbox_identity_resolver.py
│           └── ... existing email integration services
│
└── workers/
    └── deal_worker/
        ├── tasks/
        │   ├── deal_asset_email_attachment_tasks.py
        │   ├── deal_asset_post_upload_task.py
        │   └── deal_mappers/
        │       └── contact_communication_hydration_tasks.py
        ├── client.py
        └── config.py
```

Do not add repositories, interfaces, factories, base services, a new worker, or
a new queue.

## Exact file moves

| Current file | Target file | Reason |
| --- | --- | --- |
| `src/service/crm/deal_assets_service.py` | `src/service/crm/assets/deal_assets_service.py` | Places the existing API-facing asset service inside its domain package without splitting or rewriting it. |
| `src/workers/deal_worker/services/deal_asset_email_attachment_service.py` | `src/service/crm/assets/email_attachment_service.py` | Email-to-asset extraction is reusable CRM asset business logic. |
| `src/workers/deal_worker/services/deal_asset_post_upload_service.py` | `src/service/crm/assets/post_upload_service.py` | Asset state transitions, checksum, and thumbnail processing belong to the asset domain. |
| `src/workers/integrations_worker/providers/email/attachment_downloader.py` | `src/service/integrations/email/attachment_provider.py` | Gmail/Graph attachment listing, filtering, and downloading are provider capabilities, not worker orchestration. |

Move the matching tests:

| Current test | Target test |
| --- | --- |
| `tests/service/crm/test_deal_assets_service.py` | `tests/service/crm/assets/test_deal_assets_service.py` |
| `tests/workers/deal_worker/services/test_deal_asset_email_attachment_service.py` | `tests/service/crm/assets/test_email_attachment_service.py` |
| New characterization tests for post-upload | `tests/service/crm/assets/test_post_upload_service.py` |
| `tests/workers/integrations_worker/providers/email/test_attachment_downloader.py` | `tests/service/integrations/email/test_attachment_provider.py` |

Task tests stay under `tests/workers/deal_worker/tasks`.

After all imports are updated, delete only the two moved asset service files
from `src/workers/deal_worker/services`. Do not delete that directory because
claim, cleanup, change-detection, transcript, and task-publication services still
remain there.

Do not leave compatibility import wrappers. All consumers are internal and can
move atomically.

## Service responsibilities

### `DealAssetsService`

Keep the existing API-facing responsibilities together for now:

- list and fetch visible assets;
- create a pending manual file upload;
- finalize a manual upload into `PROCESSING`;
- create link assets;
- update and soft-delete assets;
- generate read, write, and thumbnail SAS URLs;
- enforce organisation and deal access.

Splitting this class into read/write/storage services would add churn without
solving the current boundary problem.

It must not publish Celery tasks or SSE events. The API router commits first,
then publishes the existing post-upload task.

Finalization must be retry-safe at the task boundary: `PENDING_UPLOAD` moves to
`PROCESSING`, while an already `PROCESSING` asset is returned unchanged so a
client retry can republish work after a broker failure.

### `DealAssetEmailAttachmentService`

Own:

- selecting active deal email associations eligible for extraction;
- selecting deterministic active mailbox copies for a canonical email;
- applying attachment filtering and MIME policy;
- downloading through the integration attachment provider;
- uploading bytes to the deterministic Azure path;
- inserting retry-safe `DealAsset` rows;
- returning extraction statistics, remaining-work state, and asset IDs requiring
  post-upload.

It must not:

- import Celery, task objects, or `src.workers`;
- publish `deal_asset.post_upload`;
- publish SSE events;
- decide when hydration or an operator repair should run.

### `DealAssetPostUploadService`

Own:

- loading the asset's current state;
- treating an already active processed asset as an idempotent success;
- computing a missing checksum;
- generating optional thumbnails;
- committing the final `ACTIVE` or `FAILED` state;
- returning the committed event payload to the caller.

It must not publish the event itself. The worker task publishes the existing
`DealAssetEventUpdate` only after the service commit succeeds.

The result must distinguish:

- processed successfully;
- already processed;
- retryable failure;
- permanent invalid input, such as a missing asset or missing blob path.

This makes the task's existing retry declaration meaningful instead of reducing
all outcomes to an ambiguous Boolean.

The already-active result includes the current event data, allowing an event
publication retry without recomputing media or mutating the asset.

### `EmailAttachmentProvider`

Own only Gmail and Microsoft Graph protocol behavior:

- provider URLs and path escaping;
- attachment metadata parsing;
- payload decoding and size validation;
- junk attachment filtering;
- listing and downloading file attachments.

Use the existing
`src.service.integrations.providers.token_resolution.get_access_token` for
tokens. The provider module must not import integration worker internals.

### Deal worker tasks

Keep:

- the stable registered task names;
- UUID/string payload parsing;
- all-organisation coordination;
- approved-organisation lookup;
- organisation locks;
- time limits and retry policy;
- publishing post-upload tasks returned by the extraction service;
- publishing asset status events returned by the post-upload service;
- correlation headers, logs, and task result serialization.

Tasks should contain orchestration, not attachment matching or asset state
rules.

## Runtime flows

### New email or newly associated historical email

```text
email sync / stakeholder-triggered hydration
              |
              v
CommunicationHydrationService writes active email association
              |
              v
hydration transaction commits
              |
              v
deal worker publishes deal_asset.extract_email_attachments_deal
              |
              v
DealAssetEmailAttachmentService loads the committed association
              |
              v
EmailAttachmentProvider lists and downloads eligible attachments
              |
              v
Azure upload + retry-safe DealAsset PROCESSING insert
              |
              v
service returns asset IDs requiring post-upload
              |
              v
task publishes deal_asset.post_upload for each returned asset
              |
              v
DealAssetPostUploadService commits ACTIVE/FAILED state
              |
              v
task publishes existing asset status event
```

Hydration never downloads attachments inside its database transaction.

### Operator repair/backfill

```text
beat: deal_asset.extract_email_attachments_all_orgs
              |
              v
one deal_asset.extract_email_attachments_org task per approved organisation
              |
              v
organisation lock
              |
              v
find deals with active associated emails having attachments
              |
              v
run the same DealAssetEmailAttachmentService
              |
              v
publish the same post-upload task for returned assets
```

The repair path is not a second mapper. It is an explicitly invoked recovery
tool for associations that already exist; no periodic Beat schedule is enabled.

### Manual file upload

```text
API creates PENDING_UPLOAD DealAsset + write SAS
              |
              v
client uploads directly to Azure
              |
              v
API finalizes asset to PROCESSING and commits
              |
              v
API publishes deal_asset.post_upload
              |
              +--> broker failure returns a retryable API error
                   and a repeated finalize republishes the same PROCESSING asset
              |
              v
same DealAssetPostUploadService and status event path
```

Manual and email-derived files converge only at post-upload. They do not share
provider extraction.

## Multi-mailbox and deduplication rules

An email may be stored once per connected mailbox, but a deal asset is not
owned by an integration.

For one canonical message:

1. load every active organisation mailbox copy allowed by the association;
2. order copies deterministically;
3. try another active copy if one mailbox cannot list or download the file;
4. stop after one copy successfully provides the attachment;
5. store the chosen integration only as provenance;
6. deduplicate by stable message/file identity for the deal, not by integration
   ID or mailbox-local attachment ID.

Database uniqueness remains the final concurrency guard. A pre-query is an
optimization, not the correctness boundary.

Replying in the same thread does not itself duplicate an attachment. A new
message is evaluated independently; an attachment is reused or skipped only
when its canonical message/file identity matches the existing deal asset.

## Failure and retry rules

- Association commit failure publishes no attachment work.
- Extraction publication failure fails/retries the hydration task after its
  idempotent association commit.
- Provider failure on one mailbox tries another eligible copy.
- Provider failure on every copy is retryable.
- Azure upload failure creates no active asset.
- A concurrent asset insert conflict returns the winning existing row.
- A `PROCESSING` or recoverable `FAILED` asset with a usable blob is eligible
  for post-upload redispatch.
- Manual finalize publication failure returns a retryable API error; retrying
  finalize reuses the same `PROCESSING` asset and republishes the task.
- Post-upload publication failure must fail the extraction task; it must not be
  swallowed after leaving a row permanently in `PROCESSING`.
- Already `ACTIVE` is successful idempotent completion and returns the existing
  status event data for safe republication.
- Thumbnail failure remains non-fatal when the original asset and checksum are
  usable.
- Asset state commits before the worker publishes the frontend status event.
- Event publication failure retries publication/processing safely; it does not
  create a second asset.

Do not introduce an outbox in this phase. Idempotent task replay plus the
explicit operator repair path are sufficient for the current delivery model.

## Implementation sequence

### Step 0: characterize the current contracts

Before moving files:

- add focused post-upload tests because none currently exist for
  `DealAssetPostUploadService`;
- prove already-active, success, missing asset, missing storage path, checksum
  failure, optional thumbnail failure, finalization failure, and failed-state
  behavior;
- record the current service and task suite result.

### Step 1: move the integration attachment provider

- move the downloader/parser/filter module;
- replace worker API-constant imports with provider-owned constants;
- use the existing service-layer token resolver;
- move tests and update imports;
- prove the moved attachment provider does not import `src.workers`.

### Step 2: create the CRM assets package

- move the existing API asset service without splitting it;
- move email extraction and post-upload services;
- update router, task, and test imports;
- keep public class names stable;
- do not change task names, routes, schemas, or database models.

### Step 3: remove worker publication from domain services

- return post-upload asset IDs from extraction;
- publish them in the extraction task after service work commits;
- return committed asset event data from post-upload;
- publish it in the post-upload task;
- make retryable failures raise into the existing Celery retry boundary.

### Step 4: delete old paths and update documentation

- delete the two old deal-worker service files;
- delete the old integration-worker attachment provider file;
- update technical documentation and test patch paths;
- verify repository searches find no old imports;
- retain every stable Celery registration and queue route.

## Required tests

### Service tests

- manual asset read/write behavior passes from the new package;
- an active associated attachment email creates one `PROCESSING` asset;
- an inactive association creates none;
- duplicate provider copies create one asset;
- one inaccessible mailbox falls back to another;
- concurrent insertion returns the existing asset;
- unsupported and junk attachments remain skipped;
- provider and Azure calls hold no database session;
- extraction returns post-upload IDs and never publishes Celery work;
- post-upload success commits `ACTIVE` and returns event data;
- already-active post-upload is success;
- repeated manual finalize reuses `PROCESSING` and republishes post-upload;
- manual finalize broker failure is not reported as successful completion;
- retryable failure remains retryable;
- permanent invalid input is distinguishable;
- thumbnail failure does not fail an otherwise valid asset.

### Task tests

- all four task names remain registered:
  - `deal_asset.extract_email_attachments_all_orgs`;
  - `deal_asset.extract_email_attachments_org`;
  - `deal_asset.extract_email_attachments_deal`;
  - `deal_asset.post_upload`;
- org locking and all-org fan-out remain unchanged;
- per-deal extraction publishes post-upload once per returned asset ID;
- empty extraction publishes nothing;
- post-upload publish failure enters task retry;
- post-upload status event is published only after service success;
- task results remain JSON serializable.

### Structural checks

```text
rg "src\.workers" src/service/crm/assets src/service/integrations/email/attachment_provider.py
rg "deal_asset_(email_attachment|post_upload)_service" src tests
rg "workers\.integrations_worker\.providers\.email\.attachment_downloader" src tests
```

The first command must return no matches. The old-path searches must return no
production or test imports.

## Acceptance criteria

The restructure is complete when:

- all reusable deal asset logic lives under `src/service`;
- provider attachment logic lives under `src/service/integrations/email`;
- workers contain only task orchestration for this flow;
- no moved asset service or attachment-provider module imports a worker;
- old files are deleted, not wrapped;
- API routes and frontend contracts are unchanged;
- Celery task names and queues remain stable;
- the deal extraction consumer remains backward compatible with the old
  two-argument payload and adds an optional bounded `email_ids` payload;
- manual upload, new-email extraction, and explicit operator repair all pass;
- duplicate mailbox copies and retries create one asset;
- post-upload cannot be silently stranded by a swallowed publish failure;
- focused tests, Ruff, compilation, and `git diff --check` pass.

## Deployment and rollback

Deploy API and deal-worker images from the same commit because Python import
paths move atomically. Task names remain compatible, and new workers accept
both old broad-repair messages and new scoped `email_ids` messages.

Do not run mixed deal-worker generations while new scoped producers are active:
drain and stop the old deal workers, then start the new generation before
resuming hydration traffic. An old consumer cannot parse the new optional
keyword. Deploy the API after the new worker generation is healthy.

Before rollout:

- confirm all four asset tasks are registered;
- run one manual upload and one email-derived attachment in staging;
- verify the row transitions through `PROCESSING` to `ACTIVE`;
- verify one frontend asset status update;
- invoke the org repair once against a bounded staging organisation.

Rollback is a code rollback. Existing `DealAsset` rows and blobs remain valid
and must not be deleted.

## Explicit non-goals

- no new asset table or migration;
- no replacement of `DealEmailAssociation`;
- no attachment download inside hydration;
- no generic event bus or outbox;
- no new frontend endpoint or SSE contract;
- no new Celery worker or queue;
- no rewrite of Azure storage;
- no split of `DealAssetsService` into speculative read/write abstractions;
- no cleanup of unrelated deal worker services;
- no deletion of assets when an email association is later removed.
