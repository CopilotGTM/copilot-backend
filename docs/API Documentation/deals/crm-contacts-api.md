# CRM Contacts API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-04-06

---

## Overview

Contacts represent individual people in your CRM — prospects, stakeholders, champions, or end-users associated with deals. Each contact is a normalised `CRMContact` record stored in the tenant-scoped `crm_contacts` table.

### Internal vs Synced Contacts

Contacts enter the system from two sources:

- **CRM-synced contacts** (`source: "hubspot"` / `"salesforce"`): Imported from external CRM integrations. Editable through this API (updates are local only — they do **not** sync back to the external CRM).
- **Calendar/email-discovered contacts** (`source: "google_calendar"` / `"microsoft_calendar"` / etc.): Discovered from calendar events or email threads. Owned by the corresponding integration.
- **Manual contacts** (`source: "manual"`): Created directly via this API. Owned by the `INTERNAL_CRM` integration.

**Frontend Implementation**: Use the `source` field to show a badge or icon indicating origin (e.g., HubSpot logo for `"hubspot"`, "Manual" badge for `"manual"`).

Canonical contact API payloads use local `contact_id` and `company_id` values.
Provider identifiers are stored in `crm_source_object_mappings` and are not part
of the contact response contract.

### Related APIs

| API | Relationship | Documentation |
|-----|-------------|---------------|
| **CRM Accounts** | Contacts linked to accounts via explicit `company_id` FK or email domain matching | `crm-accounts-api.md` |
| **Stakeholders** | Contacts become stakeholders when added to deals | `stakeholders-api.md` |
| **Deal Participants** | Adding a contact to a deal creates a participant + stakeholder | `crm-participants-api.md` |
| **CRM Deals** | Contacts participate in deals via the stakeholder relationship | `crm-deals-api.md` |

### Contact ↔ Stakeholder ↔ Deal Relationship

```
Contact (CRMContact)
  ↓ contact_id FK
Stakeholder (DealStakeholder)  ←  lives on a specific deal
  ↓ deal_id FK
Deal (CRMDeal)
```

A single contact can be a stakeholder on **multiple deals**. The `deal_count` field counts how many deals a contact participates in. The `deal_involvement` array in the detail view shows each deal with the stakeholder's role.

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

### Contact → Account (Explicit FK + Domain Matching)

Contacts are linked to accounts via two mechanisms. The **explicit FK** takes precedence over domain matching:

1. **Explicit `company_id` FK** (stored on `CRMContact`): Set via the create or update endpoint. When present, the contact is directly linked to the referenced `CRMCompany` record.
2. **Email domain matching** (fallback): When `company_id` is `null`, the system extracts the email domain and matches it against `CRMCompany.domain` at query time.

```
Contact (company_id: "d3b07384-...")
  → direct FK lookup → Account (id: "d3b07384-...")     ← explicit link (preferred)

Contact (company_id: null, email: "john@acme.com")
  → extract domain → "acme.com" → match CRMCompany.domain
  → Account (domain: "acme.com")                        ← fallback
```

This means:

- `company_id` in the list response is either the **stored FK** (when set) or **resolved at query time** from email domain matching
- `company_domain` is **extracted from the contact's email address** (not from the matched company)
- If `company_id` is not explicitly set and no account with a matching domain exists, `company_id` is `null` but `company_domain` still shows the email domain
- When `company_id` is explicitly set, `company_name` is auto-resolved from the linked account

### Contact → Deals (via Stakeholder)

Contacts are linked to deals through `DealStakeholder` records. The `deal_count` field counts only **approved** stakeholders (`approval_status = APPROVED`). Removed, rejected, and pending stakeholders are excluded from the count and from the `deal_involvement` array in the detail view.

### Source (Integration Type)

The `source` field is derived from the `integration_type` of the `KnowledgeIntegration` that owns the contact. The `INTERNAL_CRM` integration is mapped to `"manual"` for clarity; all other integration types pass through as-is.

| `source` Value | Description | Origin |
|---------------|-------------|--------|
| `"manual"` | Created directly via the Contacts API | Internal CRM (manual entry) |
| `"hubspot"` | Imported from HubSpot CRM | CRM sync |
| `"salesforce"` | Imported from Salesforce CRM | CRM sync |
| `"google_calendar"` | Discovered from a Google Calendar event | Calendar discovery |
| `"microsoft_calendar"` | Discovered from a Microsoft Calendar event | Calendar discovery |
| `"gmail"` | Discovered from Gmail thread | Email discovery |
| `"outlook"` | Discovered from Outlook thread | Email discovery |

---

## Enums & Field Reference

### research_status

Status of the AI research/enrichment for this contact.

| Value | Description | Frontend Display |
|-------|-------------|-----------------|
| `null` | Never researched | Show "Not researched" or enrichment CTA |
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
| `"marketingqualifiedlead"` | MQL (HubSpot-specific) |
| `"salesqualifiedlead"` | SQL |
| `"opportunity"` | Active sales opportunity |
| `"customer"` | Paying customer |
| `"evangelist"` | Active promoter/advocate |
| `"other"` | Other/custom stage |

### deal_status (in ContactDealInvolvement)

| Value | Description |
|-------|-------------|
| `"open"` | Active deal |
| `"won"` | Deal closed-won |
| `"lost"` | Deal closed-lost |
| `"closed"` | Deal closed (generic) |

### Field Nullability Reference

**ContactListItem fields:**

| Field | Can Be Null | When Null |
|-------|------------|-----------|
| `first_name` | Yes | Not provided or name splitting yielded no first name |
| `last_name` | Yes | Single-word name (e.g., "Madonna") |
| `full_name` | Yes | Rare — no name on contact record |
| `email` | Yes | Rare — CRM record had no email |
| `job_title` | Yes | Not provided |
| `phone` | Yes | Not provided |
| `company_name` | Yes | Not provided and no auto-resolution from domain |
| `company_id` | Yes | No explicit FK set and no account with matching email domain exists |
| `company_domain` | Yes | Contact has no email, or email has no valid domain |
| `lifecycle_stage` | Yes | Not set in CRM |
| `owner_name` | Yes | No owner assigned |
| `research_status` | Yes | Never researched |
| `last_researched_at` | Yes | Never researched |

**Additional fields in ContactDetail:**

| Field | Can Be Null | When Null |
|-------|------------|-----------|
| `mobile_phone` | Yes | Not provided |
| `linkedin_url` | Yes | Not stored in `properties.linkedin_url` |
| `address` | Yes | Not provided |
| `city` | Yes | Not provided |
| `state` | Yes | Not provided |
| `country` | Yes | Not provided |
| `contact_research` | Yes | Not yet researched |
| `last_contacted_at` | Yes | No logged contact touch |
| `last_activity_at` | Yes | No activity recorded |

---

## Response Models (TypeScript)

```ts
type UUID = string;
type ISODate = string; // ISO 8601 datetime with timezone (e.g., "2026-03-15T10:30:00Z")

/**
 * Contact item returned in list views (GET /contacts).
 * Use for table rows, cards, and search results.
 *
 * deal_count is computed from DealStakeholder records.
 * company_id is the stored FK when explicitly set, with email domain matching as fallback.
 * company_domain is extracted from the contact's email address.
 * source is derived from the integration type.
 */
export interface ContactListItem {
  contact_id: UUID;

  // Name fields
  first_name: string | null;
  last_name: string | null;
  full_name: string | null;           // Display name (may differ from first + last concatenation)

  // Communication
  email: string | null;               // Primary email (lowercased, trimmed)
  job_title: string | null;
  phone: string | null;               // Primary phone number as stored

  // Company association (stored FK when set, email domain matching as fallback)
  company_name: string | null;        // Company name string from CRM, explicit FK, or auto-resolved from domain
  company_id: UUID | null;            // Stored FK when explicitly set; otherwise resolved from email domain matching
  company_domain: string | null;      // Extracted from email address (e.g., "acme.com")

  // Segmentation
  lifecycle_stage: string | null;     // Free-form CRM value
  owner_name: string | null;          // Contact owner display name

  // Computed
  deal_count: number;                 // Deals where contact is an approved stakeholder (excludes removed/rejected/pending)
  source: string;                     // "manual" | "hubspot" | "salesforce" | "google_calendar" | "microsoft_calendar" | "gmail" | "outlook" | ...

  // Research/enrichment
  research_status: string | null;     // null | "pending" | "in_progress" | "completed" | "failed"
  last_researched_at: ISODate | null;

  // Timestamps
  created_at: ISODate;
  updated_at: ISODate;
}

/**
 * Full contact detail returned by GET /contacts/{contact_id}.
 * Extends ContactListItem with extended profile, research content,
 * and deal involvement array.
 */
export interface ContactDetail extends ContactListItem {
  // Extended communication
  additional_emails: string[];        // Secondary email addresses (may be empty array)
  mobile_phone: string | null;

  // Social
  linkedin_url: string | null;        // Stored in properties.linkedin_url JSONB

  // Location
  address: string | null;
  city: string | null;
  state: string | null;
  country: string | null;

  // Research
  contact_research: string | null;    // AI-generated research summary (markdown)

  // Activity timestamps
  last_contacted_at: ISODate | null;  // Last logged contact touch
  last_activity_at: ISODate | null;   // Latest recorded activity

  /** Deals this contact participates in as a stakeholder */
  deal_involvement: ContactDealInvolvement[];
}

/**
 * Deal involvement entry showing how a contact participates in a specific deal.
 * Nested inside ContactDetail.deal_involvement array.
 */
export interface ContactDealInvolvement {
  deal_id: UUID;
  deal_name: string | null;
  deal_status: string | null;         // "open" | "won" | "lost" | "closed"
  stage_name: string | null;          // Pipeline stage display name
  stakeholder_id: UUID;               // The DealStakeholder record linking this contact to the deal
  stance: string | null;              // Reserved for future: AI-inferred stakeholder stance
  decision_power: string | null;      // Reserved for future: AI-inferred decision power
  relationship_role: string | null;   // Role label (e.g., "decision_maker", "evaluator")
}

/**
 * Paginated response wrapper for list endpoints.
 */
export interface ContactsPage {
  items: ContactListItem[];
  page: number;                       // Current page (1-indexed)
  limit: number;                      // Items per page requested
  total_count: number;                // Total matching records
  total_pages: number;                // max(1, ceil(total_count / limit)) — always >= 1
}

/**
 * Request to create a new contact.
 */
export interface CreateContactRequest {
  email: string;                      // Required. Primary email (auto-lowercased, trimmed)
  name: string;                       // Required. Full name (auto-split into first/last)
  job_title?: string | null;
  phone?: string | null;
  linkedin_url?: string | null;       // Stored in properties.linkedin_url
  company_id?: UUID | null;           // Explicit account link. Validates account exists. Auto-resolves company_name.
}

/**
 * Request to update a contact. All fields optional.
 * Only include fields that should change.
 *
 * email: Can only be set if the contact currently has no email (null).
 * Attempting to change an existing email returns 400.
 */
export interface UpdateContactRequest {
  email?: string | null;              // Only accepted when contact.email is null (set once)
  first_name?: string | null;
  last_name?: string | null;
  full_name?: string | null;
  job_title?: string | null;
  phone?: string | null;
  mobile_phone?: string | null;
  linkedin_url?: string | null;       // Stored in properties JSONB
  company_name?: string | null;
  company_id?: UUID | null;           // Set to link account (validates exists), set to null to unlink
}

/**
 * Request to associate a contact with a deal.
 */
export interface AddContactToDealRequest {
  deal_id: UUID;                      // Required. Target deal
  relationship_role?: string | null;  // Optional role label (e.g., "decision_maker")
}

/**
 * Response after triggering contact enrichment.
 */
export interface EnrichContactResponse {
  job_id: string | null;              // UUID string of the dispatched job, or null
  message: string;                    // Human-readable status message
}
```

### Frontend Utility: Format Contact Display Name

```ts
/**
 * Build display name with fallback chain: full_name → first + last → email → "Unknown"
 */
function getContactDisplayName(contact: ContactListItem): string {
  if (contact.full_name) return contact.full_name;
  if (contact.first_name || contact.last_name) {
    return [contact.first_name, contact.last_name].filter(Boolean).join(' ');
  }
  if (contact.email) return contact.email;
  return 'Unknown Contact';
}
```

### Frontend Utility: Check if Contact is Enrichable

```ts
/**
 * Contact enrichment requires the contact to be linked to at least one deal
 * via a DealStakeholder record. Use deal_count from list or deal_involvement
 * from detail to check.
 */
function isEnrichable(contact: ContactListItem): boolean {
  return contact.deal_count > 0;
}
```

---

## Endpoints

### 1. List Contacts

```http
GET /users/crm/contacts
```

Paginated contact list with computed `deal_count`, `source`, `company_id`, and `company_domain` annotations. Results are sorted by `created_at` descending (newest first), then by `contact_id` descending for stable pagination.

#### Query Parameters

| Parameter    | Type   | Default | Validation | Description |
|-------------|--------|---------|------------|-------------|
| `page`      | integer| `1`     | `>= 1` | Page number (1-indexed) |
| `limit`     | integer| `25`    | `1–100` | Maximum contacts per page |
| `q`         | string | —       | Any string | Case-insensitive partial match on `full_name`, `email`, or `company_name` |
| `company_id`| UUID   | —       | Valid UUID | Filter contacts by account UUID (matches email domain to account domain) |

**Search behaviour (`q` parameter):**

- Matches anywhere in the string: `q=john` matches "John Doe"
- Matches on `full_name` OR `email` OR `company_name` (any one is sufficient)
- Example: `q=acme` matches company_name "Acme Corp" and email "<john@acme.com>"

**Company filter (`company_id` parameter):**

- Resolves the account's `domain`, then filters contacts whose email domain matches
- If the account has no `domain`, no contacts are filtered (returns contacts matching other filters)
- Cross-reference: use `company_id` from `GET /users/crm/accounts` list

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Contacts retrieved",
  "data": {
    "items": [
      {
        "contact_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "first_name": "John",
        "last_name": "Doe",
        "full_name": "John Doe",
        "email": "john@acme.com",
        "job_title": "VP Engineering",
        "phone": "+1-555-0101",
        "company_name": "Acme Corp",
        "company_id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
        "company_domain": "acme.com",
        "lifecycle_stage": "customer",
        "owner_name": "Jane Smith",
        "deal_count": 2,
        "source": "hubspot",
        "research_status": "completed",
        "last_researched_at": "2026-03-15T10:30:00Z",
        "created_at": "2026-01-10T08:00:00Z",
        "updated_at": "2026-03-20T14:00:00Z"
      }
    ],
    "page": 1,
    "limit": 25,
    "total_count": 156,
    "total_pages": 7
  }
}
```

**Empty results:** When no contacts match, returns `items: []`, `total_count: 0`, `total_pages: 1`.

**Note on `total_pages`:** Minimum value is `1` (even when there are zero results), unlike the accounts endpoint which returns `0`.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Page must be >= 1"` | `page` parameter < 1 |
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `429` | `"Too many requests (per-minute limit exceeded)"` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 2. Create Contact

```http
POST /users/crm/contacts
```

Creates a new contact owned by the `INTERNAL_CRM` integration. Email is normalised (lowercased, trimmed) and checked for uniqueness within the organisation.

#### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` |
| `Content-Type` | Yes | `application/json` |

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `email` | string | **Yes** | Non-empty, must contain `@` | Primary email (auto-lowercased, trimmed) |
| `name` | string | **Yes** | Non-empty | Full name (auto-split into first/last) |
| `job_title` | string | No | — | Job title |
| `phone` | string | No | — | Phone number |
| `linkedin_url` | string | No | — | LinkedIn profile URL (stored in `properties.linkedin_url`) |
| `company_id` | UUID | No | Must reference an existing account | Explicit account link. When provided, validates the account exists and auto-resolves `company_name` from the linked account. |

#### Auto-Resolution Behaviours

**Name splitting:**

| Input | `first_name` | `last_name` |
|-------|-------------|-------------|
| `"John Doe"` | `"John"` | `"Doe"` |
| `"John Michael Doe"` | `"John"` | `"Michael Doe"` |
| `"Madonna"` | `"Madonna"` | `null` |

**Account linking (`company_id`):**

Account linking is **explicit only** — there is no silent auto-linking from the email domain. The frontend dropdown handles account suggestion via `GET /accounts/suggest?email_domain=...` before the create request, so if the user wants to link, they pass `company_id` explicitly.

- If `company_id` is provided → service validates the account exists in the organisation and auto-resolves `company_name` from the linked account. Returns `404` if the account does not exist.
- If `company_id` is **not** provided → contact is created with `company_id = null` and `company_name = null`. The contact is unlinked.

**Domain matching as runtime fallback:** When `company_id` is `null`, list/detail endpoints fall back to email domain matching for **display purposes only** (returning a `company_id` in the response). This is a read-time fallback — it never writes to the FK column.

**Integration auto-creation:**
If the `INTERNAL_CRM` integration doesn't exist for the organisation, it is automatically created on first contact creation. This is idempotent.

#### Example Request

```json
{
  "email": "john@acme.com",
  "name": "John Doe",
  "job_title": "VP Engineering",
  "phone": "+1-555-0101",
  "linkedin_url": "https://linkedin.com/in/johndoe"
}
```

#### Minimal Request

```json
{
  "email": "jane@example.com",
  "name": "Jane Smith"
}
```

#### Success Response (201)

```json
{
  "status": "SUCCESS",
  "message": "Contact created",
  "data": {
    "contact_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "first_name": "John",
    "last_name": "Doe",
    "full_name": "John Doe",
    "email": "john@acme.com",
    "job_title": "VP Engineering",
    "phone": "+1-555-0101",
    "company_name": "Acme Corp",
    "company_id": null,
    "company_domain": null,
    "lifecycle_stage": null,
    "owner_name": null,
    "deal_count": 0,
    "source": "manual",
    "research_status": null,
    "last_researched_at": null,
    "created_at": "2026-04-06T10:00:00Z",
    "updated_at": "2026-04-06T10:00:00Z"
  }
}
```

**Note:** `company_id` and `company_domain` are `null` in the create response when they are not resolved during the write. These are resolved at query time in list/detail endpoints. `company_name` may be populated via auto-resolution.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Account not found"` | `company_id` provided but no matching account exists in this organisation |
| `409` | `"A contact with email 'john@acme.com' already exists"` | Non-deleted contact with same email (case-insensitive) |
| `422` | Pydantic validation error | Missing `email` or `name`, or invalid field types |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 3. Get Contact Detail

```http
GET /users/crm/contacts/{contact_id}
```

Full contact detail including extended profile fields, research content, and deal involvement array.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `contact_id` | UUID | Yes | Contact identifier |

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Contact retrieved",
  "data": {
    "contact_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "first_name": "John",
    "last_name": "Doe",
    "full_name": "John Doe",
    "email": "john@acme.com",
    "job_title": "VP Engineering",
    "phone": "+1-555-0101",
    "company_name": "Acme Corp",
    "company_id": null,
    "company_domain": null,
    "lifecycle_stage": "customer",
    "owner_name": "Jane Smith",
    "deal_count": 2,
    "source": "manual",
    "research_status": "completed",
    "last_researched_at": "2026-03-15T10:30:00Z",
    "created_at": "2026-01-10T08:00:00Z",
    "updated_at": "2026-03-20T14:00:00Z",

    "additional_emails": ["john.doe@acme.com", "jdoe@personal.com"],
    "mobile_phone": "+1-555-0102",
    "linkedin_url": "https://linkedin.com/in/johndoe",
    "address": "123 Tech Blvd",
    "city": "San Francisco",
    "state": "CA",
    "country": "US",
    "contact_research": "## Professional Background\nJohn Doe is a seasoned engineering leader...",
    "last_contacted_at": "2026-03-18T09:00:00Z",
    "last_activity_at": "2026-03-20T14:00:00Z",

    "deal_involvement": [
      {
        "deal_id": "e3f4a5b6-c7d8-9012-efab-3456789abcde",
        "deal_name": "Acme Enterprise License",
        "deal_status": "open",
        "stage_name": "Negotiation",
        "stakeholder_id": "f4a5b6c7-d8e9-0123-fabc-456789abcdef",
        "stance": null,
        "decision_power": null,
        "relationship_role": "decision_maker"
      },
      {
        "deal_id": "a5b6c7d8-e9f0-1234-abcf-56789abcdef0",
        "deal_name": "Acme Support Renewal",
        "deal_status": "won",
        "stage_name": "Closed Won",
        "stakeholder_id": "b6c7d8e9-f0a1-2345-bcda-6789abcdef01",
        "stance": null,
        "decision_power": null,
        "relationship_role": "evaluator"
      }
    ]
  }
}
```

**Notes:**

- `deal_count` equals the length of `deal_involvement` array
- `stance` and `decision_power` are reserved for future AI-inferred values (currently always `null`)
- `relationship_role` comes from the `source_reference_id` on the `DealStakeholder` record
- `company_id` and `company_domain` are `null` in the detail response (not computed in this endpoint — use the list endpoint for resolved company association)
- `linkedin_url` is extracted from `properties.linkedin_url` JSONB — it is `null` if not set

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Contact not found"` | No non-deleted contact with this ID in this organisation |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 4. Update Contact

```http
PATCH /users/crm/contacts/{contact_id}
```

Partial update — only send fields you want to change. Omitted fields remain unchanged.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `contact_id` | UUID | Yes | Contact identifier |

#### Request Body

All fields optional. Only include fields to update.

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `email` | string | See rules below | Primary email (can only be set if currently null) |
| `first_name` | string | — | First name |
| `last_name` | string | — | Last name |
| `full_name` | string | — | Full display name |
| `job_title` | string | — | Job title |
| `phone` | string | — | Phone number |
| `mobile_phone` | string | — | Mobile phone number |
| `linkedin_url` | string | — | LinkedIn URL (stored in `properties` JSONB, not a top-level column) |
| `company_name` | string | — | Company name string |
| `company_id` | UUID | Must reference an existing account, contact must not already be linked, contact must have an email | Explicit account link — **set once**. When set, validates the account exists and updates `company_name` from the account. |

**Company ID field rules — `company_id` is SET ONCE (immutable after first assignment):**

| Contact's current state | Action | Result |
|------------------------|--------|--------|
| `company_id = null` and has email | Send `company_id` with a valid account UUID | Linked, `company_name` auto-updated from account |
| `company_id = null` and **no email** | Send `company_id` | `400` — "Contact must have an email address before being linked to an account" |
| `company_id = null` | Send non-existent UUID | `404` — account not found |
| `company_id` already set (non-null) | Send any `company_id` value | `400` — "Cannot change the account linked to this contact" |
| Any state | Send `company_id: null` | `400` — "Cannot unlink a contact from its account" |
| Any state | Omit `company_id` from body | No change |

**Email field rules:**

| Contact's current email | Action | Result |
|------------------------|--------|--------|
| `null` (no email) | Send `email` in body | Email is set (lowercased, trimmed), duplicate check enforced |
| `null` (no email) | Send duplicate email | `409` — email already exists on another contact |
| Has existing email | Send `email` in body | `400` — cannot change an existing email |
| Has existing email | Omit `email` from body | No change (other fields update normally) |

**Frontend implementation:**

```ts
// Conditionally show/hide email input on the edit form
const canEditEmail = contact.email === null;
```

**Note on `linkedin_url`:** This field is stored in the `properties` JSONB column as `properties.linkedin_url`, not as a top-level database column. The API abstracts this — send it as a top-level field in the request body.

#### Example Request

```json
{
  "job_title": "CTO",
  "phone": "+1-555-0200",
  "linkedin_url": "https://linkedin.com/in/johndoe-cto"
}
```

#### Success Response (200)

Returns the updated `ContactListItem`. `deal_count` is `0`, `company_id` and `company_domain` are `null` (not re-computed in update response).

```json
{
  "status": "SUCCESS",
  "message": "Contact updated",
  "data": {
    "contact_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "full_name": "John Doe",
    "job_title": "CTO",
    "phone": "+1-555-0200",
    "deal_count": 0,
    "company_id": null,
    "company_domain": null,
    "source": "manual",
    "...": "other ContactListItem fields"
  }
}
```

**Frontend note:** After a successful update, either refetch the contact detail or merge the response into local state. `deal_count`, `company_id`, and `company_domain` will be `0`/`null` in the PATCH response — use previously cached values or refetch if needed.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Cannot change an existing email address"` | Contact already has an email and `email` was sent in body |
| `400` | `"Cannot change the account linked to this contact"` | Contact already has `company_id` set and a new value was sent |
| `400` | `"Cannot unlink a contact from its account"` | `company_id` was sent as `null` |
| `400` | `"Contact must have an email address before being linked to an account..."` | Contact has no email and `company_id` was sent |
| `404` | `"Contact not found"` | No non-deleted contact with this ID in this organisation |
| `404` | `"Account not found"` | `company_id` provided but no matching account exists in this organisation |
| `409` | `"A contact with email '...' already exists"` | Email being set is already used by another contact in this org |
| `422` | Pydantic validation error | Invalid field types in body |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 5. Enrich Contact

```http
POST /users/crm/contacts/{contact_id}/enrich
```

Trigger research enrichment for a contact. Enrichment piggybacks on the **stakeholder research pipeline**, which requires the contact to be linked to at least one deal via a `DealStakeholder` record.

#### Prerequisites

The contact must have at least one `DealStakeholder` record (i.e., be added to a deal). If not, the endpoint returns a success response with a message indicating the prerequisite.

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `contact_id` | UUID | Yes | Contact identifier |

#### Response Scenarios

**Scenario A: Enrichment dispatched (200)**

Contact has a stakeholder record and research was queued:

```json
{
  "status": "SUCCESS",
  "message": "Enrich contact request processed",
  "data": {
    "job_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "message": "Enrichment research dispatched"
  }
}
```

**Scenario B: Not linked to any deal (200)**

Contact has no `DealStakeholder` records:

```json
{
  "status": "SUCCESS",
  "message": "Enrichment not available",
  "data": {
    "job_id": null,
    "message": "Contact must be added to a deal before enrichment can run"
  }
}
```

**Frontend implementation:** Show a message like "Add this contact to a deal first" with a CTA to the "Add to Deal" action.

**Scenario C: Skipped due to cooldown (200)**

Recent enrichment was already performed and the cooldown period hasn't elapsed:

```json
{
  "status": "SUCCESS",
  "message": "Enrich contact request processed",
  "data": {
    "job_id": null,
    "message": "Enrichment skipped (cooldown)"
  }
}
```

**Frontend implementation:** Show "Recently enriched" or disable the enrich button temporarily.

#### Frontend Decision Tree

```
Is deal_count > 0?
  ├── No  → Show "Add to deal first" message, disable enrich button
  └── Yes → Call POST /enrich
              ├── job_id != null → Show "Enrichment in progress" spinner
              └── job_id == null → Show message from response (cooldown or other)
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Contact not found"` | No non-deleted contact with this ID in this organisation |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 6. Add Contact to Deal

```http
POST /users/crm/contacts/{contact_id}/add-to-deal
```

Associate a contact with a deal as a participant. This is a **two-phase process**:

1. **Synchronous**: Creates a `CRMDealParticipant` record linking the contact to the deal
2. **Asynchronous**: Dispatches a `stakeholder.sync_deal_stakeholders` worker task that creates the corresponding `DealStakeholder` record (with `contact_id`, engagement metrics, and matching)

The `DealStakeholder` record is what powers the contact detail's `deal_involvement` array and the deal's stakeholder list. Because phase 2 is async, there may be a brief delay (typically < 5 seconds) before the contact appears in `deal_involvement`.

Duplicate detection is handled — if the contact is already on the deal, it is reported in the `skipped` array instead of creating a duplicate.

#### Prerequisites

The contact **must have an email address**. Email is required for the `DealStakeholder` unique constraint `(deal_id, email)` and for stakeholder matching. If the contact has no email, the endpoint returns `400` with a message to add an email first.

**Frontend implementation:**

```ts
// Gate the "Add to Deal" button on email existence
const canAddToDeal = contact.email !== null;
// If no email, show: "Add an email address to this contact before adding to a deal"
```

#### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `contact_id` | UUID | Yes | Contact identifier |

#### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` |
| `Content-Type` | Yes | `application/json` |

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `deal_id` | UUID | **Yes** | Valid deal UUID | Target deal to add the contact to |
| `relationship_role` | string | No | — | Role label (e.g., `"decision_maker"`, `"evaluator"`, `"champion"`) |

#### Example Request

```json
{
  "deal_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "relationship_role": "decision_maker"
}
```

#### Success Response — New Association (200)

```json
{
  "status": "SUCCESS",
  "message": "Contact added to deal",
  "data": {
    "created": 1,
    "skipped": [],
    "contact_participant_changed": false
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `created` | number | Count of new participant records created (0 or 1) |
| `skipped` | array | Participants that were not created (with reason) |
| `contact_participant_changed` | boolean | Whether the contact-level participant record was modified |

#### Success Response — Already Exists (200)

```json
{
  "status": "SUCCESS",
  "message": "Contact added to deal",
  "data": {
    "created": 0,
    "skipped": [
      {
        "participant_type": "contact",
        "related_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "relationship_role": "decision_maker",
        "reason": "duplicate"
      }
    ],
    "contact_participant_changed": false
  }
}
```

**Frontend implementation:**

```ts
const result = response.data;
if (result.created > 0) {
  showSuccess('Contact added to deal');
} else if (result.skipped.length > 0) {
  showInfo('Contact is already on this deal');
}
```

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Contact must have an email address before being added to a deal..."` | Contact has no email set |
| `404` | `"Contact not found"` | No non-deleted contact with this ID in this organisation |
| `422` | Pydantic validation error | Missing `deal_id` or invalid UUID format |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

## Frontend: Account Linking Dropdown Flow

The frontend account dropdown is **split into two sections**:

- **Suggested** (top) — populated from `GET /accounts/suggest?email_domain=...`. **Shown but not auto-selected.** The user must click to confirm.
- **Search** (input + results) — typing fires `GET /accounts?q=...`. Results show under the suggestion.

The dropdown should display **account name + domain** (and optionally industry) — never the raw `company_id` UUID.

### Backend rules the frontend must respect

1. **`company_id` is set once** — never changes, never unlinks. Once a contact has an account, the dropdown should be **read-only** for that contact.
2. **Email is a precondition** — you cannot link an account to a contact that has no email. If `contact.email` is null, the dropdown must be disabled with a message like "Add an email address before linking an account."
3. **No silent auto-linking** — the backend will NOT auto-link based on email domain. If the user wants the link, the frontend must explicitly send `company_id`.
4. **Display fallback** — for unlinked contacts (`company_id = null`), list/detail responses still return a `company_id` resolved from email domain matching for display purposes only. The dropdown should treat these as "suggested" not "linked".

### Flow A: Creating a new contact

```
1. User fills in name + email
2. When email looks complete (basic regex):
   → GET /accounts/suggest?email_domain=acme.com
3. Dropdown shows:
   - Suggested: "Acme Corp — acme.com" (NOT pre-selected)
   - Search input
4. User can:
   - Click suggested → selects it
   - Type to search → GET /accounts?q=...
   - Click "Create new account" → POST /accounts inline
   - Skip → leaves dropdown empty
5. Submit → POST /contacts with company_id (or without)
```

### Flow B: Adding an account to an existing contact

```
1. Contact exists with company_id = null
2. User clicks the account dropdown
3. PRECONDITION CHECK: contact.email exists?
   - No  → dropdown disabled, "Add an email first"
   - Yes → enable, GET /accounts/suggest?email_domain=...
4. Same dropdown UX as Flow A (suggested + search + create)
5. User picks/creates → PATCH /contacts/{id} with company_id
6. Once linked → dropdown becomes read-only (set-once rule)
```

### Frontend example

```ts
// 1. Auto-suggest as user types email
async function onEmailChange(email: string) {
  const domain = email.split('@')[1];
  if (!domain || !domain.includes('.')) return;  // basic completeness check
  const { data } = await api.get(`/users/crm/accounts/suggest?email_domain=${domain}`);
  setSuggestedAccount(data);  // shown in "Suggested" section, NOT auto-selected
}

// 2. User searches for an account
async function onAccountSearch(query: string) {
  const { data } = await api.get(`/users/crm/accounts?q=${query}`);
  setSearchResults(data.items);
}

// 3. Gate the dropdown on existing contact's email + lock state
const canEditAccount = contact.email !== null && contact.company_id === null;
// canEditAccount === false  →  show read-only "Linked to {company_name}" or
//                              "Add email first" disabled state
```

### Endpoints used

| Endpoint | Purpose |
|----------|---------|
| `GET /users/crm/accounts/suggest?email_domain=...` | Auto-suggest based on email domain |
| `GET /users/crm/accounts?q=...` | Search accounts by name/domain |
| `POST /users/crm/accounts` | Create a new account inline |
| `POST /users/crm/contacts` (with `company_id`) | Create contact with account link |
| `PATCH /users/crm/contacts/{id}` (with `company_id`) | Link an existing contact (one-time) |

---

## Error Catalog

Consolidated reference of all error responses across all endpoints.

| HTTP Status | Error Message | Endpoint(s) | Cause |
|-------------|--------------|-------------|-------|
| `400` | `"Page must be >= 1"` | List contacts | Invalid page parameter |
| `400` | `"Cannot change an existing email address"` | Update | Attempted to change a non-null email |
| `400` | `"Cannot change the account linked to this contact"` | Update | Attempted to change `company_id` on a contact that already has one |
| `400` | `"Cannot unlink a contact from its account"` | Update | Attempted to set `company_id` to `null` |
| `400` | `"Contact must have an email address before being linked to an account..."` | Update | Tried to set `company_id` on a contact with no email |
| `400` | `"Contact must have an email address before being added to a deal..."` | Add to deal | Contact has no email |
| `401` | `"Invalid or missing user credentials."` | All | Missing/invalid JWT token |
| `404` | `"Contact not found"` | Get detail, Update, Enrich, Add to deal | Contact doesn't exist or is deleted |
| `404` | `"Account not found"` | Create, Update | `company_id` references a non-existent account |
| `409` | `"A contact with email '...' already exists"` | Create, Update | Duplicate email (case-insensitive) |
| `422` | Pydantic validation error | Create, Update, Add to deal | Invalid request body |
| `429` | `"Too many requests (per-minute limit exceeded)"` | All | Per-minute rate limit exceeded |
| `429` | `"Too many requests (per-hour limit exceeded)"` | All | Per-hour rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | All | Unhandled exception |

---

## Backend Architecture Notes

> This section is for backend developers working on the service layer.

### Database

**Table:** `crm_contacts` (tenant-scoped, in `org_*` schema)

**Key indexes:**

| Index | Columns | Purpose |
|-------|---------|---------|
| `ix_crm_contact_org_email` | `organisation_id, email` | Email lookups and duplicate checks |
| `ix_crm_contact_lifecycle` | `organisation_id, lifecycle_stage` | Lifecycle filtering |

**Migration:** Research fields (`contact_research`, `last_researched_at`, `research_status`) added in migration `3c7f79637798_030_add_contact_company_research_fields`.

### Service Layer

**Class:** `ContactsAccessService` (`src/service/crm/access/contacts_access_service.py`)

- Receives `org_session` and `organisation_id` at construction
- All queries filter by `organisation_id` and `is_deleted = False`
- `list_contacts()` uses:
  - Correlated subquery for `deal_count` (count of DealStakeholder records)
  - Batch-resolved `integration_type` for `source` annotation
  - `company_id` resolved from stored FK (when set) with email domain matching as fallback
- `create_contact()` auto-creates the `INTERNAL_CRM` integration if it doesn't exist; accepts optional `company_id` to set explicit account link
- `update_contact()` accepts `company_id` to link/unlink accounts; validates account exists when non-null
- `update_contact()` handles `linkedin_url` specially — stored in `properties` JSONB, not a top-level column
- `add_contact_to_deal()` delegates to `DealParticipantService` for centralized participant creation and duplicate detection

### Email Domain Extraction

```python
# Extract domain from email for company matching
def _extract_domain(email: str) -> str | None:
    if not email or "@" not in email:
        return None
    parts = email.rsplit("@", 1)
    domain = parts[1].strip().lower()
    return domain if domain else None
```

### Name Splitting Logic

```python
# Split "First Last" into (first, last). Last may be None for single names.
def _split_name(full_name: str) -> tuple[str, str | None]:
    parts = full_name.strip().split(" ", 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else None
    return first, last
```

### Enrichment Pipeline Architecture

Contact enrichment flows through the stakeholder research pipeline:

```
POST /contacts/{id}/enrich
  → Find DealStakeholder for this contact
    → dispatch_stakeholder_research_async()
      → Creates a deal_job in the deal_worker
        → Runs stakeholder research agent
          → Updates contact_research, research_status on CRMContact
```

This means enrichment requires a stakeholder linkage. A standalone contact research pipeline (independent of deals) is planned for a future phase.

### Duplicate Detection

- **Email uniqueness**: Checked via case-insensitive query (`func.lower(email) == input.lower()`) against non-deleted contacts in the same organisation
- **Not globally unique**: The same email can exist across different organisations (multi-tenant isolation)
