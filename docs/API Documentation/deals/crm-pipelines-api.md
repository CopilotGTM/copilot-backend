# CRM Pipelines API Documentation

> Version: 2.0
> Base URL: `/users/crm`
> Last Updated: 2026-07-05

---

## Overview

Pipelines define the sales process structure for an organisation. Each pipeline is an ordered list of **stages** that deals move through — from initial qualification to closed-won or closed-lost. Every organisation is provisioned with a single pre-configured "Default Pipeline" on first setup, and users can either keep using it as-is, customise it (rename the pipeline, edit stages, add new stages), or create brand-new pipelines alongside it.

Each pipeline is a normalised `CRMPipeline` record stored in the tenant-scoped `crm_pipelines` table. Its stages are `CRMDealStage` records in `crm_deal_stages`, linked by `pipeline_id`.

Provider and migration identifiers are not exposed on pipeline or stage API payloads.
External CRM source identity is tracked in `crm_source_object_mappings`; clients should use
`pipeline_id` and `stage_id` for all API interactions.

### Internal-only scope

**Important**: This API operates **only on the internal CRM integration** (`INTERNAL_CRM`). Pipelines imported from external CRM providers (HubSpot, Salesforce, Pipedrive) are read-only here — they are managed by their source system and kept in sync via the integration worker. Attempting to mutate an externally-synced pipeline through these endpoints results in a `404`.

All write endpoints resolve the internal CRM integration automatically from the authenticated user's organisation. If the organisation has not yet been provisioned with an internal CRM integration, write endpoints return `404` with a provisioning error.

### The default pipeline

Every organisation has **exactly one** default pipeline at all times. The default is the pipeline used when creating a deal without specifying a `pipeline_id`, and it is the target shown in the pipeline dropdown on the deal creation form.

Invariants the API enforces:

1. **Exactly one default exists** — organisations are seeded with a `Default Pipeline` during provisioning, and it cannot be left without a default.
2. **`is_default` cannot be cleared directly** — you cannot PATCH `is_default: false` on the current default. Instead, you PATCH `is_default: true` on another pipeline, and the API atomically demotes the previous default inside the same transaction.
3. **The default pipeline cannot be deleted** — you must promote another pipeline to default first, then delete the old one.
4. **Only usable pipelines can become default or receive new internal deals** — a usable internal CRM pipeline has at least one `open`, one `won`, and one `lost` stage.

### Usable vs draft pipelines

Admins can create a pipeline before finishing its stages. If the pipeline does not yet contain at least one `open`, one `won`, and one `lost` stage, the API treats it as a draft:

- `is_active = false`
- `properties.usable_for_internal_deals = false`
- it cannot be promoted to default
- deal creation rejects it with `409`

Adding the missing stage states makes the pipeline active and usable. This keeps setup simple for admins while preventing API clients, agents, imports, or future mobile surfaces from creating deals in a broken sales process.

CSV import and provider pipeline sync follow the same usability flag without rejecting source data: an imported or externally-synced pipeline with an incomplete shape is still stored, but it is marked inactive/unusable until the required stage states exist.

### User journey

The three supported flows this API powers:

| Flow | User action | Endpoints used |
|------|-------------|----------------|
| **Use default as-is** | Keep the pre-seeded "Default Pipeline" unchanged | `GET /pipelines` |
| **Customise default** | Rename the default pipeline, rename/add/delete its stages | `PATCH /pipelines/{id}`, `POST/PATCH/DELETE /pipelines/{id}/stages/...` |
| **Create own pipeline** | Create a brand-new pipeline with custom stages, clone an existing one, optionally promote to default | `POST /pipelines`, `POST /pipelines/{id}/clone`, `PATCH /pipelines/{id}` |

### Related APIs

| API | Relationship | Documentation |
|-----|-------------|---------------|
| **CRM Deals** | Deals reference pipelines and stages via `pipeline_id` and `stage_id` FKs | `crm-deals-api.md` |
| **CRM Settings** | Organisation-level defaults and feature flags | `crm-settings-api.md` |
| **Deprecated Deal Timeline** | Stage transitions recorded in `crm_deal_stage_history`; the legacy timeline API doc moved to `deprecated/docs/API Documentation/deals/deal-timeline-api.md` | `deprecated/docs/API Documentation/deals/deal-timeline-api.md` |
| **CRM Deals Import** | Non-default import pipelines are created via the CSV import flow | `crm-deals-import-api.md` |

### Pipeline ↔ Stage ↔ Deal relationship

```
Pipeline (CRMPipeline)
  ↓ pipeline_id FK
Stage (CRMDealStage)
  ↓ stage_id FK
Deal (CRMDeal)  ←  also carries pipeline_id FK (duplicated for fast filters)
```

A deal always has **both** a `pipeline_id` and a `stage_id`. The `stage_id` belongs to the pipeline identified by `pipeline_id` — the stage cannot live in a different pipeline.

---

## Authentication

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` (Supabase JWT) |
| `Content-Type` | Yes (for POST/PATCH) | `application/json` |

User identity (`organisation_id`, `user_id`, `user_name`, `user_email`) is extracted from the JWT. Clients never pass these explicitly. All queries are automatically scoped to the authenticated user's organisation.

### Unauthorised Response (401)

```json
{
  "detail": "Invalid or missing user credentials."
}
```

---

## Rate Limiting

Per-user, fixed-window rate limits enforced via Redis. Limits are shared across all endpoints in the same category.

| Category | Per Minute | Per Hour | Applies To |
|----------|-----------|----------|------------|
| **Read** | 120 | 2,000 | `GET` endpoints |
| **Write** | 120 | 2,000 | `POST`, `PATCH`, `DELETE` endpoints |

Configurable via environment variables: `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_HOUR`, `RATE_LIMIT_READ_PER_MINUTE`, `RATE_LIMIT_READ_PER_HOUR`.

### Rate Limit Exceeded Response (429)

```json
{
  "detail": "Too many requests (per-minute limit exceeded)"
}
```

Response includes a `Retry-After` header (seconds until the window resets).

**Fail-open behaviour**: If Redis is unavailable, the rate limiter logs a warning and allows the request through.

---

## Response Envelope

All endpoints return the shared `GenericResponse<T>` envelope:

```json
{
  "status": "SUCCESS",
  "message": "Human-readable message",
  "data": { ... }
}
```

Error responses use the same envelope:

```json
{
  "status": "ERROR",
  "message": "Human-readable error description",
  "data": null
}
```

The HTTP status code is set on the response (not inside the JSON body). Always check the HTTP status code first, then read `message` for display and `data` for payload.

---

## Data Relationships

### Pipeline → Stages

Stages belong to exactly one pipeline via `CRMDealStage.pipeline_id`. A pipeline's stages are ordered by `display_order` (nullable integer, ascending — nulls sort last). The default pipeline ships with six stages in this order:

| Order | Stage | `stage_state` | Probability | `is_closed` |
|-------|-------|---------------|-------------|-------------|
| 1 | Qualification | `open` | 10 | false |
| 2 | Discovery | `open` | 25 | false |
| 3 | Proposal | `open` | 50 | false |
| 4 | Negotiation | `open` | 75 | false |
| 5 | Closed Won | `won` | 100 | true |
| 6 | Closed Lost | `lost` | 0 | true |

### Stage → Deals

Deals reference a stage via `CRMDeal.stage_id`. A stage can hold any number of deals. When a deal is created, it defaults to the pipeline's first stage by `display_order`. Stage transitions (moving a deal from one stage to the next) are recorded in the `crm_deal_stage_history` table. The legacy deal timeline API is deprecated; callers should use stage history and activity surfaces instead.

### Snapshot propagation (why renames touch deals)

`CRMDeal` carries **denormalized snapshot fields** that capture the pipeline and stage names at the time the deal was last synced:

- `deal.pipeline_name`
- `deal.pipeline_label`
- `deal.stage_name`
- `deal.stage_label`

These columns exist so that the deal list view can render pipeline/stage names without joining back to `crm_pipelines` and `crm_deal_stages` on every row. They were designed for externally-synced pipelines (HubSpot/Salesforce) where the provider is the source of truth.

For internally-managed pipelines, this creates a consistency problem: **if we rename a stage from `"Qualification"` to `"Lead Qualification"` without updating the snapshots, the deal list would keep showing the old name until the next sync (which never comes for internal pipelines).**

The API solves this by propagating rename operations to all affected deals in the **same transaction**:

```sql
-- On PATCH /pipelines/{id} with {"name": "Sales Funnel"}:
UPDATE crm_deals
   SET pipeline_name = 'Sales Funnel', pipeline_label = 'Sales Funnel'
 WHERE organisation_id = :org AND pipeline_id = :pipeline_id AND is_deleted = false;

-- On PATCH /pipelines/{pid}/stages/{sid} with {"name": "Lead Qualification"}:
UPDATE crm_deals
   SET stage_name = 'Lead Qualification', stage_label = 'Lead Qualification'
 WHERE organisation_id = :org AND stage_id = :stage_id AND is_deleted = false;
```

**Implications for frontend:**

- After a rename, deal list items that used to show the old name will show the new name on the next refetch — no frontend cache surgery needed.
- The update is atomic with the pipeline/stage rename; there is no intermediate window where some deals show the old name and others show the new.
- Only non-deleted deals are updated. Soft-deleted deals keep their historical snapshot.

### In-use guards (why deletes can fail)

Deletes are **always soft** (`is_deleted = true`). `CRMDealStageHistory` rows keep their FK to the stage, so hard deletes would orphan history. Instead, the API blocks deletes when the target is still referenced:

| Target | Block condition | Error |
|--------|----------------|-------|
| Pipeline | `is_default = true` | `409` — "Cannot delete the default pipeline. Promote another pipeline first." |
| Pipeline | Any non-deleted deal with `pipeline_id = :target` | `409` — "Cannot delete pipeline: N deal(s) still reference it." |
| Stage | Any non-deleted deal with `stage_id = :target` | `409` — "Cannot delete stage: N deal(s) are currently in this stage. Move or delete those deals first." |

Stage delete does not move deals implicitly. Call `POST /pipelines/{pipeline_id}/stages/{stage_id}/move-deals` first when retiring a stage that still contains deals, then retry the delete.

When a pipeline is deleted successfully, the API cascade-soft-deletes all of its stages in the same transaction. This is safe because the pipeline-level guard already confirmed no deals reference the pipeline, which implies no deals reference its stages either.

---

## Enums & Field Reference

### `stage_state`

Categorisation of a stage for reporting. **Immutable after create** — updating a stage cannot change its `stage_state`.

| Value | Description | Default `probability` | Default `is_closed` |
|-------|-------------|----------------------|---------------------|
| `"open"` | In-progress stage, deal still being worked | `null` (caller-supplied) | `false` |
| `"won"` | Deal closed successfully | `100` if caller omits | `true` |
| `"lost"` | Deal lost | `0` if caller omits | `true` |

Why `stage_state` is immutable: downstream reporting (forecasts, win-rate calculations, timeline analytics) keys off this field. Allowing it to change on an existing stage would retroactively rewrite historical reports. If you need a different categorisation, create a new stage and move deals over.

### `is_closed`

Derived automatically from `stage_state` at stage creation time. `won` and `lost` are closed; `open` is not. Cannot be set or changed directly.

### `is_default` (pipeline)

Exactly one pipeline per organisation has `is_default = true`. This flag drives:

- The "default" selection on the deal creation form
- The pipeline used when creating a deal without specifying `pipeline_id`
- The pipeline that cannot be deleted until another is promoted

### `is_active` (pipeline)

For internal CRM pipelines, `is_active` means the pipeline is usable for new internal deals. A pipeline becomes active only when it has at least one `open`, one `won`, and one `lost` stage. Empty or incomplete pipelines are draft/unusable and return `is_active = false`. The field is returned in responses but not settable through the request body.

### Field Nullability Reference

**PipelineItem fields:**

| Field | Can Be Null | When Null |
|-------|------------|-----------|
| `label` | Yes | No label provided at creation (defaults to `name`) |
| `display_order` | Yes | Not set (the list endpoint sorts nulls last) |
| `updated_at` | No | Never; use this as the version for reorder/concurrency requests |
| `deal_count` | Yes | Null on mutation responses that do not recompute counts; list endpoints return zero when no non-deleted deals reference the pipeline |

**PipelineStageItem fields:**

| Field | Can Be Null | When Null |
|-------|------------|-----------|
| `label` | Yes | No label provided at creation (defaults to `name`) |
| `probability` | Yes | Stage is `open` and caller did not supply a probability |
| `display_order` | Yes | Not set (the list endpoint sorts nulls last) |
| `deal_count` | Yes | Null on mutation responses that do not recompute counts; list endpoints return zero when no non-deleted deals reference the stage |

---

## Response Models (TypeScript)

```ts
type UUID = string;

export interface CRMSettingsLastChanged {
  changed_at: string;
  actor_user_id: UUID | null;
  source: string;
  event_type: string;
}

/**
 * Stage definition returned by list endpoints and nested inside PipelineItem.
 *
 * stage_state is immutable after create. is_closed is derived from stage_state.
 * display_order is nullable — sort with nulls last for display.
 */
export interface PipelineStageItem {
  stage_id: UUID;
  name: string;                       // Display name (e.g., "Qualification")
  label: string | null;               // Human-friendly label (defaults to name if not set)
  stage_state: 'open' | 'won' | 'lost';
  probability: number | null;         // Win probability 0-100
  is_closed: boolean;                 // Derived: true iff stage_state is 'won' or 'lost'
  display_order: number | null;       // Sort order inside the pipeline (ascending, nulls last)
  deal_count: number | null;          // Filled by list reads; null on write responses
  last_changed: CRMSettingsLastChanged | null;  // Latest stage settings audit event
}

/**
 * Pipeline returned by list/create/update endpoints.
 *
 * stages is populated by GET /pipelines?include_stages=true, POST /pipelines (the
 * newly-created stages), and is empty [] on PATCH /pipelines/{id}.
 */
export interface PipelineItem {
  pipeline_id: UUID;
  name: string;                       // Display name (e.g., "Default Pipeline")
  label: string | null;               // Human-friendly label (defaults to name if not set)
  is_default: boolean;                // Exactly one pipeline per org has this true
  is_active: boolean;                 // True only when the pipeline can accept internal deals
  display_order: number | null;       // Sort order of the pipeline itself
  updated_at: string;                 // ISO timestamp used as if_updated_at for reorder/concurrency
  deal_count: number | null;          // Filled by list reads; null on write responses
  last_changed: CRMSettingsLastChanged | null;  // Latest pipeline settings audit event
  stages: PipelineStageItem[];        // Empty [] if not requested or not relevant to the response
}

/**
 * Stage definition supplied when creating a pipeline or adding a stage.
 * Used inside CreatePipelineRequest.stages and as CreateStageRequest.
 */
export interface StageInput {
  name: string;                       // Required. 1-255 chars, trimmed
  label?: string | null;              // Optional. Defaults to name
  stage_state?: 'open' | 'won' | 'lost';  // Default: "open"
  probability?: number | null;        // 0-100. Defaults: won→100, lost→0, open→null
  display_order?: number | null;      // Optional. Create-stage endpoint auto-appends if omitted
}

/**
 * Request to create a new pipeline, optionally with an initial list of stages.
 */
export interface CreatePipelineRequest {
  name: string;                       // Required. 1-255 chars, unique per org (case-insensitive)
  label?: string | null;              // Optional. Defaults to name
  set_as_default?: boolean;           // Default: false. If true, unsets current default atomically
  stages?: StageInput[] | null;       // Optional. Omit to create an empty pipeline
}

/**
 * Request to clone an existing pipeline's stage shape into a new pipeline.
 */
export interface ClonePipelineRequest {
  name: string;                       // Required. 1-255 chars, unique per org (case-insensitive)
  label?: string | null;              // Optional. Defaults to name
  set_as_default?: boolean;           // Default: false. Requires the copied stages to be usable
}

/**
 * Request to update a pipeline. if_updated_at is required; other omitted fields are untouched.
 *
 * Setting is_default=true atomically demotes the current default to false.
 * Setting is_default=false is rejected (use promote-another-pipeline pattern instead).
 * Renaming (name change) propagates to snapshot fields on all non-deleted deals in this pipeline.
 */
export interface UpdatePipelineRequest {
  name?: string;                      // 1-255 chars, unique per org (case-insensitive)
  label?: string | null;
  is_default?: boolean;               // Only true is accepted; false is rejected
  display_order?: number | null;
  if_updated_at: string;              // Required optimistic-concurrency guard
}

/**
 * Request to add a new stage to an existing pipeline. Same shape as StageInput.
 */
export type CreateStageRequest = StageInput;

/**
 * Request to update a stage. stage_state and is_closed are IMMUTABLE and not present here.
 *
 * Renaming (name change) propagates to stage_name/stage_label on all non-deleted deals
 * in this stage. stage_key is stable after create and is not regenerated on rename.
 */
export interface UpdateStageRequest {
  name?: string;                      // 1-255 chars
  label?: string | null;
  probability?: number | null;        // 0-100
  display_order?: number | null;
  if_updated_at: string;              // Required optimistic-concurrency guard
}

/**
 * Request to save the full stage order in one atomic operation.
 *
 * if_updated_at must be the PipelineItem.updated_at value the client loaded before editing.
 */
export interface ReorderStagesRequest {
  stage_ids: UUID[];                  // Complete ordered list for this pipeline
  if_updated_at: string;              // ISO timestamp from PipelineItem.updated_at
}

export interface MoveStageDealsRequest {
  destination_stage_id: UUID;         // Another non-deleted stage in the same pipeline
}

/**
 * Response wrappers for the two list shapes used by this API.
 */
export interface PipelinesResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: PipelineItem[];
}

export interface StagesResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: PipelineStageItem[];
}

export interface PipelineResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: PipelineItem;
}

export interface StageResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: PipelineStageItem;
}

export interface ReorderStagesResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: PipelineStageItem[];
}

export interface MoveStageDealsResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: { moved_deal_count: number };
}
```

### Frontend Utility: Find the Default Pipeline

```ts
/**
 * Return the default pipeline, falling back to the first pipeline if the server
 * response is malformed (defensive — should not happen in practice).
 */
function getDefaultPipeline(pipelines: PipelineItem[]): PipelineItem | undefined {
  return pipelines.find((p) => p.is_default) ?? pipelines[0];
}
```

### Frontend Utility: Sort Stages for Display

```ts
/**
 * Stages are returned sorted by display_order ascending (nulls last), but always
 * re-sort on the client to be safe — the order matters for the kanban view.
 */
function sortStages(stages: PipelineStageItem[]): PipelineStageItem[] {
  return [...stages].sort((a, b) => {
    if (a.display_order == null && b.display_order == null) return 0;
    if (a.display_order == null) return 1;
    if (b.display_order == null) return -1;
    return a.display_order - b.display_order;
  });
}
```

---

## Endpoints

### 1. List Pipelines

```http
GET /users/crm/pipelines
```

Returns all internal CRM pipelines for the authenticated user's organisation. Results are sorted by `display_order` ascending (nulls last), then by `name`. `deal_count` is populated for each returned pipeline; when `include_stages=true`, each returned stage also includes its `deal_count`.

#### Query Parameters

| Parameter | Type | Default | Validation | Description |
|-----------|------|---------|------------|-------------|
| `include_stages` | boolean | `false` | — | If `true`, each pipeline's `stages` array is populated. |
| `active_only` | boolean | `false` | — | If `true`, only pipelines usable for internal deal writes are returned. Use this for deal creation pickers. |

#### Example Request

```http
GET /users/crm/pipelines?include_stages=true
```

For deal creation dropdowns:

```http
GET /users/crm/pipelines?include_stages=true&active_only=true
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Pipelines retrieved",
  "data": [
    {
      "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "Default Pipeline",
      "label": "Default Pipeline",
      "is_default": true,
      "is_active": true,
      "display_order": 0,
      "updated_at": "2026-07-05T10:00:00Z",
      "deal_count": 12,
      "last_changed": {
        "changed_at": "2026-07-05T10:00:00Z",
        "actor_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "source": "api",
        "event_type": "crm.settings.pipeline.updated"
      },
      "stages": [
        {
          "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
          "name": "Qualification",
          "label": "Qualification",
          "stage_state": "open",
          "probability": 10,
          "is_closed": false,
          "display_order": 1,
          "deal_count": 4,
          "last_changed": {
            "changed_at": "2026-07-05T09:45:00Z",
            "actor_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "source": "api",
            "event_type": "crm.settings.stage.updated"
          }
        },
        {
          "stage_id": "6ba7b814-9dad-11d1-80b4-00c04fd430c8",
          "name": "Closed Won",
          "label": "Closed Won",
          "stage_state": "won",
          "probability": 100,
          "is_closed": true,
          "display_order": 5,
          "deal_count": 2,
          "last_changed": null
        }
      ]
    }
  ]
}
```

**Empty organisation:** If the internal CRM integration has not yet been provisioned, returns `data: []` with message `"No internal pipelines"`. This is **not** an error.

**Without `include_stages`:** Each pipeline's `stages` array is `[]`.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `429` | `"Too many requests (per-minute limit exceeded)"` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 2. Get Pipeline Stages

```http
GET /users/crm/pipelines/{pipeline_id}/stages
```

Returns all non-deleted stages for a single pipeline, ordered by `display_order` ascending (nulls last), then by `name`. Each returned stage includes `deal_count`.

Use this endpoint when you already have a `pipeline_id` and need only its stages (e.g., populating a stage dropdown on the deal edit form). Prefer `GET /pipelines?include_stages=true` when listing multiple pipelines.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pipeline_id` | UUID | Yes | Pipeline identifier |

#### Example Request

```http
GET /users/crm/pipelines/550e8400-e29b-41d4-a716-446655440000/stages
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Stages retrieved",
  "data": [
    {
      "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "name": "Qualification",
      "label": "Qualification",
      "stage_state": "open",
      "probability": 10,
      "is_closed": false,
      "display_order": 1,
      "deal_count": 4,
      "last_changed": {
        "changed_at": "2026-07-05T09:45:00Z",
        "actor_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "source": "api",
        "event_type": "crm.settings.stage.updated"
      }
    }
  ]
}
```

**Pipeline not found:** Returns an empty array `data: []`, not a 404. The access-layer query is defensive — it filters by `organisation_id` and `pipeline_id` and returns whatever matches. Callers should check the pipelines list separately if they need to distinguish "pipeline exists but has no stages" from "pipeline does not exist".

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `429` | `"Too many requests (per-minute limit exceeded)"` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 3. Create Pipeline

```http
POST /users/crm/pipelines
```

Creates a new pipeline under the internal CRM integration, optionally with an initial list of stages and optionally promoting it to the organisation default in the same transaction.

#### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` |
| `Content-Type` | Yes | `application/json` |

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `name` | string | **Yes** | 1–255 chars, trimmed, unique per org (case-insensitive) | Display name for the pipeline |
| `label` | string | No | 1–255 chars, trimmed | Human-friendly label. Defaults to `name` if omitted. |
| `set_as_default` | boolean | No | — | If `true`, atomically demotes the current default and promotes this new pipeline. Default: `false`. |
| `stages` | `StageInput[]` | No | Each stage validated per row (see below) | Initial list of stages. Omit to create an empty pipeline. |

#### Stage input validation (each item in `stages`)

| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `name` | string | **Yes** | 1–255 chars, trimmed |
| `label` | string | No | Defaults to `name` |
| `stage_state` | string | No | One of `"open"`, `"won"`, `"lost"`. Default: `"open"` |
| `probability` | number | No | 0–100. Defaults: `won`→`100`, `lost`→`0`, `open`→`null` |
| `display_order` | integer | No | Defaults to `0` for every stage if omitted — the caller is responsible for providing explicit ordering |

#### Auto-resolution behaviours

- **`stage_key`** (not in the request body) is derived from `name` via lowercase-and-slugify at creation time: `re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")`. For `"Closed Won"` the key becomes `"closed_won"`. Renaming a stage later does not rewrite this key.
- Pipeline and stage responses use local `pipeline_id` and `stage_id`. Provider IDs are stored only in `crm_source_object_mappings`.
- **`is_closed`** is derived: `stage_state in ("won", "lost")` → `true`.

#### Example Request — empty pipeline

```json
{
  "name": "Enterprise Pipeline",
  "label": "Enterprise Deals"
}
```

#### Example Request — with stages, promoted to default

```json
{
  "name": "Enterprise Pipeline",
  "label": "Enterprise Deals",
  "set_as_default": true,
  "stages": [
    { "name": "Intro Call", "display_order": 1 },
    { "name": "Technical Evaluation", "probability": 40, "display_order": 2 },
    { "name": "Commercials", "probability": 70, "display_order": 3 },
    { "name": "Signed", "stage_state": "won", "display_order": 4 },
    { "name": "Lost", "stage_state": "lost", "display_order": 5 }
  ]
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Pipeline created",
  "data": {
    "pipeline_id": "c1d2e3f4-a5b6-7890-cdef-1234567890ab",
    "name": "Enterprise Pipeline",
    "label": "Enterprise Deals",
    "is_default": true,
    "is_active": true,
    "display_order": 1,
    "updated_at": "2026-07-05T10:00:00Z",
    "deal_count": null,
    "last_changed": null,
    "stages": [
      {
        "stage_id": "d2e3f4a5-b6c7-8901-defa-234567890abc",
        "name": "Intro Call",
        "label": "Intro Call",
        "stage_state": "open",
        "probability": null,
        "is_closed": false,
        "display_order": 1,
        "deal_count": null,
        "last_changed": null
      }
    ]
  }
}
```

**Notes:**

- The response's `stages` array contains the newly-created stages in the order they were returned from the database (by `display_order` ascending, nulls last).
- If `set_as_default: true` was passed and the organisation already had a default pipeline, that pipeline's `is_default` is flipped to `false` inside the same transaction. No separate PATCH is needed.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has not been provisioned with an internal CRM integration |
| `409` | `"A pipeline named 'Enterprise Pipeline' already exists"` | Non-deleted pipeline with the same name (case-insensitive) in this org |
| `409` | `"A stage named 'Closed Won' already exists in this pipeline"` | Initial stages include a duplicate display name or generated `stage_key` |
| `422` | Pydantic validation error | Missing `name`, invalid `stage_state`, `probability` out of range, etc. |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 4. Clone Pipeline

```http
POST /users/crm/pipelines/{pipeline_id}/clone
```

Creates a new internal CRM pipeline by copying the source pipeline's stage names, labels, lifecycle states, probabilities, and display order. The new pipeline gets fresh `pipeline_id` and `stage_id` values and starts with zero deals.

This is the fastest admin setup path when the default pipeline is close to the desired shape.

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `name` | string | **Yes** | 1-255 chars, trimmed, unique per org (case-insensitive) | Display name for the new pipeline |
| `label` | string | No | 1-255 chars, trimmed | Human-friendly label. Defaults to `name` if omitted. |
| `set_as_default` | boolean | No | - | If `true`, atomically demotes the current default and promotes the clone. |

#### Example Request

```http
POST /users/crm/pipelines/550e8400-e29b-41d4-a716-446655440000/clone
```

```json
{
  "name": "Enterprise Sales",
  "set_as_default": true
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Pipeline cloned",
  "data": {
    "pipeline_id": "c1d2e3f4-a5b6-7890-cdef-1234567890ab",
    "name": "Enterprise Sales",
    "label": "Enterprise Sales",
    "is_default": true,
    "is_active": true,
    "display_order": 1,
    "updated_at": "2026-07-05T10:00:00Z",
    "deal_count": null,
    "last_changed": null,
    "stages": [
      {
        "stage_id": "d2e3f4a5-b6c7-8901-defa-234567890abc",
        "name": "Qualification",
        "label": "Qualification",
        "stage_state": "open",
        "probability": 10,
        "is_closed": false,
        "display_order": 1,
        "deal_count": null,
        "last_changed": null
      }
    ]
  }
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"A usable internal CRM pipeline must have at least one open, won, and lost stage."` | `set_as_default=true` but the copied stages are incomplete |
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has no internal CRM integration |
| `404` | `"Pipeline not found"` | Source pipeline does not exist or is soft-deleted |
| `409` | `"A pipeline named 'Enterprise Sales' already exists"` | New name collides with another active internal pipeline |
| `422` | Pydantic validation error | Missing/invalid request fields |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 5. Update Pipeline

```http
PATCH /users/crm/pipelines/{pipeline_id}
```

Partial update — only send fields to change. Omitted fields remain unchanged.

Renaming a pipeline propagates the new name/label to `pipeline_name` and `pipeline_label` on every non-deleted deal in this pipeline inside the same transaction. See the **Snapshot propagation** section above.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pipeline_id` | UUID | Yes | Pipeline identifier |

#### Request Body

Only include fields to update, plus the required `if_updated_at` precondition.

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `name` | string | 1–255 chars, unique per org (case-insensitive, excluding self) | New display name. Triggers snapshot propagation on deals. |
| `label` | string | 1–255 chars | New label. Also triggers snapshot propagation (for `pipeline_label`). |
| `is_default` | boolean | See rules below | Only `true` is accepted. `false` is rejected. |
| `display_order` | integer | — | Sort order for the pipeline itself |
| `if_updated_at` | ISO datetime string | Required | `PipelineItem.updated_at` from the loaded row; stale values return `409` |

#### `is_default` rules

| Action | Result |
|--------|--------|
| Send `is_default: true` on a pipeline that is **not** the default | The current default is demoted to `false`, this pipeline is promoted. Atomic. |
| Send `is_default: true` on the pipeline that **is already** the default | No-op (the flag is already true). |
| Send `is_default: false` on any pipeline | `400` — "Cannot unset the default pipeline directly. Promote another pipeline by setting is_default=true on it instead." |
| Omit `is_default` from the body | No change |

This design guarantees the organisation always has exactly one default pipeline. There is no API path that leaves the organisation without one.

#### Rename rules (`name`/`label`)

| Action | Result |
|--------|--------|
| Send `name` identical to the current name | No-op |
| Send `name` that matches another pipeline in the org (case-insensitive) | `409` — "A pipeline named '...' already exists" |
| Send `name` without `label` | Both `name` and `label` are updated to the new value |
| Send `name` with `label` | Both updated to their respective values |
| Send `label` without `name` | Only `label` is updated, and snapshot propagation runs for `pipeline_label` only |

#### Example Request — rename the default pipeline

```json
{
  "name": "Sales Funnel",
  "label": "Sales Funnel",
  "if_updated_at": "2026-07-05T04:25:00Z"
}
```

#### Example Request — promote another pipeline to default

```json
{
  "is_default": true,
  "if_updated_at": "2026-07-05T04:25:00Z"
}
```

#### Success Response (200)

Returns the updated `PipelineItem`. The `stages` array is empty `[]` in this response — call `GET /pipelines/{id}/stages` separately if you need the stage list after updating.

```json
{
  "status": "SUCCESS",
  "message": "Pipeline updated",
  "data": {
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "Sales Funnel",
    "label": "Sales Funnel",
    "is_default": true,
    "is_active": true,
    "display_order": 0,
    "updated_at": "2026-07-05T04:30:00Z",
    "deal_count": null,
    "last_changed": null,
    "stages": []
  }
}
```

**Frontend note:** After a successful rename, you must refetch the deals list (or invalidate the deals query cache) so that `pipeline_name` / `pipeline_label` columns on deal rows pick up the new value. The backend has already updated them — the frontend cache just needs to be invalidated.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Cannot unset the default pipeline directly. Promote another pipeline by setting is_default=true on it instead."` | `is_default: false` was sent |
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has no internal CRM integration |
| `404` | `"Pipeline not found"` | No non-deleted pipeline with this ID under the internal integration in this organisation |
| `409` | `"A pipeline named '...' already exists"` | New name collides with another pipeline (case-insensitive) |
| `409` | `"Pipeline changed since it was loaded. Refresh and retry."` | `if_updated_at` is stale |
| `422` | Pydantic validation error | Missing `if_updated_at`, invalid field types, or empty `name` string |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 6. Delete Pipeline

```http
DELETE /users/crm/pipelines/{pipeline_id}
```

Soft-deletes a non-default pipeline that has no deals referencing it. All of the pipeline's stages are cascade-soft-deleted in the same transaction.

This operation cannot be undone via the API — once soft-deleted, a pipeline is hidden from list endpoints permanently. (Backend admins can un-flip `is_deleted` directly in the database if needed.)

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pipeline_id` | UUID | Yes | Pipeline identifier |

#### Preconditions

| Check | Result on failure |
|-------|-------------------|
| Pipeline exists and `is_deleted = false` | `404` |
| `is_default = false` | `409` — "Cannot delete the default pipeline. Promote another pipeline first." |
| No non-deleted deals reference `pipeline_id` | `409` — "Cannot delete pipeline: N deal(s) still reference it." |

The deal count is computed at request time; the error message includes the exact count so the frontend can show something like "Cannot delete — 12 deals still use this pipeline."

#### Example Request

```http
DELETE /users/crm/pipelines/c1d2e3f4-a5b6-7890-cdef-1234567890ab
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Pipeline deleted",
  "data": null
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has no internal CRM integration |
| `404` | `"Pipeline not found"` | No non-deleted pipeline with this ID in this organisation |
| `409` | `"Cannot delete the default pipeline. Promote another pipeline first."` | Target is currently the default |
| `409` | `"Cannot delete pipeline: 12 deal(s) still reference it."` | At least one non-deleted deal has `pipeline_id = target` |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 7. Create Stage

```http
POST /users/crm/pipelines/{pipeline_id}/stages
```

Adds a new stage to an existing pipeline. Use this to customise the default pipeline (e.g., inserting a "Contract Review" stage before "Closed Won") or to expand a custom pipeline after creation.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pipeline_id` | UUID | Yes | Parent pipeline identifier |

#### Request Body

Same shape as `StageInput` (see Request Models).

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `name` | string | **Yes** | 1–255 chars, trimmed | Display name |
| `label` | string | No | 1–255 chars, trimmed | Defaults to `name` |
| `stage_state` | string | No | `"open"` / `"won"` / `"lost"` | Default: `"open"` |
| `probability` | number | No | 0–100 | Defaults: `won`→`100`, `lost`→`0`, `open`→`null` |
| `display_order` | integer | No | — | If omitted, auto-appended as `MAX(existing display_order) + 1`. |

**Auto-append behaviour:** Omitting `display_order` places the new stage at the end of the pipeline. This is the common case for "add a new stage" flows. To insert between existing stages, provide an explicit `display_order` and optionally PATCH the neighbouring stages to shift them.

#### Example Request

```json
{
  "name": "Contract Review",
  "stage_state": "open",
  "probability": 85
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Stage created",
  "data": {
    "stage_id": "e3f4a5b6-c7d8-9012-efab-3456789abcde",
    "name": "Contract Review",
    "label": "Contract Review",
    "stage_state": "open",
    "probability": 85,
    "is_closed": false,
    "display_order": 7,
    "deal_count": null,
    "last_changed": null
  }
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has no internal CRM integration |
| `404` | `"Pipeline not found"` | Target pipeline does not exist or is soft-deleted |
| `409` | `"A stage named 'Contract Review' already exists in this pipeline"` | Active non-deleted stage with the same display name or generated `stage_key` already exists |
| `422` | Pydantic validation error | Missing `name`, invalid `stage_state`, `probability` out of range |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 8. Update Stage

```http
PATCH /users/crm/pipelines/{pipeline_id}/stages/{stage_id}
```

Partial update for an existing stage. Only `name`, `label`, `probability`, and `display_order` are mutable. **`stage_state`, `is_closed`, and `stage_key` are immutable** — attempting to change them has no effect because they are not in the request schema.

Renaming a stage propagates the new name/label to `stage_name` and `stage_label` on every non-deleted deal in this stage inside the same transaction. The creation-time `stage_key` is not regenerated.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pipeline_id` | UUID | Yes | Parent pipeline identifier |
| `stage_id` | UUID | Yes | Stage identifier |

The parent pipeline is loaded and validated before the stage — both must exist under the internal integration in the user's organisation, otherwise `404`.

#### Request Body

Only include fields to update, plus the required `if_updated_at` precondition.

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `name` | string | 1–255 chars | New display name. Triggers snapshot propagation on deals. |
| `label` | string | 1–255 chars | New label. Also triggers snapshot propagation (for `stage_label`). |
| `probability` | number | 0–100 | Win probability for this stage |
| `display_order` | integer | — | New sort position |
| `if_updated_at` | ISO datetime string | Required | Stage `updated_at` value from the loaded settings row; stale values return `409` |

#### Rename rules

| Action | Result |
|--------|--------|
| Send `name` identical to the current name | No-op |
| Send `name` that matches another active stage name/key in the same pipeline | `409` — "A stage named '...' already exists in this pipeline" |
| Send `name` without `label` | Both `name` and `label` are updated to the new value, `stage_key` unchanged |
| Send `name` with `label` | Both updated to their respective values, `stage_key` unchanged |
| Send `label` without `name` | Only `label` is updated, snapshot propagation runs for `stage_label` only |

#### Example Request

```json
{
  "name": "Lead Qualification",
  "probability": 15,
  "if_updated_at": "2026-07-05T04:25:00Z"
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Stage updated",
  "data": {
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "name": "Lead Qualification",
    "label": "Lead Qualification",
    "stage_state": "open",
    "probability": 15,
    "is_closed": false,
    "display_order": 1,
    "deal_count": null,
    "last_changed": null
  }
}
```

**Frontend note:** As with the pipeline rename case, invalidate the deals query cache after a successful stage rename so deal rows pick up the updated `stage_name` / `stage_label`.

If you are only changing stage order, prefer `PATCH /pipelines/{pipeline_id}/stages/reorder` instead of issuing one PATCH per stage.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has no internal CRM integration |
| `404` | `"Pipeline not found"` | Parent pipeline does not exist or is soft-deleted |
| `404` | `"Stage not found"` | Stage does not exist, is soft-deleted, or does not belong to the given pipeline |
| `409` | `"Stage changed since it was loaded. Refresh and retry."` | `if_updated_at` is stale |
| `422` | Pydantic validation error | Missing `if_updated_at`, invalid field types, `probability` out of range |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 9. Reorder Stages

```http
PATCH /users/crm/pipelines/{pipeline_id}/stages/reorder
```

Saves the full stage order for one pipeline in a single transaction. This endpoint is the preferred way to persist drag-and-drop ordering from the pipeline settings UI.

The request must include every non-deleted stage in the pipeline exactly once. Partial lists, duplicate stage ids, and ids from another pipeline are rejected before any `display_order` values are changed.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pipeline_id` | UUID | Yes | Parent pipeline identifier |

#### Request Body

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `stage_ids` | UUID[] | Required, non-empty, no duplicates | Complete ordered list of stage ids for this pipeline |
| `if_updated_at` | ISO datetime string | Required | The `PipelineItem.updated_at` value the client loaded before editing |

#### Concurrency rule

The service locks the pipeline row and compares `if_updated_at` with the current `CRMPipeline.updated_at`. If another admin changed the pipeline/stage ordering after the client loaded it, the request returns `409` and no stage order is updated.

Stage create/delete and single-stage `display_order` updates also bump the parent pipeline `updated_at`, so older clients cannot silently overwrite those changes through this bulk endpoint.

#### Example Request

```json
{
  "stage_ids": [
    "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
    "6ba7b812-9dad-11d1-80b4-00c04fd430c8"
  ],
  "if_updated_at": "2026-07-05T04:25:00Z"
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Stages reordered",
  "data": [
    {
      "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "name": "Qualification",
      "label": "Qualification",
      "stage_state": "open",
      "probability": 10,
      "is_closed": false,
      "display_order": 1,
      "deal_count": null,
      "last_changed": null
    },
    {
      "stage_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
      "name": "Discovery",
      "label": "Discovery",
      "stage_state": "open",
      "probability": 25,
      "is_closed": false,
      "display_order": 2,
      "deal_count": null,
      "last_changed": null
    }
  ]
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Duplicate stage ids are not allowed"` | `stage_ids` contains the same id more than once |
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has no internal CRM integration |
| `404` | `"Pipeline not found"` | Parent pipeline does not exist or is soft-deleted |
| `409` | `"Pipeline changed since it was loaded. Refresh and retry."` | `if_updated_at` is stale |
| `409` | `"Stage reorder must include every non-deleted stage in this pipeline."` | Missing stage id, foreign stage id, or deleted/nonexistent stage id in the list |
| `422` | Pydantic validation error | Missing `stage_ids`/`if_updated_at` or invalid UUID/datetime |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 10. Move Stage Deals

```http
POST /users/crm/pipelines/{pipeline_id}/stages/{stage_id}/move-deals
```

Moves every non-deleted internal deal currently in `stage_id` to another stage in the same pipeline. Each deal is moved through the normal deal stage writer, so stage history, lifecycle changes, deal snapshots, activity projections, and mutation events follow the same path as `PATCH /users/crm/deals/{deal_id}/stage`.

Use this before deleting or retiring a stage that still has deals.

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `destination_stage_id` | UUID | Yes | Target stage in the same pipeline |

#### Example Request

```http
POST /users/crm/pipelines/550e8400-e29b-41d4-a716-446655440000/stages/6ba7b810-9dad-11d1-80b4-00c04fd430c8/move-deals
```

```json
{
  "destination_stage_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8"
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Stage deals moved",
  "data": {
    "moved_deal_count": 3
  }
}
```

`moved_deal_count` is zero when no non-deleted deals currently reference the source stage.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Source and destination stages must be different"` | Source and destination are the same stage |
| `400` | `"Destination stage must belong to the same pipeline"` | Destination stage is in another pipeline |
| `404` | `"Stage not found"` | Source or destination stage does not exist in the path pipeline |
| `409` | `"External CRM deals are read-only"` | A matching deal is no longer internal-writable |
| `409` | Required pipeline-shape message | Destination pipeline is unusable for internal deal writes |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 11. Delete Stage

```http
DELETE /users/crm/pipelines/{pipeline_id}/stages/{stage_id}
```

Soft-deletes a stage. Blocked if any non-deleted deal currently references this stage. If the stage has deals, call `POST /pipelines/{pipeline_id}/stages/{stage_id}/move-deals` first, then retry delete.

`CRMDealStageHistory` rows are preserved (they keep their FK to the stage), so stage history continues to resolve correctly after the stage is soft-deleted.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pipeline_id` | UUID | Yes | Parent pipeline identifier |
| `stage_id` | UUID | Yes | Stage identifier |

#### Preconditions

| Check | Result on failure |
|-------|-------------------|
| Parent pipeline exists and `is_deleted = false` | `404` |
| Stage exists, `is_deleted = false`, and `pipeline_id` matches | `404` |
| No non-deleted deals reference `stage_id` | `409` — "Cannot delete stage: N deal(s) are currently in this stage. Move or delete those deals first." |
| Deleting the stage would not remove the last `open`, `won`, or `lost` stage | `409` — "Cannot delete the last won stage from a usable internal CRM pipeline." |

The deal count is computed at request time; the error message includes the exact count.

#### Example Request

```http
DELETE /users/crm/pipelines/550e8400-e29b-41d4-a716-446655440000/stages/6ba7b810-9dad-11d1-80b4-00c04fd430c8
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Stage deleted",
  "data": null
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Org has no internal CRM integration |
| `404` | `"Pipeline not found"` | Parent pipeline does not exist or is soft-deleted |
| `404` | `"Stage not found"` | Stage does not exist, is soft-deleted, or does not belong to the given pipeline |
| `409` | `"Cannot delete stage: 3 deal(s) are currently in this stage. Move or delete those deals first."` | At least one non-deleted deal has `stage_id = target` |
| `409` | `"Cannot delete the last ... stage from a usable internal CRM pipeline."` | Delete would make the pipeline unusable for internal deals |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

## Frontend: Customise Pipeline Flows

The three user journeys from the Overview, expanded with the specific endpoint sequences and rules the frontend must respect.

### Flow A — Customise the default pipeline (rename + edit stages)

The most common case: an organisation keeps the default pipeline but wants to rename it and tweak its stages to match their sales process.

```
1. Fetch the default pipeline
   → GET /users/crm/pipelines?include_stages=true
   → find the pipeline where is_default === true

2. Rename it (optional)
   → PATCH /users/crm/pipelines/{default_id}
   → body: { "name": "Sales Funnel", "if_updated_at": pipeline.updated_at }
   → backend propagates pipeline_name/pipeline_label to all deals in this pipeline
   → frontend MUST invalidate the deals query cache after success

3. Rename a stage (optional)
   → PATCH /users/crm/pipelines/{default_id}/stages/{stage_id}
   → body: { "name": "Lead Qualification", "probability": 15, "if_updated_at": stage.updated_at }
   → backend propagates stage_name/stage_label to all deals in this stage
   → frontend MUST invalidate the deals query cache after success

4. Add a new stage (optional)
   → POST /users/crm/pipelines/{default_id}/stages
   → body: { "name": "Contract Review", "probability": 85 }
   → auto-appended at the end of the pipeline (display_order = max + 1)
   → to insert between existing stages, supply an explicit display_order

5. Reorder stages (optional)
   → PATCH /users/crm/pipelines/{default_id}/stages/reorder
   → body: { "stage_ids": [all stage ids in the new order], "if_updated_at": pipeline.updated_at }
   → 409 if another admin changed the pipeline after this view loaded

6. Delete a stage (optional)
   → If the stage has deals, first:
     POST /users/crm/pipelines/{default_id}/stages/{stage_id}/move-deals
     body: { "destination_stage_id": target_stage_id }
   → DELETE /users/crm/pipelines/{default_id}/stages/{stage_id}
   → 409 if any deal is currently in this stage
     → frontend should show "N deals are in this stage. Move them to another stage first."
   → retry DELETE after move-deals succeeds
```

**Key rules the frontend must respect:**

1. **`stage_state` is immutable** — the stage edit form must NOT expose an "open/won/lost" selector for existing stages. Only the create-stage form should show it.
2. **Cache invalidation after rename** — the backend has already propagated the snapshot, but the frontend's cached deal list still holds stale names until the query is invalidated. Always invalidate after a successful PATCH on pipeline or stage.
3. **Stage reorder is bulk-only for settings UI** — drag/drop should save through `/stages/reorder` using `PipelineItem.updated_at`, not one PATCH per stage.
4. **Stage delete requires deals to be moved first** — surface the deal count and call `/stages/{stage_id}/move-deals` before retrying delete.

### Flow B — Create a new pipeline and promote it to default

The "start fresh" case: the org doesn't want to keep the default pipeline and wants to replace it with a custom one.

```
1. Open the "New Pipeline" form and either collect name + stages or choose an existing pipeline to clone

2. Create the pipeline with set_as_default=true
   → POST /users/crm/pipelines
   → body: {
       "name": "Enterprise Sales",
       "set_as_default": true,
       "stages": [
         { "name": "Intro Call", "display_order": 1 },
         { "name": "Technical Eval", "probability": 40, "display_order": 2 },
         { "name": "Commercials", "probability": 70, "display_order": 3 },
         { "name": "Signed", "stage_state": "won", "display_order": 4 },
         { "name": "Lost", "stage_state": "lost", "display_order": 5 }
       ]
     }
   → backend atomically demotes the previous default and promotes this new one
   → response includes the full stages array with generated stage_ids

   The request must include at least one open, won, and lost stage. If the user is still drafting the pipeline, create it without `set_as_default`; it will remain inactive until the missing stage states are added.

   If the user chose "copy existing pipeline", use:
   → POST /users/crm/pipelines/{source_pipeline_id}/clone
   → body: { "name": "Enterprise Sales", "set_as_default": true }
   → backend copies the source stage shape and applies the same default-promotion guard.

3. (Optional) Delete the old default pipeline
   → DELETE /users/crm/pipelines/{old_default_id}
   → 409 if the old pipeline still has deals — the user must either leave it or
     move those deals to the new pipeline first (deals API, outside this doc)
```

**Key rules the frontend must respect:**

1. **`set_as_default` vs. two-step promote** — the frontend can either create with `set_as_default: true` (one request) or create with `set_as_default: false` then PATCH `is_default: true` plus that pipeline's `if_updated_at` separately (two requests). Both are equivalent in the final state. Prefer the one-request form for better UX.
2. **The old default is not deleted automatically** — promoting a new pipeline only flips the flag. If the user wants to remove the old one, they must explicitly DELETE it, and deals referencing it must be moved first.
3. **Name uniqueness is case-insensitive** — show the 409 error inline on the name field so the user can correct it.

### Flow C — Use the default pipeline as-is

No endpoint calls. The default pipeline is automatically used when creating a deal without specifying `pipeline_id`. The frontend only needs to:

```
1. GET /users/crm/pipelines?include_stages=true  (on app load)
2. getDefaultPipeline(pipelines) → cache in memory
3. On deal creation, submit without pipeline_id → backend defaults to this one
```

This is the only flow that requires zero write endpoints from this API.

---

## Error Catalog

Consolidated reference of all error responses across all 11 endpoints.

| HTTP Status | Error Message | Endpoint(s) | Cause |
|-------------|---------------|-------------|-------|
| `400` | `"Cannot unset the default pipeline directly. Promote another pipeline by setting is_default=true on it instead."` | Update pipeline | `is_default: false` sent |
| `401` | `"Invalid or missing user credentials."` | All | Missing/invalid JWT token |
| `404` | `"Internal CRM integration is not provisioned for this organisation"` | Create, Clone, Update, Delete pipeline / Create, Update, Delete stage | Org has not been provisioned with an internal CRM integration |
| `404` | `"Pipeline not found"` | Clone, Update, Delete pipeline / Create, Update, Delete stage | Pipeline does not exist, is soft-deleted, or belongs to a different organisation |
| `404` | `"Stage not found"` | Update, Move deals, Delete stage | Stage does not exist, is soft-deleted, or does not belong to the given pipeline |
| `409` | `"A pipeline named '...' already exists"` | Create, Clone, Update pipeline | New name collides with another pipeline in this org (case-insensitive) |
| `400` | `"A usable internal CRM pipeline must have at least one open, won, and lost stage."` | Create/clone/update pipeline default promotion | Pipeline is missing a required stage state |
| `400` | `"Duplicate stage ids are not allowed"` | Reorder stages | `stage_ids` contains the same id more than once |
| `400` | `"Source and destination stages must be different"` | Move deals | Source and destination stage ids are the same |
| `400` | `"Destination stage must belong to the same pipeline"` | Move deals | Destination stage belongs to another pipeline |
| `409` | `"Pipeline changed since it was loaded. Refresh and retry."` | Update pipeline, Reorder stages | `if_updated_at` is stale |
| `409` | `"Stage changed since it was loaded. Refresh and retry."` | Update stage | `if_updated_at` is stale |
| `409` | `"Stage reorder must include every non-deleted stage in this pipeline."` | Reorder stages | Reorder list is missing a stage or includes a foreign/deleted/nonexistent stage id |
| `409` | `"Cannot delete the default pipeline. Promote another pipeline first."` | Delete pipeline | Target has `is_default = true` |
| `409` | `"Cannot delete pipeline: N deal(s) still reference it."` | Delete pipeline | At least one non-deleted deal has `pipeline_id = target` |
| `409` | `"Cannot delete stage: N deal(s) are currently in this stage. Move or delete those deals first."` | Delete stage | At least one non-deleted deal has `stage_id = target` |
| `409` | `"Cannot delete the last ... stage from a usable internal CRM pipeline."` | Delete stage | Delete would remove the last required stage state |
| `422` | Pydantic validation error | Create, Clone, Update pipeline / Create, Update, Reorder stages / Move deals | Missing required fields, invalid types, out-of-range values |
| `429` | `"Too many requests (per-minute limit exceeded)"` | All | Per-minute rate limit exceeded |
| `429` | `"Too many requests (per-hour limit exceeded)"` | All | Per-hour rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | All | Unhandled exception |

---

## Backend Architecture Notes

> This section is for backend developers working on the service layer.

### Database

**Tables:** `crm_pipelines` and `crm_deal_stages` (tenant-scoped, in `org_*` schema).

**Key indexes on `crm_pipelines`:**

| Index | Columns | Purpose |
|-------|---------|---------|
| `ix_crm_pipeline_org_name` | `organisation_id, name` | Fast name lookup and duplicate check |

**Key indexes on `crm_deal_stages`:**

| Index | Columns | Purpose |
|-------|---------|---------|
| `ix_crm_stage_pipeline_order` | `pipeline_id, display_order` | Ordered stage listing per pipeline |
| `ix_crm_stage_org_name` | `organisation_id, name` | Fast name lookup |
| `ux_crm_deal_stages_internal_active_stage_key` | `organisation_id, integration_id, pipeline_id, stage_key` where active, non-deleted, and `stage_key IS NOT NULL` | Prevents concurrent duplicate active stage keys inside one pipeline |
| `ck_crm_stage_probability` | CHECK `probability` in `[0, 100]` or NULL | Probability bounds |

### Service Layer

Writes are split across three service classes for separation of concerns:

**`InternalCRMSetupService`** (`src/service/crm/internal/internal_crm_setup_service.py`)

- Provisioning: called during org creation to create the internal CRM integration and seed the `"Default Pipeline"` with its 6 stages. Idempotent via `setup_internal_crm()`.
- Lookup: `get_internal_integration()` returns the org's internal integration row.
- Also used by CSV import (`create_import_pipeline`) to create non-default pipelines with caller-supplied stage definitions.

**`PipelineAccessService`** (`src/service/crm/access/pipeline_access_service.py`)

- Read-only: `list_pipelines()` and `list_stages()`.
- Filters by `organisation_id` and `is_deleted = false`.
- Sorts by `display_order` ascending (nulls last), then by `name`.

**`PipelineWriteService`** (`src/service/crm/internal/pipeline_write_service.py`)

- Owns all invariant checks for create/update/delete of pipelines and stages.
- Resolves the internal integration id once per request via `InternalCRMSetupService.get_internal_integration()`.
- Saves stage reorder as a complete-list bulk write guarded by `PipelineItem.updated_at`.
- Commits each operation in its own transaction (end of method).
- Raises `PipelineWriteServiceError(message, status_code)` for all rule violations; the handler maps these to the response envelope.

### Request handler

**`pipelines_handler.py`** (`src/api/routers/crm/pipelines_handler.py`)

Two FastAPI dependencies wire the services:

- `get_pipeline_access_service` → yields `PipelineAccessService` for the two read endpoints.
- `get_pipeline_write_service` → yields `PipelineWriteService` for the eight write endpoints.

Each endpoint follows the same pattern:

```python
try:
    result = await service.<method>(...)
    return Response.success_response(..., data=_build_pipeline_item(result))
except PipelineWriteServiceError as exc:
    return Response.error_response(message=exc.message, status_code=exc.status_code, ...)
except Exception:
    logger.exception(...)
    return Response.error_response(message=_UNEXPECTED_ERR_MSG, status_code=500, ...)
```

Two small helpers (`_build_pipeline_item`, `_build_stage_item`) centralise the model → response-schema conversion and are shared across all endpoints.

### Snapshot propagation SQL

Both rename operations use a single `UPDATE` statement wrapped in the same transaction as the model mutation:

```python
# Pipeline rename
await session.execute(
    update(CRMDeal)
    .where(
        col(CRMDeal.organisation_id) == organisation_id,
        col(CRMDeal.pipeline_id) == pipeline.pipeline_id,
        col(CRMDeal.is_deleted).is_(False),
    )
    .values(pipeline_name=new_name, pipeline_label=new_label)
)

# Stage rename
await session.execute(
    update(CRMDeal)
    .where(
        col(CRMDeal.organisation_id) == organisation_id,
        col(CRMDeal.stage_id) == stage.stage_id,
        col(CRMDeal.is_deleted).is_(False),
    )
    .values(stage_name=new_name, stage_label=new_label)
)
```

Both are bounded by `organisation_id` + `pipeline_id`/`stage_id` + `is_deleted = false`, and both use indexed columns (`ix_crm_deal_org_pipeline`, `ix_crm_deal_org_stage`). The cost is proportional to the number of non-deleted deals in the pipeline/stage, typically a few thousand rows at most.

### In-use check SQL

Delete endpoints count referencing deals before soft-deleting:

```python
# Pipeline in-use check (inside delete_pipeline)
stmt = select(func.count()).where(
    col(CRMDeal.organisation_id) == organisation_id,
    col(CRMDeal.pipeline_id) == pipeline.pipeline_id,
    col(CRMDeal.is_deleted).is_(False),
)

# Stage in-use check (inside delete_stage)
stmt = select(func.count()).where(
    col(CRMDeal.organisation_id) == organisation_id,
    col(CRMDeal.stage_id) == stage.stage_id,
    col(CRMDeal.is_deleted).is_(False),
)
```

The count is included in the 409 error message so the frontend can display a meaningful "N deals still use this" prompt.

### Default pipeline atomic swap

The `set_as_default` flow uses a single `UPDATE` to demote the current default, followed by a scalar flag flip on the target, both inside the same transaction:

```python
await session.execute(
    update(CRMPipeline)
    .where(
        col(CRMPipeline.organisation_id) == organisation_id,
        col(CRMPipeline.integration_id) == integration_id,
        col(CRMPipeline.is_default).is_(True),
        col(CRMPipeline.is_deleted).is_(False),
    )
    .values(is_default=False)
)
target_pipeline.is_default = True
await session.commit()
```

This guarantees the organisation never has zero defaults or multiple defaults in any observable state, even across concurrent requests (the row-level lock on the current default row serialises any concurrent promotions).

### Cascade soft-delete on pipeline deletion

When a pipeline is soft-deleted, all of its stages are soft-deleted in the same transaction:

```python
pipeline.is_deleted = True
await session.execute(
    update(CRMDealStage)
    .where(
        col(CRMDealStage.organisation_id) == organisation_id,
        col(CRMDealStage.pipeline_id) == pipeline.pipeline_id,
        col(CRMDealStage.is_deleted).is_(False),
    )
    .values(is_deleted=True)
)
```

This is safe because the pipeline-level in-use guard has already confirmed no deals reference the pipeline, which by extension means no deals reference any of its stages either.

---

## Related Documents

- `crm-deals-api.md` — Deals CRUD (uses `pipeline_id` and `stage_id` from this API)
- `crm-deals-import-api.md` — CSV import flow (creates non-default pipelines via `InternalCRMSetupService.create_import_pipeline`)
- `crm-settings-api.md` — Organisation-level CRM settings
- `deprecated/docs/API Documentation/deals/deal-timeline-api.md` — Deprecated stage transition timeline API
