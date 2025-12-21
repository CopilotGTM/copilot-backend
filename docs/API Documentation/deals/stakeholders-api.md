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

## Data Types

### Enums

#### StakeholderStance

| Value           | Description                   |
|-----------------|------------------------------|
| `UNKNOWN`       | Stance not yet determined     |
| `CHAMPION`      | Actively advocates for the deal |
| `SUPPORTER`     | Generally supportive          |
| `NEUTRAL`       | No strong opinion either way  |
| `DETRACTOR`     | Has concerns or objections    |
| `BLOCKER`       | Actively opposing the deal    |

#### DecisionPower

| Value | Description |
|-------|-------------|
| `UNKNOWN` | Decision power not yet determined |
| `DECISION_MAKER` | Has final authority on the decision |
| `INFLUENCER` | Can influence the decision |
| `EVALUATOR` | Evaluates options and provides input |
| `GATEKEEPER` | Controls access to decision makers |
| `END_USER` | Will use the product/service |

#### StakeholderTemperature

| Value | Description |
|-------|-------------|
| `HOT` | Highly engaged, recent interactions |
| `WARM` | Moderately engaged |
| `COLD` | Low engagement, needs outreach |

#### StakeholderSource

| Value | Description |
|-------|-------------|
| `CRM` | Imported from CRM system |
| `CALENDAR_DISCOVERY` | Discovered from calendar events |
| `MANUAL` | Manually added by user |

### Response Models (TypeScript)

```ts
type UUID = string;

export interface StakeholderDetail {
  stakeholder_id: UUID;
  deal_id: UUID;
  contact_id: UUID | null;
  email: string;
  name: string | null;
  job_title: string | null;
  external_links: StakeholderLink[] | null;
  stance: StakeholderStance;
  decision_power: DecisionPower;
  champion_potential: number;        // 0-100 score
  motivation: string | null;
  pain_points: string[] | null;
  keywords: string[] | null;
  last_contact_at: string | null;    // ISO8601
  engagement_score: number;
  temperature: StakeholderTemperature;
  source: StakeholderSource;
}

export interface StakeholderLink {
  platform: string;                  // e.g., "linkedin", "twitter"
  url: string;
  label: string | null;
  added_at: string | null;           // ISO8601
}

export interface AvailableContactItem {
  contact_id: UUID;
  external_id: string;
  email: string;                     // Required - only contacts with email are returned
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
  added: StakeholderDetail[];
  skipped: SkippedContact[];
  total_requested: number;
  total_added: number;
  total_skipped: number;
}
```

---

## 1. Get Deal Stakeholders

Retrieves all stakeholders associated with a specific deal.

### Endpoint (Get Deal Stakeholders)

```http
GET /{deal_id}/stakeholders
```

### Path Parameters (Get Deal Stakeholders)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Success Response (200) (Get Deal Stakeholders)

```json
{
  "status": "SUCCESS",
  "message": "Stakeholders retrieved",
  "data": [
    {
      "stakeholder_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "email": "jane.doe@example.com",
      "name": "Jane Doe",
      "job_title": "Director of IT, Information Technology",
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
      "champion_potential": 92.0,
      "motivation": "Reduce technical debt and improve team productivity",
      "pain_points": ["Legacy system maintenance", "Slow deployment cycles"],
      "keywords": ["digital transformation", "modernize legacy systems"],
      "last_contact_at": "2024-11-08T14:00:00Z",
      "engagement_score": 75.0,
      "temperature": "WARM",
      "source": "CRM"
    }
  ]
}
```

### Error Responses (Get Deal Stakeholders)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `500` | Internal server error |

### Example cURL (Get Deal Stakeholders)

```bash
curl -X GET "$BASE_URL/users/crm/deals/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

---

## 2. List Available Contacts

Lists CRM contacts that can be added as stakeholders for a deal. Only returns contacts with valid email addresses. Excludes contacts that are already stakeholders for this deal.

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
      "external_id": "contact_12345",
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

### Notes

- **Email Required**: Only contacts with a valid email address are returned, as email is required for stakeholder identification
- **Search**: The `query` parameter searches across `first_name`, `last_name`, `full_name`, `email`, and `company_name`
- **Exclusion**: Contacts already added as stakeholders for this deal are automatically excluded

### Error Responses (List Available Contacts)

| Code | Description |
|------|-------------|
| `401` | Missing or invalid authentication token |
| `500` | Internal server error |

### Example cURL (List Available Contacts)

```bash
curl -X GET "$BASE_URL/users/crm/deals/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/available-contacts?query=engineering&limit=20" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

---

## 3. Add Contacts as Stakeholders

Add selected CRM contacts as stakeholders to a deal. Only contacts with valid email addresses will be added. Contacts without emails or those that already exist as stakeholders are skipped.

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
        "stance": "UNKNOWN",
        "decision_power": "UNKNOWN",
        "champion_potential": 0.0,
        "engagement_score": 0.0,
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
curl -X POST "$BASE_URL/users/crm/deals/3fa85f64-5717-4562-b3fc-2c963f66afa6/stakeholders/add-from-contacts" \
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

### Search Best Practices

- Debounce the `query` parameter input (300-500ms) to avoid excessive API calls
- Show loading state while fetching contacts
- Display company name and job title to help users identify the right contacts

### Handling Skipped Contacts

- Display skipped contacts with their reasons in a non-blocking way
- For "no email" skips, consider prompting user to update contact in CRM first

---

## Related References

- CRM Deals API: `docs/API Documentation/deals/crm-deals-api.md`
- Deal Meetings API: `docs/API Documentation/deals/deal-meetings-api.md`

