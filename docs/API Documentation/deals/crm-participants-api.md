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

## Write Request Validation

Participant write request bodies are strict. Unknown fields are rejected with
`422`; send only `participants[].participant_type`, `participants[].related_id`,
`participants[].relationship_role`, or `relationship_role` on the role update
endpoint. Add requests must include at least one item in `participants`.

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

Participants use local IDs only. Request `related_id` is the local contact,
company, or user UUID, and responses expose `related_local_id`; provider IDs are
tracked through `crm_source_object_mappings`.

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
        "related_local_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
        "related_name": "Rita Singh",
        "related_email": "rita@acme.com",
        "linkedin_url": "https://www.linkedin.com/in/rita-singh",
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
- `relationship_role` is trimmed; blank strings are stored as `null`.
- Contact participants are de-duplicated by contact/email identity, not by role; adding the same contact twice skips the duplicate even if the role differs.
- A contact participant with a primary-contact role marks the participant as primary-contact state internally.
- Company participants update the deal account snapshot (`company_id`, `company_name`, `company_domain`, and related profile fields) in the same transaction.
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
    "related_local_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
    "related_name": "Rita Singh",
    "related_email": null,
    "linkedin_url": "https://www.linkedin.com/in/rita-singh",
    "relationship_role": "decision_maker",
    "is_deleted": true
  }
}
```

Notes:
- Contact participant removal triggers stakeholder sync for the deal.

---

## 3) Update Participant Role

```http
PATCH /deals/{deal_id}/participants/{participant_id}
```

Request body:

```json
{
  "relationship_role": "se"
}
```

Success response (200):

```json
{
  "status": "SUCCESS",
  "message": "Participant updated successfully",
  "data": {
    "participant_id": "6e8c5d2a-8a2f-40f1-9e3e-2f67a945f0e7",
    "deal_id": "3f5bfc3a-3b3b-4e89-8f0c-7a4d3b55f011",
    "participant_type": "user",
    "related_local_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd",
    "related_name": "Saksham Bhutani",
    "related_email": "saksham@example.com",
    "linkedin_url": null,
    "relationship_role": "se",
    "is_deleted": false
  }
}
```

Notes:
- Only internal CRM deal participants are writable.
- Changing role updates the participant identity for de-duplication.
- `relationship_role` is trimmed; send `null` or a blank string to clear it.

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
