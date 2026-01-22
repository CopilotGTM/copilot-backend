# CRM Contacts & Companies API Documentation

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

- Write endpoints (`POST`, `PATCH`, `DELETE`): ~15/min, 100/hour per user
- Read endpoints (`GET`): ~60/min, 1000/hour per user

---

## Contacts

### 1) List Contacts

```http
GET /contacts
```

Query parameters:

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `limit` | integer | No | Items per page (1..100, default 50) |
| `cursor` | string | No | Opaque cursor from previous response |
| `q` | string | No | Search by name, email, or company |

Success response (200):

```json
{
  "status": "SUCCESS",
  "message": "Contacts retrieved successfully",
  "data": {
    "items": [
      {
        "contact_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
        "external_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
        "first_name": "Rita",
        "last_name": "Singh",
        "email": "rita@acme.com",
        "job_title": "VP Finance",
        "company_name": "Acme Corp"
      }
    ],
    "cursor": null
  }
}
```

Notes:
- Returns internal CRM contacts only.
- External CRM contacts are read-only and excluded from this list.

---

### 2) Create Contact

```http
POST /contacts
```

Request body:

```json
{
  "first_name": "Rita",
  "last_name": "Singh",
  "full_name": "Rita Singh",
  "email": "rita@acme.com",
  "job_title": "VP Finance",
  "company_name": "Acme Corp",
  "owner_user_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd",
  "additional_emails": ["rita.singh@acme.com"],
  "phone": "+1 212 555 1100",
  "properties": {
    "source": "import",
    "import_id": "imp_123"
  }
}
```

Success response (201):

```json
{
  "status": "SUCCESS",
  "message": "Contact created successfully",
  "data": {
    "contact_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
    "external_id": "e3a8f0a5-8c79-4ab3-93c0-4b3da7c8a1e6",
    "first_name": "Rita",
    "last_name": "Singh",
    "full_name": "Rita Singh",
    "email": "rita@acme.com",
    "job_title": "VP Finance",
    "company_name": "Acme Corp",
    "owner_user_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd",
    "created_at": "2026-01-20T12:00:00Z",
    "updated_at": "2026-01-20T12:00:00Z"
  }
}
```

Notes:
- Dedupes by email within the internal CRM integration.
- `owner_user_id` must be an app user ID from `GET /users`.

---

### 3) Update Contact

```http
PATCH /contacts/{contact_id}
```

Request body:

```json
{
  "job_title": "CFO",
  "email": "rita.cfo@acme.com"
}
```

Success response (200): same shape as Create Contact.

Notes:
- Updates contact participants and stakeholders when name/email/job title changes.
- External CRM contacts are read-only (HTTP 409).

---

### 4) Delete Contact

```http
DELETE /contacts/{contact_id}
```

Success response (200): same shape as Create Contact.

Notes:
- Soft deletes the contact.
- Related CRM participants are marked deleted and stakeholder sync is triggered.

---

## Companies

### 1) List Companies

```http
GET /companies
```

Query parameters:

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `limit` | integer | No | Items per page (1..100, default 50) |
| `cursor` | string | No | Opaque cursor from previous response |
| `q` | string | No | Search by name, domain, or industry |

Success response (200):

```json
{
  "status": "SUCCESS",
  "message": "Companies retrieved successfully",
  "data": {
    "items": [
      {
        "company_id": "5e2a7f0f-9d4c-4c57-8a66-2c5a2a0d4b9f",
        "external_id": "5e2a7f0f-9d4c-4c57-8a66-2c5a2a0d4b9f",
        "name": "Acme Corp",
        "domain": "acme.com",
        "industry": "Manufacturing",
        "owner_user_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd"
      }
    ],
    "cursor": null
  }
}
```

Notes:
- Returns internal CRM companies only.

---

### 2) Create Company

```http
POST /companies
```

Request body:

```json
{
  "name": "Acme Corp",
  "domain": "acme.com",
  "industry": "Manufacturing",
  "number_of_employees": 450,
  "owner_user_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd"
}
```

Success response (201):

```json
{
  "status": "SUCCESS",
  "message": "Company created successfully",
  "data": {
    "company_id": "5e2a7f0f-9d4c-4c57-8a66-2c5a2a0d4b9f",
    "external_id": "5e2a7f0f-9d4c-4c57-8a66-2c5a2a0d4b9f",
    "name": "Acme Corp",
    "domain": "acme.com",
    "industry": "Manufacturing",
    "owner_user_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd",
    "created_at": "2026-01-20T12:00:00Z",
    "updated_at": "2026-01-20T12:00:00Z"
  }
}
```

Notes:
- Dedupes by domain (preferred) or name within the internal CRM integration.

---

### 3) Update Company

```http
PATCH /companies/{company_id}
```

Request body:

```json
{
  "name": "Acme Corporation",
  "domain": "acme.com"
}
```

Success response (200): same shape as Create Company.

Notes:
- Updates related deal company fields and company participants.
- External CRM companies are read-only (HTTP 409).

---

### 4) Delete Company

```http
DELETE /companies/{company_id}
```

Success response (200): same shape as Create Company.

Notes:
- Soft deletes the company.
- Related CRM participants are marked deleted.

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
