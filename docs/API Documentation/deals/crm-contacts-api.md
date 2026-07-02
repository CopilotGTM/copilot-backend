# CRM Contacts API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-06-27

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
Deal Participant (CRMDealParticipant, participant_type=CONTACT)  ←  lives on a specific deal
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

### Contact -> Account (Stored FK + Domain Matching)

Contacts are linked to accounts via two mechanisms. The **stored FK** takes precedence over domain matching:

1. **Stored `company_id` FK** (stored on `CRMContact`): Set during contact create by email-domain account resolution, or later through the update endpoint or a dedicated account-linking flow. When present, the contact is directly linked to the referenced `CRMCompany` record.
2. **Email domain matching** (fallback): When `company_id` is `null`, the system extracts the email domain and matches it against `CRMCompany.domain` at query time.

```
Contact (company_id: "d3b07384-...")
  -> direct FK lookup -> Account (id: "d3b07384-...")     <- stored link (preferred)

Contact (company_id: null, email: "john@acme.com")
  -> extract domain -> "acme.com" -> match CRMCompany.domain
  -> Account (domain: "acme.com")                        <- fallback
```

This means:

- `company.id` in the list response is either the **stored FK** (when set) or **resolved at query time** from email domain matching.
- `company.domain` comes from the matched account when available, otherwise the contact email domain is used as display context.
- If no account is linked or matched, the list may still return a `company` object with the contact's company string/domain context; otherwise `company` is `null`.
- When `company_id` is set during update/linking, `company_name` is auto-resolved from the linked account for legacy mutation responses. Detail responses expose the account through nested `company`.

### Contact → Deals (via Deal Participant)

Contacts are linked to deals through active `CRMDealParticipant` rows where `participant_type = CONTACT`. The `deal_count` field counts only **approved** participants (`approval_status = APPROVED`). Removed, rejected, and pending participants are excluded from the count and from the `deal_involvement` array in the detail view.

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
| `full_name` | Yes | Rare — no name on contact record |
| `email` | Yes | Rare — CRM record had no email |
| `additional_emails` | No | Empty array when none are provided |
| `job_title` | Yes | Not provided |
| `phone` | Yes | Not provided |
| `mobile_phone` | Yes | Not provided |
| `linkedin_url` | Yes | Not provided |
| `company` | Yes | No explicit account, no matching email-domain account, and no company string/domain |
| `company.id` | Yes | Company context exists but no linked/resolved account ID is available |
| `company.name` | Yes | Company context exists but no name is available |
| `company.domain` | Yes | Company context exists but no domain is available |
| `owner` | Yes | No owner metadata is available |
| `owner.id` | Yes | Owner context exists but no internal owner ID is resolved |
| `owner.name` | Yes | Owner context exists but no owner display name is available |
| `owner.email` | Yes | Owner context exists but no owner email is available |
| `last_contacted_at` | Yes | No logged contact touch |
| `last_activity_at` | Yes | No activity recorded |
| `profile_enriched_at` | Yes | Structured enrichment has not run |

**Additional fields in ContactDetail:**

| Field | Can Be Null | When Null |
|-------|------------|-----------|
| `first_name` | Yes | Not provided or name splitting yielded no first name |
| `last_name` | Yes | Single-word name (e.g., "Madonna") |
| `website` | Yes | Not provided |
| `lifecycle_stage` | Yes | Not set in CRM |
| `lead_status` | Yes | Not set in CRM |
| `marketing_status` | Yes | Not set in CRM |
| `company` | Yes | No explicit account, no matching email-domain account, and no company string/domain |
| `owner` | Yes | No owner assigned |
| `mobile_phone` | Yes | Not provided |
| `linkedin_url` | Yes | Not provided |
| `address` | Yes | Not provided |
| `city` | Yes | Not provided |
| `state` | Yes | Not provided |
| `postal_code` | Yes | Not provided |
| `country` | Yes | Not provided |
| `timezone` | Yes | Not provided |
| `research_summary` | Yes | Contact has no research text and no research timestamp |
| `research_summary.summary` | Yes | Not yet researched |
| `profile_enrichment.source` | Yes | Structured enrichment has not run |
| `profile_enrichment.enriched_at` | Yes | Structured enrichment has not run |
| `last_contacted_at` | Yes | No logged contact touch |
| `last_activity_at` | Yes | No activity recorded |

---

## Response Models (TypeScript)

```ts
type UUID = string;
type ISODate = string; // ISO 8601 datetime with timezone (e.g., "2026-03-15T10:30:00Z")

/**
 * Contact item returned by GET /contacts.
 * This list-only contract is optimized for table rows, cards, and search results.
 */
export interface ContactListItem {
  contact_id: UUID;

  full_name: string | null;           // Display name (may differ from first + last concatenation)
  email: string | null;               // Primary email (lowercased, trimmed)
  additional_emails: string[];
  job_title: string | null;
  phone: string | null;
  mobile_phone: string | null;
  linkedin_url: string | null;
  company: ContactListCompany | null;
  owner: ContactListOwner | null;
  deal_count: number;                 // Deals where contact is an approved stakeholder (excludes removed/rejected/pending)
  last_contacted_at: ISODate | null;
  last_activity_at: ISODate | null;
  source: string;                     // "manual" | "hubspot" | "salesforce" | "google_calendar" | "microsoft_calendar" | "gmail" | "outlook" | ...
  is_profile_enriched: boolean;
  profile_enriched_at: ISODate | null;
  created_at: ISODate;
  updated_at: ISODate;
}

export interface ContactListCompany {
  id: UUID | null;
  name: string | null;
  domain: string | null;
}

export interface ContactListOwner {
  id: UUID | null;
  name: string | null;
  email: string | null;
}

/**
 * Legacy flat contact shape returned by create/update responses.
 * GET /contacts uses ContactListItem and GET /contacts/{contact_id} uses ContactDetail.
 */
export interface ContactMutationItem {
  contact_id: UUID;
  first_name: string | null;
  last_name: string | null;
  full_name: string | null;
  email: string | null;
  job_title: string | null;
  phone: string | null;
  company_name: string | null;
  company_id: UUID | null;
  company_domain: string | null;
  lifecycle_stage: string | null;
  owner_name: string | null;
  deal_count: number;
  source: string;
  created_at: ISODate;
  updated_at: ISODate;
}

/**
 * Account context returned by GET /contacts/{contact_id}.
 */
export interface ContactDetailCompany extends ContactListCompany {
  website: string | null;
  industry: string | null;
  linkedin_url: string | null;
  country: string | null;
}

export interface ContactResearchSummary {
  summary: string | null;             // AI/contact research text
  last_researched_at: ISODate | null;
}

export interface ContactProfileEnrichment {
  is_enriched: boolean;
  source: string | null;
  enriched_at: ISODate | null;
  data: Record<string, unknown>;
}

export interface ContactDetailFieldDefinition {
  key: string;                        // Field path in ContactDetail, e.g. "company.name"
  label: string;                      // Display label for Get Details
  data_type: string;                  // string, email, phone, url, uuid, datetime, boolean, object, etc.
  order: number;                      // Ascending display order
  section: string;                    // contact, account, owner, location, activity, research, enrichment
  filter_control: string;             // text, select, date_range, boolean, link, people, etc.
  editable: boolean;                  // Whether normal inline edit is allowed
  patch_field: keyof UpdateContactRequest | null;
  edit_control: string | null;        // text, email, phone, url, account_picker, textarea, etc.
  edit_policy:
    | "always"
    | "set_if_empty"
    | "set_once"
    | "advanced_override"
    | "read_only"
    | "related_entity";
  locked_reason?: string | null;      // Why the field is read-only in normal UI
}

/**
 * Page-ready contact detail returned by GET /contacts/{contact_id}.
 * The response is flat for contact fields and nests only real related objects.
 */
export interface ContactDetail {
  detail_field_definitions: ContactDetailFieldDefinition[];
  contact_id: UUID;
  first_name: string | null;
  last_name: string | null;
  full_name: string | null;
  job_title: string | null;

  email: string | null;
  additional_emails: string[];
  phone: string | null;
  mobile_phone: string | null;
  linkedin_url: string | null;
  website: string | null;

  source: string;
  lifecycle_stage: string | null;
  lead_status: string | null;
  marketing_status: string | null;

  company: ContactDetailCompany | null;
  owner: ContactListOwner | null;

  address: string | null;
  city: string | null;
  state: string | null;
  postal_code: string | null;
  country: string | null;
  timezone: string | null;

  last_contacted_at: ISODate | null;
  last_activity_at: ISODate | null;
  created_at: ISODate;
  updated_at: ISODate;
  source_created_at: ISODate | null;
  source_modified_at: ISODate | null;
  synced_at: ISODate;

  research_summary: ContactResearchSummary | null;
  profile_enrichment: ContactProfileEnrichment;
  deal_count: number;
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
  last_activity_at: ISODate | null;
  participant_id: UUID;               // The participant row ID used by stakeholder APIs
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
 * Lightweight contact picker item returned by GET /contacts/search.
 */
export interface ContactSearchItem {
  contact_id: UUID;
  full_name: string | null;
  email: string | null;
  job_title: string | null;
  company: ContactListCompany | null;
  match_type:
    | "exact_email"
    | "additional_email"
    | "email_prefix"
    | "exact_name"
    | "name_prefix"
    | "name"
    | "company"
    | "other";
}

/**
 * Cursor page for contact picker/typeahead search.
 */
export interface ContactSearchPage {
  items: ContactSearchItem[];
  next_cursor: string | null;
  has_more: boolean;
}

/**
 * Request to create a new contact.
 */
export interface CreateContactRequest {
  email?: string | null;              // Required unless linkedin_url or name is supplied
  name?: string | null;               // Can create a non-researchable placeholder when supplied alone
  job_title?: string | null;          // Optional. Job title when already known
  linkedin_url?: string | null;       // Required unless email or name is supplied; must be a person profile URL
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
  linkedin_url?: string | null;
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

export interface ContactActivityFeedPage {
  object_type: "CONTACT";
  object_id: UUID;
  items: ActivityFeedItem[];
  total_count: number;
  cursor: string | null;
}
```

### Frontend Utility: Format Contact Display Name

```ts
/**
 * Build display name with fallback chain: full_name → first + last → email → "Unknown"
 */
function getContactDisplayName(contact: ContactListItem): string {
  if (contact.full_name) return contact.full_name;
  if (contact.email) return contact.email;
  return 'Unknown Contact';
}
```

### Frontend Utility: Check if Manual Enrich is Available

```ts
/**
 * Manual legacy enrichment requires a deal participant. Contacts created through
 * POST /contacts are researched automatically when eligible, independent of deals.
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

Paginated contact list with computed `deal_count`, `source`, nested `company`, and nested `owner` annotations. Results are sorted by `created_at` descending (newest first), then by `contact_id` descending for stable pagination.

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
        "full_name": "John Doe",
        "email": "john@acme.com",
        "additional_emails": ["john.doe@acme.com"],
        "job_title": "VP Engineering",
        "phone": "+1-555-0101",
        "mobile_phone": "+1-555-0102",
        "linkedin_url": "https://linkedin.com/in/johndoe",
        "company": {
          "id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
          "name": "Acme Corp",
          "domain": "acme.com"
        },
        "owner": {
          "id": "c7d8e9f0-a1b2-3456-cdef-789abcdef012",
          "name": "Jane Smith",
          "email": "jane.smith@example.com"
        },
        "deal_count": 2,
        "last_contacted_at": "2026-03-18T09:00:00Z",
        "last_activity_at": "2026-03-20T14:00:00Z",
        "source": "hubspot",
        "is_profile_enriched": true,
        "profile_enriched_at": "2026-03-15T10:30:00Z",
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

### 1a. Search Contacts

```http
GET /users/crm/contacts/search
```

Lightweight contact picker/typeahead endpoint. Use this for add-to-deal pickers, add-stakeholder contact selection, browse search, and any general contact UI where company-name matches are useful. It does not return `total_count`; clients should use `next_cursor` for infinite scroll.

#### Query Parameters

| Parameter | Type | Default | Validation | Description |
|-----------|------|---------|------------|-------------|
| `q` | string | — | Any string | Search by primary email, additional emails, full name, or company name |
| `limit` | integer | `20` | `1–50` | Maximum contacts per cursor page |
| `cursor` | string | — | Opaque cursor | Cursor from the previous response |
| `company_id` | UUID | — | Valid UUID | Restrict results to contacts with this stored account link |

#### Ranking

Results are deterministic and ranked by match quality:

1. Exact primary email
2. Exact additional email
3. Primary email prefix
4. Exact full name
5. Name prefix
6. Name contains or company-name match
7. Other matched fields

Contact creation still performs its own duplicate-email check, so search endpoints are UX helpers, not the uniqueness boundary.

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Contacts search results retrieved",
  "data": {
    "items": [
      {
        "contact_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "full_name": "Sam Altman",
        "email": "sam@openai.com",
        "job_title": "CEO",
        "company": {
          "id": "d3b07384-d9a1-4e3b-8c1a-1f2e3d4c5b6a",
          "name": "OpenAI",
          "domain": "openai.com"
        },
        "match_type": "additional_email"
      }
    ],
    "next_cursor": "eyJyYW5rIjoxLCJuYW1lIjoic2FtIGFsdG1hbiIsImNvbnRhY3RfaWQiOiJhMWIyYzNkNC1lNWY2LTc4OTAtYWJjZC1lZjEyMzQ1Njc4OTAifQ==",
    "has_more": true
  }
}
```

#### Frontend Usage

- Debounce user input before calling this endpoint.
- Reset `cursor` whenever `q` or `company_id` changes.
- Fetch the next page when the dropdown scroll nears the end and `has_more` is `true`.
- Add-stakeholder clients should send the selected `contact_id` to `POST /users/crm/deals/{deal_id}/stakeholders`.
- The older `GET /users/crm/deals/{deal_id}/stakeholders/available-contacts?query=...` route remains as a compatibility alias for deployed add-stakeholder modals, but new clients should call this endpoint directly.
- For contact creation, use `/contacts/search/create-candidates` instead so company-name matches do not hide the create action.

---

### 1b. Search Contact Create Candidates

```http
GET /users/crm/contacts/search/create-candidates
```

Strict contact identity search for the create-contact flow. This endpoint searches only person identity fields and intentionally does **not** search `company_name`.

#### Query Parameters

| Parameter | Type | Default | Validation | Description |
|-----------|------|---------|------------|-------------|
| `q` | string | — | Any string | Search by primary email, additional emails, or full name |
| `limit` | integer | `20` | `1–50` | Maximum contacts per cursor page |
| `cursor` | string | — | Opaque cursor | Cursor from the previous response |

#### Ranking

Results are deterministic and ranked by identity match quality:

1. Exact primary email
2. Exact additional email
3. Primary email prefix
4. Exact full name
5. Name prefix
6. Name contains
7. Other matched fields

Use this endpoint before `POST /contacts` when the UI wants to show possible duplicates. For valid email input with no exact match, show a create-contact action; `POST /contacts` still rejects duplicate primary emails if another request creates the contact first.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | `"Invalid cursor"` | Cursor cannot be decoded or does not match the expected shape |
| `401` | `"Invalid or missing user credentials."` | Missing or invalid Bearer token |
| `429` | `"Too many requests (per-minute limit exceeded)"` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 2. Create Contact

```http
POST /users/crm/contacts
```

Creates a new contact owned by the `INTERNAL_CRM` integration. At least one of `email`, `linkedin_url`, or `name` is required. Email is normalised (lowercased, trimmed) and checked for uniqueness within the organisation when provided.
After the contact transaction commits, the API queues asynchronous contact research only for contacts with an email. Name-only contacts are saved as placeholders and do not enqueue research.

#### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| `Authorization` | Yes | `Bearer <access_token>` |
| `Content-Type` | Yes | `application/json` |

#### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `email` | string | No* | Non-empty, must contain `@` | Primary email (auto-lowercased, trimmed). Required unless `linkedin_url` or `name` is supplied. |
| `name` | string | No* | Non-empty when provided | Full name when already known. Can create a non-researchable placeholder when email and LinkedIn are absent. |
| `job_title` | string | No | Non-empty when provided | Job title when already known. |
| `linkedin_url` | string | No* | Must be a LinkedIn person profile path: `/in/` or `/pub/` | LinkedIn profile URL when already known. Required unless `email` or `name` is supplied. |

**LinkedIn URL type rule for frontend:** Contact forms must only send person profile LinkedIn URLs in `linkedin_url`: `linkedin.com/in/...` or `linkedin.com/pub/...`. Do not send organization URLs such as `linkedin.com/company/...`, `linkedin.com/school/...`, or `linkedin.com/showcase/...` to contact create/update; route those to account/company flows instead. The backend returns `400` with `Use a LinkedIn profile URL for contacts and stakeholders` when an organization URL is sent.

Unknown fields are rejected. Contact creation no longer accepts `phone` or account-link fields such as `company_id`, `account_id`, `account_name`, or `account_domain`.

#### Auto-Resolution Behaviours

**Name splitting:**

| Input | `first_name` | `last_name` |
|-------|-------------|-------------|
| `"John Doe"` | `"John"` | `"Doe"` |
| `"John Michael Doe"` | `"John"` | `"Michael Doe"` |
| `"Madonna"` | `"Madonna"` | `null` |
| omitted | `null` | `null` |

**Account linking:**

Create contact does not accept account inputs from the caller. The backend uses the email domain to resolve or create an internal CRM account through `AccountWriteService`, then stores `company_id` and `company_name` on the contact when a single account is available.

- If one active account exists with the email domain, the contact is linked to it.
- If no account exists with the email domain, an account is created with `name = domain` and `domain = domain`, then the contact is linked to it.
- If multiple accounts match the same domain, the email has no usable domain, or the contact is name-only, the contact is created without an account link.

**Integration auto-creation:**
If the `INTERNAL_CRM` integration doesn't exist for the organisation, it is automatically created on first contact creation. This is idempotent.

**Async contact research:**

After commit, `POST /contacts` attempts to enqueue `contact_research.process_job`.

- Name-only contacts are skipped because there is no email/domain identity to research.
- Free/personal email providers and their subdomains are skipped.
- Business subdomains are preserved, e.g. `buyer@test.hackme.com` researches against `test.hackme.com`.
- Provider enrichment writes structured profile fields and `profile_enrichment` first.
- A no-tool summary step writes `contact_research` only after provider data exists.
- If summary generation fails, structured enrichment remains saved.
- Queue dispatch failure is logged and does not change the `201` response.

**Async account enrichment from contact-created accounts:**

When contact creation creates a new internal CRM account from the contact email domain, the API also attempts to enqueue `account_research.process_job` for that new account after the same commit. Existing matched accounts are not re-enriched from this path.

#### Example Request

```json
{
  "email": "john@acme.com",
  "name": "John Doe",
  "job_title": "VP Sales",
  "linkedin_url": "https://linkedin.com/in/johndoe"
}
```

#### Minimal Request

```json
{
  "email": "jane@example.com"
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
    "job_title": "VP Sales",
    "phone": null,
    "company_name": "acme.com",
    "company_id": "d3b07384-d9a0-4c62-bfe9-123456789abc",
    "company_domain": null,
    "lifecycle_stage": null,
    "owner_name": null,
    "deal_count": 0,
    "source": "manual",
    "created_at": "2026-04-06T10:00:00Z",
    "updated_at": "2026-04-06T10:00:00Z"
  }
}
```

**Note:** `company_domain` is not recomputed in the create response. List/detail endpoints expose richer account context through nested `company`.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `409` | `"A contact with email 'john@acme.com' already exists"` | Non-deleted contact with same email (case-insensitive) |
| `422` | Pydantic validation error | Missing `email`, invalid field types, or removed fields such as `phone` or `company_id` |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 3. Get Contact Detail

```http
GET /users/crm/contacts/{contact_id}
```

Page-ready contact detail for the contact overview page. The response includes flat contact fields, nested account/owner context, contact research, profile enrichment metadata, and a compact deal involvement array for the right panel.

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
    "detail_field_definitions": [
      {
        "key": "full_name",
        "label": "Name",
        "data_type": "string",
        "order": 1,
        "section": "contact",
        "filter_control": "text",
        "editable": true,
        "patch_field": "full_name",
        "edit_control": "text",
        "edit_policy": "always",
        "locked_reason": null
      },
      {
        "key": "job_title",
        "label": "Job title",
        "data_type": "string",
        "order": 2,
        "section": "contact",
        "filter_control": "text",
        "editable": true,
        "patch_field": "job_title",
        "edit_control": "text",
        "edit_policy": "always",
        "locked_reason": null
      }
    ],
    "contact_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "first_name": "John",
    "last_name": "Doe",
    "full_name": "John Doe",
    "job_title": "VP Engineering",

    "email": "john@acme.com",
    "additional_emails": ["john.doe@acme.com", "jdoe@personal.com"],
    "phone": "+1-555-0101",
    "mobile_phone": "+1-555-0102",
    "linkedin_url": "https://linkedin.com/in/johndoe",
    "website": null,

    "source": "manual",
    "lifecycle_stage": "customer",
    "lead_status": null,
    "marketing_status": null,

    "company": {
      "id": "d3b07384-d9a0-4c8f-9f64-ef1234567890",
      "name": "Acme Corp",
      "domain": "acme.com",
      "website": "https://acme.com",
      "industry": "Software",
      "linkedin_url": null,
      "country": "US"
    },

    "owner": {
      "id": "b2c3d4e5-f6a7-8901-bcde-f23456789012",
      "name": "Jane Smith",
      "email": "jane@example.com"
    },

    "address": "123 Tech Blvd",
    "city": "San Francisco",
    "state": "CA",
    "postal_code": "94105",
    "country": "US",
    "timezone": "America/Los_Angeles",

    "last_contacted_at": "2026-03-18T09:00:00Z",
    "last_activity_at": "2026-03-20T14:00:00Z",
    "created_at": "2026-01-10T08:00:00Z",
    "updated_at": "2026-03-20T14:00:00Z",
    "source_created_at": "2026-01-10T08:00:00Z",
    "source_modified_at": "2026-03-20T14:00:00Z",
    "synced_at": "2026-03-20T14:05:00Z",

    "research_summary": {
      "summary": "John Doe leads engineering at Acme Corp and is likely involved in technical evaluation for CRM and GTM workflow changes.",
      "last_researched_at": "2026-03-15T10:30:00Z"
    },

    "profile_enrichment": {
      "is_enriched": true,
      "source": "apollo",
      "enriched_at": "2026-03-15T10:30:00Z",
      "data": {
        "headline": "Engineering executive"
      }
    },

    "deal_count": 2,

    "deal_involvement": [
      {
        "deal_id": "e3f4a5b6-c7d8-9012-efab-3456789abcde",
        "deal_name": "Acme Enterprise License",
        "deal_status": "open",
        "stage_name": "Negotiation",
        "last_activity_at": "2026-03-20T14:00:00Z",
        "participant_id": "f4a5b6c7-d8e9-0123-fabc-456789abcdef",
        "relationship_role": "decision_maker"
      },
      {
        "deal_id": "a5b6c7d8-e9f0-1234-abcf-56789abcdef0",
        "deal_name": "Acme Support Renewal",
        "deal_status": "won",
        "stage_name": "Closed Won",
        "last_activity_at": null,
        "participant_id": "b6c7d8e9-f0a1-2345-bcda-6789abcdef01",
        "relationship_role": "evaluator"
      }
    ]
  }
}
```

**Notes:**

- `detail_field_definitions` is the ordered metadata for the contact "Get Details" panel. Use `order` ascending and hide null/empty field values by default.
- `detail_field_definitions[].key` is a dotted path into `ContactDetail` for nested fields such as `company.name`, `owner.email`, or `research_summary.summary`.
- `deal_count` equals the length of `deal_involvement` array. Keep it as a top-level summary metric near the related-deals section; it is intentionally not repeated in `detail_field_definitions`.
- Contact detail does not return `pipeline_value`. Pipeline value belongs to account/deal rollups, not the contact "Get Details" scalar field map.
- `relationship_role` comes from the contact participant's `relationship_role`
- `deal_involvement` is intentionally compact for the contact right panel. It requires one participant-to-deal join and does not include stakeholder fields such as `stance`, `decision_power`, `temperature`, or research.
- `company` is resolved using explicit account linkage first, then email-domain matching for display fallback.
- `linkedin_url` is returned from the top-level contact profile field, with a legacy `properties.linkedin_url` fallback.
- `research_summary.summary` comes from contact-level research (`crm_contacts.contact_research`).
- `profile_enrichment.data` contains structured provider/enrichment data and may be an empty object.

#### Ordered Get Details Metadata

The backend returns the following predefined field definitions in `detail_field_definitions`.
The frontend should render by `order` ascending. Fields with no value can be hidden in the default view.
`deal_count` and `pipeline_value` are not included in this metadata: `deal_count` is already represented by `deal_involvement`, and contact detail has no `pipeline_value` field.

| Order | `key` | Label | `data_type` | `filter_control` | `editable` | `patch_field` | `edit_control` | `edit_policy` |
|---:|---|---|---|---|---|---|---|---|
| 1 | `full_name` | Name | `string` | `text` | true | `full_name` | `text` | `always` |
| 2 | `job_title` | Job title | `string` | `text` | true | `job_title` | `text` | `always` |
| 3 | `email` | Email | `email` | `email` | dynamic | `email` | `email` | `set_if_empty` |
| 4 | `additional_emails` | Additional emails | `array<email>` | `chips` | false | null | null | `read_only` |
| 5 | `phone` | Phone | `phone` | `phone` | true | `phone` | `phone` | `always` |
| 6 | `mobile_phone` | Mobile | `phone` | `phone` | true | `mobile_phone` | `phone` | `always` |
| 7 | `linkedin_url` | LinkedIn | `url` | `link` | dynamic | `linkedin_url` | `url` | `set_if_empty` |
| 8 | `website` | Website | `url` | `link` | false | null | null | `read_only` |
| 9 | `lifecycle_stage` | Lifecycle stage | `string` | `select` | false | null | null | `read_only` |
| 10 | `lead_status` | Lead status | `string` | `select` | false | null | null | `read_only` |
| 11 | `marketing_status` | Marketing status | `string` | `select` | false | null | null | `read_only` |
| 12 | `source` | Source | `string` | `select` | false | null | null | `read_only` |
| 13 | `company.id` | Account ID | `uuid` | `link` | dynamic | `company_id` | `account_picker` | `set_once` |
| 14 | `company.name` | Account | `string` | `account_link` | false | null | null | `related_entity` |
| 15 | `company.domain` | Account domain | `string` | `text` | false | null | null | `related_entity` |
| 16 | `company.website` | Account website | `url` | `link` | false | null | null | `related_entity` |
| 17 | `company.industry` | Account industry | `string` | `select` | false | null | null | `related_entity` |
| 18 | `company.linkedin_url` | Account LinkedIn | `url` | `link` | false | null | null | `related_entity` |
| 19 | `company.country` | Account country | `string` | `select` | false | null | null | `related_entity` |
| 20 | `owner.id` | Owner ID | `uuid` | `link` | false | null | null | `read_only` |
| 21 | `owner.name` | Owner | `string` | `people` | false | null | null | `read_only` |
| 22 | `owner.email` | Owner email | `email` | `email` | false | null | null | `read_only` |
| 23 | `address` | Address | `string` | `text` | false | null | null | `read_only` |
| 24 | `city` | City | `string` | `select` | false | null | null | `read_only` |
| 25 | `state` | State | `string` | `select` | false | null | null | `read_only` |
| 26 | `postal_code` | Postal code | `string` | `text` | false | null | null | `read_only` |
| 27 | `country` | Country | `string` | `select` | false | null | null | `read_only` |
| 28 | `timezone` | Timezone | `string` | `select` | false | null | null | `read_only` |
| 29 | `last_contacted_at` | Last contacted | `datetime` | `date_range` | false | null | null | `read_only` |
| 30 | `last_activity_at` | Last activity | `datetime` | `date_range` | false | null | null | `read_only` |
| 31 | `source_created_at` | CRM created date | `datetime` | `date_range` | false | null | null | `read_only` |
| 32 | `source_modified_at` | CRM modified date | `datetime` | `date_range` | false | null | null | `read_only` |
| 33 | `research_summary.summary` | Research summary | `markdown` | `text` | false | null | null | `read_only` |
| 34 | `research_summary.last_researched_at` | Last researched | `datetime` | `date_range` | false | null | null | `read_only` |
| 35 | `profile_enrichment.is_enriched` | Profile enriched | `boolean` | `boolean` | false | null | null | `read_only` |
| 36 | `profile_enrichment.source` | Enrichment source | `string` | `select` | false | null | null | `read_only` |
| 37 | `profile_enrichment.enriched_at` | Enriched at | `datetime` | `date_range` | false | null | null | `read_only` |
| 38 | `profile_enrichment.data` | Enrichment data | `object` | `advanced` | false | null | null | `read_only` |

#### Contact Editability Policy

Enterprise CRM systems usually separate field visibility from field editability. Field-level security
and property permissions decide who can edit a field, while system fields and calculated/enrichment
fields are commonly read-only or governed. Salesforce documents field-level security as the place
to control field access, HubSpot supports property-level restrictions, and Dataverse has column-level
security for sensitive fields. Apply the same idea here: `filter_control` is for list/detail filtering;
editability belongs beside it in field metadata, not inside filters.

Research references used for this policy:
[Salesforce FieldPermissions](https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_fieldpermissions.htm),
[HubSpot CRM properties](https://developers.hubspot.com/docs/api-reference/crm-properties-v3/guide),
and [Microsoft Dataverse column-level security](https://learn.microsoft.com/en-us/power-platform/admin/field-level-security).

Returned metadata shape:

```json
{
  "key": "linkedin_url",
  "label": "LinkedIn",
  "data_type": "url",
  "order": 7,
  "section": "contact",
  "filter_control": "link",
  "editable": true,
  "patch_field": "linkedin_url",
  "edit_control": "url",
  "edit_policy": "set_if_empty",
  "locked_reason": "Existing LinkedIn URL is treated as verified profile identity."
}
```

Do not add a separate frontend-only editability map long term. The scalable contract is one metadata
array per detail page where each field has display, filter, and edit hints.

##### Sales-Rep Editing Principles

- Sales reps should be able to correct obvious profile gaps that block outreach: missing name, title,
  phone, mobile phone, LinkedIn URL, and account link.
- Identity fields should be protected once set. Email and account linkage drive dedupe, stakeholder
  matching, activity matching, and account routing, so normal inline edit should not rewrite them.
- Verified enrichment should be preserved. If a LinkedIn URL or enrichment field exists from a trusted
  enrichment/source workflow, show it as locked in normal edit mode. Offer a separate "request correction"
  or advanced override flow later if product wants that.
- Related entity fields are edited on their own records. `company.name`, `company.domain`, owner fields,
  deal involvement, and research/enrichment metadata should not be patched through contact update.
- Backend remains the trust boundary. The frontend may hide controls, but `PATCH /contacts/{id}` must
  continue enforcing identity rules.

##### Current PATCH Behavior vs Recommended UI Policy

| Detail key | PATCH field | Current backend behavior | Recommended normal UI policy | Reason |
|---|---|---|---|---|
| `full_name` | `full_name` | Editable | Editable | Reps often know the correct display name. |
| `first_name` / `last_name` | `first_name`, `last_name` | Editable, not shown as primary detail rows | Editable only in an expanded name editor | Preserve simple display while allowing clean name fixes. |
| `job_title` | `job_title` | Editable | Editable | Title changes are common and useful for sales context. |
| `email` | `email` | Set only when current email is null; changing existing email returns `400` | Set if empty only | Email is an identity/dedupe key and matching input. |
| `additional_emails` | none | Read-only through current contact PATCH | Read-only until a dedicated secondary-email flow exists | Needs dedupe and primary/secondary semantics. |
| `phone` | `phone` | Editable | Editable | Low-risk outreach field. |
| `mobile_phone` | `mobile_phone` | Editable | Editable | Low-risk outreach field. |
| `linkedin_url` | `linkedin_url` | Set only when current LinkedIn URL is null; changing or clearing an existing URL returns `400` | Set if empty by default; lock existing values in normal UI | Treat profile URL as identity. Existing verified/enriched value should not be casually overwritten. |
| `website` | none in `UpdateContactRequest` | Ignored by PATCH if sent through typed API clients | Read-only for now | Low sales value on a person record; use account website. |
| `lifecycle_stage` | none in `UpdateContactRequest` | Not exposed in typed update request | Read-only until lifecycle workflow is defined | Lifecycle changes should be governed. |
| `lead_status` | none in `UpdateContactRequest` | Not exposed in typed update request | Read-only until sales-status workflow is defined | Avoid unsourced qualification churn. |
| `marketing_status` | none in `UpdateContactRequest` | Not exposed in typed update request | Read-only | Subscription/marketing consent should be governed. |
| `company.id` / `company.name` | `company_id` | `company_id` can be set once if null, contact has email, and account exists; cannot unlink/change | Set once via account picker; then read-only | Account link affects routing and rollups. Company name edits belong to account API. |
| `company.domain`, `company.website`, `company.industry`, `company.linkedin_url`, `company.country` | none | Read-only through contact PATCH | Related entity | Edit through account detail/update APIs. |
| `owner.*` | none | Read-only through contact PATCH | Read-only until assignment API exists | Owner changes need assignment/audit semantics. |
| Location fields: `address`, `city`, `state`, `postal_code`, `country`, `timezone` | same field names | Accepted by current write service if sent, though not listed as primary update fields | Editable only after product confirms sales value | Useful in some CRMs, but noisy for normal rep workflows. |
| Activity/system dates | none | Read-only | Read-only | Generated from CRM sync/activity systems. |
| `research_summary.*` | none | Read-only through contact PATCH | Read-only | AI/research output, not manually patched profile data. |
| `profile_enrichment.*` | none | Read-only through contact PATCH | Read-only | Provider/enrichment output; manual edits should go to first-class contact fields. |
| `deal_involvement[]` | none | Managed by add-to-deal/participant APIs | Related entity | Use `POST /contacts/{contact_id}/add-to-deal` and stakeholder APIs. |

##### Proposed `edit_policy` Values

| Value | Meaning | Contact examples |
|---|---|---|
| `always` | Inline edit can be shown whenever user has contact edit permission | `full_name`, `job_title`, `phone`, `mobile_phone` |
| `set_if_empty` | Show edit when the current value is null/empty; lock normal edit after a value exists | `email`, `linkedin_url` |
| `set_once` | Can be selected once, then cannot be changed/unlinked in normal flow | `company.id` via `company_id` |
| `advanced_override` | Hide from normal edit; future admin/audited override only | Existing verified `linkedin_url`, if product wants correction flow |
| `read_only` | Never editable through contact PATCH | sync dates, research, enrichment, owner fields today |
| `related_entity` | Navigate to another API/page to edit | account fields, deal involvement |

##### LinkedIn URL Recommendation

For sales reps, missing LinkedIn blocks research and outreach, so filling it should be easy. Replacing an
existing LinkedIn URL is different: it can break identity matching and overwrite enrichment. The recommended
UI policy is:

1. If `linkedin_url` is null: show inline "Add LinkedIn" and PATCH `linkedin_url`.
2. If `linkedin_url` exists: show it as a link, not a normal text input.
3. If the rep says it is wrong: use a future explicit correction flow with a reason, audit event, and ideally
   source confidence comparison. Until that exists, normal UI should not offer overwrite and the backend
   rejects normal overwrite/clear requests.

#### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `404` | `"Contact not found"` | No non-deleted contact with this ID in this organisation |
| `429` | `"Too many requests ..."` | Rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

### 4. Get Contact Activity Feed

```http
GET /users/crm/contacts/{contact_id}/activity-feed
```

Cursor-paginated CRM activity projection for a contact. This reads
`crm_activity_links -> crm_activities` for rows linked with `object_type =
CONTACT`; canonical notes and tasks remain in `crm_notes` and `crm_tasks`.

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

Contact lifecycle cards may use `source_type` values `CONTACT_CREATED`,
`CONTACT_ENRICHMENT_TRIGGERED`, and `CONTACT_ENRICHMENT_COMPLETED`.

#### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Activity feed retrieved successfully",
  "data": {
    "object_type": "CONTACT",
    "object_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "items": [],
    "total_count": 0,
    "cursor": null
  }
}
```

---

### 5. Update Contact

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
| `linkedin_url` | string | Can only be set when currently null | LinkedIn URL stored as a first-class contact profile field. Changing or clearing an existing URL returns `400`. |
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

**LinkedIn URL field rules:**

| Contact's current LinkedIn URL | Action | Result |
|-------------------------------|--------|--------|
| `null` | Send `linkedin_url` in body | LinkedIn URL is set |
| Has existing LinkedIn URL | Send a different `linkedin_url` | `400` — cannot change an existing LinkedIn URL |
| Has existing LinkedIn URL | Send `linkedin_url: null` | `400` — cannot change an existing LinkedIn URL |
| Has existing LinkedIn URL | Omit `linkedin_url` from body | No change (other fields update normally) |

Only person profile URLs are valid here: `linkedin.com/in/...` or `linkedin.com/pub/...`. Company, school, and showcase LinkedIn pages belong on account records.

**Frontend implementation:**

```ts
// Conditionally show/hide email input on the edit form
const canEditEmail = contact.email === null;
```

**Note on `linkedin_url`:** Send this as a top-level field. The contact profile stores it as a first-class contact URL, with legacy property fallback still handled in reads.

#### Example Request

```json
{
  "job_title": "CTO",
  "phone": "+1-555-0200",
  "linkedin_url": "https://linkedin.com/in/johndoe-cto"
}
```

#### Success Response (200)

Returns the updated legacy `ContactMutationItem`. `deal_count` is `0`, `company_id` and `company_domain` are not re-computed in the update response.

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
    "...": "other ContactMutationItem fields"
  }
}
```

**Frontend note:** After a successful update, either refetch the contact detail/list row or merge the response into local state. `deal_count` and resolved company display fields are not recomputed in the PATCH response — use previously cached values or refetch if needed.

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

### 6. Enrich Contact

```http
POST /users/crm/contacts/{contact_id}/enrich
```

Trigger legacy manual enrichment for a contact. This manual endpoint still piggybacks on the stakeholder research pipeline and requires a deal participant.

Contacts created through `POST /contacts` do **not** need this endpoint for first-pass research. The create route automatically queues contact-scoped research after commit when the email domain is eligible.

#### Prerequisites

For this manual endpoint, the contact must have at least one approved contact participant record (i.e., be added to a deal). If not, the endpoint returns a success response with a message indicating the prerequisite.

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

Contact has no approved contact participant records:

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

### 7. Add Contact to Deal

```http
POST /users/crm/contacts/{contact_id}/add-to-deal
```

Associate a contact with a deal as a participant. This is a **two-phase process**:

1. **Synchronous**: Creates a `CRMDealParticipant` record linking the contact to the deal
2. **Asynchronous**: Dispatches follow-up deal processing jobs for rematching and stakeholder read-model compatibility where needed

The `CRMDealParticipant` record powers the contact detail's `deal_involvement` array and the deal's stakeholder list. Because follow-up processing is async, there may be a brief delay (typically < 5 seconds) before related downstream surfaces refresh.

Duplicate detection is handled — if the contact is already on the deal, it is reported in the `skipped` array instead of creating a duplicate.

#### Prerequisites

The contact **must have an email address**. Email is required for stakeholder matching and duplicate detection. If the contact has no email, the endpoint returns `400` with a message to add an email first.

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
- **Search** (input + cursor results) — typing fires `GET /accounts/search?q=...`. Results show under the suggestion and load more with `next_cursor`.

The dropdown should display **account name + domain** (and optionally industry) — never the raw `company_id` UUID.

### Backend rules the frontend must respect

1. **`company_id` is set once** — never changes, never unlinks. The backend enforces this on PATCH.
2. **Email is a precondition** — you cannot link an account to a contact that has no email. If `contact.email` is null, the dropdown must be disabled with a message like "Add an email address before linking an account."
3. **Create resolves account by email domain** — `POST /contacts` does not accept account fields, but the backend resolves or creates an account from the email domain and links it when unambiguous.
4. **Display fallback** — for older or ambiguous unlinked contacts (`company_id = null`), list/detail responses may still return nested `company.id` from email-domain matching for display purposes only. Do not treat nested `company.id` alone as proof of a stored account link.

### Flow A: Creating a new contact

```
1. User enters email, LinkedIn URL, or name, and optionally job title
2. Submit -> POST /contacts with at least one of `email`, `linkedin_url`, or `name` plus optional `job_title`
3. Backend resolves LinkedIn through contact identity enrichment when provided, then resolves or creates an account from the final email domain when possible. Name-only creates an unresearched placeholder.
4. Response returns `company_id` and `company_name` when a stored account link was created
```

### Flow B: Adding an account to an existing contact

```
1. Contact exists with no stored account link
2. User clicks the account dropdown
3. PRECONDITION CHECK: contact.email exists?
   - No  → dropdown disabled, "Add an email first"
   - Yes → enable, GET /accounts/suggest?email_domain=...
4. Same dropdown UX as Flow A (suggested + search + create)
5. User picks/creates → PATCH /contacts/{id} with company_id
6. Once linked → backend rejects future account changes (set-once rule)
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
async function onAccountSearch(query: string, cursor?: string) {
  const params = new URLSearchParams({ q: query, limit: '20' });
  if (cursor) params.set('cursor', cursor);
  const { data } = await api.get(`/users/crm/accounts/search?${params.toString()}`);
  setSearchResults(data.items);
  setNextAccountCursor(data.next_cursor);
}

// 3. Gate the dropdown on existing contact's email.
// Do not infer stored-link state from contact.company.id alone; it may be a
// domain-match display fallback. The backend enforces the set-once rule on PATCH.
const canOpenAccountPicker = contact.email !== null;
// canOpenAccountPicker === false  →  show "Add email first" disabled state
// PATCH 400 "Cannot change the account linked to this contact" → show read-only linked state
```

### Endpoints used

| Endpoint | Purpose |
|----------|---------|
| `GET /users/crm/accounts/suggest?email_domain=...` | Auto-suggest based on email domain |
| `GET /users/crm/accounts/search?q=...&cursor=...` | Cursor search accounts by name/domain/website for dropdowns |
| `POST /users/crm/accounts` | Create a new account inline |
| `POST /users/crm/contacts` | Create contact and resolve/create an account from email domain when possible |
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
| `400` | `"Use a LinkedIn profile URL for contacts and stakeholders"` | Create, Update | An account/company LinkedIn URL was sent as a contact profile URL |
| `401` | `"Invalid or missing user credentials."` | All | Missing/invalid JWT token |
| `404` | `"Contact not found"` | Get detail, Update, Enrich, Add to deal | Contact doesn't exist or is deleted |
| `404` | `"Account not found"` | Update | `company_id` references a non-existent account |
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

**Classes:** `ContactReadService`, `ContactWriteService`, and `ContactDealLinkService` under `src/service/crm/contacts/`.

- Receives `org_session` and `organisation_id` at construction
- All queries filter by `organisation_id` and `is_deleted = False`
- `list_contacts()` uses:
  - Correlated subquery for `deal_count` (count of approved contact participant records)
  - Batch-resolved `integration_type` for `source` annotation
  - Nested `company` resolved from stored FK first, with email domain matching as display fallback
  - Nested `owner` built from contact owner metadata
- `create_contact()` auto-creates the `INTERNAL_CRM` integration if it doesn't exist; the canonical command accepts `email`, `linkedin_url`, or `name` plus optional `job_title`
- `create_contact()` does not accept `phone` or caller-supplied account-link fields; account resolution is derived from email domain through `AccountWriteService` and is skipped for name-only placeholders
- `update_contact()` accepts `company_id` to link/unlink accounts; validates account exists when non-null
- `update_contact()` handles `linkedin_url` as a top-level contact profile field
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

Contacts created through this API use the contact research pipeline when they have an email:

```
POST /contacts
  → commit CRMContact
    → dispatch_contact_research_async() only if contact.email exists
      → creates a contact_research DealJobs row
        → contact_research.process_job
          → provider enrichment writes structured CRMContact fields
          → no-tool summary agent writes contact_research when provider data exists
```

This automatic path does not require a stakeholder or deal linkage. Stakeholder-specific enrichment paths can migrate to the same contact-first pipeline later.

### Duplicate Detection

- **Email uniqueness**: Checked via case-insensitive query (`func.lower(email) == input.lower()`) against non-deleted contacts in the same organisation
- **Not globally unique**: The same email can exist across different organisations (multi-tenant isolation)
