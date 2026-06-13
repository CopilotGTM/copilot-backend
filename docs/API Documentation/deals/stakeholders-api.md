# Deal Stakeholders API Documentation

## Base URL

```text
/users/crm/deals
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

- Write endpoints (`POST`): ~15/min, 100/hour per user
- Read endpoints (`GET`): ~60/min, 1000/hour per user
- On limit breach: `429 Too Many Requests` with `Retry-After`

---

## Domain Validation

Stakeholders should belong to the company associated with the deal. The system enforces domain validation:

Stakeholder APIs expose local IDs (`stakeholder_id`, `participant_id`, `deal_id`,
`contact_id`) and do not expose provider contact IDs. During the participant
cutover, `stakeholder_id` is public/product language for the canonical
`CRMDealParticipant.participant_id`, so `stakeholder_id` and `participant_id`
have the same UUID value. Provider identity is resolved internally via
`crm_source_object_mappings`.

The legacy `deal_stakeholders` table is retained only as stale source data for
migration/validation. Runtime stakeholder list/detail/write endpoints do not
update it; they read and write active `CONTACT` rows in `crm_deal_participants`.
Reusable profile fields live on `crm_contacts`.

### How It Works

1. **Deal has a company domain**: Each deal is linked to a company with a domain (e.g., `acmecorp.com`)
2. **Backend validates**: When adding a stakeholder, the email domain must match the company domain
3. **Frontend should pre-validate**: The `company_domain` is returned in the stakeholder list response for client-side validation

### Validation Rules

| Scenario | Example | Result |
|----------|---------|--------|
| Domain matches | Deal company: `acmecorp.com`, Email: `john@acmecorp.com` | ✅ Allowed |
| Domain mismatch | Deal company: `acmecorp.com`, Email: `john@gmail.com` | ⚠️ Warning (allowed with flag) |
| No company domain | Deal has no company linked | ✅ Any domain allowed |

### Frontend Implementation

```typescript
// Check domain before submitting
function validateStakeholderEmail(email: string, companyDomain: string | null): boolean {
  if (!companyDomain) return true; // No company domain set

  const emailDomain = email.split('@')[1]?.toLowerCase();
  return emailDomain === companyDomain.toLowerCase();
}

// Show warning if domain doesn't match
if (!validateStakeholderEmail(email, deal.company_domain)) {
  showWarning("This email domain doesn't match the company domain. Are you sure?");
}
```

### API Response

The `GET /{deal_id}/stakeholders` response includes `company_domain` for frontend validation:

```json
{
  "status": "SUCCESS",
  "data": {
    "company_domain": "acmecorp.com",
    "stakeholders": [...]
  }
}
```

---

## Data Types

### Enums

#### StakeholderStance

Represents the stakeholder's attitude toward your solution/deal. AI-inferred from meeting transcripts, emails, and engagement patterns.

| Value           | Description | Action Implication |
|-----------------|------------------------------|-------------------|
| `UNKNOWN`       | Stance not yet determined | Needs more engagement to assess |
| `CHAMPION`      | Actively advocates internally for your solution | Leverage for internal selling, ask for referrals |
| `SUPPORTER`     | Generally positive, sees value | Nurture relationship, convert to champion |
| `NEUTRAL`       | No strong opinion either way | Educate on value, address specific needs |
| `DETRACTOR`     | Has concerns, reservations, or objections | Identify concerns, address objections directly |
| `BLOCKER`       | Actively opposing the deal | Understand root cause, may need executive intervention |

#### DecisionPower

Represents the stakeholder's role in the buying decision. Critical for deal strategy.

| Value | Description | Sales Strategy |
|-------|-------------|----------------|
| `UNKNOWN` | Decision power not yet determined | Clarify through discovery questions |
| `DECISION_MAKER` | Has final authority and budget sign-off | Must have buy-in to close |
| `INFLUENCER` | Can sway decision makers' opinions | Build as internal champion |
| `EVALUATOR` | Assesses technical/functional fit | Demonstrate value and capabilities |
| `GATEKEEPER` | Controls access to decision makers | Build relationship to reach key stakeholders |
| `END_USER` | Will use the product/service daily | Important for adoption, gather requirements |

#### StakeholderTemperature

Measures recent engagement level. AI-calculated based on interaction frequency and recency.

| Value | Description | Threshold | Action |
|-------|-------------|-----------|--------|
| `HOT` | Highly engaged, recent interactions | Interaction within 7 days | Maintain momentum |
| `WARM` | Moderately engaged | Interaction within 14 days | Keep engaged |
| `COLD` | Low engagement, needs outreach | No interaction for 14+ days | Re-engage urgently |

#### StakeholderSource

Where the stakeholder was discovered. Affects default approval status.

| Value | Description | Default Approval |
|-------|-------------|------------------|
| `CRM` | Imported from CRM system (contact linked to deal) | Always APPROVED |
| `CALENDAR_DISCOVERY` | Discovered from calendar events | First 5 auto-APPROVED, rest PENDING |
| `EMAIL_DISCOVERY` | Discovered from email threads | First 5 auto-APPROVED, rest PENDING |
| `MANUAL` | Manually added by user | Always APPROVED |

#### ApprovalStatus

Controls whether a stakeholder is used for deal matching (calendar/email association).

| Value | Description | Used for Matching |
|-------|-------------|-------------------|
| `APPROVED` | Active stakeholder, used in all matching | Yes |
| `PENDING` | Awaiting user approval (discovered beyond threshold) | No |
| `REJECTED` | User excluded from deal matching (never was active) | No |
| `REMOVED` | Previously approved stakeholder removed by user | No |

### Response Models (TypeScript)

```ts
type UUID = string;

export interface DealJobResponse {
  job_id: UUID | null;
  job_type: string;
  status: string;
}

/**
 * Stakeholder detail returned in list views.
 * Use this for displaying stakeholder cards/lists.
 */
export interface StakeholderDetail {
  stakeholder_id: UUID;
  participant_id: UUID;              // Canonical CRMDealParticipant ID; same as stakeholder_id
  deal_id: UUID;
  contact_id: UUID | null;           // Null if discovered (not from CRM contact)
  email: string;
  name: string | null;
  job_title: string | null;
  phone_number: string | null;          // Phone number as provided by CRM or user (stored as-is)
  external_links: StakeholderLink[] | null;

  // AI-Inferred Engagement Attributes
  stance: StakeholderStance;         // Attitude toward solution (CHAMPION to BLOCKER)
  decision_power: DecisionPower;     // Role in buying process

  motivation: string | null;         // AI-extracted: What drives this stakeholder
  pain_points: string[] | null;      // AI-extracted: Challenges they've mentioned
  keywords: string[] | null;         // AI-extracted: Key topics of interest
  last_contact_at: string | null;    // ISO8601 - Last interaction timestamp

  temperature: StakeholderTemperature;  // HOT/WARM/COLD based on recency
  source: StakeholderSource;            // How stakeholder was discovered
  approval_status: ApprovalStatus;      // APPROVED/PENDING/REJECTED/REMOVED for matching
  active_research_job: DealJobResponse | null; // Present when stakeholder research is active
}

/**
 * Full stakeholder detail with engagement history.
 * Returned when fetching a single stakeholder.
 */
export interface StakeholderFullDetail extends StakeholderDetail {
  // phone_number is inherited from StakeholderDetail
  department: string | null;         // Inferred from job title or external sources

  /**
   * Historical engagement snapshots showing how metrics changed over time.
   * Newest first, maximum 15 entries.
   * Use for trend visualization (e.g., "Was COLD, now HOT").
   */
  engagement_history: StakeholderEngagementSnapshot[];

  stakeholder_research: string | null;  // Markdown research profile, when generated
  last_researched_at: string | null;    // ISO8601
  active_research_job: DealJobResponse | null; // Reconnect target for active stakeholder research

  created_at: string | null;         // ISO8601
  updated_at: string | null;         // ISO8601
}

/**
 * Point-in-time snapshot of stakeholder engagement metrics.
 * Used to track how engagement evolves over time.
 */
export interface StakeholderEngagementSnapshot {
  recorded_at: string;               // ISO8601 - When snapshot was taken
  stance: string | null;
  temperature: string | null;
  decision_power: string | null;
}

/**
 * Response for GET /{deal_id}/stakeholders
 * Includes company_domain for frontend domain validation when adding new stakeholders.
 */
export interface StakeholderListResponse {
  company_domain: string | null;     // Domain for validation (e.g., "acmecorp.com")
  company_name: string | null;       // Company name for display
  stakeholders: StakeholderDetail[]; // List of stakeholders
}

export interface CreateStakeholderResponse {
  stakeholder: StakeholderDetail;
  /**
   * User-visible post-create jobs for deterministic rematching/sync work.
   * This never includes stakeholder_research.
   */
  processing_jobs: DealJobResponse[];
  /**
   * Research job for the added/restored stakeholder, when dispatch succeeds.
   */
  research_job: DealJobResponse | null;
}

export interface StakeholderLink {
  platform: string;                  // e.g., "linkedin", "twitter"
  url: string;
  label: string | null;
  added_at: string | null;           // ISO8601
}

/**
 * Partial update payload for PATCH /{deal_id}/stakeholders/{stakeholder_id}.
 * Only include fields that should be changed.
 */
export interface UpdateStakeholderRequest {
  name?: string | null;
  job_title?: string | null;
  phone_number?: string | null;
  department?: string | null;
  stance?: StakeholderStance;
  decision_power?: DecisionPower;
  motivation?: string | null;
  pain_points?: string[] | null;
  external_links?: StakeholderLink[] | null;
}

export interface AvailableContactItem {
  contact_id: UUID;
  email: string;                     // Required for participant-backed stakeholder identity
  first_name: string | null;
  last_name: string | null;
  full_name: string | null;
  job_title: string | null;
  company_name: string | null;
  phone: string | null;
  lifecycle_stage: string | null;
  last_activity_at: string | null;   // ISO8601
}

export interface SkippedContact {
  contact_id: UUID;
  reason: string;
}

export interface AddContactsAsStakeholdersResponse {
  // Added rows are CONTACT participants; stakeholder_id is the participant_id alias.
  added: StakeholderDetail[];
  skipped: SkippedContact[];
  total_requested: number;
  total_added: number;
  total_skipped: number;
}
```

---

## 1. Get Deal Stakeholders

Retrieves all stakeholders associated with a specific deal. Supports filtering by approval status.

### Endpoint (Get Deal Stakeholders)

```http
GET /{deal_id}/stakeholders
```

### Path Parameters (Get Deal Stakeholders)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Query Parameters (Get Deal Stakeholders)

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `include_pending` | boolean | No | `true` | Include PENDING stakeholders awaiting user approval |
| `include_rejected` | boolean | No | `false` | Include REJECTED stakeholders (excluded from matching) |

### Approval Status Context

When stakeholders are discovered from calendar events or emails:
- **First 5 per source**: Auto-approved, immediately used for deal matching
- **Additional discoveries**: Marked as PENDING, require manual approval
- **User rejected**: Marked as REJECTED, excluded from matching

Use `include_pending=true` (default) to show pending stakeholders for user review.

### Success Response (200) (Get Deal Stakeholders)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholders retrieved",
  "data": {
    "company_domain": "example.com",
    "company_name": "Example Corp",
    "stakeholders": [
      {
        "stakeholder_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "email": "jane.doe@example.com",
        "name": "Jane Doe",
        "job_title": "Director of IT, Information Technology",
        "phone_number": "+1-555-987-6543",
        "external_links": [
          {
            "platform": "linkedin",
            "url": "https://linkedin.com/in/janedoe",
            "label": "LinkedIn Profile",
            "added_at": "2025-01-15T10:30:00Z"
          }
        ],
        "stance": "SUPPORTER",
        "decision_power": "DECISION_MAKER",
        "motivation": "Reduce technical debt and improve team productivity",
        "pain_points": ["Legacy system maintenance", "Slow deployment cycles"],
        "keywords": ["digital transformation", "modernize legacy systems"],
        "last_contact_at": "2024-11-08T14:00:00Z",
        "temperature": "WARM",
        "source": "CRM",
        "approval_status": "APPROVED"
      }
    ]
  }
}
```

> **Frontend Note**: Use `company_domain` to validate new stakeholder emails before submission. Show a warning if the email domain doesn't match.

### Error Responses (Get Deal Stakeholders)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `500` | Internal server error |

### Example cURL (Get Deal Stakeholders)

```bash
curl -X GET "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

---

## 2. List Available Contacts

Lists CRM contacts that can be added as stakeholders for a deal. This is the
read-only contact picker endpoint for the add-from-contacts flow. It only
returns contacts with valid email addresses and excludes contacts that already
have a participant-backed stakeholder row for this deal.

This endpoint reads from `crm_contacts`; it does not create participants and it
does not write to the legacy `deal_stakeholders` table.

### Endpoint (List Available Contacts)

```http
GET /{deal_id}/stakeholders/available-contacts
```

### Path Parameters (List Available Contacts)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Query Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | No | - | Search string to filter by name, email, or company |
| `limit` | integer | No | 50 | Maximum number of results to return |

### Success Response (200) (List Available Contacts)

```json
{
  "status": "SUCCESS",
  "message": "Available contacts retrieved",
  "data": [
    {
      "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "email": "alice.smith@acmecorp.com",
      "first_name": "Alice",
      "last_name": "Smith",
      "full_name": "Alice Smith",
      "job_title": "VP of Engineering",
      "company_name": "Acme Corporation",
      "phone": "+1-555-123-4567",
      "lifecycle_stage": "opportunity",
      "last_activity_at": "2024-11-01T09:00:00Z"
    }
  ]
}
```

#### Response Schema (`AvailableContactItem`)

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `contact_id` | UUID | No | Internal CRM contact identifier |
| `email` | string | No | Primary email address; required before a contact can be added as a stakeholder |
| `first_name` | string | Yes | Contact first name |
| `last_name` | string | Yes | Contact last name |
| `full_name` | string | Yes | Display name used by pickers and search results |
| `job_title` | string | Yes | Current or latest known job title from the contact profile |
| `company_name` | string | Yes | Associated company/account name |
| `phone` | string | Yes | Primary phone number |
| `lifecycle_stage` | string | Yes | CRM lifecycle stage, when available |
| `last_activity_at` | datetime | Yes | Last known CRM activity timestamp |

### Notes

- **Email Required**: Only contacts with a valid email address are returned, as email is required for participant-backed stakeholder identity
- **Search**: The `query` parameter searches across `first_name`, `last_name`, `full_name`, `email`, and `company_name`
- **Exclusion**: Contacts already added as stakeholders for this deal are automatically excluded
- **Identity**: The follow-up add endpoint creates or restores `CONTACT` rows in `crm_deal_participants`; public `stakeholder_id` values map to `participant_id`

### Error Responses (List Available Contacts)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `500` | Internal server error |

### Example cURL (List Available Contacts)

```bash
curl -X GET "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/available-contacts?query=engineering&limit=20" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

---

## 3. Add Contacts as Stakeholders

Add selected CRM contacts as stakeholders to a deal. Only contacts with valid
email addresses will be added. Contacts without emails or those that already
exist as stakeholders are skipped.

Added contacts are persisted as `CONTACT` participants in
`crm_deal_participants`. The legacy `deal_stakeholders` table is not updated.

### Endpoint (Add Contacts as Stakeholders)

```http
POST /{deal_id}/stakeholders/add-from-contacts
```

### Path Parameters (Add Contacts as Stakeholders)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Request Body

```json
{
  "contact_ids": [
    "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "4fa85f64-5717-4562-b3fc-2c963f66afa7",
    "5fa85f64-5717-4562-b3fc-2c963f66afa8"
  ]
}
```

#### Request Schema (`AddContactsAsStakeholdersRequest`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `contact_ids` | UUID[] | Yes | List of contact IDs to add as stakeholders (minimum 1) |

### Success Response (200) (Add Contacts as Stakeholders)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholders added from contacts",
  "data": {
    "added": [
      {
        "stakeholder_id": "6fa85f64-5717-4562-b3fc-2c963f66afa9",
        "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "email": "alice.smith@acmecorp.com",
        "name": "Alice Smith",
        "job_title": "VP of Engineering",
        "phone_number": "+1-555-123-4567",
        "stance": "UNKNOWN",
        "decision_power": "UNKNOWN",
        "temperature": "COLD",
        "source": "CRM"
      }
    ],
    "skipped": [
      {
        "contact_id": "4fa85f64-5717-4562-b3fc-2c963f66afa7",
        "reason": "Contact has no email address"
      },
      {
        "contact_id": "5fa85f64-5717-4562-b3fc-2c963f66afa8",
        "reason": "Stakeholder with this email already exists"
      }
    ],
    "total_requested": 3,
    "total_added": 1,
    "total_skipped": 2
  }
}
```

### Skip Reasons

| Reason | Description |
| -------- | ------------- |
| `Contact not found` | The contact ID does not exist in the CRM |
| `Contact has no email address` | Contact exists but has no email |
| `Stakeholder with this email already exists` | A stakeholder with the same email is already on this deal |
| `Contact is already a stakeholder` | The contact is already linked to this deal |

### Error Responses (Add Contacts as Stakeholders)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `422` | Validation error (e.g., empty contact_ids list) |
| `500` | Internal server error |

### Example cURL (Add Contacts as Stakeholders)

```bash
curl -X POST "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/add-from-contacts" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "contact_ids": [
      "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "4fa85f64-5717-4562-b3fc-2c963f66afa7"
    ]
  }'
```

---

## 4. Add Stakeholder (Manual)

Manually add a new stakeholder to a deal. Use this when you want to add someone who is not in your CRM contacts.

### Endpoint (Add Stakeholder)

```http
POST /{deal_id}/stakeholders
```

### Path Parameters (Add Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Request Body

The request body includes UI metadata to help frontend developers build appropriate form controls.

```json
{
  "email": "john.smith@prospect.com",
  "name": "John Smith",
  "job_title": "CTO",
  "phone_number": "+1 (555) 123-4567",
  "stance": "SUPPORTER",
  "decision_power": "DECISION_MAKER",
  "department": "Technology",
  "motivation": "Looking to modernize their tech stack",
  "pain_points": ["Legacy systems", "Manual processes"],
  "external_links": [
    {
      "platform": "linkedin",
      "url": "https://linkedin.com/in/johnsmith",
      "label": "LinkedIn"
    }
  ]
}
```

### Request Field Categories

Fields are categorized to help frontend build appropriate UI:

#### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `email` | string | Email address (must be unique per deal) |

> **Domain Validation**: Frontend should validate that the email domain matches `company_domain` from the GET stakeholders response. If domains don't match, show a warning: "This email domain doesn't match the company domain ({company_domain}). Are you sure this person works at {company_name}?"

#### Primary Fields (Show Prominently)

| Field | Type | Default | Description | UI Component |
|-------|------|---------|-------------|--------------|
| `name` | string | null | Full name (strongly recommended) | Text input |
| `job_title` | string | null | Job title | Text input |
| `phone_number` | string | null | Phone number (stored as-is, no formatting) | Text input |
| `stance` | enum | UNKNOWN | Attitude toward solution | Select dropdown |
| `decision_power` | enum | UNKNOWN | Role in buying process | Select dropdown |

#### Secondary Fields (Collapsible Section)

| Field | Type | Default | Description | UI Component |
|-------|------|---------|-------------|--------------|
| `department` | string | null | Department | Text input |
| `motivation` | string | null | What drives this person | Textarea |
| `pain_points` | string[] | null | Challenges they've mentioned | Tag input |
| `external_links` | array | null | Profile links | Link builder |

> **Note on Defaults**: Fields with neutral defaults (like `stance=UNKNOWN`) allow users to skip fields they're unsure about. The AI analysis will refine these values over time based on actual interactions.

### Stance Options (for UI dropdown)

| Value | Label | Description |
|-------|-------|-------------|
| `UNKNOWN` | Unknown | Not yet determined |
| `CHAMPION` | Champion | Actively advocates for your solution |
| `SUPPORTER` | Supporter | Generally positive |
| `NEUTRAL` | Neutral | No strong opinion |
| `DETRACTOR` | Detractor | Has concerns or objections |
| `BLOCKER` | Blocker | Actively opposing |

### Decision Power Options (for UI dropdown)

| Value | Label | Description |
|-------|-------|-------------|
| `UNKNOWN` | Unknown | Not yet determined |
| `DECISION_MAKER` | Decision Maker | Has final authority and budget sign-off |
| `INFLUENCER` | Influencer | Can sway decision makers |
| `EVALUATOR` | Evaluator | Assesses technical/functional fit |
| `GATEKEEPER` | Gatekeeper | Controls access to decision makers |
| `END_USER` | End User | Will use the product daily |

### Success Response (201) (Add Stakeholder)

The stakeholder row is created synchronously. The response also returns three
visible post-create processing jobs for related email rematch, calendar rematch,
and stakeholder sync. Stakeholder research is not started here; use the manual
research trigger endpoint when the user explicitly requests research.

Writes are participant-backed: durable person/profile fields are written to
`crm_contacts` when a contact can be resolved or created, and deal-specific
behavior is written to the active `CONTACT` row in `crm_deal_participants`.

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder created successfully",
  "data": {
    "stakeholder": {
      "stakeholder_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
      "participant_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
      "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "contact_id": null,
      "email": "john.smith@prospect.com",
      "name": "John Smith",
      "job_title": "CTO",
      "phone_number": "+1 (555) 123-4567",
      "stance": "SUPPORTER",
      "decision_power": "DECISION_MAKER",
      "motivation": "Looking to modernize their tech stack",
      "pain_points": ["Legacy systems", "Manual processes"],
      "temperature": "UNKNOWN",
      "source": "MANUAL",
      "approval_status": "APPROVED",
      "active_research_job": null
    },
    "processing_jobs": [
      {
        "job_id": "54d810af-6ea7-41c0-b6b9-d74c21b94e31",
        "job_type": "deal_email_rematch",
        "status": "pending"
      },
      {
        "job_id": "0df77c72-ff0a-4cc5-80f0-ded2a84690af",
        "job_type": "deal_calendar_rematch",
        "status": "pending"
      },
      {
        "job_id": "6f1145c6-2b30-43cf-a6cd-3c147e88e58b",
        "job_type": "stakeholder_sync",
        "status": "pending"
      }
    ],
    "research_job": {
      "job_id": "8b1f9e8e-9500-4b2a-8d3a-6ac597a2c317",
      "job_type": "stakeholder_research",
      "status": "pending"
    }
  }
}
```

### Auto-Set & Default Fields

The following fields are automatically set or have neutral defaults:

| Field | Default | Reason |
|-------|---------|--------|
| `source` | `MANUAL` | Indicates manually added (not from CRM/discovery) |
| `approval_status` | `APPROVED` | Manual additions are trusted |
| `temperature` | `UNKNOWN` | Will be calculated based on interactions |
| `contact_id` | `null` or contact UUID | Contact profile link when resolved or created |
| `stance` | `UNKNOWN` | Neutral default if not provided |
| `decision_power` | `UNKNOWN` | Neutral default if not provided |

### Error Responses (Add Stakeholder)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `409` | Stakeholder with this email already exists for this deal |
| `422` | Validation error (e.g., invalid email format) |
| `500` | Internal server error |

### Example cURL (Add Stakeholder)

```bash
curl -X POST "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john.smith@prospect.com",
    "name": "John Smith",
    "job_title": "CTO",
    "phone_number": "+1 (555) 123-4567",
    "stance": "SUPPORTER",
    "decision_power": "DECISION_MAKER"
  }'
```

### Frontend Integration Notes (Add Stakeholder)

After a successful add, register every `processing_jobs[]` item in the global
activity surface and subscribe to `GET /users/crm/deals/{deal_id}/jobs/{job_id}/stream`.
Show these as deal-level processing. They run sequentially in this order: email
rematch, calendar rematch, then stakeholder sync. `research_job` is separate from
that processing chain and represents research for the added/restored stakeholder.
That returned research job is user-owned and can be subscribed to by the same
authenticated user through the generic deal job stream.
If the rematch/sync chain discovers additional stakeholders, those services may
dispatch research for the newly discovered stakeholders through the normal
dedupe/cooldown path. Refresh deal detail, stakeholders, meetings, timeline, and
related activity when the jobs complete.

**Form Layout Suggestion:**

```
┌─────────────────────────────────────────────────────────────┐
│  ADD STAKEHOLDER                                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Email*: [_________________________________]                │
│                                                             │
│  ─── Primary Details ───                                    │
│  Name:      [_________________________________]             │
│  Job Title: [_________________________________]             │
│  Phone:     [_________________________________]             │
│  Stance:    [UNKNOWN        ▼]                              │
│  Role:      [UNKNOWN        ▼]                              │
│                                                             │
│  ▶ More Details (collapsed by default)                      │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ Department: [_________________________]              │   │
│  │ Motivation: [________________________]              │   │
│  │ Pain Points: [tag1] [tag2] [+]                      │   │
│  │ LinkedIn: [_________________________]               │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│                              [Cancel]  [Add Stakeholder]    │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. Edit Stakeholder

Partially updates an existing stakeholder for a deal. Only fields included in the request are changed.
Profile fields (`name`, `job_title`, `phone_number`, `department`,
`external_links`) update `crm_contacts` first when the participant is linked to a
contact, then refresh the participant snapshot. Deal-specific fields (`stance`,
`decision_power`, `motivation`, `pain_points`) update only the participant row.

### Endpoint (Edit Stakeholder)

```http
PATCH /{deal_id}/stakeholders/{stakeholder_id}
```

### Path Parameters (Edit Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `stakeholder_id` | UUID | Yes | The unique identifier of the stakeholder |

### Request Body (Edit Stakeholder)

```json
{
  "name": "Jane Updated",
  "job_title": "Director of Engineering",
  "phone_number": "+44 20 7946 0958",
  "motivation": "Accelerate engineering delivery"
}
```

### Editable Fields

| Field | Type | Notes |
|------|------|-------|
| `name` | string \| null | Stakeholder name |
| `job_title` | string \| null | Job title |
| `phone_number` | string \| null | Phone number (stored as-is) |
| `department` | string \| null | Department |
| `stance` | enum | Stakeholder stance |
| `decision_power` | enum | Buying-role classification |
| `motivation` | string \| null | Motivation summary |
| `pain_points` | string[] \| null | Known pain points |
| `external_links` | array \| null | External profile links |

### Success Response (200) (Edit Stakeholder)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder updated",
  "data": {
    "stakeholder_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
    "participant_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
    "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "john.smith@prospect.com",
    "name": "Jane Updated",
    "job_title": "Director of Engineering",
    "phone_number": "+44 20 7946 0958",
    "source": "MANUAL",
    "approval_status": "APPROVED"
  }
}
```

### Error Responses (Edit Stakeholder)

| Code | Description |
|------|-------------|
| `400` | Empty payload or invalid null for non-nullable fields |
| `401` | Missing or invalid authentication token |
| `404` | Stakeholder not found for this deal |
| `422` | Validation error |
| `500` | Internal server error |

### Sync Behavior Note

- When users edit stakeholder fields through this endpoint, profile edits are owned by `crm_contacts` and deal-behavior edits are owned by `crm_deal_participants`.
- Later stakeholder mapper sync runs (`stakeholder.sync_deal_stakeholders`) should not overwrite app-owned participant behavior fields.
- Rejected stakeholders stay rejected during sync; mapper does not force them back to approved automatically.

### Example cURL (Edit Stakeholder)

```bash
curl -X PATCH "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/8fa85f64-5717-4562-b3fc-2c963f66afa9" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Jane Updated",
    "job_title": "Director of Engineering",
    "phone_number": "+44 20 7946 0958"
  }'
```

---

## 6. Get Stakeholder Detail

Retrieves full details of a specific stakeholder including engagement history.

### Endpoint (Get Stakeholder Detail)

```http
GET /{deal_id}/stakeholders/{stakeholder_id}
```

### Path Parameters (Get Stakeholder Detail)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `stakeholder_id` | UUID | Yes | The unique identifier of the stakeholder |

### Success Response (200) (Get Stakeholder Detail)

Returns `StakeholderFullDetail` with engagement history.

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder detail retrieved",
  "data": {
    "stakeholder_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "jane.doe@example.com",
    "name": "Jane Doe",
    "job_title": "Director of IT",
    "phone_number": "+1-555-987-6543",
    "department": "Information Technology",
    "external_links": [
      {
        "platform": "linkedin",
        "url": "https://linkedin.com/in/janedoe",
        "label": "LinkedIn Profile",
        "added_at": "2025-01-15T10:30:00Z"
      }
    ],
    "stance": "SUPPORTER",
    "decision_power": "DECISION_MAKER",
    "motivation": "Reduce technical debt and improve team productivity",
    "pain_points": ["Legacy system maintenance", "Slow deployment cycles"],
    "keywords": ["digital transformation", "modernize legacy systems"],
    "last_contact_at": "2024-11-08T14:00:00Z",
    "temperature": "WARM",
    "source": "CRM",
    "approval_status": "APPROVED",
    "engagement_history": [
      {
        "recorded_at": "2024-11-08T14:00:00Z",
        "stance": "SUPPORTER",
        "temperature": "WARM",
        "decision_power": "DECISION_MAKER"
      },
      {
        "recorded_at": "2024-10-25T10:00:00Z",
        "stance": "NEUTRAL",
        "temperature": "COLD",
        "decision_power": "INFLUENCER"
      }
    ],
    "stakeholder_research": "## Professional background\nJane leads IT modernization...",
    "last_researched_at": "2026-04-29T10:20:00Z",
    "active_research_job": {
      "job_id": "a58573fd-a0f3-4b42-89ee-02d87a70a7c6",
      "job_type": "stakeholder_research",
      "status": "in_progress"
    },
    "created_at": "2024-09-15T08:00:00Z",
    "updated_at": "2024-11-08T14:00:00Z"
  }
}
```

`active_research_job` is `null` unless a `stakeholder_research` job is currently
`pending` or `in_progress` for this stakeholder. Use it to
recover an open research modal or row state after refresh/reconnect.

### Understanding Engagement History

The `engagement_history` array shows how the stakeholder's engagement has evolved:
- **Newest first**: Most recent snapshot is first in the array
- **Max 15 entries**: Shows up to 15 historical snapshots
- **Trend visualization**: Use to show stakeholder journey (e.g., "Was COLD, now HOT")

Frontend can visualize this as:
- A timeline showing stance changes
- Temperature trend indicator

### Error Responses (Get Stakeholder Detail)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `404` | Stakeholder not found for this deal |
| `500` | Internal server error |

### Example cURL (Get Stakeholder Detail)

```bash
curl -X GET "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/7fa85f64-5717-4562-b3fc-2c963f66afa8" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

---

## 7. Trigger Stakeholder Research

Manually starts stakeholder research for a specific stakeholder. Stakeholder
add/re-add also returns `research_job` for the added/restored stakeholder, while
this endpoint is used when the user explicitly triggers research later.

### Endpoint (Trigger Stakeholder Research)

```http
POST /{deal_id}/stakeholders/{stakeholder_id}/research
```

### Path Parameters (Trigger Stakeholder Research)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `stakeholder_id` | UUID | Yes | The stakeholder to research |

### Success Response (200) (Research Dispatched)

```json
{
  "status": "SUCCESS",
  "message": "Research job dispatched",
  "data": {
    "job_id": "a58573fd-a0f3-4b42-89ee-02d87a70a7c6",
    "job_type": "stakeholder_research",
    "status": "pending"
  }
}
```

If the server skips dispatch because recent research is already available, the
same response shape is returned with `job_id: null` and `status: "completed"`.

Subscribe to the generic deal job stream with the returned `job_id`:

```http
GET /users/crm/deals/{deal_id}/jobs/{job_id}/stream
```

The stream emits normal job frames: `ready`, `progress.update`, optional
`answer.replace` on completed reconnect, `finalize`, and `complete`.

### Error Responses (Trigger Stakeholder Research)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `403` | User cannot access this stakeholder/job |
| `404` | Stakeholder not found for this deal |
| `500` | Internal server error |

---

## 8. Approve Stakeholder

Approve a PENDING stakeholder for deal matching. Changes approval_status from PENDING to APPROVED.

### Endpoint (Approve Stakeholder)

```http
POST /{deal_id}/stakeholders/{stakeholder_id}/approve
```

### Path Parameters (Approve Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `stakeholder_id` | UUID | Yes | The unique identifier of the stakeholder |

### When to Use

When stakeholders are discovered from calendar events or emails beyond the first 5:
- They're marked as PENDING by default
- Frontend should display these for user review
- User can approve to include in deal matching

### Success Response (200) (Approve Stakeholder)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder approved",
  "data": {
    "stakeholder_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "discovered.contact@example.com",
    "name": "Discovered Contact",
    "approval_status": "APPROVED",
    "source": "CALENDAR_DISCOVERY",
    ...
  }
}
```

### Error Responses (Approve Stakeholder)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `404` | Stakeholder not found |
| `422` | Invalid status transition (e.g., already APPROVED or REJECTED) |
| `500` | Internal server error |

### Example cURL (Approve Stakeholder)

```bash
curl -X POST "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/7fa85f64-5717-4562-b3fc-2c963f66afa8/approve" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 9. Reject Stakeholder

Reject a stakeholder to exclude from deal matching. Works for PENDING or APPROVED stakeholders.

### Endpoint (Reject Stakeholder)

```http
POST /{deal_id}/stakeholders/{stakeholder_id}/reject
```

### Path Parameters (Reject Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `stakeholder_id` | UUID | Yes | The unique identifier of the stakeholder |

### Use Cases

- **Incorrect discovery**: Stakeholder discovered but not actually relevant to deal
- **Exclude from matching**: Don't want calendar/email events matched to this person
- **Cleanup**: Remove noise from stakeholder list

### Success Response (200) (Reject Stakeholder)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder rejected",
  "data": {
    "stakeholder_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "irrelevant.contact@example.com",
    "name": "Irrelevant Contact",
    "approval_status": "REJECTED",
    ...
  }
}
```

### What Happens When Rejected

- **Not used for calendar matching**: Events with this attendee won't auto-associate
- **Not used for email matching**: Emails with this person won't auto-associate
- **Hidden by default**: Not shown in stakeholder list unless `include_rejected=true`
- **Reversible**: Can be re-approved later if needed (future endpoint)

### Error Responses (Reject Stakeholder)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `404` | Stakeholder not found |
| `422` | Invalid status transition (e.g., already REJECTED) |
| `500` | Internal server error |

### Example cURL (Reject Stakeholder)

```bash
curl -X POST "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/7fa85f64-5717-4562-b3fc-2c963f66afa8/reject" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 10. Remove Stakeholder

Remove an approved stakeholder from the deal. This is different from rejection - removal is for stakeholders that were previously active but should no longer be associated with the deal.

### Endpoint (Remove Stakeholder)

```http
DELETE /{deal_id}/stakeholders/{stakeholder_id}
```

### Path Parameters (Remove Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `stakeholder_id` | UUID | Yes | The unique identifier of the stakeholder |

### When to Use

- **Stakeholder left the company**: Contact no longer relevant to the deal
- **Role change**: Person moved to a different role not involved in the decision
- **Clean up active list**: Remove stakeholders who were active but are no longer relevant
- **Undo manual addition**: Remove a manually added stakeholder that shouldn't be there

### Difference from Reject

| Action | Use Case | Original Status | Result |
|--------|----------|-----------------|--------|
| **Reject** | Stakeholder was discovered but shouldn't be included | PENDING or APPROVED | REJECTED |
| **Remove** | Stakeholder was active but is no longer relevant | APPROVED only | REMOVED |

Both REJECTED and REMOVED stakeholders are excluded from deal matching. The distinction helps with:
- **Analytics**: Track how many stakeholders were removed vs rejected
- **Audit trail**: Understand the history of stakeholder changes
- **Potential restoration**: Different workflows for re-adding rejected vs removed stakeholders

### Success Response (200) (Remove Stakeholder)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder removed",
  "data": {
    "stakeholder_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "former.contact@example.com",
    "name": "Former Contact",
    "approval_status": "REMOVED",
    "source": "CRM",
    ...
  }
}
```

### What Happens When Removed

- **Not used for calendar matching**: Events with this attendee won't auto-associate
- **Not used for email matching**: Emails with this person won't auto-associate
- **Hidden by default**: Not shown in stakeholder list (requires explicit filter to show)
- **Historical data preserved**: Previous engagement history is retained for audit

### Error Responses (Remove Stakeholder)

| Code | Description |
|------|-------------|
| `400` | Stakeholder is not in APPROVED status (only approved stakeholders can be removed) |
| `400` | Stakeholder is already removed |
| `401` | Missing or invalid authentication token |
| `404` | Stakeholder not found |
| `500` | Internal server error |

### Example cURL (Remove Stakeholder)

```bash
curl -X DELETE "$BASE_URL/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/7fa85f64-5717-4562-b3fc-2c963f66afa8" \
  -H "Authorization: Bearer $TOKEN"
```

---

## Error Codes Summary

| HTTP Code | Reason | Suggested Action |
| ----------- | -------- | ------------------ |
| 401 | Missing/invalid token | Ensure valid bearer token |
| 422 | Validation failure | Fix request parameters |
| 429 | Rate limit exceeded | Respect `Retry-After` header |
| 500 | Unexpected server error | Retry with backoff; contact support |

---

## Frontend Integration Notes

### Typical Flow

1. **View Stakeholders**: Call `GET /{deal_id}/stakeholders` to display the stakeholder list
2. **Add Stakeholders**:
   - User clicks "Add Stakeholder" button
   - Call `GET /{deal_id}/stakeholders/available-contacts` to show available contacts
   - User selects one or more contacts
   - Call `POST /{deal_id}/stakeholders/add-from-contacts` with selected contact IDs
   - Display success/skip summary to user

3. **View Stakeholder Detail**:
    - User clicks on a stakeholder card
    - Call `GET /{deal_id}/stakeholders/{stakeholder_id}` for full details
    - Display engagement history as a trend visualization

4. **Edit Stakeholder**:
   - User edits stakeholder profile fields in detail view
   - Call `PATCH /{deal_id}/stakeholders/{stakeholder_id}` with only changed fields
   - Refresh stakeholder card/detail after success
   - Edits made through PATCH are preserved during stakeholder mapper sync

5. **Manage Pending Stakeholders** (Approval Flow):
    - Display PENDING stakeholders with visual indicator (badge, different styling)
    - Show "Approve" and "Reject" action buttons for pending items
    - On approve: Call `POST /{deal_id}/stakeholders/{stakeholder_id}/approve`
    - On reject: Call `POST /{deal_id}/stakeholders/{stakeholder_id}/reject`
    - Update UI immediately (optimistic update) or refetch list

6. **Remove Active Stakeholders**:
    - Show "Remove" action on APPROVED stakeholders (e.g., in dropdown menu or detail view)
    - On remove: Call `DELETE /{deal_id}/stakeholders/{stakeholder_id}`
    - Show confirmation dialog: "Remove this stakeholder from the deal?"
    - Update UI immediately or refetch list

### Stakeholder Cards UI

Display key metrics with appropriate indicators:

| Field | Display Suggestion |
|-------|-------------------|
| `stance` | Color-coded badge (Green: CHAMPION/SUPPORTER, Yellow: NEUTRAL, Red: DETRACTOR/BLOCKER) |
| `decision_power` | Icon + label (Crown: DECISION_MAKER, Megaphone: INFLUENCER, etc.) |
| `temperature` | Colored indicator (Red: HOT, Orange: WARM, Blue: COLD) |
| `approval_status` | PENDING: Yellow badge with action buttons; REMOVED: Gray badge (if shown) |

### Search Best Practices

- Debounce the `query` parameter input (300-500ms) to avoid excessive API calls
- Show loading state while fetching contacts
- Display company name and job title to help users identify the right contacts

### Handling Skipped Contacts

- Display skipped contacts with their reasons in a non-blocking way
- For "no email" skips, consider prompting user to update contact in CRM first

### Handling Pending Stakeholders

- Show a badge or section header: "X stakeholders pending approval"
- Explain to users: "These contacts were discovered in calendar events/emails"
- Provide bulk approve/reject actions for efficiency

---

## AI-Generated Insights

Stakeholder data is analyzed by AI to generate actionable insights such as:

- **Engagement Gaps**: Decision makers not contacted recently
- **Sentiment Decline**: Champions showing reduced enthusiasm
- **Champion Opportunities**: High-potential stakeholders identified
- **Stakeholder Risks**: Competitive mentions, stance shifts

These insights are available via a separate API endpoint. See the **Stakeholder Insights API** documentation for details.

---

## Related References

- **Stakeholder Insights API**: `docs/API Documentation/deals/stakeholder-insights-api.md`
- CRM Deals API: `docs/API Documentation/deals/crm-deals-api.md`
- Deal Meetings API: `docs/API Documentation/deals/deal-meetings-api.md`
- Risks & Blockers API: `docs/API Documentation/deals/risks-blockers-api.md`
