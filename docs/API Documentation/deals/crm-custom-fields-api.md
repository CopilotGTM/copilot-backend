# CRM Custom Columns (Custom Fields) API Documentation

> Version: 2.0
> Base URL: `/users/crm`
> Last Updated: 2026-04-09

---

## Overview

CRM deals have two kinds of fields:

- **Standard fields** — built into the schema (name, amount, close date, owner, …). These are present on every deal in every organisation.
- **Custom columns** — defined per organisation to track domain-specific data (for example "Priority", "Implementation Effort %", "Renewal Risk Score"). Each organisation manages its own list.

This API powers the **Settings → Custom Columns** page where users create, rename, and remove their organisation's custom columns. The same definitions are also consumed by the bulk-CSV-import flow (which has its own service in the backend) and by every deal-list / deal-detail view that needs to render the column values.

A custom column has two name fields you should understand before reading the rest of this doc:

- **`key`** — the immutable machine slug (for example `priority`). Stored on every deal as a property name. **Never changes after creation.**
- **`label`** — the display name shown in the UI (for example `"Priority"`). **The only thing rename touches.** Renaming the label updates the display name everywhere automatically; no deal data is touched. See **Data Relationships → Rename behaviour** below for the detailed reason.

### What gets stored where

| Thing | Lives in | Keyed by |
|-------|----------|----------|
| Column **definition** (label, data type, options, validation rules, …) | `crm_custom_field_definitions` | `field_id` |
| Column **value** on a specific deal (e.g. `"High"` for that deal's "Priority") | `crm_deals.properties` (JSONB blob) | `field.key` |

There is **no separate values table**. A deal's custom column values ride along inside its `properties` JSONB blob. This is why the `key` is the load-bearing identifier and the `label` is purely display.

### Internal-only scope

This API is for **internally-managed** custom columns. Externally-synced fields from HubSpot/Salesforce/Pipedrive are managed by their source CRM and reflected via the integration sync workers; this API does not create, modify, or delete them. All endpoints scope writes to definitions owned by the calling organisation.

### Related APIs

| API | Relationship | Documentation |
|-----|-------------|---------------|
| **CRM Deals** | Deals carry custom column values inside `properties` JSONB; the deal list/detail endpoints render them using definitions from this API | `crm-deals-api.md` |
| **CRM Deals Import** | CSV import uses a separate (bulk-flavoured) service to auto-create custom column definitions when an import sees an unrecognised `custom_*` column | `crm-deals-import-api.md` |
| **CRM Pipelines** | Standard pipeline/stage fields, separate from custom columns | `crm-pipelines-api.md` |

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
| **Read** | 120 | 2,000 | `GET` and the formula validation endpoint |
| **Write** | 120 | 2,000 | `POST` `/custom-fields`, `PATCH`, `DELETE` |

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

### Definition → Deal storage

The definition row tells the system what kind of data this column accepts and how to render it. The actual values are written into each deal's `properties` JSONB blob, keyed by the column's machine `key`:

```
Definition (CRMCustomFieldDefinition)
  field_id:    "c1d2..."
  key:         "priority"          ← immutable; the only identifier deals know
  label:       "Priority"          ← display only; can be renamed
  data_type:   "select"
  options:     { "values": ["High", "Medium", "Low"] }

Deal (CRMDeal)
  deal_id:     "a1b2..."
  properties:  { "priority": "High", "renewal_risk_score": 7.5 }
                  ↑ keyed by field.key, not field_id
```

When the deal list view renders the "Priority" column, it joins the deal's `properties.priority` value against the active custom column definition for that key, and displays the latter's `label`.

### Rename behaviour (this is the most important section)

A custom column can be renamed by `PATCH`-ing its `label`. This changes the display name everywhere — instantly, for every deal and every list view — **without touching any deal data**. The reason:

- Deal values are stored under `properties.<key>`. The key is `priority`, not `Priority`.
- The `label` is read from the definition row at display time, not stored on each deal.
- When you change the label from `"Priority"` to `"Deal Priority"`, the next list-view fetch joins against the (renamed) definition and shows the new label automatically.

This is unlike pipelines/stages, which carry denormalized snapshot fields on `crm_deals` and require an explicit propagation step on rename. Custom columns have no snapshot fields, so the API has nothing to propagate.

Two consequences worth highlighting for the frontend:

1. **You don't need to invalidate the deals query cache** after a custom column rename — but you DO need to invalidate the **definitions** cache so the new label gets pulled. The deal rows themselves are unchanged.
2. **The `key` is fixed forever after creation.** There is no API path to change it. If you got the key wrong at create time, the only fix is to delete and re-create.

### Immutable fields

These fields cannot be changed after creation. Any attempt to PATCH them is silently ignored (the schema doesn't even expose them):

| Field | Why immutable |
|-------|--------------|
| `key` | Stored on every deal; changing it would orphan all existing values. |
| `data_type` | Existing values were written under one type's parsing rules; switching the type would invalidate them. |
| `entity_type` | The column belongs to one entity (currently always `deal`). Cross-entity moves are not supported. |
| `is_predefined` | A server-managed flag for system columns that are seeded via DB scripts and protected from deletion. Cannot be set or cleared via the API. |

If you need a different data type for an existing column, the workflow is: create a new column with the new type, manually copy values across (out of scope for this API), then delete the old column.

### In-use guard on delete

Custom columns are **soft-deleted** (`is_deleted = true`, `is_active = false`). The delete is **blocked** when any non-deleted deal in the organisation has the column's `key` present in its `properties` JSONB blob. The block is enforced by this query:

```sql
SELECT COUNT(*) FROM crm_deals
 WHERE organisation_id = :org
   AND is_deleted = false
   AND properties ? :field_key;   -- PG JSONB key-existence operator
```

If the count is greater than zero, the API returns `409` with the affected deal count in the message:

> `Cannot delete custom column 'priority': 7 deal(s) currently use this column. Remove the column data from those deals first.`

There is **no migration path** in this version — you cannot ask the API to bulk-clear the column's value or move values to another column as part of the delete. Callers must remove the value from those deals themselves (via the deals API) and retry the delete.

**Why this matters**: this protects users from accidentally losing sales data. A future enhancement may add an opt-in "delete and clear" parameter, but for now the strict block is intentional.

### Predefined columns cannot be deleted

If `is_predefined = true` on a column, the API rejects any delete attempt with `409 "Predefined custom columns cannot be deleted."` Predefined columns exist for system-managed fields seeded by admin scripts. Today the predefined-fields list is empty in production, but the guard is in place for future use.

---

## Enums & Field Reference

### `data_type`

The value type a column accepts. Set at creation time, immutable afterward.

| Value | Meaning | Storage in `properties` JSONB | Notes |
|-------|---------|------------------------------|-------|
| `text` | Free-form string | string | Default if `data_type` is omitted |
| `integer` | Whole number | integer | Cast errors at create/import return 400 |
| `number` | Decimal number | number | |
| `currency` | Monetary amount | number | Currency is implied by org settings |
| `percentage` | Percentage 0-100 | number | UI may render with `%` suffix |
| `boolean` | Yes/No flag | boolean | |
| `date` | Date only | string (ISO `YYYY-MM-DD`) | |
| `datetime` | Date + time | string (ISO 8601) | |
| `select` | Single value from a fixed list | string | **Requires** `options.values` (non-empty list of strings) |
| `multiselect` | Multiple values from a fixed list | array of strings | **Requires** `options.values` |
| `formula` | Computed value derived from other columns | (computed) | **Requires** `formula` payload; cannot reference itself; all referenced keys must already exist |

### `entity_type`

| Value | Description |
|-------|-------------|
| `deal` | Currently the only supported entity. Custom columns on contacts, accounts, etc. are not yet supported. |

### Formula DSL operators

Formula fields use a small JSON expression DSL. The validate-formula endpoint enforces these rules; create/update validates them too.

| Operator | Args | Meaning |
|----------|------|---------|
| `add` | 1+ | Sum of args |
| `sub` | 1+ | Subtraction (left to right) |
| `mul` | 1+ | Product of args |
| `div` | 1+ | Division (left to right) |
| `concat` | 1+ | String concatenation |
| `coalesce` | 1+ | First non-null arg |
| `if` | exactly 3 | `if(cond, then, else)` |

A formula leaf is one of:

- `{ "field": "<key>" }` — reference another column by its key
- `{ "value": <literal> }` — a JSON literal (string, number, bool, null)

A formula cannot reference its own key, and every referenced key must point to a non-deleted, active definition in the same organisation.

### Field nullability

| Field | Can be null | When |
|-------|-------------|------|
| `label` | No | Required at create; cannot be cleared on update |
| `description` | Yes | Not provided |
| `display_order` | Yes | Not provided (the list endpoint sorts nulls last) |
| `options` | No (defaults to `{}`) | Always present, may be empty |
| `validation` | No (defaults to `{}`) | Always present, may be empty |
| `formula` | Yes | Only set when `data_type = "formula"` |

---

## Response Models (TypeScript)

```ts
type UUID = string;
type ISODate = string; // ISO 8601 datetime with timezone

export type CRMCustomFieldDataType =
  | 'text'
  | 'integer'
  | 'number'
  | 'date'
  | 'datetime'
  | 'currency'
  | 'percentage'
  | 'boolean'
  | 'select'
  | 'multiselect'
  | 'formula';

export type CRMCustomFieldEntity = 'deal';

/**
 * Custom column definition returned by GET / POST / PATCH endpoints.
 *
 * key, data_type, entity, and is_predefined are immutable after creation.
 * The label is the only display-name field — renaming it does NOT touch
 * any deal rows.
 */
export interface CustomFieldDefinitionItem {
  field_id: UUID;
  entity: CRMCustomFieldEntity;
  key: string;                          // Immutable machine slug
  label: string;                        // Display name (mutable)
  data_type: CRMCustomFieldDataType;    // Immutable
  required: boolean;                    // Whether this column is required on create/update
  is_predefined: boolean;               // Server-managed; cannot be deleted via API
  is_active: boolean;                   // Hidden from active use when false
  display_order: number | null;
  description: string | null;
  options: Record<string, unknown>;     // For select/multiselect: { values: string[] }
  validation: Record<string, unknown>;  // Type-specific validation rules
  formula: Record<string, unknown> | null;  // Only set when data_type === "formula"
  created_at: ISODate;
  updated_at: ISODate;
}

/**
 * Request to create a new custom column.
 *
 * key is normalised by the backend: leading "custom_" is stripped, hyphens
 * become underscores, the result must match ^[a-z][a-z0-9_]{1,62}$.
 *
 * is_predefined is silently forced to false — system columns are seeded
 * via DB scripts only.
 */
export interface CreateCustomFieldRequest {
  entity?: CRMCustomFieldEntity;        // Default: "deal"
  key: string;                          // Required, 2-63 chars after normalisation
  label: string;                        // Required, 1-255 chars
  data_type?: CRMCustomFieldDataType;   // Default: "text"
  required?: boolean;                   // Default: false
  is_predefined?: boolean;              // Ignored — always false at create
  display_order?: number | null;
  description?: string | null;
  options?: Record<string, unknown> | null;
  validation?: Record<string, unknown> | null;
  formula?: Record<string, unknown> | null;
}

/**
 * Request to update a custom column.
 *
 * Only the fields present in this interface are mutable. key, data_type,
 * entity, and is_predefined are NOT here and cannot be changed.
 *
 * Unset fields are not touched. To clear an optional field, send null.
 */
export interface UpdateCustomFieldRequest {
  label?: string;                       // 1-255 chars; cannot be empty
  required?: boolean | null;
  is_active?: boolean | null;
  display_order?: number | null;
  description?: string | null;
  options?: Record<string, unknown> | null;
  validation?: Record<string, unknown> | null;
  formula?: Record<string, unknown> | null;
}

/**
 * Request to validate a formula expression standalone (without committing).
 * Used by the settings UI to give live feedback while editing a formula.
 */
export interface ValidateFormulaRequest {
  entity?: CRMCustomFieldEntity;        // Default: "deal"
  formula: Record<string, unknown>;
}

/**
 * Response from the validate-formula endpoint.
 */
export interface ValidateFormulaResponse {
  is_valid: boolean;
  dependencies: string[];               // Sorted list of referenced field keys
}

/**
 * Response wrappers.
 */
export interface CustomFieldsResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: CustomFieldDefinitionItem[];
}

export interface CustomFieldResponse {
  status: 'SUCCESS' | 'ERROR';
  message: string;
  data: CustomFieldDefinitionItem;
}
```

### Frontend Utility: Build a deal renderer for a custom column

```ts
/**
 * Given a deal's properties blob and a list of definitions, return the
 * display value for a column. Handles missing definitions and missing
 * values gracefully.
 */
function renderCustomColumnValue(
  deal: { properties: Record<string, unknown> },
  definition: CustomFieldDefinitionItem,
): string | null {
  const raw = deal.properties[definition.key];
  if (raw === null || raw === undefined) return null;

  switch (definition.data_type) {
    case 'multiselect':
      return Array.isArray(raw) ? raw.join(', ') : String(raw);
    case 'boolean':
      return raw ? 'Yes' : 'No';
    case 'percentage':
      return `${raw}%`;
    default:
      return String(raw);
  }
}
```

### Frontend Utility: Detect a delete-blocked error

```ts
/**
 * The delete-in-use error is a 409 with a message of the form
 * "Cannot delete custom column '<key>': N deal(s) currently use this column..."
 * Surface the count to the user so they know exactly how much data they need
 * to clean up before retrying.
 */
function parseInUseError(message: string): { count: number } | null {
  const match = message.match(/(\d+) deal\(s\) currently use this column/);
  if (!match) return null;
  return { count: Number(match[1]) };
}
```

---

## Endpoints

### 1. List Custom Columns

```http
GET /users/crm/custom-fields
```

Returns custom column definitions for the authenticated user's organisation, sorted predefined-first then by `display_order` ascending (nulls last) then by `created_at`.

#### Query Parameters

| Parameter | Type | Default | Validation | Description |
|-----------|------|---------|------------|-------------|
| `entity` | enum | `deal` | One of: `deal` | Entity scope. Currently only `deal` is supported. |
| `include_inactive` | boolean | `false` | — | If `true`, also returns columns with `is_active=false` (e.g. archived columns shown on the settings page). |

#### Example Request

```http
GET /users/crm/custom-fields?entity=deal&include_inactive=false
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Custom fields retrieved",
  "data": [
    {
      "field_id": "c1d2e3f4-a5b6-7890-cdef-1234567890ab",
      "entity": "deal",
      "key": "priority",
      "label": "Priority",
      "data_type": "select",
      "required": false,
      "is_predefined": false,
      "is_active": true,
      "display_order": 1,
      "description": "Sales rep's assessment of deal urgency",
      "options": { "values": ["High", "Medium", "Low"] },
      "validation": {},
      "formula": null,
      "created_at": "2026-04-08T10:00:00Z",
      "updated_at": "2026-04-08T10:00:00Z"
    },
    {
      "field_id": "d2e3f4a5-b6c7-8901-defa-234567890abc",
      "entity": "deal",
      "key": "renewal_risk_score",
      "label": "Renewal Risk Score",
      "data_type": "percentage",
      "required": false,
      "is_predefined": false,
      "is_active": true,
      "display_order": 2,
      "description": null,
      "options": {},
      "validation": {},
      "formula": null,
      "created_at": "2026-04-08T10:05:00Z",
      "updated_at": "2026-04-08T10:05:00Z"
    }
  ]
}
```

**Empty organisation:** Returns `data: []` with message `"Custom fields retrieved"`. This is **not** an error.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `429` | `"Too many requests (per-minute limit exceeded)"` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 2. Create Custom Column

```http
POST /users/crm/custom-fields
```

Creates a new custom column under the calling user's organisation. Validates that the key is unique (case-sensitive lowercase, after normalisation), the data type is supported, and any type-specific options are present.

#### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` |
| `Content-Type` | Yes | `application/json` |

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `entity` | enum | No | One of: `deal` | Entity scope. Default `deal`. |
| `key` | string | **Yes** | 2-63 chars after normalisation; matches `^[a-z][a-z0-9_]{1,62}$` | Machine slug. The backend strips a leading `custom_` prefix if present, lowercases, and replaces non-`[a-z0-9_]` with underscores. |
| `label` | string | **Yes** | 1-255 chars, trimmed | Display name shown in the UI |
| `data_type` | enum | No | See data type table | Default: `text` |
| `required` | boolean | No | — | Default: `false`. Whether this column is required on deal create/update. |
| `is_predefined` | boolean | No | — | **Ignored.** Server always sets to `false` regardless of input. |
| `display_order` | integer | No | — | Sort hint for the settings UI |
| `description` | string | No | Max 500 chars | Admin-facing explanation |
| `options` | object | Conditional | See below | Type-specific config. Required for `select` / `multiselect`. |
| `validation` | object | No | — | Type-specific validation rules. May be empty. |
| `formula` | object | Conditional | Required when `data_type=formula`; forbidden otherwise | Formula DSL expression |

#### Type-specific validation

| `data_type` | Required `options` | Required `formula` |
|-------------|--------------------|---------------------|
| `text`, `integer`, `number`, `currency`, `percentage`, `boolean`, `date`, `datetime` | None | Must be omitted or null |
| `select`, `multiselect` | `options.values` must be a non-empty array of strings | Must be omitted or null |
| `formula` | None | Must be a JSON object using the formula DSL; all referenced keys must already exist in the org |

#### Auto-resolution behaviours

- **`key` normalisation**: Leading `custom_` stripped → lowercased → hyphens and spaces become underscores → consecutive underscores collapsed → leading/trailing underscores trimmed. Result must match `^[a-z][a-z0-9_]{1,62}$` or the request is rejected with 400.
- **`label` trimming**: Leading/trailing whitespace removed; empty result is rejected with 400.
- **`is_predefined` override**: Always set to `false` server-side. Predefined columns are seeded by admin scripts only.

#### Example Request — simple text column

```json
{
  "entity": "deal",
  "key": "priority",
  "label": "Priority",
  "data_type": "select",
  "options": { "values": ["High", "Medium", "Low"] },
  "description": "Sales rep's assessment of deal urgency"
}
```

#### Example Request — formula column

```json
{
  "entity": "deal",
  "key": "expected_revenue",
  "label": "Expected Revenue",
  "data_type": "formula",
  "formula": {
    "op": "mul",
    "args": [
      { "field": "amount" },
      { "field": "probability" }
    ]
  }
}
```

#### Success Response (201)

```json
{
  "status": "SUCCESS",
  "message": "Custom column created",
  "data": {
    "field_id": "c1d2e3f4-a5b6-7890-cdef-1234567890ab",
    "entity": "deal",
    "key": "priority",
    "label": "Priority",
    "data_type": "select",
    "required": false,
    "is_predefined": false,
    "is_active": true,
    "display_order": null,
    "description": "Sales rep's assessment of deal urgency",
    "options": { "values": ["High", "Medium", "Low"] },
    "validation": {},
    "formula": null,
    "created_at": "2026-04-09T08:00:00Z",
    "updated_at": "2026-04-09T08:00:00Z"
  }
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Column key must start with a letter and contain only lowercase letters, digits, and underscores (max 63 chars)"` | Key fails the regex after normalisation |
| `400` | `"Column key cannot be empty"` | Key is empty after normalisation |
| `400` | `"Column label cannot be empty"` | Label is whitespace-only |
| `400` | `"Select and multiselect columns require a non-empty options.values list"` | `data_type` is `select`/`multiselect` and `options.values` is missing or empty |
| `400` | `"Formula payload is only allowed when data_type='formula'"` | Caller passed `formula` for a non-formula type |
| `400` | `"Formula columns require a formula payload"` | `data_type=formula` but no `formula` body |
| `400` | `"Formula cannot reference its own column key"` | The formula references the column being created |
| `400` | `"Formula references unknown columns: <list>"` | Formula references keys that don't exist in the org |
| `400` | `"Unsupported formula op '<op>'"` | Op is not in `add/sub/mul/div/concat/coalesce/if` |
| `409` | `"A custom column with key 'priority' already exists"` | Non-deleted column with the same key already exists in this org |
| `422` | Pydantic validation error | Missing `key`/`label`, invalid types, out-of-range values |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 3. Update Custom Column

```http
PATCH /users/crm/custom-fields/{field_id}
```

Partial update — only send fields you want to change. Omitted fields are untouched. **Renaming the `label` does NOT touch any deal rows** (see Data Relationships → Rename behaviour).

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `field_id` | UUID | Yes | Custom column identifier |

#### Request Body

All fields optional. Only send what you want to change. The schema does **not** expose `key`, `data_type`, `entity`, or `is_predefined` — those are immutable.

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `label` | string | 1-255 chars, trimmed; cannot be empty | New display name |
| `required` | boolean | — | Whether this column is required on deal create/update |
| `is_active` | boolean | — | Hide from active use without deleting (set `false` to archive) |
| `display_order` | integer or null | — | Sort hint for the settings UI |
| `description` | string or null | Max 500 chars | Admin-facing explanation |
| `options` | object | If column is `select`/`multiselect`, `options.values` must remain a non-empty list | Type-specific config |
| `validation` | object | — | Type-specific validation rules |
| `formula` | object | Allowed only when the column's `data_type=formula`; revalidated against current org keys | Formula DSL expression |

#### Example Request — rename only

```json
{
  "label": "Deal Priority"
}
```

After this request, every deal list view will show "Deal Priority" as the column header on its next refetch. No deal rows are touched.

#### Example Request — update select options

```json
{
  "options": {
    "values": ["Critical", "High", "Medium", "Low"]
  }
}
```

#### Example Request — archive (hide without deleting)

```json
{
  "is_active": false
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Custom column updated",
  "data": {
    "field_id": "c1d2e3f4-a5b6-7890-cdef-1234567890ab",
    "entity": "deal",
    "key": "priority",
    "label": "Deal Priority",
    "data_type": "select",
    "required": false,
    "is_predefined": false,
    "is_active": true,
    "display_order": null,
    "description": "Sales rep's assessment of deal urgency",
    "options": { "values": ["High", "Medium", "Low"] },
    "validation": {},
    "formula": null,
    "created_at": "2026-04-09T08:00:00Z",
    "updated_at": "2026-04-09T08:30:00Z"
  }
}
```

**Frontend note:** Invalidate the **definitions** query cache after a successful PATCH so the UI picks up the new label. The deals query cache does **not** need invalidation — deal data is unchanged.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Column label cannot be empty"` | Label is whitespace-only |
| `400` | `"Select and multiselect columns require a non-empty options.values list"` | Updating options for a select column to empty/missing values |
| `400` | `"Formula payload is only allowed when data_type='formula'"` | Sent `formula` on a non-formula column |
| `400` | `"Formula cannot reference its own column key"` | Formula update references this same column |
| `400` | `"Formula references unknown columns: <list>"` | Formula update references missing keys |
| `404` | `"Custom column not found"` | No non-deleted column with this id in this org |
| `422` | Pydantic validation error | Invalid field types in body |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 4. Delete Custom Column

```http
DELETE /users/crm/custom-fields/{field_id}
```

Soft-deletes a custom column (sets `is_deleted = true` and `is_active = false`). **Blocked when any non-deleted deal in the organisation still has the column's key in its `properties` JSONB.**

This is the protection that prevents accidental data loss. There is no migration path — callers must clear the column's value from those deals first, then retry.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `field_id` | UUID | Yes | Custom column identifier |

#### Preconditions

| Check | Result on failure |
|-------|-------------------|
| Column exists and `is_deleted = false` | `404 "Custom column not found"` |
| `is_predefined = false` | `409 "Predefined custom columns cannot be deleted."` |
| No non-deleted deals have `key` in `properties` JSONB | `409 "Cannot delete custom column '<key>': N deal(s) currently use this column. Remove the column data from those deals first."` |

The deal count is computed at request time and included in the error message so the frontend can show it directly to the user.

#### Example Request

```http
DELETE /users/crm/custom-fields/c1d2e3f4-a5b6-7890-cdef-1234567890ab
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Custom column deleted",
  "data": null
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Custom column not found"` | Column doesn't exist, is soft-deleted, or belongs to a different org |
| `409` | `"Predefined custom columns cannot be deleted."` | Target has `is_predefined=true` |
| `409` | `"Cannot delete custom column 'priority': 7 deal(s) currently use this column. Remove the column data from those deals first."` | At least one non-deleted deal has the column's `key` in its `properties` |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 5. Validate Formula

```http
POST /users/crm/custom-fields/validate-formula
```

Standalone validation of a formula expression — useful for the settings UI to give live feedback as the user edits a formula, before they commit a create/update. Does not write anything.

#### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` |
| `Content-Type` | Yes | `application/json` |

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity` | enum | No | Default: `deal` |
| `formula` | object | **Yes** | Formula DSL expression to validate |

#### Example Request

```json
{
  "entity": "deal",
  "formula": {
    "op": "add",
    "args": [
      { "field": "amount" },
      { "op": "mul", "args": [{ "field": "amount" }, { "value": 0.1 }] }
    ]
  }
}
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Formula is valid",
  "data": {
    "is_valid": true,
    "dependencies": ["amount"]
  }
}
```

`dependencies` is a sorted list of all field keys the formula references — useful for the UI to show "this formula depends on: …".

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Formula must be a JSON object"` | Top-level formula is not an object |
| `400` | `"Formula nodes must be JSON objects"` | A nested node is not an object |
| `400` | `"Unsupported formula op '<op>'"` | Op is not in the supported set |
| `400` | `"Formula op args must be a list"` | `args` is not an array |
| `400` | `"Formula 'if' expects exactly 3 args"` | `if` op given wrong number of args |
| `400` | `"Formula '<op>' expects at least one arg"` | Non-`if` op given an empty args list |
| `400` | `"Formula references unknown columns: <list>"` | Referenced keys don't exist in the org |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

## Frontend: Settings Flows

The four user journeys this API powers, with the specific endpoint sequences and rules the frontend must respect.

### Flow A — Create a new custom column

```
1. User opens Settings → Custom Columns → "+ New Column"

2. Form collects:
   - key       (the user types a friendly name; frontend slugifies it for preview)
   - label     (defaults to the typed name; user can edit)
   - data_type (select dropdown of the 11 types)
   - For select/multiselect: a values editor that produces options.values
   - For formula: a formula builder (POST validate-formula on each edit for live feedback)

3. Submit:
   → POST /users/crm/custom-fields with the body
   → 201 success: refresh the columns list, show success toast
   → 409 duplicate key: show inline error on the key field
   → 400 type-specific: show error near the offending field
```

**Key frontend rules:**

1. **Preview the normalised key** — show the user what the slug will look like as they type the friendly name (`"Customer Tier!"` → `customer_tier`). Reduces "where did my column go?" surprises.
2. **`data_type` selector locks after creation** — immutable. Make it read-only on the edit form.
3. **`is_predefined` is not user-settable** — don't even show it on the create form.

### Flow B — Rename a custom column

```
1. User clicks the pencil icon next to a column on the settings page

2. Form pre-fills with current label

3. User changes label → submit
   → PATCH /users/crm/custom-fields/{field_id} with { "label": "New Name" }
   → 200 success
   → Invalidate the custom-fields query cache (so the UI re-fetches and shows the new label)
   → DO NOT invalidate the deals query cache — deal rows are unchanged
```

**Key frontend rules:**

1. **The label change shows up everywhere automatically** — no per-deal update needed. Tell the user "Renamed" not "Renaming…" — there's no async work.
2. **The key field is read-only** on the edit form. Slug never changes after creation.

### Flow C — Delete a custom column

```
1. User clicks the trash icon next to a column

2. Show confirmation modal: "Delete column 'Priority'? This cannot be undone."

3. User confirms
   → DELETE /users/crm/custom-fields/{field_id}
   → 200 success: column disappears from the list
   → 409 with "currently use this column": show the in-use error UX (below)
   → 409 "Predefined custom columns cannot be deleted.": show "This column is system-managed and cannot be removed"
```

#### Flow C.1 — In-use error UX

When the API returns:

```json
{
  "status": "ERROR",
  "message": "Cannot delete custom column 'priority': 7 deal(s) currently use this column. Remove the column data from those deals first.",
  "data": null
}
```

The frontend should:

1. Parse the deal count from the message (use the `parseInUseError` utility above).
2. Show a modal like:
   > **Can't delete this column yet**
   >
   > 7 deals currently have a value for "Priority". To delete the column, you need to remove its value from those deals first.
   >
   > [Show the 7 deals →]   [Cancel]
3. The "Show the 7 deals" button should navigate to the deals list filtered by `properties->priority IS NOT NULL` (or whatever the deals API supports for filtering by custom column presence).
4. After the user clears the values, they can come back and retry the delete.

This protects users from accidental data loss while giving them a clear path to clean up.

### Flow D — Archive (hide without deleting)

If a user wants to "remove" a column from active use but isn't ready to delete it (or can't because deals reference it), they can archive it:

```
1. User clicks "Archive" instead of "Delete"

2. → PATCH /users/crm/custom-fields/{field_id} with { "is_active": false }
   → 200 success
   → The column disappears from the active list (default GET) but stays in include_inactive=true views
```

Archived columns:
- Are no longer suggested in the deal-create / deal-edit UI.
- Are still rendered on existing deals that have a value (because the definition still exists, just inactive).
- Can be restored at any time via `PATCH { "is_active": true }`.

This is the "soft" alternative to delete — no data is lost, and the column can come back.

---

## Error Catalog

Consolidated reference of all error responses across all 5 endpoints.

| HTTP Status | Error Message | Endpoint(s) | Cause |
|-------------|---------------|-------------|-------|
| `400` | `"Column key must start with a letter and contain only lowercase letters, digits, and underscores (max 63 chars)"` | Create | Key fails normalisation regex |
| `400` | `"Column key cannot be empty"` | Create | Key empty after normalisation |
| `400` | `"Column key must be a string"` | Create | Key not a JSON string |
| `400` | `"Column label cannot be empty"` | Create, Update | Label whitespace-only |
| `400` | `"Column label must be a string"` | Create, Update | Label not a JSON string |
| `400` | `"Select and multiselect columns require a non-empty options.values list"` | Create, Update | Missing `options.values` for select-type column |
| `400` | `"Formula payload is only allowed when data_type='formula'"` | Create, Update | `formula` sent on a non-formula column |
| `400` | `"Formula columns require a formula payload"` | Create | `data_type=formula` but no `formula` body |
| `400` | `"Formula must be a JSON object"` | Create, Update, Validate | Formula not an object |
| `400` | `"Formula nodes must be JSON objects"` | Create, Update, Validate | A nested formula node not an object |
| `400` | `"Formula cannot reference its own column key"` | Create, Update | Formula references the column being saved |
| `400` | `"Formula references unknown columns: <list>"` | Create, Update, Validate | Formula references keys that don't exist |
| `400` | `"Unsupported formula op '<op>'"` | Create, Update, Validate | Op not in the supported set |
| `400` | `"Formula op args must be a list"` | Create, Update, Validate | `args` not an array |
| `400` | `"Formula 'if' expects exactly 3 args"` | Create, Update, Validate | `if` op with wrong arg count |
| `400` | `"Formula '<op>' expects at least one arg"` | Create, Update, Validate | Empty args list on a non-`if` op |
| `401` | `"Invalid or missing user credentials."` | All | Missing/invalid JWT token |
| `404` | `"Custom column not found"` | Update, Delete | Column doesn't exist, is soft-deleted, or belongs to a different org |
| `409` | `"A custom column with key '<key>' already exists"` | Create | Non-deleted column with the same key in this org |
| `409` | `"Predefined custom columns cannot be deleted."` | Delete | Target has `is_predefined=true` |
| `409` | `"Cannot delete custom column '<key>': N deal(s) currently use this column. Remove the column data from those deals first."` | Delete | At least one non-deleted deal has the key in `properties` |
| `422` | Pydantic validation error | Create, Update, Validate | Missing required fields, invalid types, out-of-range values |
| `429` | `"Too many requests (per-minute limit exceeded)"` | All | Per-minute rate limit exceeded |
| `429` | `"Too many requests (per-hour limit exceeded)"` | All | Per-hour rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | All | Unhandled exception |

---

## Backend Architecture Notes

> This section is for backend developers working on the service layer.

### Database

**Table:** `crm_custom_field_definitions` (tenant-scoped, in `org_*` schema)

**Key constraints:**

| Constraint | Columns | Purpose |
|------------|---------|---------|
| `uq_crm_custom_field_org_entity_key_state` | `(organisation_id, entity_type, key, is_deleted)` | Allows a soft-deleted column to coexist with a newly-created one with the same key |
| `ck_crm_custom_field_key_format` | CHECK `key ~ '^[a-z][a-z0-9_]{1,62}$'` | Defence-in-depth against bad keys reaching the DB |

**Key indexes:**

| Index | Columns | Purpose |
|-------|---------|---------|
| `ix_crm_custom_field_org_entity_active` | `(organisation_id, entity_type, is_active, is_deleted)` | Fast listing of active columns per org |
| (PK) | `field_id` | Primary key |

**Migration:** `migrations/org_migrations/versions/3c7f79637797_029_add_crm_custom_field_definitions.py`

### Where deal values live

Deal values for custom columns live inside `crm_deals.properties` (JSONB column, not nullable, server default `'{}'`). They are keyed by `field.key`. There is **no separate values table**. The deal-write path (and the bulk-import path) writes them as `properties[key] = parsed_value`.

This is why the `key` is the load-bearing identifier and the `label` is purely display:

```python
# How a value gets written (simplified, see deal_import_service.py:1302-1352)
props: dict[str, Any] = {}
for col_name, raw_value in row.items():
    match = _CUSTOM_PREFIX_RE.match(col_name.strip())
    if not match: continue
    key = match.group(1)
    definition = custom_fields_by_key.get(key)
    props[key] = self._coerce_custom_value(key=key, raw_value=raw_value, definition=definition)
deal.properties = props
```

### Service Layer

Two services exist for custom columns. They serve different paths and the user owns the decision of when to retire the import-side service.

**`CustomFieldWriteService`** (`src/service/crm/internal/custom_field_write_service.py`) — **the UI-facing CRUD service** that this API uses.

- **Methods**: `list_fields`, `get_field`, `create_field`, `update_field`, `delete_field`, `validate_formula`.
- **Strict** at every boundary: rejects empty/invalid keys, rejects type-incompatible options/formula, refuses to delete in-use columns, refuses to delete predefined columns, silently ignores immutability-violation attempts.
- **Owns its commits** — each public write method calls `flush()` then `commit()` then `refresh()` before returning. The handler does not need to commit.
- **Errors**: raises `CustomFieldWriteServiceError(message, status_code)` for all rule violations.

**`CustomFieldService`** (`src/service/crm/internal/custom_field_service.py`) — **the legacy bulk-import service**, used by `deal_import_handler.py:242-300`.

- Permissive: auto-creates definitions for unknown CSV columns, doesn't enforce the in-use guard, does not commit (caller-driven transaction control).
- Will be retired in a future cleanup PR. Until then, leave it alone — the import handler still depends on it.

### The delete-in-use SQL

The only new SQL introduced by this API:

```python
from sqlalchemy import func, select
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

stmt = select(func.count()).where(
    col(CRMDeal.organisation_id) == self.organisation_id,
    col(CRMDeal.is_deleted).is_(False),
    col(CRMDeal.properties).op("?")(field.key),
)
result = await self.session.execute(stmt)
deal_count = int(result.scalar() or 0)
```

The `?` operator is the PostgreSQL JSONB top-level key-existence test. It tests whether `properties` contains the given string as a top-level key — independent of the value (so `{"priority": null}` still counts as in-use). The WHERE filters by `organisation_id` first (btree index), so this stays cheap even without a GIN index on `properties`. If deal volume per org grows large, a partial functional index can be added later.

### Formula validation

Formulas use a JSON DSL with seven supported ops (`add`, `sub`, `mul`, `div`, `concat`, `coalesce`, `if`). The validator walks the formula tree, collects every `{ "field": "<key>" }` reference, and checks each referenced key against the org's currently-active definitions. Self-references and references to unknown keys are rejected at create/update time and at standalone-validate time.

The validator is deliberately simple — it does NOT type-check the formula (e.g., it doesn't reject `{ "op": "add", "args": [{ "field": "deal_name" }, { "field": "amount" }] }` even though that's a string + number). Type-checking is left to the runtime evaluator (when formula evaluation lands).

### Request handler

**`custom_fields_handler.py`** (`src/api/routers/crm/custom_fields_handler.py`)

One FastAPI dependency wires the service:

```python
async def get_custom_field_service(user_ids, session) -> AsyncIterator[CustomFieldWriteService]:
    service = CustomFieldWriteService(session=session, organisation_id=..., user_id=...)
    try:
        yield service
    finally:
        await service.close()
```

Each endpoint follows the same `try / except CustomFieldWriteServiceError / except Exception` pattern as the pipelines and contacts handlers. There is no per-endpoint commit — the service commits internally.

A small `_to_response_item()` helper converts the model row to the `CustomFieldDefinitionItem` Pydantic schema for the response.

---

## Related Documents

- `crm-deals-api.md` — Deals CRUD; deal `properties` JSONB carries custom column values
- `crm-deals-import-api.md` — CSV import flow that consumes the legacy `CustomFieldService`
- `crm-pipelines-api.md` — Standard pipeline/stage management (separate concern from custom columns)
