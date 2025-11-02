# CRM Deals API Documentation

## Base URL
```
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
  limit?: number;           // 1..100, default 20
  cursor?: string;          // opaque pagination cursor
  statuses?: CRMDealStatus[];  // filter by deal status
  pipeline_ids?: UUID[];    // filter by pipeline UUIDs
  owner_external_ids?: string[];  // filter by owner external IDs
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

### Authentication
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

## 1. List Deals (Paginated)

### Endpoint
```http
GET /users/crm/deals?limit=20&cursor=<opaque>&statuses=open&statuses=won&pipeline_ids=<uuid>&owner_external_ids=<id>&q=enterprise
```

### Description
Returns a cursor-paginated list of CRM deals for the current organisation with optional filters. Ordered by `created_at DESC, deal_id DESC` for consistent pagination.

### Query Parameters
| Parameter            | Type           | Required | Description                                                       |
|---------------------|----------------|----------|-------------------------------------------------------------------|
| `limit`             | integer        | No       | Items per page (1..100, default 20)                               |
| `cursor`            | string         | No       | Opaque cursor from previous response                              |
| `statuses`          | array<string>  | No       | Filter by status (`open`, `won`, `lost`, `closed`; repeat param) |
| `pipeline_ids`      | array<UUID>    | No       | Filter by pipeline UUIDs (repeat parameter)                       |
| `owner_external_ids`| array<string>  | No       | Filter by owner external IDs from CRM (repeat parameter)          |
| `q`                 | string         | No       | Case-insensitive substring match on deal name                     |

### Success Response (200)
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

### Notes
- Empty `items` array with `cursor: null` indicates no results
- Cursor is `null` when no more pages exist
- Pass `cursor` from previous response to fetch next page
- All filters are ANDed together
- Multiple values for the same filter (e.g., multiple statuses) are ORed

### Error Responses
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

### Example cURL
```bash
curl -X GET "$BASE_URL/users/crm/deals?limit=20&statuses=open&statuses=won&q=enterprise" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

### Example TypeScript
```typescript
async function listDeals(params: ListDealsParams): Promise<DealsPage> {
  const queryParams = new URLSearchParams();
  if (params.limit) queryParams.append('limit', params.limit.toString());
  if (params.cursor) queryParams.append('cursor', params.cursor);
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
```

---

## 2. Get Deal Statistics

### Endpoint
```http
GET /users/crm/deals/stats
```

### Description
Returns aggregate statistics for all CRM deals in the organisation. Useful for dashboard widgets and overview cards.

### Authentication
Required

### Success Response (200)
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

### Notes
- Amounts are calculated from deals with non-null `amount` values
- Pipelines without names are excluded from `by_pipeline`
- Statistics exclude soft-deleted deals
- Currency is not normalized; `total_amount` and `average_amount` mix currencies

### Error Responses
- `429 Too Many Requests` — rate limit exceeded
- `500 Internal Server Error` — unexpected error

### Example cURL
```bash
curl -X GET "$BASE_URL/users/crm/deals/stats" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

### Example TypeScript
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

### Endpoint
```http
GET /users/crm/deals/{deal_id}
```

### Description
Retrieves full details for a specific CRM deal by its internal UUID. Returns all available fields including metadata, associations, and activity timeline.

### Authentication
Required

### Path Parameters
| Parameter | Type | Required | Description                 |
|-----------|------|----------|-----------------------------|
| `deal_id` | UUID | Yes      | Internal deal UUID          |

### Success Response (200)
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
    "updated_at": "2025-10-24T15:30:00Z"
  }
}
```

### Error Responses
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

### Example cURL
```bash
curl -X GET "$BASE_URL/users/crm/deals/f47ac10b-58cc-4372-a567-0e02b2c3d479" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

### Example TypeScript
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

## Related References
- CRM Sync Architecture: `docs/Technical Documentation/integration-architecture-proposal.md`
- Integration Management: `docs/API Documentation/integrations-api.md`
- OAuth Flow: `docs/API Documentation/oauth-data-flow.md`
