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

Stakeholder APIs expose local IDs (`participant_id`, `deal_id`, `contact_id`)
and do not expose provider contact IDs. `participant_id` is the canonical
`CRMDealParticipant.participant_id` and is the ID used for stakeholder detail,
edit, approval, removal, and research routes. Provider identity is resolved
internally via `crm_source_object_mappings`.

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
  participant_id: UUID;              // Canonical CRMDealParticipant ID
  deal_id: UUID;
  contact_id: UUID | null;           // Null if discovered (not from CRM contact)
  email: string;
  name: string | null;
  job_title: string | null;
  phone_number: string | null;          // Phone number as provided by CRM or user (stored as-is)
  linkedin_url: string | null;          // First-class LinkedIn profile URL, when known
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
  stakeholder_research: string | null;  // Cached research profile, when already available
  last_researched_at: string | null;    // ISO8601
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

/**
 * Create payload for POST /{deal_id}/stakeholders.
 * Send only identity fields. At least one field must be present.
 */
export interface CreateStakeholderRequest {
  contact_id?: UUID | null;          // Existing CRM contact selected from search
  name?: string | null;              // Manual name or optional override for selected contact
  email?: string | null;             // Manual email identity
  linkedin_url?: string | null;      // Manual LinkedIn identity
}

export interface StakeholderLink {
  platform: string;                  // e.g., "linkedin", "twitter"
  url: string;
  label: string | null;
  added_at: string | null;           // ISO8601
}

/**
 * Partial update payload for PATCH /{deal_id}/stakeholders/{participant_id}.
 * Only include fields that should be changed.
 */
export interface UpdateStakeholderRequest {
  name?: string | null;
  job_title?: string | null;
  phone_number?: string | null;
  linkedin_url?: string | null;
  department?: string | null;
  stance?: StakeholderStance;
  decision_power?: DecisionPower;
  motivation?: string | null;
  pain_points?: string[] | null;
  external_links?: StakeholderLink[] | null;
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
        "participant_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "email": "jane.doe@example.com",
        "name": "Jane Doe",
        "job_title": "Director of IT, Information Technology",
        "phone_number": "+1-555-987-6543",
        "linkedin_url": "https://linkedin.com/in/janedoe",
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
        "active_research_job": null
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

## 2. Add Stakeholder

Manually add a new stakeholder to a deal. Use this when you want to quickly capture someone who is not already selected from CRM contacts.

For typeahead/search in the add-stakeholder modal, prefer the canonical contact
search endpoint:

```http
GET /users/crm/contacts/search?q={query}
```

The deal-scoped legacy picker URL is still supported for deployed clients:

```http
GET /{deal_id}/stakeholders/available-contacts?query={query}
```

It delegates to the same contact search implementation and returns the older
flat available-contact list shape. New clients should use
`GET /users/crm/contacts/search` and then send the selected `contact_id` to the
create endpoint below. Do not use the removed bulk
`/{deal_id}/stakeholders/add-from-contacts` endpoint.

### Endpoint (Add Stakeholder)

```http
POST /{deal_id}/stakeholders
```

### Path Parameters (Add Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Request Body

Frontend should send only stakeholder identity fields on create. If the user
selects a result from contact search, send `contact_id` by itself or with a
user-edited `name`.

```json
{
  "name": "John Smith",
  "email": "john.smith@prospect.com",
  "linkedin_url": "https://www.linkedin.com/in/johnsmith"
}
```

### Create Request Contract

Send these fields only. At least one of `contact_id`, `name`, `email`, or
`linkedin_url` must be present.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `contact_id` | UUID | Conditionally | Existing CRM contact selected from contact search. Backend copies email, name, job title, phone, LinkedIn, external links, and cached research from the contact. |
| `name` | string | Conditionally | Stakeholder name. Can create an incomplete placeholder when this is the only known value. |
| `email` | string | Conditionally | Email address. Used for contact resolution, dedupe, domain checks, communication matching, and automatic research dispatch. |
| `linkedin_url` | string | Conditionally | LinkedIn profile URL. Used for contact resolution, dedupe, and enrichment when email is not known. |

Do not send profile, buying-role, or engagement fields on create. The backend/enrichment pipeline owns:

- `job_title`
- `phone_number`
- `department`
- `stance`
- `decision_power`
- `motivation`
- `pain_points`
- `external_links`
- `temperature`
- `source`
- `approval_status`

Those fields may be returned in responses and may be editable through the stakeholder edit endpoint where supported, but they should not be part of the add-stakeholder form.

> **Domain Validation**: If `email` is provided, frontend should validate that the email domain matches `company_domain` from the GET stakeholders response. If domains don't match, show a warning: "This email domain doesn't match the company domain ({company_domain}). Are you sure this person works at {company_name}?"

### Minimal Request Examples

Email-only:

```json
{
  "email": "john.smith@prospect.com"
}
```

Name-only placeholder:

```json
{
  "name": "John Smith"
}
```

LinkedIn-only:

```json
{
  "linkedin_url": "https://www.linkedin.com/in/johnsmith"
}
```

Selected contact:

```json
{
  "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

Selected contact with a user-edited display name:

```json
{
  "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "name": "John Smith"
}
```

### Success Response (201) (Add Stakeholder)

The stakeholder row is created synchronously. The response also returns three
visible post-create processing jobs for related email rematch, calendar rematch,
and stakeholder sync when the stakeholder has an email. Contact-backed
stakeholders enqueue `contact_research` when cached contact research is missing;
unlinked email/LinkedIn placeholders use the temporary `stakeholder_research`
path until enrichment can resolve a contact. Name-only placeholders return no
jobs.

Writes are participant-backed: durable person/profile fields are written to
`crm_contacts` when a contact can be resolved or created, and deal-specific
behavior is written to the active `CONTACT` row in `crm_deal_participants`.

`research_job.job_type` is therefore one of:

- `contact_research` when `stakeholder.contact_id` is present and fresh contact
  research was not already copied.
- `stakeholder_research` for unlinked email/LinkedIn placeholders.

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder created successfully",
  "data": {
    "stakeholder": {
      "participant_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
      "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "contact_id": null,
      "email": "john.smith@prospect.com",
      "name": "John Smith",
      "job_title": "CTO",
      "phone_number": "+1 (555) 123-4567",
      "linkedin_url": "https://www.linkedin.com/in/johnsmith",
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

Selected contact response when cached contact research already exists:

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder created successfully",
  "data": {
    "stakeholder": {
      "participant_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
      "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "email": "john.smith@prospect.com",
      "name": "John Smith",
      "job_title": "CTO",
      "phone_number": "+1 (555) 123-4567",
      "linkedin_url": "https://www.linkedin.com/in/johnsmith",
      "stance": "UNKNOWN",
      "decision_power": "UNKNOWN",
      "temperature": "UNKNOWN",
      "source": "CRM",
      "approval_status": "APPROVED",
      "stakeholder_research": "## Professional background\nJohn leads platform engineering...",
      "last_researched_at": "2026-06-20T10:20:00Z",
      "active_research_job": null
    },
    "processing_jobs": [
      {
        "job_id": "bddb0b2d-efae-4d93-b76d-2a2d3ebbbd3a",
        "job_type": "stakeholder_sync",
        "status": "pending"
      }
    ],
    "research_job": null
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
    "linkedin_url": "https://www.linkedin.com/in/johnsmith",
    "stance": "SUPPORTER",
    "decision_power": "DECISION_MAKER"
  }'
```

### Frontend Integration Notes (Add Stakeholder)

After a successful add, register every `processing_jobs[]` item in the global
activity surface and subscribe to `GET /users/crm/deals/{deal_id}/jobs/{job_id}/stream`.
Show these as deal-level processing. They run sequentially in this order: email
rematch, calendar rematch, then stakeholder sync. `research_job` is separate from
that processing chain. For contact-backed stakeholders it represents contact
research; for unlinked email/LinkedIn placeholders it represents temporary
stakeholder research. That returned research job is user-owned and can be
subscribed to by the same authenticated user through the generic deal job stream.
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
│  Name:     [_________________________________]              │
│  Email:    [_________________________________]              │
│  LinkedIn: [_________________________________]              │
│                                                             │
│                              [Cancel]  [Add Stakeholder]    │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Edit Stakeholder

Partially updates an existing stakeholder for a deal. Only fields included in the request are changed.
Profile fields (`name`, `job_title`, `phone_number`, `linkedin_url`, `department`,
`external_links`) update `crm_contacts` first when the participant is linked to a
contact, then refresh the participant snapshot. Deal-specific fields (`stance`,
`decision_power`, `motivation`, `pain_points`) update only the participant row.

### Endpoint (Edit Stakeholder)

```http
PATCH /{deal_id}/stakeholders/{participant_id}
```

### Path Parameters (Edit Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `participant_id` | UUID | Yes | The CRMDealParticipant row ID |

### Request Body (Edit Stakeholder)

```json
{
  "name": "Jane Updated",
  "job_title": "Director of Engineering",
  "phone_number": "+44 20 7946 0958",
  "linkedin_url": "https://www.linkedin.com/in/jane-updated",
  "motivation": "Accelerate engineering delivery"
}
```

### Editable Fields

| Field | Type | Notes |
|------|------|-------|
| `name` | string \| null | Stakeholder name |
| `job_title` | string \| null | Job title |
| `phone_number` | string \| null | Phone number (stored as-is) |
| `linkedin_url` | string \| null | LinkedIn profile URL |
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
    "participant_id": "8fa85f64-5717-4562-b3fc-2c963f66afa9",
    "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "john.smith@prospect.com",
    "name": "Jane Updated",
    "job_title": "Director of Engineering",
    "phone_number": "+44 20 7946 0958",
    "linkedin_url": "https://www.linkedin.com/in/jane-updated",
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

## 4. Get Stakeholder Detail

Retrieves full details of a specific stakeholder including engagement history.

### Endpoint (Get Stakeholder Detail)

```http
GET /{deal_id}/stakeholders/{participant_id}
```

### Path Parameters (Get Stakeholder Detail)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `participant_id` | UUID | Yes | The CRMDealParticipant row ID |

### Success Response (200) (Get Stakeholder Detail)

Returns `StakeholderFullDetail` with engagement history.

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder detail retrieved",
  "data": {
    "participant_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "jane.doe@example.com",
    "name": "Jane Doe",
    "job_title": "Director of IT",
    "phone_number": "+1-555-987-6543",
    "linkedin_url": "https://linkedin.com/in/janedoe",
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

## 5. Trigger Stakeholder Research

Manually starts stakeholder research for a specific stakeholder. Stakeholder
add/re-add may return `contact_research` for contact-backed rows or temporary
`stakeholder_research` for unlinked email/LinkedIn placeholders. This endpoint
is used when the user explicitly triggers participant-scoped stakeholder
research later.

### Endpoint (Trigger Stakeholder Research)

```http
POST /{deal_id}/stakeholders/{participant_id}/research
```

### Path Parameters (Trigger Stakeholder Research)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `participant_id` | UUID | Yes | The CRMDealParticipant row ID to research |

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

## 6. Remove Stakeholder

Remove a stakeholder from the deal.

### Endpoint (Remove Stakeholder)

```http
DELETE /{deal_id}/stakeholders/{participant_id}
```

### Path Parameters (Remove Stakeholder)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `participant_id` | UUID | Yes | The CRMDealParticipant row ID |

### When to Use

- **Stakeholder left the company**: Contact no longer relevant to the deal
- **Role change**: Person moved to a different role not involved in the decision
- **Clean up active list**: Remove stakeholders who were active but are no longer relevant
- **Undo manual addition**: Remove a manually added stakeholder that shouldn't be there

### Success Response (200) (Remove Stakeholder)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholder removed",
  "data": {
    "participant_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "email": "former.contact@example.com",
    "name": "Former Contact",
    "approval_status": "APPROVED",
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
   - User enters one identity value: `name`, `email`, `linkedin_url`, or selects an existing contact from `GET /users/crm/contacts/search`
   - Existing deployed clients may call `GET /{deal_id}/stakeholders/available-contacts?query=...`; that route is a compatibility alias over contact search
   - Call `POST /{deal_id}/stakeholders` with the inferred identity payload
   - Display the created stakeholder and any returned processing or research job

3. **View Stakeholder Detail**:
    - User clicks on a stakeholder card
    - Call `GET /{deal_id}/stakeholders/{participant_id}` for full details
    - Display engagement history as a trend visualization

4. **Edit Stakeholder**:
   - User edits stakeholder profile fields in detail view
   - Call `PATCH /{deal_id}/stakeholders/{participant_id}` with only changed fields
   - Refresh stakeholder card/detail after success
   - Edits made through PATCH are preserved during stakeholder mapper sync

5. **Remove Stakeholders**:
    - Show "Remove" action in the stakeholder dropdown menu or detail view
    - On remove: Call `DELETE /{deal_id}/stakeholders/{participant_id}`
    - Show confirmation dialog: "Remove this stakeholder from the deal?"
    - Update UI immediately or refetch list

### Stakeholder Cards UI

Display key metrics with appropriate indicators:

| Field | Display Suggestion |
|-------|-------------------|
| `stance` | Color-coded badge (Green: CHAMPION/SUPPORTER, Yellow: NEUTRAL, Red: DETRACTOR/BLOCKER) |
| `decision_power` | Icon + label (Crown: DECISION_MAKER, Megaphone: INFLUENCER, etc.) |
| `temperature` | Colored indicator (Red: HOT, Orange: WARM, Blue: COLD) |
| `approval_status` | APPROVED: active stakeholder; REMOVED: Gray badge if shown in historical views |

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
