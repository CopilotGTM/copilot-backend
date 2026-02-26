# CRM Deals API Documentation

> Version: 1.3
> Base URL: `/users/crm`
> Last Updated: 2026-02-10

---

## Overview

This document defines the API contracts for CRM Deal operations. **Frontend developers should reference this document** to ensure request/response consistency.

### Internal vs External CRM Deals

All deal response objects include an `is_internal` boolean field:
- **Internal CRM deals** (`is_internal: true`): Created and managed within the platform. These deals are fully editable - users can update, move stages, assign owners, and delete them.
- **External CRM deals** (`is_internal: false`): Synced from external CRM systems (HubSpot, Salesforce). These deals are read-only and cannot be modified through the API.

**Frontend Implementation**: Use the `is_internal` field to conditionally show edit/delete buttons and actions. Only enable these for internal deals.

---

## 1. Create Deal

### Endpoint

```
POST /users/crm/deals
```

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |
| Content-Type | Yes | application/json |

### Request Body

| Field | Type | Required | Validation | Description |
|-------|------|----------|------------|-------------|
| `name` | string | **Yes** | 1-500 chars, non-empty | Deal name |
| `pipeline_id` | UUID | No | Valid pipeline UUID | Defaults to default pipeline |
| `stage_id` | UUID | No | Must belong to pipeline | Defaults to first stage |
| `owner_user_id` | UUID | No | Valid user UUID from `/users/crm/users` | Deal owner |
| `amount` | number | No | >= 0 | Deal value |
| `currency` | string | No | 3-char ISO code (USD, INR) | Currency code |
| `close_date` | string | No | ISO 8601 datetime | Target close date |
| `expected_close_date` | string | No | ISO 8601 datetime | Forecasted close date |
| `deal_type` | string | No | Max 128 chars | "New Business", "Renewal", "Expansion", "Upsell" |
| `priority` | string | No | Max 64 chars | "High", "Medium", "Low" |
| `lead_source` | string | No | Max 128 chars | "Inbound", "Outbound", "Referral", etc. |
| `description` | string | No | - | Deal notes/description |
| `account` | object | No | - | Optional account details captured at deal creation |
| `account.name` | string | No | - | Company/account name (optional if domain provided) |
| `account.domain` | string | No | - | Company domain (required to create/link company) |
| `account.website` | string | No | - | Company website URL (optional) |
| `account.linkedin_url` | string | No | - | Company LinkedIn URL (optional) |
| `team_members` | array | No | - | Internal team members to attach as participants |
| `team_members[].user_id` | UUID | Yes | Valid user UUID from `/users/crm/users` | Internal team member |
| `team_members[].role` | string | No | Max 128 chars | Team role label (e.g., "SE", "CSM") |

### Example Request

```json
{
  "name": "Acme Corp - Enterprise License",
  "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
  "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "amount": 50000,
  "currency": "USD",
  "close_date": "2026-03-15T00:00:00Z",
  "expected_close_date": "2026-03-31T00:00:00Z",
  "deal_type": "New Business",
  "priority": "High",
  "lead_source": "Inbound",
  "description": "Enterprise deal for 100 seats",
  "account": {
    "name": "Acme Corp",
    "domain": "acme.com",
    "linkedin_url": "https://www.linkedin.com/company/acme"
  },
  "team_members": [
    {
      "user_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd",
      "role": "SE"
    }
  ]
}
```

### Minimal Request (Only Required Field)

```json
{
  "name": "Quick Deal Entry"
}
```

### Notes on Account + Team Members

- If `account.domain` is provided, the system will create or link an internal CRM company
  and attach it to the deal.
- If `account.domain` is missing but `account.name` is provided, the deal will store
  `company_name` without creating a company record.
- `team_members` are added as internal CRM participants (`participant_type = user`).

### Success Response (201)

```json
{
  "status": "SUCCESS",
  "message": "Deal created successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "amount": 50000,
    "currency": "USD",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "pipeline_name": "Default Pipeline",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "stage_name": "Qualification",
    "deal_status": "open",
    "owner_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "owner_name": "John Doe",
    "owner_email": "john@example.com",
    "close_date": "2026-03-15T00:00:00Z",
    "expected_close_date": "2026-03-31T00:00:00Z",
    "deal_type": "New Business",
    "priority": "High",
    "lead_source": "Inbound",
    "description": "Enterprise deal for 100 seats",
    "company_external_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "created_at": "2026-01-22T10:00:00Z"
  }
}
```

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 400 | Validation error | Missing required field or invalid value |
| 404 | Pipeline/Stage not found | Invalid pipeline_id or stage_id |
| 409 | Internal CRM not configured | Org doesn't have internal CRM setup |
| 422 | Validation error | Field validation failed (details in response) |
| 500 | Internal server error | Unexpected error |

---

## 2. List Deals

### Endpoint

```
GET /users/crm/deals
```

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 40 | Items per page (1-100) |
| `page` | integer | 1 | Page number (1-indexed) |
| `statuses` | array[enum] | - | Filter by deal statuses (`open`, `won`, `lost`, `closed`) |
| `pipeline_ids` | array[uuid] | - | Filter by **pipeline_id** values from `/users/crm/pipelines` |
| `owner_ids` | array[uuid] | - | Filter by internal owner IDs from `/users/crm/deals/owners` (not page-scoped) |
| `owner_external_ids` | array[string] | - | Deprecated. Filter by CRM owner external IDs |
| `min_amount` | number | - | Minimum deal amount |
| `max_amount` | number | - | Maximum deal amount |
| `min_close_date` | string | - | Minimum close date (ISO 8601) |
| `max_close_date` | string | - | Maximum close date (ISO 8601) |
| `q` | string | - | Case-insensitive search by deal name or company name |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deals retrieved successfully",
  "data": {
    "active_import_job": {
      "job_id": "9f2c1b62-9d8a-4f1c-bac4-7f5b2a8f5b0b",
      "job_type": "deal_import",
      "status": "in_progress",
      "stream_url": "/users/crm/deals/import/jobs/9f2c1b62-9d8a-4f1c-bac4-7f5b2a8f5b0b/events"
    },
    "items": [
      {
        "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
        "name": "Acme Corp - Enterprise License",
        "amount": 50000,
        "currency": "USD",
        "currency_symbol": "$",
        "stage_name": "Qualification",
        "deal_status": "open",
        "owner_details": [
          {
            "owner_id": "f2c8a7ce-1c0e-4b0b-8c07-1a9b6b8b0c1d",
            "external_id": "83275159",
            "name": "John Doe",
            "email": "john@example.com"
          }
        ],
        "close_date": "2026-03-15T00:00:00Z",
        "is_internal": true,
        "created_at": "2026-01-22T10:00:00Z"
      }
    ],
    "page": 1,
    "limit": 40,
    "total_count": 120,
    "total_pages": 3
  }
}
```

#### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `is_internal` | boolean | `true` if deal is from internal CRM (editable), `false` if from external CRM like HubSpot/Salesforce (read-only). Frontend should enable edit/delete actions only for internal deals. |
| `owner_details` | array | Owner list (typically 0..1). Use `owner_id` for filtering and linking. |
| `currency_symbol` | string \| null | Symbol derived from `currency` (for example `$`, `₹`). Frontend should use this for icon/amount decoration. |
| `active_import_job` | object \| null | Current user's active import job for SSE reconnection after refresh. Null when no import is active. |

---

## 3. Get Deal Detail

### Endpoint

```
GET /users/crm/deals/{deal_id}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `deal_id` | UUID | Deal UUID |

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_empty_custom_columns` | boolean | `true` | When `true`, response includes active custom field keys with `null` values even if the deal has no stored value yet. |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal retrieved successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "amount": 50000,
    "currency": "USD",
    "currency_symbol": "$",
    "predicted_amount": 5000,
    "probability": 10,
    "forecast_category": "Commit",
    "pipeline_external_id": "default_pipeline",
    "pipeline_name": "Default Pipeline",
    "pipeline_label": "Default Pipeline",
    "stage_external_id": "qualification",
    "stage_name": "Qualification",
    "stage_label": "Qualification",
    "deal_status": "open",
    "owner_details": [
      {
        "owner_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "external_id": "83275159",
        "name": "John Doe",
        "email": "john@example.com"
      }
    ],
    "deal_team": [],
    "company_external_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "company_industry": "Manufacturing",
    "company_country": "United States",
    "primary_contact_external_id": "contact_123",
    "primary_contact_name": "Jane Buyer",
    "primary_contact_email": "jane@acme.com",
    "close_date": "2026-03-15T00:00:00Z",
    "expected_close_date": "2026-03-31T00:00:00Z",
    "closed_won_at": null,
    "closed_lost_at": null,
    "source_created_at": "2026-01-22T10:00:00Z",
    "last_activity_at": "2026-01-23T09:30:00Z",
    "stage_entered_at": "2026-01-22T10:00:00Z",
    "stage_updated_at": "2026-01-23T09:00:00Z",
    "deal_age_days": 19,
    "deal_type": "New Business",
    "priority": "High",
    "lead_source": "Inbound",
    "closed_reason": null,
    "crm_url": "https://crm.example/deal/123",
    "description": "Enterprise deal for 100 seats",
    "is_internal": true,
    "deal_health": "ON_TRACK",
    "deal_health_reason": "Recent meeting activity and healthy stage progression",
    "custom_columns": {
      "client_relationship_type": "Existing Client - Upsell",
      "our_revenue_inr": "25000000",
      "target_go_live_date": null
    },
    "custom_column_definitions": [
      {
        "key": "our_revenue_inr",
        "label": "Our Revenue (INR)",
        "data_type": "currency",
        "required": false,
        "options": {},
        "validation": {},
        "currency_code": "INR",
        "currency_symbol": "₹"
      },
      {
        "key": "target_go_live_date",
        "label": "Target Go Live Date",
        "data_type": "datetime",
        "required": false,
        "options": {},
        "validation": {},
        "currency_code": null,
        "currency_symbol": null
      }
    ],
    "source_modified_at": "2026-01-23T09:30:00Z",
    "last_seen_at": "2026-01-23T09:30:00Z",
    "synced_at": "2026-01-23T09:30:00Z",
    "associated_contact_count": 4,
    "associated_company_count": 1,
    "note_count": 2,
    "deal_summary_data": null,
    "active_deal_jobs": null,
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T12:00:00Z"
  }
}
```

#### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `is_internal` | boolean | `true` if deal is from internal CRM (editable), `false` if from external CRM like HubSpot/Salesforce (read-only). Frontend should enable edit/delete actions only for internal deals. |
| `custom_columns` | object \| null | For internal CRM deals, includes imported custom key/value metadata. With `include_empty_custom_columns=true`, active custom keys are included even when values are `null`. |
| `custom_column_definitions` | array \| null | Active custom field metadata for internal deals. Use `data_type` to render controls; for `currency` fields use `currency_code` + `currency_symbol`. |
| `owner_details` | array | Canonical owner payload. Use this list (0..1 for internal CRM) instead of legacy single-owner fields. |
| `currency_symbol` | string \| null | Symbol derived from `currency` (for example `$`, `₹`) for consistent detail rendering. |
| `company_country` | string \| null | Country/geography of the associated company. Populated from the linked CRM company record. |
| `source_modified_at` / `last_seen_at` / `synced_at` | datetime | System read-only sync metadata for advanced view and troubleshooting. |

---

## 4. Update Deal

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`. External CRM deals (HubSpot, Salesforce) are read-only.

### Endpoint

```
PATCH /users/crm/deals/{deal_id}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `deal_id` | UUID | Deal UUID |

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |
| Content-Type | Yes | application/json |

### Request Body

**All fields are optional** - only include fields you want to update. Omitted fields remain unchanged.

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `name` | string | 1-500 chars, non-empty | Deal name |
| `amount` | number | >= 0 | Deal value (can be null) |
| `currency` | string | 3-char ISO code (USD, INR) | Currency code |
| `close_date` | string | ISO 8601 datetime | Target close date |
| `expected_close_date` | string | ISO 8601 datetime | Forecasted close date |
| `deal_type` | string | Max 128 chars | "New Business", "Renewal", "Expansion", "Upsell" |
| `priority` | string | Max 64 chars | "High", "Medium", "Low" |
| `lead_source` | string | Max 128 chars | "Inbound", "Outbound", "Referral", etc. |
| `description` | string | - | Deal notes/description |
| `probability` | number | 0-100 | Win probability (%) |
| `forecast_category` | string | Max 64 chars | Forecast bucket (Pipeline / Best Case / Commit / Closed) |
| `custom_properties` | object | keys must exist in custom field definitions | Custom field values keyed by custom field key (`custom_` prefix accepted and normalized) |

**Note**: To update `pipeline_id`, `stage_id`, or `owner_user_id`, use the dedicated endpoints below.
`source_created_at`, `last_activity_at`, and `crm_url` are read-only and returned by deal detail API.
Use `POST /users/crm/deals/{deal_id}/close` to set close outcome and `closed_reason`.

**Custom field validation**:
- Unknown custom keys are rejected (`400`).
- Formula custom fields are read-only in this endpoint.
- Values are type-checked against field definitions.
- Supported custom field types include `text`, `number`, `integer`, `boolean`, `date`, `datetime`, and `currency` (plus select/multiselect/formula where applicable).

### Example Request

Update only amount and close date:
```json
{
  "amount": 75000,
  "close_date": "2026-04-15T00:00:00Z"
}
```

Update all editable fields:
```json
{
  "name": "Acme Corp - Enterprise Plus",
  "amount": 75000,
  "currency": "USD",
  "close_date": "2026-04-15T00:00:00Z",
  "expected_close_date": "2026-04-30T00:00:00Z",
  "deal_type": "Expansion",
  "priority": "High",
  "lead_source": "Inbound",
  "description": "Upgraded to enterprise plus tier with 200 seats"
}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise Plus",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T15:30:00Z"
  }
}
```

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 400 | No fields to update | Request body is empty or contains no valid fields |
| 404 | Deal not found | Invalid deal_id or deal doesn't belong to org |
| 409 | External CRM deals are read-only | Tried to edit a deal synced from HubSpot/Salesforce (`is_internal: false`) |
| 422 | Validation error | Field validation failed (details in response) |
| 500 | Internal server error | Unexpected error |

---

## 5. Move Stage

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
POST /users/crm/deals/{deal_id}/stage
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `deal_id` | UUID | Deal UUID |

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |
| Content-Type | Yes | application/json |

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `stage_id` | UUID | **Yes** | UUID of the target stage (must belong to same pipeline) |

### Example Request

```json
{
  "stage_id": "9ba7b810-9dad-11d1-80b4-00c04fd430c9"
}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal stage updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "9ba7b810-9dad-11d1-80b4-00c04fd430c9",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T15:45:00Z"
  }
}
```

**Note**: Moving to a new stage automatically creates a stage history record with timestamp.

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 400 | Missing stage_id | Request missing required field |
| 404 | Stage not found | Invalid stage_id or stage doesn't belong to deal's pipeline |
| 409 | External CRM deals are read-only | Tried to move a deal synced from HubSpot/Salesforce |
| 500 | Internal server error | Unexpected error |

---

## 6. Close Deal

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
POST /users/crm/deals/{deal_id}/close
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `outcome` | enum | **Yes** | `won` or `lost` |
| `closed_reason` | string | Conditionally required | Required when `outcome=lost` |
| `close_date` | string | No | ISO 8601 datetime; defaults to current time |
| `stage_id` | UUID | No | Optional explicit closed stage; must match outcome state |

### Example Request (Won)

```json
{
  "outcome": "won",
  "closed_reason": "Signed annual contract"
}
```

### Example Request (Lost)

```json
{
  "outcome": "lost",
  "closed_reason": "Budget frozen for current quarter"
}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal closed successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "9ba7b810-9dad-11d1-80b4-00c04fd430c9",
    "deal_status": "won",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-02-10T16:15:00Z"
  }
}
```

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 400 | Invalid close request | Invalid outcome or missing `closed_reason` for lost outcome |
| 404 | Deal/stage not found | Invalid deal_id or stage cannot be resolved |
| 409 | External CRM deals are read-only | Tried to close a synced external deal |
| 500 | Internal server error | Unexpected error |

---

## 7. Reopen Deal

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
POST /users/crm/deals/{deal_id}/reopen
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `stage_id` | UUID | No | Optional target open stage |
| `clear_closed_reason` | boolean | No | When true, clears previously stored close reason |

### Example Request

```json
{
  "clear_closed_reason": true
}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal reopened successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-02-10T16:20:00Z"
  }
}
```

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 404 | Deal/stage not found | Invalid deal_id or open stage not found |
| 409 | External CRM deals are read-only | Tried to reopen a synced external deal |
| 500 | Internal server error | Unexpected error |

---

## 8. Assign Owner

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
POST /users/crm/deals/{deal_id}/owner
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `deal_id` | UUID | Deal UUID |

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |
| Content-Type | Yes | application/json |

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `owner_user_id` | UUID or null | **Yes** | User UUID from `/users/crm/users` list, or `null` to clear owner |

### Example Requests

Assign owner:
```json
{
  "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"
}
```

Clear owner (unassign):
```json
{
  "owner_user_id": null
}
```

### Success Response (200)

Assigned:
```json
{
  "status": "SUCCESS",
  "message": "Deal owner assigned successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T15:50:00Z"
  }
}
```

Cleared:
```json
{
  "status": "SUCCESS",
  "message": "Deal owner cleared successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "owner_user_id": null,
    ...
  }
}
```

**Note**: On first owner assignment, if no CRM users exist, the system auto-syncs all org users to CRM (MVP behavior).

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 404 | User not found | Invalid owner_user_id or user not available in CRM |
| 409 | External CRM deals are read-only | Tried to assign owner to a deal synced from HubSpot/Salesforce |
| 500 | Internal server error | Unexpected error |

---

## 9. Update Deal Account

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
PATCH /users/crm/deals/{deal_id}/account
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | No | Company/account name |
| `domain` | string | No | Company domain (recommended; used to create/link company) |
| `website` | string | No | Company website URL |
| `linkedin_url` | string | No | Company LinkedIn URL |
| `industry` | string | No | Company industry classification |
| `country` | string | No | Company country/geography |

### Behavior

- If `domain` (or `website`) is provided, the system creates or links a company by domain
  and attaches it to the deal.
- If only `name` is provided, the deal stores `company_name` without creating a company record.

### Example Request

```json
{
  "name": "Acme Corp",
  "domain": "acme.com",
  "linkedin_url": "https://www.linkedin.com/company/acme"
}
```

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal account updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "company_external_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-23T11:00:00Z"
  }
}
```

---

## 10. Update Primary Contact

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
PATCH /users/crm/deals/{deal_id}/primary-contact
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `primary_contact_name` | string | No | Display name for primary contact |
| `primary_contact_email` | string | No | Email for primary contact (recommended) |

At least one of the two fields must be provided.

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal primary contact updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "company_external_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-02-10T10:30:00Z"
  }
}
```

---

## 11. Delete Deal

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`. **This is a soft delete** - the deal is marked as deleted but remains in the database.

### Endpoint

```
DELETE /users/crm/deals/{deal_id}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `deal_id` | UUID | Deal UUID |

### Request Headers

| Header | Required | Value |
|--------|----------|-------|
| Authorization | Yes | Bearer {token} |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal deleted successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "external_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "closed",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T16:00:00Z"
  }
}
```

**Behavior:**
- Deal is marked `is_deleted: true` in database
- Deal status changed to `CLOSED`
- Deal no longer appears in list/search results
- Deal can still be accessed by direct ID (for audit purposes)

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 404 | Deal not found | Invalid deal_id or deal already deleted |
| 409 | External CRM deals are read-only | Tried to delete a deal synced from HubSpot/Salesforce (`is_internal: false`) |
| 500 | Internal server error | Unexpected error |

**Frontend Recommendation**: Show a confirmation modal before calling this endpoint:
```
"Are you sure you want to delete this deal? This action cannot be undone."
[Cancel] [Delete Deal]
```

---

## Supporting APIs

### Get Currencies

```
GET /users/crm/settings/currencies
```

**Response:**
```json
{
  "status": "SUCCESS",
  "data": {
    "items": [
      { "code": "USD", "name": "US Dollar", "symbol": "$" },
      { "code": "INR", "name": "Indian Rupee", "symbol": "₹" }
    ],
    "default_code": "USD"
  }
}
```

### Get All Deal Form Options

```
GET /users/crm/settings/deal-options
```

Returns currencies, deal types, priorities, lead sources, and forecast categories in a single call.

### Search Users (for Owner Selection)

```
GET /users/crm/users?q={search}&limit=20
```

**Response:**
```json
{
  "status": "SUCCESS",
  "data": {
    "items": [
      {
        "user_id": "uuid",
        "name": "John Doe",
        "email": "john@example.com",
        "role": "sales_rep",
        "job_title": "Account Executive"
      }
    ],
    "cursor": null
  }
}
```

### Get Deal Owners (for Filters)

```
GET /users/crm/deals/owners?limit=200&q={search}
```

**Response:**
```json
{
  "status": "SUCCESS",
  "data": {
    "items": [
      {
        "owner_id": "f2c8a7ce-1c0e-4b0b-8c07-1a9b6b8b0c1d",
        "name": "Jane Smith",
        "email": "jane@example.com"
      }
    ]
  }
}
```

Use `owner_id` values from this endpoint with the `owner_ids` filter in `GET /users/crm/deals`.

### Get Pipelines (with Stages)

```
GET /users/crm/pipelines?include_stages=true
```

**Response:**
```json
{
  "status": "SUCCESS",
  "data": {
    "items": [
      {
        "pipeline_id": "uuid",
        "name": "Default Pipeline",
        "is_default": true,
        "stages": [
          {
            "stage_id": "uuid",
            "name": "Qualification",
            "label": "Qualification",
            "probability": 10,
            "display_order": 1,
            "stage_state": "open"
          }
        ]
      }
    ]
  }
}
```

---

## Common Error Response Format

```json
{
  "status": "ERROR",
  "message": "Error description",
  "data": null
}
```

### Validation Error (422)

```json
{
  "status": "ERROR",
  "message": "Validation failed",
  "detail": [
    {
      "loc": ["body", "name"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```

---

## Frontend Implementation Notes

1. **Always send dates in ISO 8601 format**: `2026-03-15T00:00:00Z`
2. **UUIDs must be valid**: Use the exact UUID format returned by APIs
3. **Currency codes**: Only use codes from `/settings/currencies` (currently USD, INR)
4. **Currency icon rendering**: Use `currency_symbol` from deal list/detail responses (fallback by matching `currency` with `/settings/currencies`).
5. **Owner selection**: Use user_id from `/users/crm/users` search
6. **Stage selection**: Use stage_id from `/pipelines?include_stages=true`
7. **Optional fields**: Don't send `null` for optional fields - omit them entirely
8. **Account + Team**: Use `account` + `team_members` to capture company + internal team
   in one request. Company is created/linked only when `account.domain` is supplied.
9. **Owner filter options**: Use `/users/crm/deals/owners` and filter by `owner_ids`; do not derive options from the current page.
10. **Close flow**: Use `POST /users/crm/deals/{deal_id}/close` and `POST /users/crm/deals/{deal_id}/reopen` for lifecycle transitions instead of generic edit.
11. **Custom field control rendering**: Use `data.custom_column_definitions` from `GET /users/crm/deals/{deal_id}` to choose controls (`date` -> date picker, `datetime` -> date-time picker, `currency` -> numeric input with currency symbol/code).
12. **Import reconnect on refresh**: Read `data.active_import_job` from `GET /users/crm/deals`; when present, reconnect SSE using `stream_url` and continue showing import progress modal/state.

---

## Related Documents

- `deal-workflow-specification.md` - Complete workflow specification
- `create-deal-fields-specification.md` - Field-level specification
- `internal-crm-frontend-stepwise-integration.md` - Integration guide

---

## Deal Import (CSV)

### Download Template

```
GET /users/crm/deals/import/template?file_type=csv
```

### Create Upload URL

```
POST /users/crm/deals/import/upload-url
```

Body:
```
{
  "filename": "deals_import.csv"
}
```

### Process Uploaded CSV

```
POST /users/crm/deals/import/process
```

Body:
```
{
  "storage_path": "crm_imports/deals/<user>/<id>/deals_import.csv"
}
```

### SSE Stream (Import Job)

```
GET /users/crm/deals/import/jobs/{job_id}/events
```

See `docs/API Documentation/deals/crm-deals-import-api.md` for full request/response and SSE payloads.

---

## Changelog

### 2026-02-26

1. Added `company_country` to `GET /users/crm/deals/{deal_id}` response. Populated from linked CRM company record.
2. Added `industry` and `country` fields to `PATCH /users/crm/deals/{deal_id}/account` request. These are now editable via the account update endpoint.
3. Expanded `q` search parameter in `GET /users/crm/deals` to match against both deal name and company name (OR condition).

### 2026-02-10

1. Expanded `GET /users/crm/deals/{deal_id}` response to include broader deal, timeline, association, and system metadata.
2. Added `include_empty_custom_columns` query parameter behavior to detail docs.
3. Extended `PATCH /users/crm/deals/{deal_id}` contract with additional core fields and `custom_properties`.
4. Added new endpoint: `PATCH /users/crm/deals/{deal_id}/primary-contact`.
5. Documented custom field update validation behavior for interactive deal edits.
6. Restricted `PATCH /users/crm/deals/{deal_id}` to disallow editing `source_created_at` and `last_activity_at` (read-only timeline fields).
7. Added dedicated lifecycle endpoints: `POST /users/crm/deals/{deal_id}/close` and `POST /users/crm/deals/{deal_id}/reopen`.
8. Moved `closed_reason` handling out of generic update and into the dedicated close flow.
9. Added `currency_symbol` to deal list/detail responses so frontend can render currency icons without hardcoded defaults.
10. Documented `datetime` and `currency` as supported custom field types for interactive deal updates.
11. Added explicit frontend guidance to render `date`/`datetime`/`currency` custom field controls from backend `data_type`.
12. Added `active_import_job` to deal list response so frontend can reconnect deal-import SSE after refresh.
13. Added `custom_column_definitions` to deal detail response, including `data_type` and currency context (`currency_code`, `currency_symbol`) for custom field UI rendering.
14. Restricted `PATCH /users/crm/deals/{deal_id}` to disallow editing `crm_url` (read-only detail metadata).
