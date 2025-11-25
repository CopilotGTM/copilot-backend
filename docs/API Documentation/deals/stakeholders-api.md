# Deal Stakeholders API

This API manages stakeholders associated with CRM deals, including retrieving stakeholder lists, manually adding stakeholders, and updating their details.

**Base URL**: `/users/crm/deals`

## 1. Get Deal Stakeholders

Retrieves all stakeholders associated with a specific deal.

- **Endpoint**: `GET /{deal_id}/stakeholders`
- **Method**: `GET`
- **Tags**: `crm`, `stakeholders`

### Path Parameters (Get Deal Stakeholders)

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `deal_id` | UUID | The unique identifier of the deal. |

### Response (Get Deal Stakeholders)

Returns a list of stakeholders linked to the deal.

```json
{
  "status": "SUCCESS",
  "message": "Stakeholders retrieved successfully",
  "data": [
    {
      "stakeholder_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "deal_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "email": "jane@example.com",
      "name": "Jane Doe",
      "job_title": "CTO",
      "stance": "SUPPORTER",
      "decision_power": "DECISION_MAKER",
      "champion_potential": 0.85,
      "engagement_score": 75.0,
      "temperature": "WARM",
      "source": "CRM"
    }
  ]
}
```

#### Response Schema (`StakeholderDetail`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `stakeholder_id` | UUID | Unique identifier for the stakeholder record. |
| `deal_id` | UUID | The deal this stakeholder belongs to. |
| `contact_id` | UUID | Linked CRM contact ID (if available). |
| `email` | string | Stakeholder email address. |
| `name` | string | Full name. |
| `job_title` | string | Job title. |
| `stance` | enum | Attitude towards the deal (e.g., `SUPPORTER`, `DETRACTOR`). |
| `decision_power` | enum | Influence level (e.g., `DECISION_MAKER`, `INFLUENCER`). |
| `champion_potential` | float | Calculated score indicating champion likelihood. |
| `engagement_score` | float | Score based on interaction frequency/quality. |
| `temperature` | enum | Relationship warmth (e.g., `COLD`, `WARM`, `HOT`). |
| `source` | enum | Origin of the stakeholder (e.g., `CRM`, `EMAIL`). |

---

## 2. Create Stakeholder

Manually adds a stakeholder to a deal.

- **Endpoint**: `POST /{deal_id}/stakeholders`
- **Method**: `POST`
- **Tags**: `crm`, `stakeholders`

### Path Parameters (Create Stakeholder)

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `deal_id` | UUID | The unique identifier of the deal. |

### Request Body (Create Stakeholder)

```json
{
  "email": "new.stakeholder@example.com",
  "name": "New Stakeholder",
  "job_title": "VP of Engineering",
  "stance": "NEUTRAL",
  "decision_power": "INFLUENCER"
}
```

#### Request Schema (`CreateStakeholderRequest`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `email` | string | **Required**. Email address of the stakeholder. |
| `name` | string | Full name. |
| `job_title` | string | Job title. |
| `stance` | enum | Initial stance (default: `UNKNOWN`). |
| `decision_power` | enum | Initial decision power (default: `UNKNOWN`). |
| `external_links` | list | List of social/external profile links. |

### Response (Create Stakeholder)

Returns the created stakeholder details.

---

## 3. Update Stakeholder

Updates manual fields for a stakeholder (Stance, Power, Motivation).

- **Endpoint**: `PATCH /{deal_id}/stakeholders/{stakeholder_id}`
- **Method**: `PATCH`
- **Tags**: `crm`, `stakeholders`

### Path Parameters (Update Stakeholder)

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `deal_id` | UUID | The unique identifier of the deal. |
| `stakeholder_id` | UUID | The unique identifier of the stakeholder. |

### Request Body (Update Stakeholder)

```json
{
  "stance": "CHAMPION",
  "motivation": "Wants to modernize tech stack",
  "pain_points": ["Legacy system maintenance", "Slow deployment cycles"]
}
```

#### Request Schema (`UpdateStakeholderRequest`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `stance` | enum | Updated stance. |
| `decision_power` | enum | Updated decision power. |
| `motivation` | string | Key motivation for the deal. |
| `pain_points` | list[string] | List of identified pain points. |
| `external_links` | list | Updated list of external links. |

### Response (Update Stakeholder)

Returns the updated stakeholder details.

---

## 4. List Available Contacts

Lists contacts that can be added as stakeholders for a deal. This excludes contacts that are already stakeholders.

- **Endpoint**: `GET /{deal_id}/stakeholders/available-contacts`
- **Method**: `GET`
- **Tags**: `crm`, `stakeholders`

### Path Parameters (List Available Contacts)

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `deal_id` | UUID | The unique identifier of the deal. |

### Query Parameters

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `query` | string | Optional search string for name or email. |
| `limit` | integer | Maximum number of results to return (default: 50). |

### Response (List Available Contacts)

Returns a list of available contacts.

```json
{
  "status": "SUCCESS",
  "message": "Available contacts retrieved successfully",
  "data": [
    {
      "contact_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "external_id": "contact_123",
      "first_name": "Alice",
      "last_name": "Wonderland",
      "email": "alice@example.com",
      "job_title": "Product Manager",
      "company_name": "Acme Corp"
    }
  ]
}
```

#### Response Schema (`ContactListItem`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `contact_id` | UUID | Internal unique identifier for the contact. |
| `external_id` | string | External system identifier. |
| `first_name` | string | First name. |
| `last_name` | string | Last name. |
| `email` | string | Email address. |
| `job_title` | string | Job title. |
| `company_name` | string | Associated company name. |
