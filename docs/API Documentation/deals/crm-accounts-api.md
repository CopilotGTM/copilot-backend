# CRM Accounts API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-06-23

---

## Overview

Accounts represent CRM companies/organisations that your deals and contacts belong to. Each account is a normalised `CRMCompany` record stored in the tenant-scoped `crm_companies` table.

### Internal vs Synced Accounts

All account records include an implicit origin via their `integration_id`:

- **Internal accounts** (created via this API): Owned by the `INTERNAL_CRM` integration. Fully editable — users can update all fields, trigger enrichment, and manage relationships.
- **CRM-synced accounts** (imported from HubSpot, Salesforce, etc.): Owned by the corresponding CRM integration. Editable through this API (updates are local only — they do **not** sync back to the external CRM).

**Frontend Implementation**: Use the local `company_id` to render and navigate accounts. Provider identifiers are stored in `crm_source_object_mappings` and are not part of canonical account API payloads.

### Related APIs

| API | Relationship | Documentation |
|-----|-------------|---------------|
| **CRM Contacts** | Contacts linked to accounts via email domain matching | `crm-contacts-api.md` |
| **CRM Deals** | Deals linked to accounts via `company_id` | `crm-deals-api.md` |
| **Stakeholders** | Stakeholders linked to contacts, which link to accounts | `stakeholders-api.md` |
| **Deal Participants** | Participants may reference contacts from an account | `crm-participants-api.md` |

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
| **Write** | 120 | 2,000 | `POST`, `PATCH` endpoints |

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

### Account → Contacts (FK + Domain Matching)

Contacts can be linked to accounts via **two mechanisms**, checked in priority order:

1. **Explicit FK** (`crm_contacts.company_id` → `crm_companies.company_id`): Takes precedence. When a contact has a `company_id` set, it is always associated with that account regardless of email domain.
2. **Email domain matching** (`contact.email` domain vs `crm_companies.domain`): Fallback when no explicit FK exists. Matches the contact's email domain against the account's `domain` field using case-insensitive suffix matching.

```
Account (company_id: "aaa...", domain: "acme.com")

Contact (company_id: "aaa...")               ← matched via FK (takes precedence)
Contact (email: "john@acme.com")             ← matched via domain
Contact (email: "jane@acme.com")             ← matched via domain
Contact (email: "bob@other.com")             ← NOT matched
Contact (company_id: "bbb...", email: "x@acme.com")  ← matched to account "bbb..." (FK wins)
```

**Implications for frontend:**

- Changing an account's `domain` will change which contacts appear under it (for domain-matched contacts)
- A contact with an explicit `company_id` is always linked to that account, even if its email domain doesn't match
- A contact with no email, no `company_id`, and a non-matching domain won't appear under any account
- The domain matching is case-insensitive

### Account -> Deals

```
Account (company_id: "bbb...")
  -> Deal.company_id
Deal (company_id: "bbb...")  <- matched
```

### Computed Metrics

These fields are calculated at query time, **not stored** in the database:

| Field | Computation |
|-------|------------|
| `contact_count` | Count of non-deleted contacts whose email domain matches the account's domain |
| `deal_count` | Count of non-deleted deals whose `company_id` matches the account |
| `pipeline_value` | Sum of `amount` from matched deals where `deal_status = 'open'` only |

**Note on `deal_count` in account contact endpoints:** The standalone `GET /accounts/{company_id}/contacts` response includes a per-contact `deal_count`. Account detail keeps nested contacts compact and does not include per-contact deal counts.

---

## Enums & Field Reference

### account_research.status

Status of the account research content used by account detail.

| Value | Description | Frontend Display |
|-------|-------------|-----------------|
| `"not_available"` | Account research storage/generation is not available yet | Show empty/coming-soon research state |
| `"pending"` | Research queued | Show spinner or "Pending..." |
| `"in_progress"` | Research actively running | Show spinner or progress indicator |
| `"completed"` | Research finished successfully | Show research content |
| `"failed"` | Research encountered an error | Show retry CTA |

### lifecycle_stage

Free-form string from the CRM provider. Common values:

| Value | Description |
|-------|-------------|
| `"subscriber"` | Newsletter or content subscriber |
| `"lead"` | Marketing qualified lead |
| `"opportunity"` | Active sales opportunity |
| `"customer"` | Paying customer |
| `"evangelist"` | Active promoter/advocate |
| `"other"` | Other/custom stage |

### Field Nullability Reference

| Field | Can Be Null | When Null |
|-------|------------|-----------|
| `name` | Yes | Rare — CRM record had no company name |
| `domain` | Yes | No domain was set or derivable |
| `website` | Yes | Not provided |
| `industry` | Yes | Not provided |
| `lifecycle_stage` | Yes | Not set in CRM |
| `owner_name` | Yes | No owner assigned |
| `owner` | Yes | No owner ID, name, or email is assigned |
| `number_of_employees` | Yes | Not provided |
| `annual_revenue` | Yes | Not provided |
| `phone` | Yes | Not provided |
| `country` | Yes | Not provided |
| `linkedin_url` | Yes | Not provided |
| `description` | Yes | Not provided (detail view only) |
| `account_research.content` | Yes | Account research is not available or not completed |
| `account_research.last_researched_at` | Yes | Account research is not available or never completed |

---

## Response Models (TypeScript)

```ts
type UUID = string;
type ISODate = string; // ISO 8601 datetime with timezone (e.g., "2026-03-15T10:30:00Z")

/**
 * Account item returned in page-based list views (GET /accounts).
 * Use for table rows and account index pages.
 *
 * contact_count, deal_count, and pipeline_value are computed at query time.
 */
export interface AccountListOwner {
  id: UUID | null;
  name: string | null;
  email: string | null;
}

export interface AccountListItem {
  company_id: UUID;
  name: string | null;
  domain: string | null;              // Normalised: lowercase, no protocol/www
  website: string | null;             // Full URL (may include protocol)
  industry: string | null;
  lifecycle_stage: string | null;     // Free-form CRM value
  owner_name: string | null;          // Backwards-compatible display field
  owner: AccountListOwner | null;     // Preferred owner object for new UI columns
  number_of_employees: number | null;
  annual_revenue: number | null;      // Decimal (up to 2 decimal places)
  phone: string | null;
  country: string | null;
  linkedin_url: string | null;
  is_active: boolean;                 // Always true for active records (soft-deleted records are filtered out)

  // Computed metrics (calculated per request, not stored)
  contact_count: number;              // Contacts with email domain matching account domain
  deal_count: number;                 // Deals linked via company_id
  pipeline_value: number;             // Sum of open deal amounts (0.0 if no open deals)

  // Timestamps
  created_at: ISODate;
  updated_at: ISODate;
}

/**
 * Stable research wrapper for the account detail main panel.
 * Account research generation/persistence is not implemented yet.
 */
export interface AccountResearchSummary {
  status: "not_available" | "pending" | "in_progress" | "completed" | "failed";
  content: string | null;
  last_researched_at: ISODate | null;
}

/**
 * Full account detail returned by GET /accounts/{company_id}.
 * Extends AccountListItem with description, location, account research placeholder,
 * and nested compact contacts + deals arrays.
 */
export interface AccountDetail extends AccountListItem {
  description: string | null;
  company_type: string | null;        // e.g., "Private", "Public", "Subsidiary"
  address: string | null;
  city: string | null;
  state: string | null;
  postal_code: string | null;
  timezone: string | null;            // IANA timezone (e.g., "America/Los_Angeles")
  account_research: AccountResearchSummary;

  /** Contacts whose email domain matches this account's domain */
  contacts: AccountDetailContactItem[];

  /** Deals linked via company_id */
  deals: AccountDealItem[];
}

/**
 * Contact summary nested inside AccountDetail.
 * Minimal fields for display in account detail view.
 */
export interface AccountDetailContactItem {
  contact_id: UUID;
  full_name: string | null;
  email: string | null;
  job_title: string | null;
}

/**
 * Contact summary returned by GET /accounts/{company_id}/contacts.
 */
export interface AccountContactItem extends AccountDetailContactItem {
  deal_count: number;
}

/**
 * Deal summary nested inside AccountDetail.
 * Minimal fields for display in account detail view.
 */
export interface AccountDealItem {
  deal_id: UUID;
  name: string | null;
  amount: number | null;
  currency: string | null;            // ISO 4217 (e.g., "USD", "INR")
  deal_status: string | null;         // "open" | "won" | "lost" | "closed"
  stage_name: string | null;          // Pipeline stage display name
  owner_name: string | null;          // Deal owner display name
}

/**
 * Account suggestion returned by GET /accounts/suggest.
 * Used to auto-select an account when creating/editing a contact.
 * Returns null (not this interface) when no match is found.
 */
export interface AccountSuggestion {
  company_id: UUID;
  name: string | null;
  domain: string | null;
  match_type: "domain_match";             // Currently only domain matching is supported
}

/**
 * Paginated response wrapper for list endpoints.
 */
export interface AccountsPage {
  items: AccountListItem[];
  page: number;                       // Current page (1-indexed)
  limit: number;                      // Items per page requested
  total_count: number;                // Total matching records
  total_pages: number;                // ceil(total_count / limit)
}

/**
 * Lightweight account item returned by GET /accounts/search.
 * Use for dropdowns, typeahead, and infinite-scroll pickers.
 */
export interface AccountSearchItem {
  company_id: UUID;
  name: string | null;
  domain: string | null;
  website: string | null;
  industry: string | null;
}

/**
 * Cursor response wrapper for account picker search.
 */
export interface AccountSearchPage {
  items: AccountSearchItem[];
  next_cursor: string | null;         // Opaque cursor for the next request
  has_more: boolean;                  // true when next_cursor is non-null
}

/**
 * Request to create a new account.
 */
export interface CreateAccountRequest {
  name?: string | null;               // Optional when domain or website is supplied
  domain?: string | null;             // Optional; auto-normalised when provided
  website?: string | null;            // Optional; domain can be derived when provided
  industry?: string | null;
  linkedin_url?: string | null;
}

/**
 * Request to update an account. All fields optional.
 * Only include fields that should change.
 */
export interface UpdateAccountRequest {
  name?: string | null;
  domain?: string | null;             // Auto-normalised if provided
  website?: string | null;
  industry?: string | null;
  description?: string | null;
  phone?: string | null;
  linkedin_url?: string | null;
  number_of_employees?: number | null;
  annual_revenue?: number | null;
}

/**
 * Response after triggering account enrichment.
 */
export interface EnrichAccountResponse {
  job_id: string | null;              // null until enrichment pipeline is implemented
  message: string;                    // Human-readable status message
}

export interface ActivityFeedItem {
  activity_id: UUID;
  activity_type: string;
  source_type: string;
  source_record_id: UUID | null;
  source_provider: string | null;
  source_integration_id: UUID | null;
  source_url: string | null;
  title: string | null;
  summary: string | null;
  preview: string | null;
  occurred_at: ISODate;
  actor_name: string | null;
  actor_email: string | null;
  display_payload: Record<string, unknown>;
  link_role: string | null;
  link_source: string | null;
  confidence_score: number | null;
  created_at: ISODate;
  updated_at: ISODate;
}

export interface AccountActivityFeedPage {
  object_type: "COMPANY";
  object_id: UUID;
  items: ActivityFeedItem[];
  total_count: number;
  cursor: string | null;
}
```

## Endpoints

### 1. List Accounts

```http
GET /users/crm/accounts
```

Paginated account list with computed `contact_count`, `deal_count`, and `pipeline_value`. Results are sorted alphabetically by `name` (ascending), then by `company_id` for stable pagination when names collide.

#### Query Parameters

| Parameter  | Type    | Default | Validation | Description |
|-----------|---------|---------|------------|-------------|
| `page`    | integer | `1`     | `>= 1` | Page number (1-indexed) |
| `limit`   | integer | `25`    | `1–100` | Maximum accounts per page |
| `q`       | string  | —       | Any string | Case-insensitive partial match on `name` OR `domain` |
| `industry`| string  | —       | Any string | Case-insensitive partial match on `industry` |

**Search behaviour (`q` parameter):**

- Matches anywhere in the string: `q=acm` matches "Acme Corp"
- Matches on `name` OR `domain` (not both required)
- Example: `q=tech` matches name "TechCorp" and domain "techstartup.io"

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Accounts retrieved successfully",
  "data": {
    "items": [
      {
        "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
        "name": "Acme Corp",
        "domain": "acme.com",
        "website": "https://acme.com",
        "industry": "Technology",
        "lifecycle_stage": "customer",
        "owner_name": "Jane Smith",
        "owner": {
          "id": "9a8b7c6d-e5f4-4321-9876-123456789abc",
          "name": "Jane Smith",
          "email": "jane@example.com"
        },
        "number_of_employees": 250,
        "annual_revenue": 50000000.00,
        "phone": "+1-555-0100",
        "country": "US",
        "linkedin_url": "https://linkedin.com/company/acme-corp",
        "is_active": true,
        "contact_count": 12,
        "deal_count": 3,
        "pipeline_value": 150000.00,
        "created_at": "2026-01-10T08:00:00Z",
        "updated_at": "2026-03-20T14:00:00Z"
      }
    ],
    "page": 1,
    "limit": 25,
    "total_count": 48,
    "total_pages": 2
  }
}
```

**Empty results:** When no accounts match, returns `items: []`, `total_count: 0`, `total_pages: 0`.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Page must be >= 1"` | `page` parameter < 1 |
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `429` | `"Too many requests (per-minute limit exceeded)"` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 2. Suggest Account for Contact

```http
GET /users/crm/accounts/suggest
```

Suggests a matching account for a given email domain. Powers the account dropdown auto-suggestion when creating or editing contacts — the frontend extracts the email domain from the contact's email address and calls this endpoint to pre-select the matching account.

#### Query Parameters

| Parameter | Type | Required | Validation | Description |
|-----------|------|----------|------------|-------------|
| `email_domain` | string | **Yes** | Non-empty | Email domain to match (e.g., `acme.com`) |

#### Example Request

```http
GET /users/crm/accounts/suggest?email_domain=acme.com
```

#### Success Response — Match Found (200)

```json
{
  "status": "SUCCESS",
  "message": "Account suggestion found",
  "data": {
    "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
    "name": "Acme Corp",
    "domain": "acme.com",
    "match_type": "domain_match"
  }
}
```

#### Success Response — No Match (200)

```json
{
  "status": "SUCCESS",
  "message": "No matching account found",
  "data": null
}
```

**Frontend implementation:** When the user types or selects a contact email, extract the domain portion (everything after `@`), call this endpoint, and if `data` is non-null, pre-select the returned account in the account dropdown. If `data` is null, leave the dropdown unselected.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `422` | Pydantic validation error | Missing or empty `email_domain` parameter |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 3. Search Accounts with Cursor

```http
GET /users/crm/accounts/search
```

Lightweight cursor-based account search for dropdowns, typeahead, and infinite-scroll pickers. This endpoint does not compute `contact_count`, `deal_count`, `pipeline_value`, or `total_count`; use `GET /users/crm/accounts` for the full table view.

#### Query Parameters

| Parameter | Type | Default | Validation | Description |
|-----------|------|---------|------------|-------------|
| `q` | string | — | Optional | Case-insensitive partial match on `name`, `domain`, or `website` |
| `limit` | integer | `20` | `1–50` | Maximum accounts per cursor page |
| `cursor` | string | — | Opaque token | Cursor returned by the previous response |

#### Example Request

```http
GET /users/crm/accounts/search?q=acme&limit=20
```

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Accounts search results retrieved",
  "data": {
    "items": [
      {
        "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
        "name": "Acme Corp",
        "domain": "acme.com",
        "website": "https://acme.com",
        "industry": "Technology"
      }
    ],
    "next_cursor": "eyJuYW1lIjoiYWNtZSBjb3JwIiwiY29tcGFueV9pZCI6ImQzYjA3Mzg0LWQ5YTEtNGUzYi04YzFhLTFmMmUzZDRjNWI2YSJ9",
    "has_more": true
  }
}
```

Use `next_cursor` on the next request. When `next_cursor` is `null`, there are no more rows for the current query.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Invalid cursor"` | Cursor is malformed or not produced by this endpoint |
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `422` | Pydantic validation error | Invalid `limit` |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

### 4. Create Account

```http
POST /users/crm/accounts
```

Creates a new account owned by the `INTERNAL_CRM` integration. Creation requires at least one identity field: `name`, `domain`, or `website`. When `domain` or `website` is supplied, the service derives a normalised domain, checks it for uniqueness, and stores it as the account's primary domain. If `name` is omitted, the account name falls back to the normalised domain (for example, `acme.com`).

#### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` |
| `Content-Type` | Yes | `application/json` |

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `name` | string | Conditionally | Non-empty when present | Company display name. Required only when neither `domain` nor `website` is supplied |
| `domain` | string | Conditionally | Auto-normalised when present | Company domain. Can satisfy the identity requirement without `name` |
| `website` | string | Conditionally | Used to derive `domain` when `domain` is omitted | Full website URL. Can satisfy the identity requirement without `name` |
| `industry` | string | No | — | Industry classification |
| `linkedin_url` | string | No | — | LinkedIn company page URL |

#### Domain Normalisation

The `domain` field is automatically cleaned before storage. If only `website` is supplied, the same normalisation is applied to `website` and the result is stored as `domain`.

| Input | Stored As | Rule Applied |
|-------|-----------|--------------|
| `"https://www.ACME.com/about"` | `"acme.com"` | Strip protocol, www, path, lowercase |
| `"ACME.COM"` | `"acme.com"` | Lowercase |
| `"www.acme.com"` | `"acme.com"` | Strip www |
| `"http://acme.com?ref=123"` | `"acme.com"` | Strip protocol, query string |
| `""` or `"  "` | `null` | Empty/whitespace → null |

#### Example Request

```json
{
  "name": "Acme Corp",
  "domain": "https://www.acme.com",
  "website": "https://acme.com",
  "industry": "Technology",
  "linkedin_url": "https://linkedin.com/company/acme-corp"
}
```

#### Minimal Request

```json
{
  "name": "Quick Account"
}
```

#### Domain-only Request

```json
{
  "domain": "https://www.acme.com"
}
```

This creates an account with `name: "acme.com"` and `domain: "acme.com"` if no existing account already owns that normalised domain.

#### Success Response (201)

```json
{
  "status": "SUCCESS",
  "message": "Account created successfully",
  "data": {
    "company_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "name": "Acme Corp",
    "domain": "acme.com",
    "website": "https://acme.com",
    "industry": "Technology",
    "lifecycle_stage": null,
    "owner_name": null,
    "owner": null,
    "number_of_employees": null,
    "annual_revenue": null,
    "phone": null,
    "country": null,
    "linkedin_url": "https://linkedin.com/company/acme-corp",
    "is_active": true,
    "contact_count": 0,
    "deal_count": 0,
    "pipeline_value": 0.0,
    "created_at": "2026-04-06T10:00:00Z",
    "updated_at": "2026-04-06T10:00:00Z"
  }
}
```

**Note:** `contact_count`, `deal_count`, and `pipeline_value` are always `0` in the create response because relationships are computed at query time.

#### Error Responses

| HTTP Status | `message` | `data` | When |
|-------------|-----------|--------|------|
| `409` | `"An account with domain 'acme.com' already exists"` | `{"company_id": "..."}` | Non-deleted account with same normalised domain exists |
| `409` | `"An account named 'Acme' already exists"` | `{"company_id": "..."}` | No domain/website was provided and a non-deleted account with the exact same name exists |
| `409` | `"Internal CRM integration not configured"` | `null` | Org has no INTERNAL_CRM integration (should not happen in normal operation) |
| `422` | Pydantic validation error | Validation details | Invalid request body, including when `name`, `domain`, and `website` are all missing or blank |
| `429` | `"Too many requests ..."` | `null` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | `null` | Unhandled server error |

**Frontend: handling 409 duplicate domain:**

```ts
if (response.status === 409 && response.data?.company_id) {
  // Offer to navigate to the existing account
  showError(`Account already exists`, {
    action: { label: "View existing", href: `/accounts/${response.data.company_id}` }
  });
}
```

---

### 5. Get Account Detail

```http
GET /users/crm/accounts/{company_id}
```

Full account detail including account fields, a stable account research placeholder, compact contacts, and account deal summaries.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `company_id` | UUID | Yes | Account identifier |

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Account detail retrieved successfully",
  "data": {
    "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
    "name": "Acme Corp",
    "domain": "acme.com",
    "website": "https://acme.com",
    "industry": "Technology",
    "lifecycle_stage": "customer",
    "owner_name": "Jane Smith",
    "owner": {
      "id": "9a8b7c6d-e5f4-4321-9876-123456789abc",
      "name": "Jane Smith",
      "email": "jane@example.com"
    },
    "number_of_employees": 250,
    "annual_revenue": 50000000.00,
    "phone": "+1-555-0100",
    "country": "US",
    "linkedin_url": "https://linkedin.com/company/acme-corp",
    "is_active": true,
    "contact_count": 5,
    "deal_count": 2,
    "pipeline_value": 75000.00,
    "created_at": "2026-01-10T08:00:00Z",
    "updated_at": "2026-03-20T14:00:00Z",

    "description": "Enterprise software company specialising in cloud infrastructure",
    "company_type": "Private",
    "address": "123 Tech Blvd",
    "city": "San Francisco",
    "state": "CA",
    "postal_code": "94105",
    "timezone": "America/Los_Angeles",
    "account_research": {
      "status": "not_available",
      "content": null,
      "last_researched_at": null
    },

    "contacts": [
      {
        "contact_id": "c1d2e3f4-a5b6-7890-cdef-123456789abc",
        "full_name": "John Doe",
        "email": "john@acme.com",
        "job_title": "VP Engineering"
      },
      {
        "contact_id": "d2e3f4a5-b6c7-8901-defa-23456789abcd",
        "full_name": "Sarah Chen",
        "email": "sarah@acme.com",
        "job_title": "CTO"
      }
    ],

    "deals": [
      {
        "deal_id": "e3f4a5b6-c7d8-9012-efab-3456789abcde",
        "name": "Acme Enterprise License",
        "amount": 75000.00,
        "currency": "USD",
        "deal_status": "open",
        "stage_name": "Negotiation",
        "owner_name": "Jane Smith"
      },
      {
        "deal_id": "f4a5b6c7-d8e9-0123-fabc-456789abcdef",
        "name": "Acme Support Renewal",
        "amount": 25000.00,
        "currency": "USD",
        "deal_status": "won",
        "stage_name": "Closed Won",
        "owner_name": "Jane Smith"
      }
    ]
  }
}
```

**Note:** `contact_count` and `deal_count` in the detail response reflect the actual lengths of the `contacts` and `deals` arrays. `pipeline_value` is the sum of `amount` from deals where `deal_status = "open"` only (won/lost/closed deals are excluded). Per-contact deal counts are intentionally not included in account detail contacts; use the standalone account contacts endpoint or contact detail when that relationship context is needed.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Account not found"` | No non-deleted account with this ID in this organisation |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 6. Get Account Activity Feed

```http
GET /users/crm/accounts/{company_id}/activity-feed
```

Cursor-paginated CRM activity projection for an account/company. This reads
`crm_activity_links -> crm_activities` for rows linked with `object_type =
COMPANY`; canonical notes and tasks remain in `crm_notes` and `crm_tasks`.

#### Query Parameters

| Parameter | Type | Default | Validation | Description |
|-----------|------|---------|------------|-------------|
| `limit` | integer | `20` | `1-100` | Maximum feed items per page |
| `cursor` | string | - | Opaque | Cursor returned by the previous response |
| `activity_types` | string | - | Comma-separated | Filter by projected activity type |
| `source_types` | string | - | Comma-separated | Filter by projected source type |
| `start_date` | datetime | - | ISO 8601 | Include activities at/after this time |
| `end_date` | datetime | - | ISO 8601 | Include activities at/before this time |
| `sort` | `asc` or `desc` | `desc` | Enum | Sort by linked activity occurrence time |

Contact lifecycle cards linked to an account may use `source_type` values
`CONTACT_CREATED`, `CONTACT_ENRICHMENT_TRIGGERED`, and
`CONTACT_ENRICHMENT_COMPLETED`.

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Activity feed retrieved successfully",
  "data": {
    "object_type": "COMPANY",
    "object_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
    "items": [],
    "total_count": 0,
    "cursor": null
  }
}
```

---

### 7. Update Account

```http
PATCH /users/crm/accounts/{company_id}
```

Partial update — only send fields you want to change. Omitted fields remain unchanged. Domain is normalised if provided. Updating `domain`, or updating `website` while omitting `domain`, also checks that no other active account in the organisation already owns the resulting normalised domain.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `company_id` | UUID | Yes | Account identifier |

#### Request Body

All fields optional. Only include fields to update.

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `name` | string | — | Company name |
| `domain` | string | — | Domain (auto-normalised) |
| `website` | string | — | Website URL |
| `industry` | string | — | Industry classification |
| `description` | string | — | Company description |
| `phone` | string | — | Phone number |
| `linkedin_url` | string | — | LinkedIn URL |
| `number_of_employees` | integer | — | Employee count |
| `annual_revenue` | number | — | Annual revenue (decimal) |

#### Example Request

```json
{
  "industry": "Enterprise Software",
  "number_of_employees": 500,
  "annual_revenue": 75000000.00
}
```

#### Success Response (200)

Returns the updated `AccountListItem`. `contact_count`, `deal_count`, and `pipeline_value` are returned as `0` in the update response (not re-computed for performance).

```json
{
  "status": "SUCCESS",
  "message": "Account updated successfully",
  "data": {
    "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
    "name": "Acme Corp",
    "industry": "Enterprise Software",
    "number_of_employees": 500,
    "annual_revenue": 75000000.00,
    "contact_count": 0,
    "deal_count": 0,
    "pipeline_value": 0.0,
    "...": "other AccountListItem fields"
  }
}
```

**Frontend note:** After a successful update, either refetch the account detail or merge the response into local state. The `contact_count`/`deal_count`/`pipeline_value` fields will be `0` in the PATCH response — use previously cached values or refetch if needed.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Account not found"` | No non-deleted account with this ID in this organisation |
| `409` | `"An account with domain '...' already exists"` | Updated domain/website normalises to a domain owned by another active account |
| `422` | Pydantic validation error | Invalid field types in body |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 8. Get Account Contacts

```http
GET /users/crm/accounts/{company_id}/contacts
```

Returns contacts whose email domain matches the account's `domain`. If the account has no `domain` set, returns an empty array.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `company_id` | UUID | Yes | Account identifier |

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Account contacts retrieved successfully",
  "data": [
    {
      "contact_id": "c1d2e3f4-a5b6-7890-cdef-123456789abc",
      "full_name": "John Doe",
      "email": "john@acme.com",
      "job_title": "VP Engineering",
      "deal_count": 0
    }
  ]
}
```

**Note:** `deal_count` is always `0` in this endpoint (not computed for performance). Use the account detail endpoint or the contacts list endpoint for accurate deal counts.

**Empty results:** Returns `data: []` when:

- The account has no `domain` set
- No contacts have emails matching the account domain
- The account doesn't exist (no error — just empty array)

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 9. Get Account Deals

```http
GET /users/crm/accounts/{company_id}/deals
```

Returns deals linked to the account via local `company_id`.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `company_id` | UUID | Yes | Account identifier |

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Account deals retrieved successfully",
  "data": [
    {
      "deal_id": "e3f4a5b6-c7d8-9012-efab-3456789abcde",
      "name": "Acme Enterprise License",
      "amount": 75000.00,
      "currency": "USD",
      "deal_status": "open",
      "stage_name": "Negotiation",
      "owner_name": "Jane Smith"
    }
  ]
}
```

**Note:** Both open and closed/won/lost deals are returned. Filter by `deal_status` client-side if needed.

**Empty results:** Returns `data: []` when:

- No deals have a matching `company_id`
- The account doesn't exist (no error — just empty array)

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 10. Enrich Account

```http
POST /users/crm/accounts/{company_id}/enrich
```

Trigger account enrichment with external data sources. **Currently a placeholder** — the company research pipeline is deferred to a future phase. The endpoint validates that the account exists and belongs to the organisation, then returns an accepted response.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `company_id` | UUID | Yes | Account identifier |

#### Success Response (202 Accepted)

```json
{
  "status": "SUCCESS",
  "message": "Enrichment request accepted",
  "data": {
    "job_id": null,
    "message": "Account enrichment will be available in a future release"
  }
}
```

**Frontend implementation:** Show a disabled or "coming soon" state for the enrich button. The endpoint exists to reserve the API contract — when the pipeline is built, it will return a `job_id` that can be polled for progress.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Account not found"` | No non-deleted account with this ID in this organisation |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

## Error Catalog

Consolidated reference of all error responses across all endpoints.

| HTTP Status | Error Message | Endpoint(s) | Cause |
|-------------|--------------|-------------|-------|
| `400` | `"Page must be >= 1"` | List accounts | Invalid page parameter |
| `400` | `"Invalid cursor"` | Search accounts | Malformed or stale cursor token |
| `401` | `"Invalid or missing user credentials."` | All | Missing/invalid JWT token |
| `404` | `"Account not found"` | Get detail, Update, Enrich | Account doesn't exist or is deleted |
| `409` | `"An account with domain '...' already exists"` | Create, Update | Duplicate normalised domain |
| `409` | `"An account named '...' already exists"` | Create | Duplicate exact account name when no domain/website was supplied |
| `409` | `"Internal CRM integration not configured"` | Create | Missing INTERNAL_CRM integration |
| `422` | Pydantic validation error | Create, Update, Suggest, Search | Invalid request body or query parameters |
| `429` | `"Too many requests (per-minute limit exceeded)"` | All | Per-minute rate limit exceeded |
| `429` | `"Too many requests (per-hour limit exceeded)"` | All | Per-hour rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | All | Unhandled exception |

---

## Backend Architecture Notes

> This section is for backend developers working on the service layer.

### Database

**Table:** `crm_companies` (tenant-scoped, in `org_*` schema)

**Key indexes:**

| Index | Columns | Purpose |
|-------|---------|---------|
| `ix_crm_company_org_domain` | `organisation_id, domain` | Domain-based lookups and duplicate checks |
| `ix_crm_company_org_name` | `organisation_id, name` | Name search and sorting |
| `ix_crm_company_lifecycle` | `organisation_id, lifecycle_stage` | Lifecycle filtering |
| `ix_crm_company_synced` | `organisation_id, synced_at` | Sync recency queries |

**Account research:** `GET /accounts/{company_id}` currently returns a stable `account_research` placeholder with `status = "not_available"`. Durable account research storage/generation is deferred to a later implementation.

### Service Layer

**Classes:** `AccountReadService` and `AccountWriteService` (`src/service/crm/accounts/`)

- Receives `org_session` and `organisation_id` at construction
- All queries filter by `organisation_id` and `is_deleted = False`
- `AccountReadService.list_accounts()` uses correlated subqueries for `contact_count`, `deal_count`, `pipeline_value` to avoid N+1
- `AccountReadService.search_accounts()` is a lightweight cursor search for pickers; it returns no totals or aggregate metrics
- `AccountWriteService.create_account()` requires at least one of `name`, `domain`, or `website`, checks domain uniqueness when a domain or website is supplied, falls back to exact name duplicate checks for name-only creates, derives `name` from the normalised domain when missing, and auto-creates `INTERNAL_CRM` integration if missing
- `update_account()` normalises domain if provided, derives domain from `website` when `domain` is omitted, rejects duplicate domains owned by another account, and only updates allowed fields (ignores unknown fields)
- Domain normalisation strips protocol (`https://`), `www.` prefix, paths, query strings, and lowercases

### Domain Matching SQL Pattern

Contact-to-account matching uses:

```sql
-- Contact count per company
SELECT count(contact_id)
FROM crm_contacts
WHERE organisation_id = :org_id
  AND is_deleted = false
  AND email ILIKE concat('%@', crm_companies.domain)
```

This means domain matching is case-insensitive and suffix-based. A company with domain `acme.com` will match contacts with emails like `john@ACME.COM`, `jane@acme.com`, etc.
