# CRM Deals API Documentation

## Base URL

```text
/users/crm
```

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope.

```json
{
  "status": "SUCCESS | ERROR",

  "message": "Human-readable message",

  "data": { ... } | null
}
```

---

## Authentication

- Required: Bearer token (`Authorization: Bearer <access_token>`)
- User identity (organisation, user, role) resolved from the token; clients never pass these explicitly

---

## Rate Limiting (defaults)

- Write endpoints (`POST`, `DELETE`): ~15/min, 100/hour per user
- Read endpoints (`GET`): ~60/min, 1000/hour per user

- On limit breach: `429 Too Many Requests` with `Retry-After`

---

## Deal Status Values

| Status   | Meaning                                     | UI Guidance                     |

|----------|---------------------------------------------|---------------------------------|
| `open`   | Deal is active and in progress              | Show as active pipeline item    |
| `won`    | Deal has been closed successfully           | Display with success indicator  |

| `lost`   | Deal has been closed unsuccessfully         | Show as closed/lost             |

| `closed` | Deal has been closed (generic)              | Mark as completed               |

---

## Data Types

### Enums

- `CRMDealStatus`: `"open" | "won" | "lost" | "closed"`

### Request Query Parameters (TypeScript)

```ts

type UUID = string;


export interface ListDealsParams {
  limit?: number;           // 1..100, default 50
  cursor?: string;          // opaque pagination cursor (ignored if fetch_all=true)
  fetch_all?: boolean;      // fetch all deals without pagination (max 500)

  statuses?: CRMDealStatus[];  // filter by deal status
  pipeline_ids?: UUID[];    // filter by pipeline UUIDs
  owner_external_ids?: string[];  // filter by owner external IDs

  min_amount?: number;      // filter by minimum deal amount
  max_amount?: number;      // filter by maximum deal amount
  min_close_date?: string;  // filter by minimum close date (ISO8601)
  max_close_date?: string;  // filter by maximum close date (ISO8601)

  q?: string;              // case-insensitive search on deal name
}
```

### Response Models (TypeScript)

```ts
export interface DealListItem {
  deal_id: UUID;

  external_id: string;

  name: string | null;
  amount: number | null;
  currency: string | null;
  deal_status: CRMDealStatus;

  probability: number | null;

  close_date: string | null;  // ISO8601
  stage_name: string | null;
  stage_label: string | null;
  pipeline_name: string | null;

  pipeline_label: string | null;

  owner_external_id: string | null;  // Required for owner_external_ids filter
  owner_name: string | null;
  owner_email: string | null;
  company_external_id: string | null;
  company_name: string | null;

  company_domain: string | null;

  primary_contact_external_id: string | null;
  primary_contact_name: string | null;
  primary_contact_email: string | null;
  created_at: string;  // ISO8601

  updated_at: string;  // ISO8601

  synced_at: string;   // ISO8601
}

export interface DealsPage {

  items: DealListItem[];
  cursor: string | null;  // null if no more results
}


export interface DealDetail {
  // Identity
  deal_id: UUID;
  external_id: string;

  
  // Core business context
  name: string | null;

  amount: number | null;
  currency: string | null;
  amount_home_currency: number | null;
  deal_status: CRMDealStatus;
  probability: number | null;
  predicted_amount: number | null;
  close_date: string | null;  // ISO8601
  expected_close_date: string | null;  // ISO8601
  closed_won_at: string | null;  // ISO8601
  closed_lost_at: string | null;  // ISO8601

  closed_reason: string | null;
  
  // Sales process snapshot
  stage_external_id: string | null;
  stage_name: string | null;
  stage_label: string | null;
  pipeline_external_id: string | null;
  pipeline_name: string | null;
  pipeline_label: string | null;
  pipeline_id: UUID | null;
  stage_id: UUID | null;
  forecast_category: string | null;
  deal_type: string | null;
  priority: string | null;
  lead_source: string | null;
  
  // Ownership & associations
  owner_external_id: string | null;
  owner_name: string | null;
  owner_email: string | null;
  company_external_id: string | null;
  company_name: string | null;
  company_domain: string | null;
  primary_contact_external_id: string | null;
  primary_contact_name: string | null;
  primary_contact_email: string | null;
  
  // Activity & timing
  source_created_at: string | null;  // ISO8601
  last_activity_at: string | null;  // ISO8601
  stage_entered_at: string | null;  // ISO8601
  stage_updated_at: string | null;  // ISO8601
  synced_at: string;  // ISO8601
  source_modified_at: string | null;  // ISO8601

  last_seen_at: string | null;  // ISO8601
  
  // Freeform details
  description: string | null;
  
  // Integration metadata
  integration_id: UUID;

  organisation_id: UUID;
  
  // Timestamps
  created_at: string;  // ISO8601
  updated_at: string;  // ISO8601
  context: DealContext | null;

  // Deal Summary (AI-generated)
  deal_summary_status: string | null;  // NOT_STARTED, IN_PROGRESS, COMPLETED, FAILED
  deal_summary_data: Record<string, unknown> | null;  // AI-generated summary data

  // Active deal-level jobs (for SSE reconnection)
  active_deal_jobs: DealSummaryJobsStatus | null;
}

// Active deal-level jobs status
export interface DealSummaryJobsStatus {
  deal_summary: DealJobResponse | null;  // Active deal summary job, if any
  // Future: stakeholder_analysis, deal_risk_analysis, etc.
}

// Job response for active job tracking
export interface DealJobResponse {
  job_id: UUID;
  job_type: string;
  status: string;  // PENDING, IN_PROGRESS, COMPLETED, FAILED
}

export interface DealContext {
  pipeline: DealPipelineContext | null;
  timeline: DealTimelineContext | null;
  financials: DealFinancialContext | null;
  associations: DealAssociationsContext | null;
  stage_history: DealStageHistoryItem[] | null;
  metrics: Record<string, unknown> | null;
  properties: Record<string, unknown> | null;
  health: string | null;
  time_tracked: string | null;
}

export interface PipelineStage {
  stage_id: UUID;
  external_id: string;
  name: string;
  label: string | null;
  display_order: number | null;
  probability: number | null;
  is_closed: boolean;
  
  // Merged history fields
  status: "upcoming" | "active" | "completed" | "skipped";
  entered_at: string | null;    // ISO8601
  exited_at: string | null;     // ISO8601
  duration_days: number | null;
}

export interface DealPipelineContext {
  pipeline_id: UUID | null;
  pipeline_external_id: string | null;
  pipeline_name: string | null;
  pipeline_label: string | null;

  stage_id: UUID | null;
  stage_external_id: string | null;
  stage_name: string | null;
  stage_label: string | null;
  forecast_category: string | null;
  deal_type: string | null;
  priority: string | null;

  lead_source: string | null;
  stages: PipelineStage[] | null;
}

export interface DealTimelineContext {
  source_created_at: string | null;
  close_date: string | null;
  expected_close_date: string | null;
  closed_won_at: string | null;
  closed_lost_at: string | null;
  last_activity_at: string | null;
  stage_entered_at: string | null;
  stage_updated_at: string | null;
  synced_at: string | null;
  source_modified_at: string | null;
  last_seen_at: string | null;
  days_to_close: number | null;
  deal_age_days: number | null;
}

export interface DealFinancialContext {
  amount: number | null;
  currency: string | null;
  amount_home_currency: number | null;
  predicted_amount: number | null;
  probability: number | null;

}

export interface DealStageHistoryItem {
  history_id: UUID;
  stage_external_id: string | null;
  stage_name: string | null;
  stage_label: string | null;
  from_stage_external_id: string | null;
  entered_at: string;           // ISO8601
  exited_at: string | null;     // ISO8601, null if still in this stage
  duration_days: number | null; // Whole days spent in this stage (approx.)
  amount_at_stage: number | null;
  probability_at_stage: number | null;
  source: string | null;        // e.g. "hubspot", "system"
}

export interface DealAssociationsContext {
  owner_external_id: string | null;
  owner_name: string | null;
  owner_email: string | null;
  technical_owner_external_id: string | null;
  technical_owner_name: string | null;
  technical_owner_email: string | null;
  company_external_id: string | null;
  company_name: string | null;
  company_domain: string | null;
  company_industry: string | null;
  primary_contact_external_id: string | null;

  primary_contact_name: string | null;

  primary_contact_email: string | null;
}

export interface DealsStats {
  total: number;
  by_status: Record<string, number>;
  by_pipeline: Record<string, number>;
  total_amount: number | null;
  average_amount: number | null;
}

// Standard envelope used by all endpoints
export interface ApiEnvelope<T> {
  status: "SUCCESS" | "ERROR";
  message: string;
  data: T | null;
}
```

---

## Production Conventions

### Authentication Requirements

- Bearer JWT in `Authorization: Bearer <token>`
- All operations are scoped to the authenticated user's organisation

### Content Type

- Request: `Content-Type: application/json`
- Response: `Content-Type: application/json`

### Pagination

- Cursor-based keyset pagination for efficient traversal of large datasets
- Treat `cursor` as opaque and pass back verbatim
- Cursors encode `(created_at, deal_id)` for consistent ordering

### Enrichment & Optional Fields

- `owner_name`/`owner_email` are denormalized from the users directory when available; otherwise null.
- `company_name`/`company_domain` and `primary_contact_name`/`primary_contact_email` are populated via enrichment after deal sync; they may be null on the first read and appear after enrichment completes.
- Stage IDs (`stage_external_id`) reflect HubSpot's internal slugs (e.g., `qualifiedtobuy`, `appointmentscheduled`). Use `stage_label` for display text.

### Soft Deletes

- The API excludes soft-deleted (archived) deals by default (`is_deleted = false`). Deleted/archived deals are still ingested for internal lineage and history.

### Stage History

- Stage transitions are derived from HubSpot `propertiesWithHistory` and stored internally for analytics. A public endpoint for stage history may be added later; contact us if you need it.

### Time & Timestamps

- ISO 8601 UTC with `Z` suffix
- Example: `2025-10-25T10:30:00Z`

### Error Model

- Envelope always present with HTTP status aligned to error:
  - 400: invalid input (bad cursor, invalid filter)
  - 401: missing/invalid token
  - 403: forbidden (invalid user context)
  - 404: resource not found (deal not found)
  - 429: rate limited
  - 500: unhandled server error

```json
{
  "status": "ERROR",
  "message": "Human-readable reason",
  "data": null
}
```

### Security & Tenancy

- All operations are evaluated against the authenticated `organisation_id`
- Queries exclude soft-deleted items (`is_deleted = false`)
- No cross-organisation access is permitted

### Retry & Backoff Recommendations

- Reads (list/get): retry with exponential backoff (200ms → 400ms → 800ms; jitter) up to 3 attempts
- On `429`: respect `Retry-After` header
- On `5xx`: retry with backoff, cap at 3 attempts

---

## Security & Compliance (SOC 2 Alignment)

### Transport Security

- Enforce TLS 1.2+; recommend HSTS at the edge
- All API access over HTTPS only
- CORS: restrict in production to trusted origins

### Authentication & Authorization

- Bearer JWT on every call
- Endpoints enforce organisation scoping for all reads
- Future: role-based authorization for admin vs user scopes

### Input Validation & Integrity

- Pydantic validation rejects invalid UUIDs, negative limits, malformed cursors
- Cursor-based pagination guards against numeric enumeration
- Query string filters validated for correct enum values

### Logging & Auditability

- Log request outcomes and resource IDs (deal_id, organisation_id)
- Redact potentially sensitive fields; apply log retention and access controls
- Track access patterns for security monitoring

### Availability & Resilience

- Per-user rate limiting (read budgets) with `429` + `Retry-After`
- Graceful degradation on database connection issues
- Connection pooling and timeouts prevent resource exhaustion

### Privacy & Data Handling

- Deal data is confidential business information
- Avoid logging deal content or amounts
- Support future purge/retention policies at the org level

### Change Management & Versioning

- Document non-breaking additions
- Coordinate breaking changes with clients
- Maintain backward compatibility for response schemas

---

## 1. List Deals

### Endpoint for List Deals

```http
# Paginated (default)
GET /users/crm/deals?limit=50&cursor=<opaque>&statuses=open&statuses=won&q=enterprise

# Fetch all deals (recommended for <300 deals)
GET /users/crm/deals?fetch_all=true&statuses=open
```

> **Note:** Always call `/users/crm/deals` with no trailing slash. FastAPI normalizes routes and will respond with a `307 Temporary Redirect` when the request includes an extra `/`, so use the canonical path to avoid the extra hop.

### Description for List Deals

Returns a list of CRM deals for the current organisation with optional filters. Supports two modes:

- **Pagination Mode (default):** Cursor-based pagination for large datasets
- **Fetch All Mode:** Set `fetch_all=true` to retrieve all deals in one request (max 500) — ideal for small datasets where client-side filtering is preferred

Ordered by `created_at DESC, deal_id DESC` for consistent results.

### Query Parameters for List Deals

| Parameter            | Type           | Required | Description                                                       |
|---------------------|----------------|----------|-------------------------------------------------------------------|
| `limit`             | integer        | No       | Items per page (1..100, default 50). Ignored if `fetch_all=true`  |
| `cursor`            | string         | No       | Opaque cursor from previous response. Ignored if `fetch_all=true` |
| `fetch_all`         | boolean        | No       | Fetch all deals in one request (max 500). Default: `false`        |
| `statuses`          | array&lt;string&gt;  | No       | Filter by status (`open`, `won`, `lost`, `closed`; repeat param) |
| `pipeline_ids`      | array&lt;UUID&gt;    | No       | Filter by pipeline UUIDs (repeat parameter)                       |
| `owner_external_ids`| array&lt;string&gt;  | No       | Filter by owner external IDs from CRM (repeat parameter)          |
| `min_amount`        | number         | No       | Filter by minimum deal amount                                     |
| `max_amount`        | number         | No       | Filter by maximum deal amount                                     |
| `min_close_date`    | string         | No       | Filter by minimum close date (ISO8601)                            |
| `max_close_date`    | string         | No       | Filter by maximum close date (ISO8601)                            |
| `q`                 | string         | No       | Case-insensitive substring match on deal name                     |

### Success Response (200) for List Deals

```json
{
  "status": "SUCCESS",
  "message": "Deals retrieved successfully",
  "data": {
    "items": [
      {
        "deal_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "external_id": "deal_12345",
        "name": "Enterprise Software License",
        "amount": 250000.00,
        "currency": "USD",
        "deal_status": "open",
        "probability": 75.0,
        "close_date": "2025-12-31T23:59:59Z",
        "stage_name": "Negotiation",
        "stage_label": "Contract Review",
        "pipeline_name": "Sales Pipeline",
        "pipeline_label": "Standard Sales Process",
        "owner_name": "Alex Fin",
        "owner_email": "alex.fin@example.com",
        "company_external_id": "company_789",
        "primary_contact_external_id": "contact_456",
        "created_at": "2025-10-01T08:00:00Z",
        "updated_at": "2025-10-24T15:30:00Z",
        "synced_at": "2025-10-25T10:00:00Z"
      }
    ],
    "cursor": "eyJjcmVhdGVkX2F0IjoiMjAyNS0xMC0wMVQwODowMDowMFoiLCJkZWFsX2lkIjoiZjQ3YWMxMGItNThjYy00MzcyLWE1NjctMGUwMmIyYzNkNDc5In0="
  }
}
```

### Notes for List Deals

- **Fetch All Mode (`fetch_all=true`):**
  - Returns all matching deals in a single response (max 500)
  - `cursor` is always `null` (no pagination)
  - Recommended for datasets <300 deals for instant client-side filtering
  - All filters still apply server-side

- **Pagination Mode (default):**
  - Empty `items` array with `cursor: null` indicates no results
  - Cursor is `null` when no more pages exist
  - Pass `cursor` from previous response to fetch next page

- All filters are ANDed together
- Multiple values for the same filter (e.g., multiple statuses) are ORed

### Error Responses for List Deals

- `400 Bad Request` — invalid cursor or filter values

  ```json
  {
    "status": "ERROR",
    "message": "Invalid cursor",
    "data": null
  }
  ```

- `429 Too Many Requests` — rate limit exceeded
- `500 Internal Server Error` — unexpected error

  ```json
  {
    "status": "ERROR",
    "message": "An unexpected error occurred while processing your request",
    "data": null
  }
  ```

### Example cURL for List Deals

```bash
# Paginated request
curl -X GET "$BASE_URL/users/crm/deals?limit=50&statuses=open&statuses=won&q=enterprise" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"

# Fetch all deals (for client-side filtering)
curl -X GET "$BASE_URL/users/crm/deals?fetch_all=true" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

### Example TypeScript for List Deals

```typescript
async function listDeals(params: ListDealsParams): Promise<DealsPage> {
  const queryParams = new URLSearchParams();
  
  // Fetch all mode (recommended for small datasets)
  if (params.fetch_all) {
    queryParams.append('fetch_all', 'true');
  } else {
    // Pagination mode
    if (params.limit) queryParams.append('limit', params.limit.toString());
    if (params.cursor) queryParams.append('cursor', params.cursor);
  }
  
  // Filters (work with both modes)
  if (params.statuses) params.statuses.forEach(s => queryParams.append('statuses', s));
  if (params.pipeline_ids) params.pipeline_ids.forEach(id => queryParams.append('pipeline_ids', id));
  if (params.owner_external_ids) params.owner_external_ids.forEach(id => queryParams.append('owner_external_ids', id));
  if (params.q) queryParams.append('q', params.q);
  
  const response = await fetch(`${BASE_URL}/users/crm/deals?${queryParams}`, {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json'
    }
  });
  
  const envelope: ApiEnvelope<DealsPage> = await response.json();
  if (envelope.status === 'ERROR') throw new Error(envelope.message);
  return envelope.data!;
}

// Example: Fetch all deals for client-side filtering
async function getAllDealsForFiltering(): Promise<DealListItem[]> {
  const page = await listDeals({ fetch_all: true });
  return page.items; // All deals, filter client-side
}
```

---

## 2. Get Deal Statistics

### Endpoint for Get Deal Statistics

```http
GET /users/crm/deals/stats
```

### Description for Get Deal Statistics

Returns aggregate statistics for all CRM deals in the organisation. Useful for dashboard widgets and overview cards.

### Authentication for Get Deal Statistics

Required

### Success Response (200) for Get Deal Statistics

```json
{
  "status": "SUCCESS",
  "message": "Statistics retrieved successfully",
  "data": {
    "total": 1247,
    "by_status": {
      "open": 823,
      "won": 312,
      "lost": 98,
      "closed": 14
    },
    "by_pipeline": {
      "Sales Pipeline": 945,
      "Partner Pipeline": 234,
      "Enterprise Pipeline": 68
    },
    "total_amount": 45678900.50,
    "average_amount": 36621.34
  }
}
```

### Response Fields

| Field            | Type                  | Description                                          |
|------------------|-----------------------|------------------------------------------------------|
| `total`          | integer               | Total count of active deals (excludes soft-deleted)  |
| `by_status`      | object                | Count grouped by deal status                         |
| `by_pipeline`    | object                | Count grouped by pipeline name                       |
| `total_amount`   | number \| null        | Sum of all deal amounts (null if no amounts)         |
| `average_amount` | number \| null        | Average deal amount (null if no amounts)             |

### Notes for Get Deal Statistics

- Amounts are calculated from deals with non-null `amount` values
- Pipelines without names are excluded from `by_pipeline`
- Statistics exclude soft-deleted deals
- Currency is not normalized; `total_amount` and `average_amount` mix currencies

### Error Responses for Get Deal Statistics

- `429 Too Many Requests` — rate limit exceeded
- `500 Internal Server Error` — unexpected error

### Example cURL for Get Deal Statistics

```bash
curl -X GET "$BASE_URL/users/crm/deals/stats" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

### Example TypeScript for Get Deal Statistics

```typescript
async function getDealsStats(): Promise<DealsStats> {
  const response = await fetch(`${BASE_URL}/users/crm/deals/stats`, {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json'
    }
  });
  
  const envelope: ApiEnvelope<DealsStats> = await response.json();
  if (envelope.status === 'ERROR') throw new Error(envelope.message);
  return envelope.data!;
}
```

---

## 3. Get Deal Details

### Endpoint for Get Deal Details

```http
GET /users/crm/deals/{deal_id}
```

### Description for Get Deal Details

Retrieves full details for a specific CRM deal by its internal UUID. Returns all available fields including metadata, associations, activity timeline, AI-generated deal summary (if completed), and active deal-level jobs (for SSE reconnection).

### Authentication for Get Deal Details

Required

### Path Parameters for Get Deal Details

| Parameter | Type | Required | Description                 |
|-----------|------|----------|-----------------------------|
| `deal_id` | UUID | Yes      | Internal deal UUID          |

### Success Response (200) for Get Deal

```json
{
  "status": "SUCCESS",
  "message": "Deal retrieved successfully",
  "data": {
    "deal_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "external_id": "deal_12345",
    "name": "Enterprise Software License",
    "amount": 250000.00,
    "currency": "USD",
    "amount_home_currency": 250000.00,
    "deal_status": "open",
    "probability": 75.0,
    "predicted_amount": 187500.00,
    "close_date": "2025-12-31T23:59:59Z",
    "expected_close_date": "2025-12-15T23:59:59Z",
    "closed_won_at": null,
    "closed_lost_at": null,
    "closed_reason": null,
    "stage_external_id": "stage_001",
    "stage_name": "Negotiation",
    "stage_label": "Contract Review",
    "pipeline_external_id": "pipeline_001",
    "pipeline_name": "Sales Pipeline",
    "pipeline_label": "Standard Sales Process",
    "pipeline_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
    "stage_id": "b2c3d4e5-f6a7-4b5c-9d0e-1f2a3b4c5d6e",
    "forecast_category": "Best Case",
    "deal_type": "New Business",
    "priority": "High",
    "lead_source": "Inbound Marketing",
    "owner_external_id": "user_123",
    "owner_name": "Alex Fin",
    "owner_email": "alex.fin@example.com",
    "company_external_id": "company_789",
    "primary_contact_external_id": "contact_456",
    "source_created_at": "2025-09-15T10:00:00Z",
    "last_activity_at": "2025-10-24T15:30:00Z",
    "stage_entered_at": "2025-10-20T09:00:00Z",
    "stage_updated_at": "2025-10-24T15:30:00Z",
    "synced_at": "2025-10-25T10:00:00Z",
    "source_modified_at": "2025-10-24T15:30:00Z",
    "last_seen_at": "2025-10-25T10:00:00Z",
    "description": "Annual enterprise software license for 500 users with premium support",
    "integration_id": "c3d4e5f6-a7b8-4c5d-9e0f-1a2b3c4d5e6f",
    "organisation_id": "d4e5f6a7-b8c9-4d5e-0f1a-2b3c4d5e6f7a",
    "created_at": "2025-10-01T08:00:00Z",
    "updated_at": "2025-10-24T15:30:00Z",
    "context": {
      "pipeline": {
        "pipeline_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
        "pipeline_label": "Standard Sales Process",
        "stage_label": "Contract Review",
        "stages": [
          {
            "stage_id": "b2c3d4e5-f6a7-4b5c-9d0e-1f2a3b4c5d6e",
            "external_id": "stage_001",
            "name": "Discovery",
            "label": "Discovery",
            "display_order": 0,
            "probability": 0.1,
            "is_closed": false,
            "status": "completed",
            "entered_at": "2025-05-12T00:00:00Z",
            "exited_at": "2025-05-24T00:00:00Z",
            "duration_days": 12
          },
          {
            "stage_id": "c3d4e5f6-a7b8-4c5d-9e0f-1a2b3c4d5e6f",
            "external_id": "stage_002",
            "name": "Technical Validation",
            "label": "Technical Validation",
            "display_order": 1,
            "probability": 0.4,
            "is_closed": false,
            "status": "active",
            "entered_at": "2025-05-24T00:00:00Z",
            "exited_at": null,
            "duration_days": 25
          }
        ]
      },
      "timeline": {
        "source_created_at": "2025-09-15T10:00:00Z",
        "last_activity_at": "2025-10-24T15:30:00Z",
        "days_to_close": 13,
        "deal_age_days": 40
      },
      "financials": {
        "amount": 250000.00,
        "currency": "USD",
        "probability": 75.0
      },
      "associations": {
        "owner_name": "Alex Fin",
        "owner_email": "alex.fin@example.com",
        "technical_owner_name": "Mrinmoy N",
        "technical_owner_email": null,
        "company_name": "Acme Corp",
        "company_industry": "Cyber Security"
      },
      "stage_history": [
        {
          "history_id": "h1",
          "stage_label": "Discovery",
          "entered_at": "2025-05-12T00:00:00Z",
          "exited_at": "2025-05-24T00:00:00Z",
          "duration_days": 12
        },
        {
          "history_id": "h2",
          "stage_label": "Technical Validation",
          "entered_at": "2025-05-24T00:00:00Z",
          "exited_at": null,
          "duration_days": 25
        }
      ],
      "metrics": {
        "health_status": "STALLED",
        "time_tracked_hours": 47
      },
      "properties": {
        "industry": "Cyber Security"
      },
      "health": "STALLED",
      "time_tracked": "47 hours"
    },
    "deal_summary_status": "COMPLETED",
    "deal_summary_data": {
      "schema_version": "1.0.0",
      "generated_at": "2025-10-25T10:30:00Z",
      "data": {
        "overview": "Enterprise software license deal with Acme Corp...",
        "key_stakeholders": ["Alex Fin", "John Smith"],
        "next_steps": ["Schedule technical review", "Send proposal"],
        "risks": ["Timeline concerns", "Budget constraints"]
      }
    },
    "active_deal_jobs": null
  }
}
```

### Notes for Get Deal Details

- **Deal Summary**: When `deal_summary_status` is `COMPLETED`, the `deal_summary_data` field contains AI-generated summary data including overview, key stakeholders, next steps, and risks.
- **Active Jobs**: The `active_deal_jobs` field contains any currently running deal-level jobs (e.g., deal summary generation). Use this for SSE reconnection - if a job is `IN_PROGRESS`, subscribe to the SSE stream for real-time updates.
- **Job Statuses**: `PENDING` (queued), `IN_PROGRESS` (running), `COMPLETED` (done), `FAILED` (error).

### Error Responses for Get Deal Details

- `404 Not Found` — deal does not exist or access denied

  ```json
  {
    "status": "ERROR",
    "message": "Deal not found",
    "data": null
  }
  ```

- `429 Too Many Requests` — rate limit exceeded
- `500 Internal Server Error` — unexpected error

### Example cURL for Get Deal Details

```bash
curl -X GET "$BASE_URL/users/crm/deals/f47ac10b-58cc-4372-a567-0e02b2c3d479" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

### Example TypeScript for Get Deal Details

```typescript
async function getDealById(dealId: string): Promise<DealDetail> {
  const response = await fetch(`${BASE_URL}/users/crm/deals/${dealId}`, {
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json'
    }
  });
  
  if (response.status === 404) {
    throw new Error('Deal not found');
  }
  
  const envelope: ApiEnvelope<DealDetail> = await response.json();
  if (envelope.status === 'ERROR') throw new Error(envelope.message);
  return envelope.data!;
}
```

---

## Error Codes Summary

| HTTP Code | Reason                                              | Suggested Action                         |
|-----------|------------------------------------------------------|------------------------------------------|
| 400       | Validation failure (invalid cursor, bad filter)      | Fix request parameters                   |
| 401       | Missing/invalid token                                | Ensure valid bearer token                |
| 403       | Access denied (cross-org access)                     | Confirm user permissions                 |
| 404       | Deal not found                                       | Verify `deal_id` exists                  |
| 429       | Rate limit exceeded                                  | Respect `Retry-After` header             |
| 500       | Unexpected server error                              | Retry with backoff; contact support      |

---

## Frontend Integration Notes

### Typical Flow

1. **Dashboard Overview**
   - Call `GET /users/crm/deals/stats` to populate dashboard cards (total deals, by status, etc.)

2. **List View**
   - Call `GET /users/crm/deals` with filters to show paginated table
   - Apply filters from UI (status dropdown, search box, pipeline filter)
   - Implement infinite scroll or "Load More" button using `cursor`

3. **Detail View**
   - Call `GET /users/crm/deals/{deal_id}` when user clicks a deal row
   - Display all fields in a detail panel or modal

4. **Search & Filter**
   - Debounce search input (q parameter) to avoid excessive API calls
   - Combine multiple filters (status + pipeline + search)
   - Reset cursor when filters change

### Pagination Best Practices

- Store `cursor` from last successful response
- Show loading state while fetching next page
- Disable "Load More" button when `cursor` is null
- Handle network errors gracefully (show retry button)

### Performance Optimization

- Cache `stats` response for 30-60 seconds
- Implement request deduplication for rapid filter changes
- Consider virtual scrolling for large deal lists
- Batch status badge rendering in list view

### Error Handling

```typescript
try {
  const deals = await listDeals({ limit: 20, statuses: ['open'] });
  renderDeals(deals);
} catch (error) {
  if (error.status === 429) {
    // Show "Too many requests, please wait" message
    showRateLimitWarning();
  } else if (error.status >= 500) {
    // Show generic error with retry
    showRetryableError();
  } else {
    // Show specific error message
    showError(error.message);
  }
}
```

### Real-time Updates

- Deals data is synced from CRM systems periodically (not real-time)
- Implement manual refresh button for users who need latest data
- Consider polling `stats` endpoint every 5-10 minutes for dashboard
- Show `synced_at` timestamp to indicate data freshness

---

## 4. Generate Deal Summary (AI)

### Endpoint for Generate Deal Summary

```http
POST /users/crm/deals/{deal_id}/deal-summary/generate
```

### Description for Generate Deal Summary

Initiates an AI-powered summary generation for a deal. This analyzes ALL meeting transcripts associated with the deal to produce a comprehensive deal summary including:

- Overall deal narrative and progression
- Key themes across meetings
- Relationship timeline
- Critical decisions made
- Outstanding items and risks
- Recommended next steps

The operation is **idempotent** - if a summary job is already running for this deal, the existing job is returned instead of creating a new one.

### Authentication for Generate Deal Summary

Required

### Path Parameters for Generate Deal Summary

| Parameter | Type | Required | Description                 |
|-----------|------|----------|-----------------------------|
| `deal_id` | UUID | Yes      | Internal deal UUID          |

### Success Response (202 Accepted) for Generate Deal Summary

```json
{
  "status": "SUCCESS",
  "message": "Deal summary generation initiated",
  "data": {
    "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
    "job_type": "deal_summary",
    "status": "PENDING"
  }
}
```

### Response When Job Already Running

```json
{
  "status": "SUCCESS",
  "message": "Deal summary generation already in progress",
  "data": {
    "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
    "job_type": "deal_summary",
    "status": "IN_PROGRESS"
  }
}
```

### Response Fields for Generate Deal Summary

| Field      | Type   | Description                                                |
|------------|--------|------------------------------------------------------------|
| `job_id`   | UUID   | Job ID to use for SSE stream subscription                  |
| `job_type` | string | Type of job (`deal_summary`)                               |
| `status`   | string | Job status: `PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`|

### Error Responses for Generate Deal Summary

- `404 Not Found` — deal does not exist
- `429 Too Many Requests` — rate limit exceeded
- `500 Internal Server Error` — failed to schedule job

### Example cURL for Generate Deal Summary

```bash
curl -X POST "$BASE_URL/users/crm/deals/f47ac10b-58cc-4372-a567-0e02b2c3d479/deal-summary/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"
```

### Example TypeScript for Generate Deal Summary

```typescript
interface DealJobResponse {
  job_id: string;
  job_type: string;
  status: string;
}

async function generateDealSummary(dealId: string): Promise<DealJobResponse> {
  const response = await fetch(
    `${BASE_URL}/users/crm/deals/${dealId}/deal-summary/generate`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      }
    }
  );
  
  const envelope: ApiEnvelope<DealJobResponse> = await response.json();
  if (envelope.status === 'ERROR') throw new Error(envelope.message);
  return envelope.data!;
}
```

---

## 5. Deal Summary SSE Stream

### Endpoint for Deal Summary SSE Stream

```http
GET /users/crm/deals/{deal_id}/deal-summary/stream/{job_id}
```

### Description for Deal Summary SSE Stream

Server-Sent Events (SSE) stream for real-time deal summary job progress and results.

Subscribe to this endpoint with the `job_id` returned from the generate endpoint. The stream emits progress events during generation and the final result when complete.

**Reconnection Support**: If the user disconnects and reconnects, the stream resumes from the current state or delivers the cached result if already completed.

### Authentication for Deal Summary SSE Stream

Required

### Path Parameters for Deal Summary SSE Stream

| Parameter | Type | Required | Description                                           |
|-----------|------|----------|-------------------------------------------------------|
| `deal_id` | UUID | Yes      | Internal deal UUID                                    |
| `job_id`  | UUID | Yes      | Job ID from POST `/deal-summary/generate` response    |

### SSE Event Types

| Event Type  | Description                                              |
|-------------|----------------------------------------------------------|
| `ready`     | Initial event when stream connects, contains job_id      |
| `ping`      | Heartbeat (every ~25s) to keep connection alive          |
| `frame`     | Progress updates, content chunks, or finalize signal     |
| `complete`  | Job finished successfully - stream will close            |
| `error`     | Error occurred - check `reconnect` flag                  |
| `timeout`   | Stream timeout (5 min default) - reconnect if needed     |

### Frame Types (within `frame` event)

| Frame Type       | Description                                         |
|------------------|-----------------------------------------------------|
| `progress.update`| Progress updates with message, stage, and percentage|
| `answer.replace` | Final result content (full summary data)            |
| `error`          | Error during processing                             |
| `finalize`       | Signals job completion, stream will close           |

### SSE Event Format

```text
event: ready
data: {"job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"}

event: frame
data: {"type": "progress.update", "message": "Loading deal context...", "stage": "context", "progress": 0.1, ...}

event: frame
data: {"type": "progress.update", "message": "Found 5 meetings. Fetching transcripts...", "stage": "transcripts", "progress": 0.25, ...}

event: frame
data: {"type": "progress.update", "message": "Analyzing 5 meeting transcripts...", "stage": "analysis", "progress": 0.6, ...}

event: frame
data: {"type": "answer.replace", "content": {...summaryData}, "job_type": "deal_summary", "job_id": "...", ...}

event: frame
data: {"type": "finalize", "reason": "done", ...}

event: complete
data: {"job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"}
```

### Progress Update Frame Structure

```json
{
  "type": "progress.update",
  "message": "Loading deal context...",
  "stage": "context",
  "progress": 0.1,
  "sequence": 0,
  "timestamp": 1733234567.123,
  "job_type": "deal_summary",
  "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
}
```

### Progress Stages

| Stage            | Progress | Description                         |
|------------------|----------|-------------------------------------|
| `context`        | 0.10     | Loading deal context                |
| `stakeholders`   | 0.15     | Loading stakeholders                |
| `meetings`       | 0.20     | Loading meeting history             |
| `transcripts`    | 0.25     | Starting transcript fetch           |
| `transcript_fetch`| 0.25-0.60| Fetching individual transcripts    |
| `analysis`       | 0.60     | Analyzing transcripts               |
| `generation`     | 0.70     | Generating AI summary               |
| `saving`         | 0.90     | Saving results to database          |

### Answer Replace Frame Structure (Final Result)

The `answer.replace` frame contains the full summary data in the `content` field:

```json
{
  "type": "answer.replace",
  "content": {
    "schema_version": "1.0",
    "generated_at": "2025-12-03T10:30:00Z",
    "data": {
      "overview": "Enterprise software license deal with Acme Corp spanning 5 meetings over 3 months...",
      "key_stakeholders": [
        {"name": "John Smith", "role": "Decision Maker", "sentiment": "positive"},
        {"name": "Jane Doe", "role": "Technical Lead", "sentiment": "neutral"}
      ],
      "meeting_timeline": [
        {"date": "2025-09-15", "type": "Discovery", "key_points": ["Initial requirements gathered"]},
        {"date": "2025-10-01", "type": "Demo", "key_points": ["Product demo well received"]}
      ],
      "next_steps": [
        "Schedule technical deep-dive with engineering team",
        "Send updated proposal with custom pricing"
      ],
      "risks": [
        "Budget approval pending Q1 review",
        "Competitor evaluation ongoing"
      ],
      "overall_sentiment": "positive",
      "deal_health": "on_track"
    }
  },
  "sequence": 8,
  "timestamp": 1733234567.123,
  "job_type": "deal_summary",
  "job_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
}
```

**Note:** The `content` field contains the same structure as `deal_summary_data` in the Deal Detail response. Frontend should extract this from `frame.content` when `frame.type === "answer.replace"`.

### Example TypeScript for Deal Summary SSE Stream

```typescript
function subscribeToDealSummaryStream(
  dealId: string,
  jobId: string,
  callbacks: {
    onProgress?: (progress: number, stage: string) => void;
    onChunk?: (content: string) => void;
    onComplete?: (result: DealSummaryResult) => void;
    onError?: (error: string) => void;
  }
): EventSource {
  const eventSource = new EventSource(
    `${BASE_URL}/users/crm/deals/${dealId}/deal-summary/stream/${jobId}`,
    { 
      // Note: For auth, you may need to use fetch with ReadableStream instead
      // or pass token via query param if your backend supports it
    }
  );

  eventSource.addEventListener('progress', (event) => {
    const data = JSON.parse(event.data);
    callbacks.onProgress?.(data.progress, data.stage);
  });

  eventSource.addEventListener('chunk', (event) => {
    const data = JSON.parse(event.data);
    callbacks.onChunk?.(data.content);
  });

  eventSource.addEventListener('complete', (event) => {
    const data = JSON.parse(event.data);
    callbacks.onComplete?.(data.result);
    eventSource.close();
  });

  eventSource.addEventListener('error', (event) => {
    if (event.data) {
      const data = JSON.parse(event.data);
      callbacks.onError?.(data.error);
    }
    eventSource.close();
  });

  return eventSource;
}

// Usage example
const job = await generateDealSummary(dealId);

const stream = subscribeToDealSummaryStream(dealId, job.job_id, {
  onProgress: (progress, stage) => {
    console.log(`${progress}% - ${stage}`);
    updateProgressBar(progress);
  },
  onChunk: (content) => {
    appendToSummaryPreview(content);
  },
  onComplete: (result) => {
    displayFinalSummary(result);
  },
  onError: (error) => {
    showErrorMessage(error);
  }
});
```

### Reconnection Pattern

If the user navigates away and returns, check `active_deal_jobs` in the deal detail response:

```typescript
async function checkAndReconnectToSummaryJob(dealId: string) {
  const deal = await getDealById(dealId);
  
  // Check if there's an active summary job
  if (deal.active_deal_jobs?.deal_summary) {
    const { job_id, status } = deal.active_deal_jobs.deal_summary;
    
    if (status === 'IN_PROGRESS' || status === 'PENDING') {
      // Reconnect to the stream
      return subscribeToDealSummaryStream(dealId, job_id, callbacks);
    }
  }
  
  // No active job - check if summary already exists
  if (deal.deal_summary_status === 'COMPLETED' && deal.deal_summary_data) {
    displayFinalSummary(deal.deal_summary_data);
  }
  
  return null;
}
```

### Notes for Deal Summary SSE Stream

- The stream automatically closes after sending the `complete` or `error` event
- If the job completes before the client connects, the `complete` event is sent immediately
- Progress percentages are approximate and may not be strictly linear
- For authenticated SSE, consider using fetch with ReadableStream or EventSource polyfill

---

## 6. List Deal Tasks

### Endpoint for List Deal Tasks

```http
GET /users/crm/deals/{deal_id}/tasks
```

### Description for List Deal Tasks

Returns a cursor-paginated list of all tasks linked to the specified deal. Use this for the full Tasks tab or when you need filters beyond the right-rail widget (status, priority, type).

### Query Parameters for List Deal Tasks

| Parameter   | Type         | Required | Description                                     |
|-------------|--------------|----------|-------------------------------------------------|
| `limit`     | integer      | No       | Items per page (1..100, default 50)             |
| `cursor`    | string       | No       | Opaque cursor from previous response            |
| `statuses`  | array&lt;string&gt; | No      | Filter by task status (repeat parameter)        |
| `priorities`| array&lt;string&gt; | No      | Filter by priority (repeat parameter)           |
| `task_types`| array&lt;string&gt; | No      | Filter by task type/category (repeat parameter) |

### Success Response (200) for List Deal Tasks

```json
{
  "status": "SUCCESS",
  "message": "Tasks retrieved successfully",
  "data": {
    "items": [
      {
        "task_id": "83fb2504-1d18-4cf6-a8db-0bb1370df5b0",
        "external_id": "task_10001",
        "subject": "Follow up on data migration timeline concerns",
        "status": "IN_PROGRESS",
        "task_type": "AI",
        "priority": "HIGH",
        "due_date": "2025-05-02T17:00:00Z",
        "owner_name": "Mrinmoy N",
        "owner_email": "mrinmoy@example.com",
        "deal_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "deal_external_id": "deal_12345",
        "is_overdue": false,
        "created_at": "2025-04-18T10:00:00Z",
        "updated_at": "2025-04-19T12:00:00Z",
        "synced_at": "2025-04-19T12:05:00Z"
      }
    ],
    "cursor": null
  }
}
```

### Notes for List Deal Tasks

- `is_overdue` is computed server-side to avoid timezone drift on the client.
- Ordering is `created_at DESC, task_id DESC` for stable pagination.

---

## 7. Get Task Detail

### Endpoint for Get Task Detail

```http
GET /users/crm/tasks/{task_id}
```

### Description for Get Task Detail

Returns the full payload for a single task (body, reminders, metadata). Use when clicking a task card to open the drawer/editor.

### Success Response (200) for Get Task Detail

```json
{
  "status": "SUCCESS",
  "message": "Task retrieved successfully",
  "data": {
    "task_id": "83fb2504-1d18-4cf6-a8db-0bb1370df5b0",
    "external_id": "task_10001",
    "subject": "Follow up on data migration timeline concerns",
    "body": "Confirm migration blockers with Kai by EOD.",
    "status": "IN_PROGRESS",
    "task_type": "AI",
    "priority": "HIGH",
    "owner_name": "Mrinmoy N",
    "owner_email": "mrinmoy@example.com",
    "due_date": "2025-05-02T17:00:00Z",
    "completed_date": null,
    "reminder_date": "2025-05-02T13:00:00Z",
    "source_created_at": "2025-04-18T10:00:00Z",
    "source_modified_at": "2025-04-19T12:00:00Z",
    "properties": {
      "playbook": "Discovery call follow-up"
    },
    "metrics": null,
    "integration_id": "c3d4e5f6-a7b8-4c5d-9e0f-1a2b3c4d5e6f",
    "organisation_id": "d4e5f6a7-b8c9-4d5e-0f1a-2b3c4d5e6f7a",
    "synced_at": "2025-04-19T12:05:00Z",
    "created_at": "2025-04-18T10:00:00Z",
    "updated_at": "2025-04-19T12:00:00Z",
    "is_overdue": false
  }
}
```

### Notes for Get Task Detail

- Returns `404` if the task does not belong to the authenticated organisation.
- `properties` and `metrics` echo any CRM-specific custom payloads for advanced use cases.

---

## Related References

- CRM Sync Architecture: `docs/Technical Documentation/integration-architecture-proposal.md`
- Integration Management: `docs/API Documentation/integrations-api.md`
- OAuth Flow: `docs/API Documentation/oauth-data-flow.md`
