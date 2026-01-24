# CRM Deals API Documentation

> Version: 1.0
> Base URL: `/users/crm`
> Last Updated: 2026-01-23

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
| `owner_external_ids` | array[string] | - | Filter by CRM owner external IDs (not internal user_id) |
| `min_amount` | number | - | Minimum deal amount |
| `max_amount` | number | - | Maximum deal amount |
| `min_close_date` | string | - | Minimum close date (ISO 8601) |
| `max_close_date` | string | - | Maximum close date (ISO 8601) |
| `q` | string | - | Case-insensitive search by deal name |

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deals retrieved successfully",
  "data": {
    "items": [
      {
        "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
        "name": "Acme Corp - Enterprise License",
        "amount": 50000,
        "currency": "USD",
        "stage_name": "Qualification",
        "deal_status": "open",
        "owner_name": "John Doe",
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

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal retrieved successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "amount": 50000,
    "currency": "USD",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "pipeline_name": "Default Pipeline",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "stage_name": "Qualification",
    "stage_probability": 10,
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
    "is_internal": true,
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T12:00:00Z"
  }
}
```

#### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `is_internal` | boolean | `true` if deal is from internal CRM (editable), `false` if from external CRM like HubSpot/Salesforce (read-only). Frontend should enable edit/delete actions only for internal deals. |

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

**Note**: To update `pipeline_id`, `stage_id`, or `owner_user_id`, use the dedicated endpoints below.

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

## 6. Assign Owner

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

## 7. Update Deal Account

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

## 8. Delete Deal

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
4. **Owner selection**: Use user_id from `/users/crm/users` search
5. **Stage selection**: Use stage_id from `/pipelines?include_stages=true`
6. **Optional fields**: Don't send `null` for optional fields - omit them entirely
7. **Account + Team**: Use `account` + `team_members` to capture company + internal team
   in one request. Company is created/linked only when `account.domain` is supplied.

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
