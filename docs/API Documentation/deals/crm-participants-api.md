# CRM Deal Participants API Documentation

<!-- markdownlint-disable MD033 MD060 -->

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
- User identity (organisation, user, role) resolved from the token

---

## Rate Limiting (defaults)

- Write endpoints (`POST`, `DELETE`): ~15/min, 100/hour per user

---

## Participant Types

`participant_type` accepts:

- `contact`
- `company`
- `user`

---

## 1) Add Participants

```http
POST /deals/{deal_id}/participants
```

Request body:

```json
{
  "participants": [
    {
      "participant_type": "contact",
      "related_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
      "relationship_role": "decision_maker"
    },
    {
      "participant_type": "company",
      "related_id": "5e2a7f0f-9d4c-4c57-8a66-2c5a2a0d4b9f"
    },
    {
      "participant_type": "user",
      "related_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd",
      "relationship_role": "sc"
    }
  ]
}
```

Success response (201):

```json
{
  "status": "SUCCESS",
  "message": "Participants added successfully",
  "data": {
    "created": [
      {
        "participant_id": "6e8c5d2a-8a2f-40f1-9e3e-2f67a945f0e7",
        "deal_id": "3f5bfc3a-3b3b-4e89-8f0c-7a4d3b55f011",
        "participant_type": "contact",
        "related_external_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
        "related_local_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
        "related_name": "Rita Singh",
        "related_email": "rita@acme.com",
        "relationship_role": "decision_maker",
        "is_deleted": false
      }
    ],
    "skipped": [],
    "total_requested": 3,
    "total_created": 3,
    "total_skipped": 0
  }
}
```

Notes:
- Only internal CRM deals are writable. External CRM deals return HTTP 409.
- `related_id` must belong to the same internal CRM integration.
- Contact participants trigger stakeholder sync for the deal.
- Use `GET /contacts`, `GET /companies`, and `GET /users`
  to resolve selectable IDs.

---

## 2) Remove Participant

```http
DELETE /deals/{deal_id}/participants/{participant_id}
```

Success response (200):

```json
{
  "status": "SUCCESS",
  "message": "Participant removed successfully",
  "data": {
    "participant_id": "6e8c5d2a-8a2f-40f1-9e3e-2f67a945f0e7",
    "deal_id": "3f5bfc3a-3b3b-4e89-8f0c-7a4d3b55f011",
    "participant_type": "contact",
    "related_external_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
    "related_local_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
    "related_name": "Rita Singh",
    "related_email": null,
    "relationship_role": "decision_maker",
    "is_deleted": true
  }
}
```

Notes:
- Contact participant removal triggers stakeholder sync for the deal.

---

## Error Model

```json
{
  "status": "ERROR",
  "message": "Human-readable reason",
  "data": null
}
```

<!-- markdownlint-enable MD033 MD060 -->
