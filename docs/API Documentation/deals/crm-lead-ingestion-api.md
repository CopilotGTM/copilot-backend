# CRM Lead Intake APIs

Lead intake is form-first. Customers create a lead form under authenticated CRM
routes, define external field keys, map those keys to lead columns or custom
properties, and submit through either the public website channel or a
form-scoped private key.

The direct `/private/v1/crm/leads` endpoint remains available as an advanced Direct
Lead API for callers that already send normalized lead objects. It does not use
lead-form mappings and cannot create `source_type = "form"` leads.

Lead `status = "converted"` is reserved for
`POST /users/crm/leads/{lead_id}/convert`. Authenticated lead create/update and
private ingestion reject `converted` because conversion must also stamp
`converted_deal_id` and `converted_at`.

## Authenticated Lead Form Management

Base path: `/users/crm`

### Create Lead Form

`POST /lead-forms`

Request:

```json
{
  "name": "Website demo form",
  "description": "Main website lead capture",
  "status": "active",
  "allowed_origins": ["https://www.example.com"],
  "settings": {
    "submit_label": "Book demo"
  },
  "fields": [
    {
      "external_key": "email",
      "label": "Work email",
      "target_type": "lead_field",
      "target_key": "email",
      "data_type": "email",
      "required": true,
      "display_order": 0
    },
    {
      "external_key": "company_size",
      "label": "Company size",
      "target_type": "property",
      "target_key": "company_size",
      "data_type": "number",
      "required": false,
      "display_order": 1
    }
  ]
}
```

Rules:

- `external_key` values must be unique per form.
- `target_type = "lead_field"` must use a supported `CRMLead` column mapping.
- `target_type = "property"` requires `target_key`.
- `target_type = "ignore"` accepts the field but does not write it to the lead.

The response includes `public_form_key` and `public_form_url`.

### Manage Form Private Keys

Form-scoped private keys are created under the form, not at org-level:

- `POST /lead-forms/{form_id}/private-keys`
- `GET /lead-forms/{form_id}/private-keys`
- `PATCH /lead-forms/{form_id}/private-keys/{key_id}/status`

Create request:

```json
{
  "name": "Website backend production",
  "allowed_ips": ["203.0.113.10"],
  "expires_at": null,
  "rate_limit_policy": {
    "per_minute": 300,
    "per_hour": 10000
  }
}
```

Create response returns the plaintext secret once:

```json
{
  "status": "SUCCESS",
  "message": "Lead form private key created",
  "data": {
    "key_id": "skid_example",
    "secret": "cgtm_live_example",
    "status": "active",
    "created_at": "2026-05-03T10:00:00Z"
  }
}
```

List responses never include `secret` or `secret_hash`. Status values are
`active`, `paused`, and `revoked`; revoked keys cannot be reactivated.

## Public Website Form API

Base path: `/public/v1`

### Get Public Form Definition

`GET /forms/{public_form_key}`

Returns browser-safe field metadata only. Internal `target_type` and
`target_key` mappings are not exposed.

### Submit Public Form

`POST /forms/{public_form_key}/submissions`

Request:

```json
{
  "fields": {
    "email": "buyer@example.com",
    "company_size": 250
  },
  "context": {
    "landing_page": "https://www.example.com/demo"
  }
}
```

Success:

```json
{
  "submission_id": "5b449cf1-53e2-4d11-a3df-0f90987351f4",
  "status": "accepted"
}
```

Rules:

- Browser CORS uses the form's `allowed_origins`; exact origin matches receive
  `Access-Control-Allow-Origin` on preflight and successful responses.
- Origins that are not listed on the form do not receive CORS headers and
  browser preflight requests are rejected.
- Public rate limits are scoped by form key and client IP.
- Unknown submitted field keys are rejected.
- Public submissions create `CRMLead.source_type = "form"`.

## Private Mapped Form API

Base path: `/private/v1`

### Submit Mapped Form Payload

`POST /forms/submissions`

Headers:

```http
X-CopilotGTM-Key-Id: skid_example
Authorization: Bearer cgtm_live_example
Idempotency-Key: meta-lead-98765
```

Request:

```json
{
  "fields": {
    "email": "buyer@example.com",
    "company_size": 250
  },
  "context": {
    "source": "meta_ads"
  }
}
```

Success:

```json
{
  "submission_id": "5b449cf1-53e2-4d11-a3df-0f90987351f4",
  "lead_id": "9c39f11e-0fa7-45c0-bd94-44cd7d39f191",
  "status": "accepted"
}
```

Rules:

- The key must have `key_type = "private_api"` and `resource_type = "lead_form"`.
- The form id is resolved from the key's `resource_id`.
- `Idempotency-Key` is required and scoped to the form plus private key id.
  Retries with the same key return the existing accepted submission/lead pair.
- Idempotency is enforced by dedicated database columns and unique indexes; the
  replay token is immutable and is not freed by later soft deletion.
- Key status, expiry, IP allowlist, and key-scoped rate limits are enforced.
- Unknown submitted field keys are rejected.
- Private mapped form submissions create `CRMLead.source_type = "api"` while
  still linking `form_id` and `form_submission_id`.

## Direct Lead API

Base path: `/private/v1`

### Create Normalized Lead

`POST /crm/leads`

This endpoint is for advanced server-to-server callers that already produce the
normalized lead payload. It does not read lead forms or create
`LeadFormSubmission` rows.

Headers:

```http
X-CopilotGTM-Key-Id: skid_direct_example
Authorization: Bearer cgtm_live_example
Idempotency-Key: direct-lead-98765
```

Request:

```json
{
  "external_id": "leadgen-98765",
  "source_type": "api",
  "source_channel": "meta_ads",
  "source_name": "Q2 demo campaign",
  "source_ref": "campaign_123",
  "full_name": "Asha Rao",
  "email": "asha@example.com",
  "company_name": "ExampleCo",
  "company_domain": "example.com",
  "properties": {
    "lead_temperature": "warm"
  },
  "data_payload": {
    "raw_source_reference": "meta-leadgen-98765"
  }
}
```

Success:

```json
{
  "lead_id": "9c39f11e-0fa7-45c0-bd94-44cd7d39f191",
  "status": "accepted"
}
```

Rules:

- Direct keys use `resource_type = "lead_ingestion"` and `resource_id = organisation_id`.
- `Idempotency-Key` is required and scoped to the private key id. Retries with
  the same key return the existing accepted lead.
- Idempotency is enforced by dedicated database columns and unique indexes; the
  replay token is immutable and is not freed by later soft deletion.
- Unknown top-level fields are rejected.
- At least one of `email`, `phone`, `full_name`, or `company_name` is required.
- `source_type = "form"` is rejected.
- `status = "converted"` is rejected; use the authenticated conversion endpoint.
