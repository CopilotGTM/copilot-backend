# Deal Meetings API

This API manages calendar meetings associated with CRM deals, including retrieving meeting lists, generating AI briefings, and updating association status.

**Base URL**: `/users/crm/deals`

## 1. Get Deal Meetings

Retrieve all meetings associated with a specific deal. This includes meeting details, attendee status, and any generated briefing data.

- **Endpoint**: `GET /{deal_id}/meetings`
- **Method**: `GET`
- **Tags**: `crm`, `meetings`

### Path Parameters (Generate Meeting Briefing)

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `deal_id` | UUID | The unique identifier of the deal. |

### Response (Generate Meeting Briefing)

Returns a list of meetings linked to the deal.

```json
{
  "status": "SUCCESS",
  "message": "Meetings retrieved successfully",
  "data": [
    {
      "event_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "subject": "Discovery Call with Acme Corp",
      "start_at": "2025-11-25T14:00:00Z",
      "status": "SUGGESTED",
      "stage": "Stage 2 - Validation",
      "attendees": [
        {
          "name": "Jane Doe",
          "email": "jane@acme.com",
          "status": "accepted"
        },
        {
          "name": "John Smith",
          "email": "john@mycompany.com",
          "status": "accepted"
        }
      ],
      "meeting_prep_status": "NOT_STARTED",
      "meeting_prep_data": null
    }
  ]
}
```

#### Response Schema (`DealMeetingResponse`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `event_id` | UUID | Unique identifier for the calendar event. |
| `subject` | string | Subject or title of the meeting. |
| `start_at` | datetime | Start time of the meeting (UTC). |
| `status` | enum | Association status: `SUGGESTED`, `CONFIRMED`, `REJECTED`. |
| `stage` | string | The current stage of the deal (e.g., "Discovery"). |
| `attendees` | list | List of attendees with name, email, and response status. |
| `meeting_prep_status` | enum | Status of the briefing generation: `NOT_STARTED`, `GENERATING`, `COMPLETED`, `FAILED`. |
| `meeting_prep_data` | object | The generated briefing data (if available). |

---

## 2. Generate Meeting Briefing

Trigger the AI agent to generate a strategic briefing for a specific meeting.

- **Endpoint**: `POST /{deal_id}/meetings/{event_id}/briefing`
- **Method**: `POST`
- **Tags**: `crm`, `meetings`

### Path Parameters (Update Meeting Status)

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `deal_id` | UUID | The unique identifier of the deal. |
| `event_id` | UUID | The unique identifier of the meeting event. |

### Response (Update Meeting Status)

Returns the generated briefing data.

```json
{
  "status": "SUCCESS",
  "message": "Briefing generated successfully",
  "data": {
    "objective": "Validate integration feasibility...",
    "summary": "Customer emphasized compliance visibility...",
    "stakeholder_analysis": "Lisa Wong is focused on audit readiness...",
    "pain_points": [
      "We lose track of compliance evidence every quarter.",
      "Our patching process is still mostly manual."
    ],
    "suggested_questions": [
      "What triggers escalation to security leadership?",
      "How do you currently track compliance evidence?"
    ],
    "potential_objections": [
      "We already have something similar."
    ],
    "checklist": [
      "Confirm definition of success metrics",
      "Clarify integration ownership"
    ]
  }
}
```

#### Response Schema (`BriefingData`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `objective` | string | The primary goal of the meeting. |
| `summary` | string | Concise summary of the deal context. |
| `stakeholder_analysis` | string | Key insights about the attendees. |
| `pain_points` | list[string] | Known pain points to address. |
| `suggested_questions` | list[string] | Strategic questions to ask. |
| `potential_objections` | list[string] | Likely objections and rebuttals. |
| `checklist` | list[string] | Pre-meeting preparation checklist. |

---

## 3. Update Meeting Status

Update the status of a meeting association (e.g., to confirm a suggested match or reject an incorrect one).

- **Endpoint**: `PATCH /{deal_id}/meetings/{event_id}/status`
- **Method**: `PATCH`
- **Tags**: `crm`, `meetings`

### Path Parameters

| Parameter | Type | Description |
| :--- | :--- | :--- |
| `deal_id` | UUID | The unique identifier of the deal. |
| `event_id` | UUID | The unique identifier of the meeting event. |

### Request Body

```json
{
  "status": "CONFIRMED"
}
```

| Field | Type | Description |
| :--- | :--- | :--- |
| `status` | enum | New status: `SUGGESTED`, `CONFIRMED`, `REJECTED`. |

### Response

```json
{
  "status": "SUCCESS",
  "message": "Meeting status updated to CONFIRMED",
  "data": null
}
```
