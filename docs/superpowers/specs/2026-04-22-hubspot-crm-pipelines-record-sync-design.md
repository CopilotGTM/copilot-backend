# HubSpot CRM Pipelines + Stages Record Sync — Design

**Status:** 2026-04-22 — first spec in the HubSpot CRM *record* sync series. Builds on `2026-04-19-hubspot-crm-schema-sync-design.md` (schema sync, already implemented). Introduces the shared record-sync infrastructure (folder layout, Celery chain, backfill contract) that later phases (users, companies, contacts, deals, stage history, associations) will reuse.

## 1. Goal

Ingest HubSpot **deal pipelines** and their **stages** into CopilotGTM's internal CRM tables (`crm_pipelines`, `crm_deal_stages`) so that subsequent record-sync phases — most importantly deals (phase 6) — can **resolve `pipeline_id` / `stage_id` foreign keys at write time** instead of relying on batched post-hoc backfills.

Sync is strictly **HubSpot → CopilotGTM** (ingest-only). No write-back. Runs once post-connect. Future real-time updates arrive via HubSpot webhooks — not an in-process scheduler — so no recurring cron for record syncs. A manual re-run endpoint can dispatch the same Tier 3 task.

## 2. Strategic Context (non-normative)

The record-sync series loads CRM data in **dependency order** so that every fact row writes with fully resolved FKs:

```
1. Schema              ─ exists
2. Pipelines + stages  ─ THIS SPEC; dimensional, no deps
3. Users (owners merged) ─ dimensional, no deps
4. Companies           ─ needs Users (company.owner_id)
5. Contacts            ─ needs Users + Companies
6. Deals               ─ needs all above; ALL FKs resolve at write time
7. Stage history       ─ needs Pipelines + Deals
8. Associations        ─ needs Deals + Companies + Contacts
```

Building pipelines *before* deals means `CRMDeal.pipeline_id`, `stage_id`, `stage_name`, `stage_label` land correctly populated on the first write. The backfill contract this spec introduces is kept as a safety-net API for edge cases, not a load-bearing mechanism.

## 3. Scope

### In scope

- Fetching HubSpot deal pipelines (with nested stages) via `GET /crm/v3/pipelines/deals`.
- Deterministic normalization of HubSpot pipeline/stage shapes into `CRMPipeline` / `CRMDealStage` rows.
- Active-snapshot persistence: insert new, update existing, deactivate missing (no hard/soft delete).
- FK resolution *within* the writer: stages get `pipeline_id` resolved against pipelines written earlier in the same run.
- One Tier 3 Celery task: `integrations.sync_hubspot_pipelines`, reusable by post-connect chain and future manual re-run.
- Sync history via `SyncHistoryManager`; structured metadata into `KnowledgeIntegration.connection_metadata.record_sync.pipelines`.
- **Shared record-sync infrastructure** (first phase to land it):
  - `src/service/integrations/crm/records/common/hubspot_chain.py` — Celery chain builder used by `InitialSyncService`.
  - `src/service/integrations/crm/records/common/backfill.py` — backfill contract + batched-UPDATE helpers (declared here; consumed by later phases).
  - `src/service/integrations/crm/records/common/record_sync_types.py` — shared DTOs.
- Rewiring `InitialSyncService._trigger_hubspot_schema_sync()` → dispatches the full post-connect chain (schema → pipelines), so pipelines run automatically after schema sync succeeds.

### Out of scope

- Users / companies / contacts / deals / stage history / associations record sync — each is a separate spec.
- Salesforce record sync.
- HubSpot webhook ingestion path.
- Modifications to consumers of `CRMPipeline` / `CRMDealStage` (read handlers, services, UI).
- Any write-back path to HubSpot.
- Recurring schedule / Kubernetes CronJob for record syncs. (Schema sync keeps its existing 14-day cron — unchanged.)
- Tests — designed as concepts in §14, implemented as a single end-phase task per the plan convention.

## 4. Product Rules

1. Every HubSpot deal pipeline becomes one row in `crm_pipelines` scoped to the syncing `KnowledgeIntegration`.
2. Every stage inside a pipeline becomes one row in `crm_deal_stages` linked to its pipeline via `pipeline_id` (FK), with the HubSpot stage ID mirrored in `external_id` and `stage_key`.
3. Pipelines and stages absent from the current HubSpot response are **deactivated** (`is_active=false`, `last_seen_at` left as-is). No hard delete, no soft delete. Historical references from `CRMDeal` rows stay renderable.
4. HubSpot stage probability (`metadata.probability`, 0.0–1.0) is scaled to `CRMDealStage.probability` (0–100).
5. HubSpot's `metadata.isClosed` plus probability determine `CRMDealStage.stage_state`:
   - `isClosed=false` → `OPEN`
   - `isClosed=true`, `probability == 1.0` → `WON`
   - `isClosed=true`, `probability < 1.0` → `LOST`

## 5. High-Level Pipeline

```
[ Entry points ]                              [ Orchestrator ]                [ Units ]

post-connect chain  ──┐                                                       ┌─ 1. Fetcher
  (schema ─► pipelines)                                                       │    /crm/v3/pipelines/deals → raw pipelines+stages
                      │                                                       │
future manual re-run ─┼─►  Tier 3 ─► service.run(                             ├─ 2. Normalizer
                      │    Celery     org_id, integration_id )                │    HubSpot shape → NormalizedPipeline + NormalizedStage[]
future webhook        │    task                                               │
(retrigger)         ──┘                                                       └─ 3. Writer (two-pass)
                                                                                     pass A: upsert pipelines, build external_id→pipeline_id map
                                                                                     pass B: upsert stages with resolved pipeline_id
                                                                                     then deactivate missing
```

Fresh-session lifecycle (matches schema sync):

1. **Service Session A — extract integration state.** Load `KnowledgeIntegration`, verify HubSpot + active + owned by `organisation_id`, read credentials into `IntegrationTokenInfo`, release session.
2. **No DB session held.** Fetcher calls HubSpot with OAuth token refresh via `call_with_token_retry`.
3. **In-memory normalization.** Pure-Python pass builds the normalized pipeline/stage set.
4. **Service Session B — persist.** Open a fresh session; writer runs two passes and deactivation; commit; release.
5. **Return `PipelineSyncResult`** to the Tier 3 task, which owns sync-history + `connection_metadata` writes.

### Idempotency guarantees

- Re-running with the same HubSpot payload yields the same active set. Pipelines are keyed by the existing `(integration_id, external_id)` unique constraint (`uq_crm_pipeline_external`). Stages are keyed by `(integration_id, external_id)` (`uq_crm_stage_external`).
- Re-runs update `last_seen_at` and metadata fields; never insert duplicates.
- Stage `pipeline_id` FK is always re-resolved at write time against the current run's pipeline set, so renaming/replacing a HubSpot pipeline cannot leave orphaned stages.

### Tenant isolation invariants (enforced on every DB op)

- All reads and writes filter by `organisation_id` AND `integration_id`.
- The writer refuses to issue any statement missing these filters.
- No cross-integration pipeline/stage mutation is possible.

## 6. HubSpot API Contract

### Endpoint

```
GET https://api.hubapi.com/crm/v3/pipelines/deals
```

Scope required: `crm.objects.deals.read` (covered — see `HubSpotProvider._get_scopes`). No `archived=false` param needed; the endpoint returns active pipelines by default. Pipeline archiving is rare enough that we do **not** request archived pipelines in this phase.

### Response shape (relevant subset)

```json
{
  "results": [
    {
      "id": "default",
      "label": "Sales Pipeline",
      "displayOrder": 0,
      "default": true,
      "active": true,
      "objectType": "DEAL",
      "createdAt": "2024-01-01T00:00:00Z",
      "updatedAt": "2024-06-15T12:30:00Z",
      "stages": [
        {
          "id": "appointmentscheduled",
          "label": "Appointment Scheduled",
          "displayOrder": 0,
          "metadata": { "probability": "0.2", "isClosed": "false" },
          "createdAt": "...",
          "updatedAt": "..."
        }
      ]
    }
  ]
}
```

### Pagination

HubSpot's `/crm/v3/pipelines/deals` returns all pipelines in a single response (it is not a cursored collection — pipelines per portal are capped in HubSpot's product). The fetcher does **not** implement paging for this endpoint.

### Retry / auth

- 401 after token refresh → surface as `provider_auth` failure (non-retryable by this task; reauth is a separate flow).
- 429 / 5xx → Celery retry with backoff via the Tier 3 task's `max_retries` config.
- Transport errors → Celery retry.

## 7. Normalization

Pure functions, no I/O. One module: `src/service/integrations/crm/records/pipelines/pipeline_normalizer.py`.

### Types

```python
@dataclass(slots=True, frozen=True)
class NormalizedStage:
    external_id: str                       # HubSpot stage id
    name: str                              # HubSpot "label" (we use label for both name and label)
    label: str
    stage_key: str                         # equals external_id for HubSpot
    stage_state: CRMStageState             # OPEN | WON | LOST
    probability: float | None              # 0–100 (from HubSpot 0.0–1.0)
    is_closed: bool
    display_order: int | None
    source_modified_at: datetime | None    # from updatedAt
    data_payload: dict[str, Any]           # raw HubSpot stage, unmodified

@dataclass(slots=True, frozen=True)
class NormalizedPipeline:
    external_id: str                       # HubSpot pipeline id
    name: str                              # HubSpot "label"
    label: str
    object_type: str                       # "deals"
    is_default: bool                       # HubSpot "default" field
    display_order: int | None
    source_modified_at: datetime | None
    data_payload: dict[str, Any]           # raw HubSpot pipeline (without stages array)
    stages: list[NormalizedStage]
```

### Rules

- **`name` / `label`.** HubSpot gives us only `label`. We populate both `name` and `label` columns with it. A future migration may derive `name` differently; until then, equality is intentional.
- **`stage_key`.** HubSpot uses the stage id string as the API identifier — we mirror it into both `external_id` and `stage_key` to match the column's intended semantics.
- **Probability scaling.** HubSpot returns `metadata.probability` as a string like `"0.2"`. We parse-to-float, multiply by 100, round to two decimals, and bound to [0, 100]. `None` if parsing fails (WARNING log).
- **`stage_state` derivation.** Pure function of `isClosed` + `probability`. Ambiguous case (`isClosed=true` with `probability=null`) → `LOST` with a WARNING log.
- **`is_default`.** From HubSpot's `default` field. Falls back to `id == "default"` if the field is absent (old portals sometimes omit it).
- **`source_modified_at`.** Parsed from `updatedAt`; `None` if missing/unparseable.
- **`data_payload`.** For pipelines, strip the `stages` array before storing (to avoid duplication with the stage rows). For stages, store the whole stage dict.

### Error classes at normalization time

- **Skipped pipeline.** Missing `id` or non-string `id` → `skipped_pipelines += 1`, WARNING log, drop the entire pipeline (including its stages).
- **Skipped stage.** Missing `id` or non-string `id` → `skipped_stages += 1`, WARNING log, drop the stage; parent pipeline still written.
- **Type fallback.** Unparseable `probability` or `displayOrder` → store `None`, WARNING log, do not count as skipped.

## 8. Persistence & Active-Snapshot Semantics

### Writer contract

One class: `src/service/integrations/crm/records/pipelines/pipeline_writer.py`.

```python
class HubSpotPipelineWriter:
    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None: ...

    async def apply(
        self,
        *,
        pipelines: list[NormalizedPipeline],
    ) -> PipelineWriteResult: ...
```

Mandatory scoping on every statement:

- `organisation_id = <current>`
- `integration_id = <current HubSpot integration>`
- `object_type = 'deals'` (for pipelines)
- `is_deleted = false`

### Write algorithm (two passes + deactivation)

**Pass A — pipelines.**

1. Load the existing non-deleted pipeline rows for this `(organisation_id, integration_id, object_type='deals')`, keyed by `external_id`, remembering each row's prior `is_active`.
2. For each `NormalizedPipeline`:
   - **New key** → insert with `is_active=true`, `synced_at=now`, `last_seen_at=now`, `source_modified_at`, `name`, `label`, `display_order`, `is_default`, `data_payload`. Bump `pipelines_created`.
   - **Existing, was active** → update `name`, `label`, `display_order`, `is_default`, `source_modified_at`, `data_payload`, `last_seen_at`. Leave `is_active=true`. Bump `pipelines_updated`.
   - **Existing, was inactive** → same updates plus `is_active=true`. Bump `pipelines_reactivated`.
3. Commit pass A (flush only, no explicit commit yet) and collect `{external_id → pipeline_id}` into `pipeline_map`.

**Pass B — stages.**

1. Load the existing non-deleted stage rows **scoped by `(organisation_id, integration_id)`** — not by current pipeline membership — keyed by `external_id`. This is the invariant that makes cascade deactivation expressible in Pass C: we need to see every stage the integration owns, including stages under pipelines that disappeared this run.
2. For each `NormalizedStage` belonging to each pipeline in `pipeline_map`:
   - Resolve `new_pipeline_id = pipeline_map[parent.external_id]`.
   - **New key** → insert with all normalized fields + `pipeline_id = new_pipeline_id` + `is_active=true`, `last_seen_at=now`. Bump `stages_created`.
   - **Existing, was active** → update `pipeline_id = new_pipeline_id` (so a stage that moved to a different parent pipeline gets its FK corrected), plus `name`, `label`, `stage_key`, `stage_state`, `probability`, `is_closed`, `display_order`, `source_modified_at`, `data_payload`, `last_seen_at`. Leave `is_active=true`. Bump `stages_updated`.
   - **Existing, was inactive** → same field updates as above, plus `is_active=true`. Bump `stages_reactivated`.

**Pass C — deactivation (pipelines, then stages).**

- **Pipelines.** Any existing pipeline row for this `(organisation_id, integration_id, object_type='deals')` whose `external_id` is **not** in the incoming set, and was previously `is_active=true`, gets `is_active=false`. Bump `pipelines_deactivated`.
- **Stages.** Any existing stage row for this `(organisation_id, integration_id)` that was previously `is_active=true` and is **not** in the incoming stage set (regardless of current `pipeline_id`) gets `is_active=false`. Bump `stages_deactivated`. This one query covers both cases:
  1. Stage still attached to a live pipeline but removed in this sync.
  2. Stage attached to a pipeline that disappeared this sync — the cascade.
- The "cascade from a deactivated pipeline" is therefore not a separate rule; it falls out of the single query above because any stage under a removed pipeline is, by definition, not in the incoming set.

Commit once at the end of all three passes.

### 8.1 Note on `is_active` for `CRMDealStage`

The existing `CRMDealStage` model **does not yet have an `is_active` column.** `CRMPipeline` does; `CRMDealStage` does not (see `src/domain/models/organisation_tables/sync/crm/deals.py`). This spec requires one.

**Implementation implication:** the plan includes an Alembic migration in the `org_migrations` tree adding:

```python
is_active: bool = Field(
    default=True,
    sa_column=Column(Boolean, nullable=False, server_default=text("true")),
)
```

to `CRMDealStage`, with an index on `(pipeline_id, is_active)` for the active-set queries the writer performs.

### Result shape

```python
@dataclass(slots=True)
class PipelineWriteResult:
    pipelines_created: int
    pipelines_updated: int
    pipelines_reactivated: int
    pipelines_deactivated: int
    stages_created: int
    stages_updated: int
    stages_reactivated: int
    stages_deactivated: int
```

Counts bubble up into `PipelineSyncResult` and into `connection_metadata.record_sync.pipelines`.

## 9. Shared Record-Sync Infrastructure (introduced by this phase)

This phase is the first record sync to land, so it also delivers the scaffolding that phases 3–8 reuse. Three files, one folder.

### 9.1 `src/service/integrations/crm/records/common/record_sync_types.py`

Shared DTOs:

```python
@dataclass(slots=True)
class RecordSyncResult:
    provider: str           # "hubspot"
    object_type: str        # "pipeline" | "user" | "company" | "contact" | "deal" | ...
    organisation_id: UUID
    integration_id: UUID
    started_at: datetime
    completed_at: datetime
    # per-sync detail lives on subclasses

    def to_metadata(self) -> dict[str, Any]: ...
```

`PipelineSyncResult` (defined in `crm/records/pipelines/pipeline_types.py`) extends this with the write-result counts from §8.

### 9.2 `src/service/integrations/crm/records/common/hubspot_chain.py`

The post-connect orchestrator builder. Single source of truth for the HubSpot initial-sync chain.

```python
from celery.canvas import Signature, chain

from src.workers.integrations_worker.client import task_signature

def build_hubspot_initial_sync_chain(
    organisation_id: UUID,
    integration_id: UUID,
) -> Signature:
    """Returns a Celery chain signature for the full HubSpot initial sync."""
    kwargs = {
        "organisation_id": str(organisation_id),
        "integration_id": str(integration_id),
    }
    return chain(
        task_signature("integrations.sync_hubspot_deal_schema", kwargs=kwargs),
        task_signature("integrations.sync_hubspot_pipelines",  kwargs=kwargs),
        # phase 3: integrations.sync_hubspot_users
        # phase 4: integrations.sync_hubspot_companies
        # phase 5: integrations.sync_hubspot_contacts
        # phase 6: integrations.sync_hubspot_deals
        # phase 7: integrations.sync_hubspot_stage_history
        # phase 8: integrations.sync_hubspot_associations

        # Terminal finalize link — sole writer of initial_sync_status="success".
        # Must stay LAST. New phases append ABOVE this line.
        task_signature("integrations.mark_hubspot_initial_sync_succeeded", kwargs=kwargs),
    ).set(link_error=task_signature(
        "integrations.mark_hubspot_initial_sync_failed",
        kwargs=kwargs,
    ))
```

- `task_signature(...)` is a tiny helper added in this phase to `workers/integrations_worker/client.py`. It builds an **immutable** signature (`immutable=True`) against the integrations Celery client (`get_integrations_celery_client()`) using `client.signature(name, kwargs=kwargs, immutable=True)`. Immutable signatures prevent results from propagating as the first positional arg of the next task — our tasks all take `organisation_id` and `integration_id` by kwarg only.
- **The helper does NOT force a queue.** Per-task queue routing comes from Celery's `task_routes` configured on the integrations app — the same mechanism that puts `integrations.sync_hubspot_deal_schema` on `integrations_priority` today without any caller-side routing. This way each Tier 3 task's declared queue in `task_routes` remains the single source of truth, and the chain helper cannot accidentally downgrade a priority task to the default queue. Adding a new task to the chain requires registering its queue in `task_routes`, not in this helper.
- Each future phase appends one `task_signature(...)` line here and adds a new Tier 3 task module. No change to `InitialSyncService`, no change to earlier phases.
- `link_error` attaches to every link in the chain (Celery 5 behavior). The linked task sets `connection_metadata.initial_sync_status = "failed"` and records which phase failed (task name from the Celery request context).

### 9.3 `src/service/integrations/crm/records/common/backfill.py`

The backfill contract. Declared now; no implementations until later phases need it.

```python
@dataclass(slots=True, frozen=True)
class DealFkBackfillOp:
    """One batched UPDATE describing how to resolve a CRMDeal FK column."""
    external_col: str        # e.g. "pipeline_external_id"
    fk_col: str              # e.g. "pipeline_id"
    name_col: str | None     # e.g. "pipeline_name"
    label_col: str | None    # e.g. "pipeline_label"
    mapping: Mapping[str, tuple[UUID, str | None, str | None]]
        # external_id → (internal_id, name, label)

@dataclass(slots=True)
class BackfillResult:
    rows_matched: int
    rows_updated: int
    rows_skipped: int        # FK already set or _name/_label already fresh

async def backfill_crm_deals(
    session: AsyncSession,
    *,
    organisation_id: UUID,
    integration_id: UUID,
    ops: Sequence[DealFkBackfillOp],
) -> BackfillResult:
    """Apply FK-resolution UPDATEs to crm_deals, scoped to (org, integration)."""
```

- **Idempotent by construction.** The idempotency predicate depends on which display columns the op carries. There are exactly three shapes and one rule per shape:
  | Op shape | `name_col` | `label_col` | UPDATE predicate (in addition to org/integration scoping) |
  |---|---|---|---|
  | FK-only | `None` | `None` | `<fk_col> IS NULL` |
  | FK + name | set | `None` | `<fk_col> IS NULL OR <name_col> IS DISTINCT FROM :name` |
  | FK + name + label | set | set | `<fk_col> IS NULL OR <name_col> IS DISTINCT FROM :name OR <label_col> IS DISTINCT FROM :label` |

  The op constructor **rejects** the forbidden combination `name_col=None, label_col=set` at import time via a `__post_init__` check — there is no well-defined semantics for "update label without updating name," and silently allowing it would produce drift. Given a valid op, a no-op re-run touches zero rows.
- **Scoped.** Every statement filters `organisation_id = :org AND integration_id = :int` AND `<external_col> = :external_id` AND `is_deleted = false`.
- **Phase 2 usage.** Pipelines don't call `backfill_crm_deals` — no deals exist yet. The file ships empty-of-callers; it's wired up by phases 3/4/5 (users, companies, contacts) for the rare case where a late-arriving referent needs to patch existing deal rows.

Why define this now: it's the only piece of shared infrastructure that requires an explicit API contract. Defining it in phase 2 forces the contract design to happen once, not piecemeal.

## 10. Folder Layout

### New files (service layer)

```
src/service/integrations/crm/records/
├── __init__.py
├── common/
│   ├── __init__.py
│   ├── backfill.py               # §9.3
│   ├── hubspot_chain.py          # §9.2
│   └── record_sync_types.py      # §9.1
└── pipelines/
    ├── __init__.py
    ├── pipeline_sync_service.py  # HubSpotPipelineRecordSyncService
    ├── pipeline_normalizer.py    # §7
    ├── pipeline_writer.py        # §8
    └── pipeline_types.py         # NormalizedPipeline, NormalizedStage, PipelineSyncResult, PipelineWriteResult
```

### New files (worker layer)

```
src/workers/integrations_worker/
├── providers/crm/hubspot/
│   └── pipelines_fetcher.py                      # §6
└── tasks/crm/
    ├── hubspot_pipeline_record_sync_task.py      # Tier 3 — this phase's task
    ├── hubspot_initial_sync_succeeded_task.py    # chain terminal (success)
    └── hubspot_initial_sync_failed_task.py       # link_error handler (failure)
```

### Modified files

- `src/service/integrations/initial_sync_service.py` — `_trigger_hubspot_schema_sync(...)` becomes `_trigger_hubspot_initial_sync(...)`, which calls `build_hubspot_initial_sync_chain(...).apply_async()`. The metadata that used to set `schema_sync.deals.status = "queued"` now also sets `record_sync.pipelines.status = "queued"` (and adds placeholders for future phases as they ship).
- `src/workers/integrations_worker/tasks/crm/hubspot_deal_schema_task.py` — two behavior changes required for chain correctness (see §11.x):
  1. On `SoftTimeLimitExceeded`, call `record_sync_failure(...)` and **re-raise** instead of returning `{"status": "timed_out"}`. Returning succeeds the task in Celery's eyes and would let the chain advance past a timeout.
  2. In `_update_integration_metadata`, **remove** the `if status == "success": metadata["initial_sync_status"] = "success"` write. The per-phase `schema_sync.deals.status` write stays; `initial_sync_status` is now owned exclusively by the chain's terminal finalize task.
- `src/workers/integrations_worker/config.py` — add `TASK_LIMITS` entries for `integrations.sync_hubspot_pipelines`, `integrations.mark_hubspot_initial_sync_succeeded`, and `integrations.mark_hubspot_initial_sync_failed`. Terminal finalize/fail tasks get short limits (soft=30, hard=45, retries=2).
- `src/workers/integrations_worker/celery_app.py` (or wherever `task_routes` lives) — register the three new task names with their queues. Pipelines go on `integrations_priority`; the finalize/fail terminators also go on `integrations_priority` so they co-schedule with the phase tasks they follow.
- `src/workers/integrations_worker/tasks/__init__.py` — import the three new task modules so Celery discovers them.
- `src/workers/integrations_worker/client.py` — add `task_signature(name, kwargs)` helper used by `hubspot_chain.py`. The helper calls `get_integrations_celery_client().signature(name, kwargs=kwargs, immutable=True)` and does **not** set `queue=`; per-task routing stays in `task_routes`.
- `migrations/org_migrations/versions/<new>.py` — adds `is_active` column + index to `crm_deal_stages` (see §8.1).

### Unchanged files (confirming non-impact)

- `src/service/integrations/crm/schema/*` — pure-logic modules untouched. Only the **task module** that wraps the service changes, per §11.x.
- `src/workers/integrations_worker/scheduled_jobs.py` — untouched. No new scheduled job for pipelines record sync; schema sync's entry stays.

## 11. Sync Orchestration

### Tier 3 Celery task

- **Name:** `integrations.sync_hubspot_pipelines`
- **Queue:** `integrations_priority`
- **Retries:** 3, base delay 60 s, exponential backoff
- **Soft / hard time limit:** 300 s / 360 s (pipelines per portal are small; 5 minutes is ample)

Lifecycle (deliberately **not** a one-to-one mirror of the current schema sync task; see §11.x for the concrete divergences and why):

1. Coerce `organisation_id` and `integration_id` from strings to UUIDs.
2. `SyncHistoryManager.start_fresh(...)` in Session 1, release.
3. `HubSpotPipelineRecordSyncService().run(organisation_id=..., integration_id=...)` → `PipelineSyncResult`.
4. Session 2: `SyncHistoryManager.complete_fresh(status=SUCCESS, records_processed=pipelines + stages, metadata=<result>)` + update `connection_metadata.record_sync.pipelines.status = "success"` (and its counts). **Do not** touch `connection_metadata.initial_sync_status` here — that field is owned by the chain's terminal finalize task (§11.3), never by an individual phase.
5. `SoftTimeLimitExceeded` → call `record_sync_failure(...)` to mark history `FAILED` with `error_class="timeout"`, then **re-raise** (let the `SoftTimeLimitExceeded` propagate out of the task). Returning a dict here is a successful Celery completion — which would let the chain continue to the next link — so the task must raise. This also implies a matching change to the existing schema sync task, which currently *returns* `{"status": "timed_out"}` at `hubspot_deal_schema_task.py:122`; see §10 "Modified files."
6. `PipelineSyncConfigurationError` → mark history `FAILED` with `error_class="configuration"`; **re-raise** so `link_error` fires and the chain aborts.
7. Provider/DB exception → mark history `FAILED` with `error_class="provider"` or `"persistence"`; **re-raise** for Celery retry; if Celery exhausts retries, the final failure propagates and `link_error` fires.

### 11.x Divergences from the current schema sync task (the plan will apply these fixes to both)

Two behaviors in the existing schema sync task are incorrect in a chained world and must be changed before (or as part of) landing this phase:

- **Timeout path.** The current schema task returns on `SoftTimeLimitExceeded` (`hubspot_deal_schema_task.py:122`). That is a successful Celery completion and would let the chain advance past a timeout. The schema task must re-raise, matching rule 5 above.
- **Premature `initial_sync_status="success"`.** The current schema task's `_update_integration_metadata` sets `metadata["initial_sync_status"] = "success"` when its own step succeeds (`hubspot_deal_schema_task.py:192`). In a single-task world this was correct; in a chain it is wrong because the initial sync is not yet complete. The schema task must stop writing `initial_sync_status`; it only writes `schema_sync.deals.status`. The chain's terminal finalize task (§11.3) is the sole writer of `initial_sync_status = "success"`.

Both changes are listed in §10 "Modified files" and in the sequenced steps in §18.

### 11.3 Terminal finalize task (sole writer of `initial_sync_status="success"`)

New Tier 3 task `integrations.mark_hubspot_initial_sync_succeeded(organisation_id, integration_id)` is appended as the **last** link in the chain. Its only job:

1. Load the integration.
2. Set `connection_metadata.initial_sync_status = "success"`.
3. Stamp `connection_metadata.initial_sync_completed_at = <now UTC>`.
4. Commit, return `{"status": "success"}`.

Because every prior phase task re-raises on failure, this terminal task only runs if every upstream link succeeded — so it owns the `success` write by construction. No intermediate task writes `initial_sync_status`. When more phases land, they slot in before this terminal task and the contract holds.

### Post-connect chain dispatch

`InitialSyncService._trigger_hubspot_initial_sync()`:

```python
def _trigger_hubspot_initial_sync(self, org_id_str: str, int_id_str: str) -> None:
    build_hubspot_initial_sync_chain(
        organisation_id=UUID(org_id_str),
        integration_id=UUID(int_id_str),
    ).apply_async()
    logger.info(
        "Triggered HubSpot initial sync chain: integration=%s", int_id_str,
    )
```

`InitialSyncService.apply_post_connect_state(...)` is updated to write:

```json
{
  "initial_sync_status": "queued",
  "schema_sync":  { "deals":     { "status": "queued" } },
  "record_sync":  { "pipelines": { "status": "queued" } }
}
```

Salesforce's placeholder behavior is untouched.

The chain built by `build_hubspot_initial_sync_chain` (shown in §9.2) therefore has this full shape as of this phase:

```
chain(
    schema_deals.si(...),
    pipelines.si(...),
    mark_hubspot_initial_sync_succeeded.si(...),   # terminal — sets initial_sync_status=success
).set(
    link_error=mark_hubspot_initial_sync_failed.si(...),  # sets initial_sync_status=failed
)
```

### `link_error` handler (failure terminator)

Tier 3 task `integrations.mark_hubspot_initial_sync_failed(organisation_id, integration_id)`:

1. Load the integration, set `connection_metadata.initial_sync_status = "failed"`.
2. Read the failed chain link's task name from the error callback's arguments (Celery passes the failing task's UUID; resolve name via `AsyncResult(uuid).name` on the integrations client) and stamp it into `connection_metadata.initial_sync_failed_phase`.
3. Does **not** re-raise — this task is the terminator of the chain's error branch.

### Manual re-run

Any future endpoint that wants to re-trigger the full chain calls `build_hubspot_initial_sync_chain(...).apply_async()`. Any future endpoint that wants to re-trigger *only* pipelines dispatches `integrations.sync_hubspot_pipelines` directly. Same Tier 3 task; no new task shape. A standalone dispatch of `integrations.sync_hubspot_pipelines` does **not** flip `initial_sync_status` — that field is chain-owned.

### Service result shape

```python
@dataclass(slots=True)
class PipelineSyncResult(RecordSyncResult):
    fetched_pipelines: int
    fetched_stages: int
    pipelines_created: int
    pipelines_updated: int
    pipelines_reactivated: int
    pipelines_deactivated: int
    stages_created: int
    stages_updated: int
    stages_reactivated: int
    stages_deactivated: int
    skipped_pipelines: int
    skipped_stages: int

    def to_metadata(self) -> dict[str, Any]: ...
```

## 12. Error Handling & Observability

### Failure classes

1. **Configuration / validation** (non-retryable, chain-aborting)
   - Integration missing, wrong type, inactive, or not owned by `organisation_id`.
   - Credentials missing or expired past refresh.
   - → Mark sync history `FAILED` with `error_class="configuration"`. Re-raise. Chain aborts; `link_error` writes `initial_sync_status="failed"`.

2. **Provider / API** (retryable with bounds)
   - HubSpot 401 after refresh → `error_class="provider_auth"`; no retry; chain aborts.
   - HubSpot 429 → Celery retry with backoff.
   - HubSpot 5xx → Celery retry with backoff.
   - Network / timeout → Celery retry.

3. **Persistence / normalization** (surface, don't silently downgrade)
   - Skipped pipelines/stages per §7 — WARNING log, continue the run. Counts exposed in the result.
   - DB errors → mark history `FAILED`; re-raise for Celery retry; chain aborts.

### Sync history

One `SyncHistoryRecord` per run via `SyncHistoryManager`. The model's `SyncStatus` enum (see `src/domain/models/organisation_tables/sync/integration_models.py`) is the only source of truth for terminal values — it exposes `QUEUED | IN_PROGRESS | SUCCESS | FAILED | PARTIAL`. There is no `TIMED_OUT` status; do not invent one in this phase.

- **Terminal status set by this task:** `SUCCESS` or `FAILED`. `PARTIAL` is reserved for future phases that process many independent records (e.g., deals) where some succeed and some fail — pipelines is all-or-nothing, so `PARTIAL` never applies here.
- **Timeout mapping.** A timeout is reported as `status=FAILED` with `error_class="timeout"` inside `sync_metadata`. The semantic signal lives in metadata, not in the enum.
- **Context fields live in `sync_metadata` (JSON)**, not as top-level columns. `SyncHistoryRecord` does not have `entity` / `object_type` / `provider` columns. The task writes:
  ```json
  {
    "entity": "record",
    "object_type": "pipeline",
    "provider": "hubspot",
    ...result counts...
  }
  ```
  via the `metadata=` argument to `SyncHistoryManager.complete_fresh`.
- On failure, `error_details` carries `{"message": str, "class": "configuration" | "provider_auth" | "provider" | "persistence" | "timeout"}`.

### Connection metadata

```json
{
  "record_sync": {
    "pipelines": {
      "status": "success",
      "last_synced_at": "2026-04-22T02:00:00Z",
      "fetched_pipelines": 4,
      "fetched_stages": 28,
      "pipelines_created": 0,
      "pipelines_updated": 4,
      "pipelines_reactivated": 0,
      "pipelines_deactivated": 0,
      "stages_created": 0,
      "stages_updated": 28,
      "stages_reactivated": 0,
      "stages_deactivated": 0,
      "skipped_pipelines": 0,
      "skipped_stages": 0,
      "error_class": null,
      "error_message": null
    }
  }
}
```

### Logging

- Structured logs with `organisation_id`, `integration_id`, `task_name`, `sync_history_id`.
- Per-run summary at INFO: fetched/created/updated/deactivated counts.
- Per-skip WARNING with pipeline/stage id + reason.
- No HubSpot access tokens or refresh tokens ever logged.

### Non-goals

- No silent "best-effort success" downgrades. Failures must be visible in sync history and `connection_metadata`, and the Celery chain must abort.
- No auto-repair of stage-state ambiguity. Ambiguous rows are written as `LOST` with a WARNING log; correcting the source is a HubSpot-side fix.

## 13. Write-Side Isolation

- Writer filters `organisation_id`, `integration_id`, `is_deleted=false`, and (for pipelines) `object_type='deals'` on every statement.
- No cross-integration mutation is possible — a pipeline row belonging to a different HubSpot integration in the same org is invisible to this writer.
- `CRMPipeline.object_type` stays `"deals"`; future contact/company pipelines (if HubSpot ever ships them and we choose to sync) would use different values, driven by a different Tier 3 task.

## 14. Testing (end-phase, single task)

Single plan task covering:

**Unit (pure, no DB, no network):**

- `normalize_pipeline()` — happy path, missing fields, unparseable probability, ambiguous stage_state (WON/LOST boundary), `default` fallback via `id == "default"`.
- `normalize_stage()` — happy path, missing id (skip), unparseable display_order, isClosed with probability=null (LOST fallback).
- `build_hubspot_initial_sync_chain()` — returns a chain with signatures in the right order (schema → pipelines → finalize), all immutable, with `link_error` attached; finalize task is the last positional link; no queue override applied.
- `DealFkBackfillOp` invariants — rejects ops with empty mapping; rejects ops naming nonexistent columns on `CRMDeal` (import-time check similar to `hubspot_deal_mapping` validator); rejects the forbidden `name_col=None, label_col=set` combination; predicate shape matches the table in §9.3 (FK-only vs FK+name vs FK+name+label).

**Component (in-memory DB):**

- `HubSpotPipelineWriter.apply()` — insert new; update existing (including `pipeline_id` when a stage's parent pipeline changes); deactivate pipelines missing from the run; deactivate stages missing from the run (both cases: parent pipeline still present, and parent pipeline removed — the single query in Pass C covers both); reactivate previously-inactive; refuse cross-integration writes; scope by `(organisation_id, integration_id)` on every statement.
- `backfill_crm_deals()` — no-op path (empty ops); FK-only op predicate; FK+name op predicate; FK+name+label op predicate; idempotent re-run (second call touches zero rows); scope enforcement (won't touch another integration's deals).
- Alembic migration for `CRMDealStage.is_active` — upgrade + downgrade.

**Integration (mocked HubSpot HTTP):**

- `HubSpotPipelinesFetcher` — happy path, 401-then-refresh, 429 retry, 5xx retry, malformed payload (non-list `results`).
- `HubSpotPipelineRecordSyncService.run()` — end-to-end with a fixture payload; asserts counts in `PipelineSyncResult` and rows in DB.

**Worker:**

- Tier 3 `sync_hubspot_pipelines_task` — success path; configuration failure re-raises (chain-aborting, verified via asserting that downstream links are not dispatched when run with `task_always_eager=True`); `SoftTimeLimitExceeded` re-raises (regression guard for the chained-timeout bug that existed in the schema task); sync-history written with `status=FAILED` + `error_class="timeout"` in metadata; sync-history start/complete calls.
- `mark_hubspot_initial_sync_succeeded_task` — writes `initial_sync_status="success"` + `initial_sync_completed_at` timestamp; is idempotent; returns without raising.
- `mark_hubspot_initial_sync_failed_task` — writes `initial_sync_status="failed"` and `initial_sync_failed_phase` without re-raising.
- Post-connect — HubSpot dispatches the full chain; Salesforce still applies `not_implemented` placeholder; metadata keys populated correctly at connect time (`initial_sync_status="queued"`, `schema_sync.deals.status="queued"`, `record_sync.pipelines.status="queued"`).

**Regression:**

- Existing HubSpot schema-sync tests updated: the schema task **must** re-raise on `SoftTimeLimitExceeded` (not return), and **must not** write `initial_sync_status="success"`. New assertions lock these behaviors in so the chained-world invariants can't silently regress.
- End-to-end chain test (Celery eager mode): schema success + pipelines success + finalize → `initial_sync_status="success"`. Schema failure → finalize does not run, `link_error` runs, `initial_sync_status="failed"`. Pipelines failure → same outcome.

## 15. Future Extension Path

- **Users / companies / contacts / deals / stage history / associations.** Each drops into the same folder pattern under `src/service/integrations/crm/records/<entity>/` and appends one `.si(...)` to `build_hubspot_initial_sync_chain`. Deals (phase 6) is the first consumer of the write-time FK-resolution pattern at scale — it looks up `pipeline_map` and `stage_map` via fresh session-scoped queries at the start of its writer.
- **Webhook ingestion.** A future `hubspot_webhook_handler` routes `deal.propertyChange` and friends to the same writers this phase introduces. The writers are already single-row-capable (they iterate the incoming list); a webhook path passes a list of one.
- **Salesforce.** Separate fetcher, separate normalizer, same writer patterns. `CRMPipeline.object_type` lets the table multi-home across providers cleanly.

## 16. Open Decisions Resolved

- **Sync direction?** HubSpot → internal only, ingest-only.
- **Scope of phase 2?** Deal pipelines + deal stages only. No ticket/custom-object pipelines.
- **Recurring schedule?** None for record syncs. Webhook subscriptions replace recurring polls once those land. Schema sync's 14-day cron stays.
- **Chain orchestration mechanism?** Celery `chain(...)` with immutable signatures and `link_error`. Centralized in `hubspot_chain.py`. Chain always ends with a dedicated finalize task that is the **sole** writer of `initial_sync_status="success"`.
- **Who writes `initial_sync_status`?** Only two tasks: `mark_hubspot_initial_sync_succeeded` (terminal) and `mark_hubspot_initial_sync_failed` (link_error). No per-phase task touches it. The existing schema task is modified to stop writing it (§10, §11.x).
- **Timeout handling?** `SoftTimeLimitExceeded` **re-raises** out of the task after recording `error_class="timeout"` in sync-history metadata. Returning a dict would succeed the task in Celery and let the chain advance — a bug the existing schema task has today and that this phase fixes.
- **Sync-history status values?** Only the model's `SyncStatus` enum (`QUEUED|IN_PROGRESS|SUCCESS|FAILED|PARTIAL`). No invented `TIMED_OUT`. Timeout = `FAILED` with `error_class="timeout"` in `sync_metadata`. Context fields (`entity`, `object_type`, `provider`) live in `sync_metadata`, not as columns.
- **Queue routing?** Per-task, owned by Celery `task_routes`. The chain helper does **not** override queue. Pipelines runs on `integrations_priority` (same as schema sync).
- **Backfill strategy?** Declared in `common/backfill.py` as a contract with three op shapes (FK-only, FK+name, FK+name+label) and one predicate per shape. Phase 2 has no deals to backfill; phases 3–5 consume it.
- **Owners / users?** Merged into a single `CRMUser` sync in phase 3 fed by `/crm/v3/owners`. Not this phase.
- **`CRMDealStage.is_active`?** Column added in this phase's migration (§8.1).
- **Deactivation semantics?** `is_active=false` on missing rows. No soft/hard delete. Stage-deactivation query scopes by `(organisation_id, integration_id)`, which covers both in-place removal and cascade from a removed pipeline — no separate cascade rule needed.
- **Tests in plan?** Single end-phase task.

## 17. Documentation Changes Required After Implementation

- `src/workers/integrations_worker/README.md` — document the new Tier 3 task, the initial-sync chain, the `link_error` handler, and the shared `hubspot_chain.py`.
- `docs/Technical Documentation/integrations/architecture-overview.md` — state: HubSpot deal schema sync live, HubSpot pipelines+stages record sync live, remaining record syncs scheduled by phase.
- `docs/Technical Documentation/integrations/sync-by-provider.md` — HubSpot CRM section: pipelines record sync supported, task name, dependency position in the chain.
- `CLAUDE.md` — no update needed; the existing "Three-Tier Architecture" guidance already covers this pattern.

## 18. Implementation Readiness

This design is ready to convert into a step-by-step implementation plan. Sequenced steps:

1. Alembic migration adding `CRMDealStage.is_active` + index on `(pipeline_id, is_active)`.
2. Shared infrastructure: `records/common/record_sync_types.py`, `records/common/backfill.py` (contract + validators, no callers yet).
3. `task_signature(...)` helper in `workers/integrations_worker/client.py`.
4. Fix the existing schema task (`hubspot_deal_schema_task.py`) **before** the chain is wired up:
   - re-raise on `SoftTimeLimitExceeded` instead of returning;
   - stop writing `initial_sync_status` from `_update_integration_metadata`.
   Land as its own commit with a regression test so the change is reviewable independently of the new code.
5. `records/common/hubspot_chain.py` with schema + pipelines + finalize signatures, and `link_error` wired.
6. `PipelinesFetcher` (worker provider).
7. `pipeline_normalizer.py` + `pipeline_types.py` (pure).
8. `pipeline_writer.py` — two-pass upsert + single-query deactivation scoped by `(organisation_id, integration_id)`.
9. `pipeline_sync_service.py` (fresh-session orchestrator).
10. Tier 3 task `hubspot_pipeline_record_sync_task.py` — re-raises on timeout, never writes `initial_sync_status`.
11. Tier 3 task `hubspot_initial_sync_succeeded_task.py` — sole writer of `initial_sync_status="success"`.
12. Tier 3 task `hubspot_initial_sync_failed_task.py` — link_error terminator, writes `initial_sync_status="failed"` + failed-phase name.
13. Update `config.py` `TASK_LIMITS` for the three new task names.
14. Register the three new tasks in `celery_app.py` `task_routes` (all on `integrations_priority`).
15. Update `tasks/__init__.py` to import the three new modules.
16. Update `InitialSyncService`: rename `_trigger_hubspot_schema_sync` → `_trigger_hubspot_initial_sync`, dispatch the chain, add `record_sync.pipelines.status="queued"` to post-connect metadata.
17. Documentation updates per §17.
18. Tests (single end-phase task covering §14), including the regression guards for the schema task changes from step 4.

No change to consumers of `CRMPipeline` / `CRMDealStage` is needed — they already accept provider-written rows, and new `is_active` semantics on stages are additive.
