# CRM Deals API Documentation

> Version: 1.7
> Base URL: `/users/crm`
> Last Updated: 2026-07-19

---

## Overview

This document defines the API contracts for CRM Deal operations. **Frontend developers should reference this document** to ensure request/response consistency.

### Internal vs External CRM Deals

All deal response objects include an `is_internal` boolean field:
- **Internal CRM deals** (`is_internal: true`): Created and managed within the platform. These deals are editable through field-specific endpoints: inline PATCH for core fields, stage/lifecycle actions for status changes, owner assignment for routing, account/contact actions for relationships, and delete for soft removal.
- **External CRM deals** (`is_internal: false`): Synced from external CRM systems (HubSpot, Salesforce). These deals are read-only and cannot be modified through the API.

**Frontend Implementation**: Use the `is_internal` field to conditionally show edit/delete buttons and actions. Only enable these for internal deals.

### Write Request Validation

All JSON write request bodies in this API are strict. Unknown fields are rejected
with `422` instead of being ignored. Send only the fields listed for the target
endpoint; for example, do not send `deal_status` to create/update endpoints,
`stage_name` to the stage action, or `owner_name` to the owner assignment action.

Successful create/update/action endpoints return `DealWriteResponse`. It includes
the refreshed core deal fields needed to update a list row or detail header without
guessing locally. It does not include full detail-only objects such as
`detail_field_definitions`, `deal_team`, `deal_summary_data`, or
`custom_column_definitions`; fetch `GET /users/crm/deals/{deal_id}` for the full
detail view.

### Identifier Contract

CRM API payloads expose canonical local IDs such as `deal_id`, `company_id`,
`contact_id`, `pipeline_id`, `stage_id`, and `owner_id`. Provider identifiers from
HubSpot, Salesforce, or migration imports are stored in `crm_source_object_mappings`
and are not part of canonical deal API responses.

### Ownership and Account Profile Semantics

- Deal owner means the CRM user currently responsible for driving the deal, not
  necessarily the user who created the row.
- `owner_user_id` in write payloads maps to the deal's resolved `owner_id`.
  Display with `owner_details` / `owner_name` / `owner_email`.
- `company_id`, `company_name`, and `company_domain` are deal association
  snapshots stored on the deal. Deal list rows also expose the same account as
  `account_details` so account cells can be rendered like `owner_details` and
  linked by `account_details[0].company_id`.
- `company_industry`, `company_website`, `company_linkedin_url`, and
  `company_country` are account profile fields from the linked CRM company row.
  They are returned on deal detail/write responses for convenience, but the
  durable source is the account/company record.
- `primary_contact_id`, `primary_contact_name`, and `primary_contact_email` are
  the deal's primary-contact snapshot. Deal list rows also expose the same
  contact as `primary_contact_details` so contact cells can link by
  `primary_contact_details[0].contact_id`. Deal detail and primary-contact write
  responses also include `primary_contact_details[0].phone_number` when the
  linked contact has a phone or mobile phone.

### Frontend Migration Checklist

Use this checklist when updating the deal UI:

- Add `predicted_amount` to deal list, search, and write-response handling.
- Add `stage_id` and `pipeline_id` to deal list/search rows; use these IDs for
  filter preselection and stage actions, not labels.
- Render deal list columns from `user_preferences.crm.deals.list.columns`.
- Do not render `deal_health` or `deal_health_reason` in the deal list; those
  fields are detail-only.
- Treat `DealWriteResponse` as the canonical response for create, inline update,
  stage/lifecycle, owner, account, primary-contact, and delete actions.
- Render predefined deal detail rows from `detail_field_definitions`; do not
  hardcode the order or editability.
- For create/edit custom fields, prefer `GET /users/crm/schema?entity=deal`.
  It returns standard fields, active internal custom fields, provider fields,
  required flags, data types, select/multiselect options, and writeability before
  `POST /deals`.
- `custom_column_definitions` on deal detail remains as a compatibility payload
  for existing detail rendering. New create/edit/import/agent consumers should
  use the schema endpoint instead.
- Keep `associated_contact_count`, `associated_company_count`, and `note_count`
  as top-level summary values, not Get Details rows.
- Route edits by `edit_policy`: `always` -> inline PATCH, `stage_action` ->
  stage endpoint, `owner_assignment` -> owner endpoint, `account_action` ->
  account endpoint, `primary_contact_action` -> primary-contact endpoint.
- For manual POC selection, use active approved Buying Committee contact
  stakeholders on the deal as the picker source. Do not let users choose an
  archived contact, removed/rejected/pending stakeholder, or unrelated contact as
  the deal POC.
- Read the deal timeline from `GET /users/crm/deals/{deal_id}/activity-feed`.
  Use `occurred_at` for feed ordering and source-specific values in
  `display_payload` (for example meeting `start_at`) for event detail labels.

---

## Frontend TypeScript Contracts

```ts
type UUID = string;
type ISODate = string;

export interface DealOwnerDetail {
  owner_id: UUID | null;
  name: string | null;
  email: string | null;
}

export interface DealAccountDetail {
  company_id: UUID | null;
  name: string | null;
  domain: string | null;
}

export interface DealPrimaryContactDetail {
  contact_id: UUID | null;
  name: string | null;
  email: string | null;
  phone_number: string | null;
}

export interface DealTeamMember {
  participant_id: UUID | null;
  user_id: UUID | null;
  name: string | null;
  email: string | null;
  role: string;
}

export interface DealCustomColumnDefinition {
  key: string;
  label: string;
  data_type:
    | "text"
    | "number"
    | "integer"
    | "boolean"
    | "date"
    | "datetime"
    | "currency"
    | "percentage"
    | "select"
    | "multiselect"
    | "formula"
    | string;
  required: boolean;
  options: Record<string, unknown>;
  validation: Record<string, unknown>;
  currency_code: string | null;
  currency_symbol: string | null;
  editable: boolean;
  patch_field: `custom_columns.${string}` | null;
  edit_control: string | null;
  edit_policy: "custom_columns" | "formula" | "read_only";
  locked_reason: string | null;
}

export interface DealJobResponse {
  job_id: UUID | null;
  job_type: string;
  status: string;
}

export interface DealSummaryJobsStatus {
  deal_summary: DealJobResponse | null;
  stakeholder_processing: DealJobResponse[];
}

export interface DealListItem {
  deal_id: UUID;
  name: string | null;
  amount: number | null;
  predicted_amount: number | null;
  currency: string | null;
  currency_symbol: string | null;
  deal_status: string;
  probability: number | null;
  close_date: ISODate | null;
  expected_close_date: ISODate | null;
  stage_id: UUID | null;
  stage_name: string | null;
  stage_label: string | null;
  pipeline_id: UUID | null;
  pipeline_name: string | null;
  pipeline_label: string | null;
  owner_details: DealOwnerDetail[];
  company_id: UUID | null;
  company_name: string | null;
  company_domain: string | null;
  account_details: DealAccountDetail[];
  primary_contact_id: UUID | null;
  primary_contact_name: string | null;
  primary_contact_email: string | null;
  primary_contact_details: DealPrimaryContactDetail[];
  last_activity_at: ISODate | null;
  deal_team: DealTeamMember[];
  is_internal: boolean;
  created_at: ISODate;
  updated_at: ISODate;
  synced_at: ISODate;
}

export interface DealsPage {
  items: DealListItem[];
  page: number;
  limit: number;
  total_count: number;
  total_pages: number;
}

export interface DealSearchItem {
  deal_id: UUID;
  name: string | null;
  amount: number | null;
  predicted_amount: number | null;
  currency: string | null;
  currency_symbol: string | null;
  deal_status: string | null;
  stage_id: UUID | null;
  stage_name: string | null;
  pipeline_id: UUID | null;
  pipeline_name: string | null;
  company_id: UUID | null;
  company_name: string | null;
  company_domain: string | null;
  owner_details: DealOwnerDetail[];
  is_internal: boolean;
  created_at: ISODate;
  updated_at: ISODate;
}

export interface DealSearchPage {
  items: DealSearchItem[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface DealDetailFieldDefinition {
  key: string;
  label: string;
  data_type: string;
  order: number;
  section:
    | "core"
    | "value"
    | "ai"
    | "ownership"
    | "account"
    | "contact"
    | "timeline"
    | "process"
    | string;
  filter_control: string;
  editable: boolean;                  // True only when this field can be inline-edited on internal CRM deals
  patch_field: keyof DealUpdateRequest | null;
  edit_control: string | null;
  edit_policy:
    | "always"
    | "stage_action"
    | "owner_assignment"
    | "participants_action"
    | "account_action"
    | "primary_contact_action"
    | "generated"
    | "lifecycle_action"
    | "system"
    | "read_only";
  locked_reason: string | null;
}

export interface CRMFieldSchemaItem {
  field_id: UUID | null;
  entity: "deal" | string;
  key: string;
  label: string;
  type: string;
  order: number;
  section: string | null;
  source: "standard" | "internal" | "provider" | string;
  ownership: "system" | "internal" | string;
  integration_id: UUID | null;
  provider_name: string | null;
  provider_field_key: string | null;
  required: boolean;
  read_only: boolean;
  nullable: boolean;
  editable: boolean;
  sortable: boolean;
  filterable: boolean;
  searchable: boolean;
  input_control: string | null;
  options: Record<string, unknown>;
  validation: Record<string, unknown>;
  lifecycle_state: "active" | string;
  write_path: string | null;
  write_policy:
    | "always"
    | "custom_columns"
    | "formula"
    | "provider_read_only"
    | "stage_action"
    | "owner_assignment"
    | "participants_action"
    | "account_action"
    | "primary_contact_action"
    | "lifecycle_action"
    | "system"
    | "read_only"
    | string;
  read_only_reason: string | null;
  internal_write: boolean;
  internal_write_validation: {
    required: boolean;
    option_membership: boolean;
    [key: string]: boolean;
  };
}

export interface CRMEntitySchemaResponse {
  entity: "deal";
  fields: CRMFieldSchemaItem[];
}

export interface DealAccountInput {
  company_id?: UUID | null;
  name?: string | null;
  domain?: string | null;
  website?: string | null;
  linkedin_url?: string | null;
  industry?: string | null;
  country?: string | null;
}

export interface DealTeamMemberInput {
  user_id: UUID;
  role?: string | null;
}

export interface DealContactParticipantInput {
  contact_id: UUID;
}

export interface DealCreateRequest {
  name: string;
  amount?: number | null;
  currency?: string | null;
  pipeline_id?: UUID | null;
  stage_id?: UUID | null;
  owner_user_id?: UUID | null;
  source_created_at?: ISODate | null;
  expected_close_date?: ISODate | null;
  description?: string | null;
  deal_type?: string | null;
  priority?: string | null;
  lead_source?: string | null;
  probability?: number | null;
  forecast_category?: string | null;
  custom_columns?: Record<string, unknown>;
  custom_properties?: Record<string, unknown>; // Deprecated alias for custom_columns
  account?: DealAccountInput | null;
  team_members?: DealTeamMemberInput[] | null;
  contact_participants?: DealContactParticipantInput[] | null;
  primary_contact_name?: string | null;
  primary_contact_email?: string | null;
}

export interface DealUpdateRequest {
  name?: string;
  amount?: number | null;
  currency?: string | null;
  source_created_at?: ISODate | null;
  expected_close_date?: ISODate | null;
  description?: string | null;
  deal_type?: string | null;
  priority?: string | null;
  lead_source?: string | null;
  probability?: number | null;
  forecast_category?: string | null;
  custom_columns?: Record<string, unknown>;
  custom_properties?: Record<string, unknown>; // Deprecated alias for custom_columns
}

export interface DealPrimaryContactUpdateRequest {
  primary_contact_id?: UUID | null;
  primary_contact_name?: string | null;
  primary_contact_email?: string | null;
}

export interface DealCloseRequest {
  outcome: "won" | "lost";
  closed_reason?: string | null;
  close_date?: ISODate | null;
  stage_id?: UUID | null;
}

export interface DealReopenRequest {
  stage_id?: UUID | null;
  clear_closed_reason?: boolean;
}

export interface DealStageMoveRequest {
  stage_id: UUID;
}

export interface DealOwnerAssignRequest {
  owner_user_id: UUID | null;
}

export interface DealWriteResponse {
  deal_id: UUID;
  name: string | null;
  amount: number | null;
  predicted_amount: number | null;
  currency: string | null;
  currency_symbol: string | null;
  pipeline_id: UUID | null;
  pipeline_name: string | null;
  pipeline_label: string | null;
  stage_id: UUID | null;
  stage_name: string | null;
  stage_label: string | null;
  deal_status: string;
  probability: number | null;
  forecast_category: string | null;
  owner_user_id: UUID | null;
  owner_name: string | null;
  owner_email: string | null;
  owner_details: DealOwnerDetail[];
  is_internal: boolean;
  company_id: UUID | null;
  company_name: string | null;
  company_domain: string | null;
  company_industry: string | null;
  company_website: string | null;
  company_linkedin_url: string | null;
  company_country: string | null;
  primary_contact_id: UUID | null;
  primary_contact_name: string | null;
  primary_contact_email: string | null;
  primary_contact_details: DealPrimaryContactDetail[];
  source_created_at: ISODate | null;
  close_date: ISODate | null;
  expected_close_date: ISODate | null;
  closed_won_at: ISODate | null;
  closed_lost_at: ISODate | null;
  closed_reason: string | null;
  stage_entered_at: ISODate | null;
  stage_updated_at: ISODate | null;
  deal_type: string | null;
  priority: string | null;
  lead_source: string | null;
  description: string | null;
  created_at: ISODate;
  updated_at: ISODate;
  synced_at: ISODate | null;
}

export interface DealDetail {
  detail_field_definitions: DealDetailFieldDefinition[];
  deal_id: UUID;
  name: string | null;
  amount: number | null;
  currency: string | null;
  currency_symbol: string | null;
  predicted_amount: number | null;
  deal_status: string;
  probability: number | null;
  forecast_category: string | null;
  stage_id: UUID | null;
  stage_name: string | null;
  stage_label: string | null;
  pipeline_id: UUID | null;
  pipeline_name: string | null;
  pipeline_label: string | null;
  source_created_at: ISODate | null;
  close_date: ISODate | null;
  expected_close_date: ISODate | null;
  closed_won_at: ISODate | null;
  closed_lost_at: ISODate | null;
  last_activity_at: ISODate | null;
  stage_entered_at: ISODate | null;
  stage_updated_at: ISODate | null;
  deal_age_days: number | null;
  owner_details: DealOwnerDetail[];
  deal_team: DealTeamMember[];
  company_id: UUID | null;
  company_name: string | null;
  company_domain: string | null;
  company_industry: string | null;
  company_website: string | null;
  company_linkedin_url: string | null;
  company_country: string | null;
  primary_contact_id: UUID | null;
  primary_contact_name: string | null;
  primary_contact_email: string | null;
  primary_contact_details: DealPrimaryContactDetail[];
  deal_type: string | null;
  priority: string | null;
  lead_source: string | null;
  closed_reason: string | null;
  crm_url: string | null;
  description: string | null;
  deal_health: string | null;
  deal_health_reason: string | null;
  is_internal: boolean;
  custom_columns: Record<string, unknown> | null;
  custom_column_definitions: DealCustomColumnDefinition[] | null;
  time_tracked: string | null;
  source_modified_at: ISODate | null;
  last_seen_at: ISODate | null;
  associated_contact_count: number | null;
  associated_company_count: number | null;
  note_count: number | null;
  deal_summary_data: Record<string, unknown> | null;
  active_deal_jobs: DealSummaryJobsStatus | null;
  created_at: ISODate;
  updated_at: ISODate;
  synced_at: ISODate;
}
```

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
| `pipeline_id` | UUID | No | Valid usable internal pipeline UUID | Defaults to default pipeline |
| `stage_id` | UUID | No | Must belong to the usable pipeline | Defaults to first stage |
| `owner_user_id` | UUID | No | Valid user UUID from `/users/crm/users` | Deal owner |
| `amount` | number | No | >= 0 | Deal value |
| `currency` | string | No | 3-char ISO code (USD, INR) | Currency code |
| `source_created_at` | string | No | ISO 8601 datetime | Deal start / CRM-created date. Defaults to current timestamp when omitted |
| `expected_close_date` | string | No | ISO 8601 datetime | Forecasted close date |
| `deal_type` | string | No | Max 128 chars | "New Business", "Renewal", "Expansion", "Upsell" |
| `priority` | string | No | Max 64 chars | "High", "Medium", "Low" |
| `lead_source` | string | No | Max 128 chars | "Inbound", "Outbound", "Referral", etc. |
| `probability` | number | No | 0-100 | Win probability (%) |
| `forecast_category` | string | No | Max 64 chars | Forecast bucket (Pipeline / Best Case / Commit / Closed) |
| `custom_columns` | object | Required when active internal custom fields are marked `required`; otherwise optional | non-empty object; keys must exist in active internal custom field definitions; required fields must be present; select/multiselect values must match active configured option values; omit instead of sending `null` or `{}` when no custom values are being set | Public custom field write payload keyed by custom field key. Use the exact key from custom field metadata; `custom_` prefix stripping is only a compatibility fallback |
| `custom_properties` | object | No | same as `custom_columns`; cannot conflict when both are sent | Deprecated alias for `custom_columns` kept for older clients |
| `description` | string | No | - | Deal notes/description |
| `account` | object | No | - | Optional account details captured at deal creation |
| `account.company_id` | UUID | No | Existing internal CRM company UUID | Link an existing company instead of creating from account details |
| `account.name` | string | No | - | Company/account name (optional if domain provided) |
| `account.domain` | string | No | - | Company domain (required to create/link company) |
| `account.website` | string | No | - | Company website URL (optional) |
| `account.industry` | string | No | - | Company industry (optional) |
| `account.country` | string | No | - | Company country/geography (optional) |
| `account.linkedin_url` | string | No | - | Company LinkedIn URL (optional) |
| `team_members` | array | No | - | Internal team members to attach as participants |
| `team_members[].user_id` | UUID | Yes | Valid user UUID from `/users/crm/users` | Internal team member |
| `team_members[].role` | string | No | Max 128 chars | Team role label (e.g., "SE", "CSM") |
| `contact_participants` | array | No | - | Existing CRM contacts to attach as deal stakeholders |
| `contact_participants[].contact_id` | UUID | Yes | Existing CRM contact UUID | Contact to add as a stakeholder |
| `primary_contact_name` | string | No | - | Primary contact display name snapshot |
| `primary_contact_email` | string | No | Valid email string | Primary contact email snapshot |

### Example Request

```json
{
  "name": "Acme Corp - Enterprise License",
  "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
  "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "amount": 50000,
  "currency": "USD",
  "source_created_at": "2026-01-22T10:00:00Z",
  "expected_close_date": "2026-03-31T00:00:00Z",
  "deal_type": "New Business",
  "priority": "High",
  "lead_source": "Inbound",
  "probability": 10,
  "forecast_category": "Pipeline",
  "custom_columns": {
    "our_revenue_inr": 25000000
  },
  "description": "Enterprise deal for 100 seats",
  "account": {
    "name": "Acme Corp",
    "domain": "acme.com",
    "linkedin_url": "https://www.linkedin.com/company/acme",
    "industry": "Manufacturing",
    "country": "United States"
  },
  "team_members": [
    {
      "user_id": "6d20ab77-5ab4-4c53-9c2f-0500c1c052cd",
      "role": "SE"
    }
  ],
  "contact_participants": [
    {
      "contact_id": "8b7f5f3b-0e3e-4a5c-a7e5-5f31cb0a1e13"
    }
  ],
  "primary_contact_name": "Jane Buyer",
  "primary_contact_email": "jane@acme.com"
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
- If `account.company_id` is provided, the deal is linked to that existing company. If the
  selected company came from an external provider, the backend copies it into the internal
  CRM integration before linking it to the deal.
- If `account.domain` is missing but `account.name` is provided, the deal will store
  `company_name` without creating a company record.
- `team_members` are added as internal CRM participants (`participant_type = user`).
- `contact_participants` are existing CRM contacts added as stakeholders after the deal
  is created. The create route owns this follow-up so each accepted participant can
  trigger stakeholder processing jobs and auto-research exactly once. If a selected
  contact came from an external CRM integration, it is copied into the internal CRM
  integration before the stakeholder participant is created.
- Contact participant duplicate checks use normalized email identity (trimmed lowercase)
  before creating or copying contact records. If a selected contact resolves to an
  email already active on the deal, it is returned in `skipped` instead of creating
  another stakeholder or processing job.
- `primary_contact_name` and `primary_contact_email` set the deal's primary-contact
  snapshot during creation. For later changes, use
  `PATCH /users/crm/deals/{deal_id}/primary-contact`.
- `custom_columns` follows the same validation as update: unknown keys and formula
  custom fields are rejected, and values are type-checked against active internal deal
  custom field definitions before the deal is created. Active internal custom fields
  marked `required` must be present on create. `select` values must match one active
  configured option value; `multiselect` values must all match active configured
  option values. Omit `custom_columns` when not setting custom fields; explicit `null`
  and `{}` are rejected. `custom_properties` is still accepted as a deprecated alias,
  but the two fields must match if both are sent. Keys must normalize to a non-empty
  custom field key. Keys are normalized before validation, so duplicate normalized
  keys such as `our_revenue` and `custom_our_revenue` are rejected.
- Create forms and agent/tool create flows should fetch
  `GET /users/crm/schema?entity=deal` before submit. Deal detail still returns
  `custom_column_definitions` for an existing deal, but create flows should use
  the schema endpoint because no deal ID exists yet.
- The selected pipeline must be usable for internal deals: at least one `open`,
  one `won`, and one `lost` stage. Draft/incomplete pipelines return `409`
  instead of accepting a deal into a broken sales process.

### Success Response (201)

`data` uses the full `DealWriteResponse` contract above.

```json
{
  "status": "SUCCESS",
  "message": "Deal created successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "amount": 50000,
    "predicted_amount": 5000,
    "currency": "USD",
    "currency_symbol": "$",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "pipeline_name": "Default Pipeline",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "stage_name": "Qualification",
    "deal_status": "open",
    "probability": 10,
    "forecast_category": "Pipeline",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "owner_name": "John Doe",
    "owner_email": "john@example.com",
    "owner_details": [
      {
        "owner_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "name": "John Doe",
        "email": "john@example.com"
      }
    ],
    "is_internal": true,
    "source_created_at": "2026-01-22T10:00:00Z",
    "close_date": null,
    "expected_close_date": "2026-03-31T00:00:00Z",
    "closed_won_at": null,
    "closed_lost_at": null,
    "closed_reason": null,
    "stage_entered_at": "2026-01-22T10:00:00Z",
    "stage_updated_at": "2026-01-22T10:00:00Z",
    "deal_type": "New Business",
    "priority": "High",
    "lead_source": "Inbound",
    "description": "Enterprise deal for 100 seats",
    "company_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "company_industry": "Manufacturing",
    "company_website": null,
    "company_linkedin_url": "https://www.linkedin.com/company/acme",
    "company_country": "United States",
    "primary_contact_id": null,
    "primary_contact_name": null,
    "primary_contact_email": null,
    "primary_contact_details": [],
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T10:00:00Z",
    "synced_at": "2026-01-22T10:00:00Z"
  }
}
```

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 400 | Validation error | Missing required field or invalid value |
| 400 | Missing required custom fields | One or more active internal required custom columns were omitted from `custom_columns` |
| 400 | Invalid option for custom field | A `select`/`multiselect` custom value was not in the active configured `options.values` list |
| 404 | Pipeline/Stage not found | Invalid pipeline_id or stage_id |
| 409 | Internal CRM not configured | Org doesn't have internal CRM setup |
| 409 | Unusable pipeline | Pipeline is missing at least one `open`, `won`, or `lost` stage |
| 422 | Validation error | Field validation failed or request included unsupported fields |
| 500 | Internal server error | Unexpected error |

---

## 2. Search Deals

### Endpoint

```
GET /users/crm/deals/search
```

Lightweight cursor search for deal pickers and typeahead. This endpoint searches only deal identity fields: `name` and `company_name`. It does not return `total_count`; use `next_cursor` and `has_more` for infinite scroll. Cursor ordering uses a trimmed lowercase deal name, or trimmed lowercase company name when the deal name is blank.

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | - | Case-insensitive search by deal name or company name |
| `limit` | integer | 20 | Items per cursor page (1-50) |
| `cursor` | string | - | Opaque cursor returned by the previous response |

### Success Response (200)

`data` uses the `DealSearchPage` contract above.

```json
{
  "status": "SUCCESS",
  "message": "Deals search results retrieved",
  "data": {
    "items": [
      {
        "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
        "name": "Acme Corp - Enterprise License",
        "amount": 50000,
        "predicted_amount": 5000,
        "currency": "USD",
        "currency_symbol": "$",
        "deal_status": "open",
        "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "stage_name": "Qualification",
        "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
        "pipeline_name": "Default Pipeline",
        "company_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
        "company_name": "Acme Corp",
        "company_domain": "acme.com",
        "owner_details": [
          {
            "owner_id": "f2c8a7ce-1c0e-4b0b-8c07-1a9b6b8b0c1d",
            "name": "John Doe",
            "email": "john@example.com"
          }
        ],
        "is_internal": true,
        "created_at": "2026-01-22T10:00:00Z",
        "updated_at": "2026-01-22T10:00:00Z"
      }
    ],
    "next_cursor": null,
    "has_more": false
  }
}
```

---

## 3. List Deals

### Endpoint

```
GET /users/crm/deals
```

### Frontend Default Columns

The backend returns the default deal-list column order in
`user_preferences.crm.deals.list.columns` from login and `/users/auth/me`.
Use that preference list to decide which table columns to render and in what
order. The current backend default is:

```json
[
  "name",
  "company_name",
  "stage",
  "amount",
  "expected_close_date",
  "owner",
  "primary_contact_name",
  "last_activity_at"
]
```

These column IDs map to `DealListItem` fields as follows:

| Column ID | Payload Path | Notes |
|-----------|--------------|-------|
| `name` | `item.name` | Deal name. Link to deal detail. |
| `company_name` | `item.company_name` | Associated account/company. |
| `stage` | `item.stage_label ?? item.stage_name` | Use labels when available. |
| `amount` | `item.amount`, `item.currency`, `item.currency_symbol` | Currency-decorated value. |
| `expected_close_date` | `item.expected_close_date` | Forecasted close date. |
| `owner` | `item.owner_details[0]?.name` | Owner list is usually 0..1 item. |
| `primary_contact_name` | `item.primary_contact_name` | Primary contact display snapshot. |
| `last_activity_at` | `item.last_activity_at` | Last known deal activity timestamp. |

`probability` and `forecast_category` are still returned on list rows for
compatibility but are not default table columns. `deal_health` and
`deal_health_reason` are not part of the list response; they remain detail-only
AI context from `GET /users/crm/deals/{deal_id}`.

### Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 40 | Items per page (1-100) |
| `page` | integer | 1 | Page number (1-indexed) |
| `statuses` | array[enum] | - | Filter by deal statuses (`open`, `won`, `lost`, `closed`) |
| `pipeline_ids` | array[uuid] | - | Filter by **pipeline_id** values from `/users/crm/pipelines` |
| `owner_ids` | array[uuid] | - | Filter by internal owner IDs from `/users/crm/deals/owners` (not page-scoped) |
| `min_amount` | number | - | Minimum deal amount |
| `max_amount` | number | - | Maximum deal amount |
| `min_close_date` | string | - | Minimum actual close date (ISO 8601). Closed deals only, because open deals have `close_date = null`. |
| `max_close_date` | string | - | Maximum actual close date (ISO 8601). Closed deals only, because open deals have `close_date = null`. |
| `min_expected_close_date` | string | - | Minimum expected/forecast close date (ISO 8601). Use this for open-pipeline forecast filtering. |
| `max_expected_close_date` | string | - | Maximum expected/forecast close date (ISO 8601). Use this for open-pipeline forecast filtering. |
| `q` | string | - | Case-insensitive search by deal name or company name |

### Success Response (200)

`data` uses the `DealsPage` contract above.

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
        "predicted_amount": 5000,
        "currency": "USD",
        "currency_symbol": "$",
        "deal_status": "open",
        "probability": 10,
        "close_date": "2026-03-15T00:00:00Z",
        "expected_close_date": "2026-03-31T00:00:00Z",
        "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "stage_name": "Qualification",
        "stage_label": "Qualification",
        "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
        "pipeline_name": "Default Pipeline",
        "pipeline_label": "Default Pipeline",
        "owner_details": [
          {
            "owner_id": "f2c8a7ce-1c0e-4b0b-8c07-1a9b6b8b0c1d",
            "name": "John Doe",
            "email": "john@example.com"
          }
        ],
        "company_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
        "company_name": "Acme Corp",
        "company_domain": "acme.com",
        "primary_contact_id": "44ef8f41-c980-4b12-987e-3b7fd7ed8c3e",
        "primary_contact_name": "Jane Buyer",
        "primary_contact_email": "jane@acme.com",
        "primary_contact_details": [
          {
            "contact_id": "44ef8f41-c980-4b12-987e-3b7fd7ed8c3e",
            "name": "Jane Buyer",
            "email": "jane@acme.com",
            "phone_number": null
          }
        ],
        "last_activity_at": "2026-01-23T08:30:00Z",
        "deal_team": [],
        "is_internal": true,
        "created_at": "2026-01-22T10:00:00Z",
        "updated_at": "2026-01-22T12:00:00Z",
        "synced_at": "2026-01-23T09:30:00Z"
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
| `stage_id` / `pipeline_id` | UUID \| null | Stable identifiers for the current stage and pipeline. Use these for preselecting filters and stage actions. |
| `stage_label` / `pipeline_label` | string \| null | Display labels for the current stage and pipeline. |
| `company_id` / `company_name` / `company_domain` | UUID/string \| null | Linked account identifiers shown in the list row and searchable by company name. |
| `primary_contact_id` / `primary_contact_name` / `primary_contact_email` | UUID/string \| null | Primary contact snapshot shown in the list row. |
| `primary_contact_details` | array | Primary contact details as a list (typically 0..1). Use `contact_id` for linking. `phone_number` is included when hydrated; list rows may return `null` for phone if the row was not contact-hydrated. |
| `last_activity_at` | datetime \| null | Most recent activity timestamp associated with the deal. |
| `deal_team` | array | Internal deal team members attached as participants. |
| `currency_symbol` | string \| null | Symbol derived from `currency` (for example `$`, `₹`). Frontend should use this for icon/amount decoration. |
| `updated_at` / `synced_at` | datetime | Row freshness and provider sync timestamps. |

---

## 4. Get Deal Detail

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

`data` uses the `DealDetail` contract above.

```json
{
  "status": "SUCCESS",
  "message": "Deal retrieved successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "amount": 50000,
    "currency": "USD",
    "currency_symbol": "$",
    "predicted_amount": 5000,
    "probability": 10,
    "forecast_category": "Commit",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "pipeline_name": "Default Pipeline",
    "pipeline_label": "Default Pipeline",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "stage_name": "Qualification",
    "stage_label": "Qualification",
    "deal_status": "open",
    "owner_details": [
      {
        "owner_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "name": "John Doe",
        "email": "john@example.com"
      }
    ],
    "deal_team": [],
    "company_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "company_industry": "Manufacturing",
    "company_website": "https://acme.com",
    "company_linkedin_url": "https://www.linkedin.com/company/acme",
    "company_country": "United States",
    "primary_contact_id": "44ef8f41-c980-4b12-987e-3b7fd7ed8c3e",
    "primary_contact_name": "Jane Buyer",
    "primary_contact_email": "jane@acme.com",
    "primary_contact_details": [
      {
        "contact_id": "44ef8f41-c980-4b12-987e-3b7fd7ed8c3e",
        "name": "Jane Buyer",
        "email": "jane@acme.com",
        "phone_number": "+15551234567"
      }
    ],
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
        "currency_symbol": "₹",
        "editable": true,
        "patch_field": "custom_columns.our_revenue_inr",
        "edit_control": "currency",
        "edit_policy": "custom_columns",
        "locked_reason": null
      },
      {
        "key": "target_go_live_date",
        "label": "Target Go Live Date",
        "data_type": "datetime",
        "required": false,
        "options": {},
        "validation": {},
        "currency_code": null,
        "currency_symbol": null,
        "editable": true,
        "patch_field": "custom_columns.target_go_live_date",
        "edit_control": "datetime",
        "edit_policy": "custom_columns",
        "locked_reason": null
      }
    ],
    "detail_field_definitions": [
      {
        "key": "name",
        "label": "Deal name",
        "data_type": "string",
        "order": 1,
        "section": "core",
        "filter_control": "text",
        "editable": true,
        "patch_field": "name",
        "edit_control": "text",
        "edit_policy": "always",
        "locked_reason": null
      },
      {
        "key": "owner_details",
        "label": "Owner",
        "data_type": "array",
        "order": 12,
        "section": "ownership",
        "filter_control": "people",
        "editable": false,
        "patch_field": null,
        "edit_control": null,
        "edit_policy": "owner_assignment",
        "locked_reason": "Use the deal owner assignment action."
      }
    ],
    "source_modified_at": "2026-01-23T09:30:00Z",
    "last_seen_at": "2026-01-23T09:30:00Z",
    "synced_at": "2026-01-23T09:30:00Z",
    "associated_contact_count": 4,
    "associated_company_count": 1,
    "note_count": 2,
    "deal_summary_data": null,
    "active_deal_jobs": {
      "deal_summary": null,
      "stakeholder_processing": [
        {
          "job_id": "54d810af-6ea7-41c0-b6b9-d74c21b94e31",
          "job_type": "deal_email_rematch",
          "status": "in_progress"
        },
        {
          "job_id": "0df77c72-ff0a-4cc5-80f0-ded2a84690af",
          "job_type": "deal_calendar_rematch",
          "status": "pending"
        },
        {
          "job_id": "6f1145c6-2b30-43cf-a6cd-3c147e88e58b",
          "job_type": "stakeholder_sync",
          "status": "pending"
        }
      ]
    },
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
| `custom_column_definitions` | array \| null | Active custom field metadata for internal deals. Use `data_type` to render controls; for `currency` fields use `currency_code` + `currency_symbol`. Use `editable`, `patch_field`, `edit_control`, and `edit_policy` to drive custom-field editing. |
| `detail_field_definitions` | array | Ordered metadata for predefined Get Details fields. Use this as the source of truth for display order and inline editability. Custom fields are described by `custom_column_definitions`. |
| `owner_details` | array | Canonical owner payload. Use this list (0..1 for internal CRM) instead of legacy single-owner fields. |
| `currency_symbol` | string \| null | Symbol derived from `currency` (for example `$`, `₹`) for consistent detail rendering. |
| `company_industry` / `company_website` / `company_linkedin_url` / `company_country` | string \| null | Account profile values populated from the linked CRM company record. These are not `CRMDeal` columns. |
| `associated_contact_count` / `associated_company_count` / `note_count` | number \| null | Top-level relationship/provider metrics. Display near relationship sections; do not duplicate as Get Details scalar rows. |
| `source_modified_at` / `last_seen_at` / `synced_at` | datetime | System read-only sync metadata for advanced view and troubleshooting. |
| `active_deal_jobs` | object \| null | Active deal-level jobs for reconnect. `stakeholder_processing` contains active `deal_email_rematch`, `deal_calendar_rematch`, and `stakeholder_sync` jobs created by stakeholder add. |

`active_deal_jobs.stakeholder_processing` is empty or omitted when no active
post-stakeholder-create processing is running. Use it to restore a deal-level
banner after a page reload. Stakeholder research is not included here; it is
returned on stakeholder detail as `active_research_job`.

### Deal Detail Metadata

- `detail_field_definitions` is the ordered metadata for predefined deal fields in the "Get Details" panel.
- `detail_field_definitions[].editable: true` means the field is accepted by `PATCH /users/crm/deals/{deal_id}` using `patch_field` when `is_internal` is also `true`. External CRM deals stay read-only.
- Owner, stage/status, account, primary-contact, lifecycle, participant, generated, and system fields use `edit_policy` to point the frontend to the correct dedicated action or explain why the value is locked.
- `custom_columns` are intentionally not repeated in `detail_field_definitions`; render individual custom field controls from `custom_column_definitions`.
- `custom_column_definitions[].editable: true` means patch `custom_columns` using the returned `patch_field` path, for example `custom_columns.our_revenue_inr`.
- Formula custom fields return `edit_policy: "formula"` and are read-only; the write endpoint rejects formula updates.
- `deal_summary_data` and `active_deal_jobs` are intentionally not metadata rows because they are structured sections, not scalar Get Details fields.

Edit policy meanings:

| `edit_policy` | Meaning | Endpoint |
|---|---|---|
| `always` | Inline edit is allowed for internal CRM deals. | `PATCH /users/crm/deals/{deal_id}` |
| `stage_action` | Stage/status changes must move through pipeline stage logic. | `POST /users/crm/deals/{deal_id}/stage` |
| `owner_assignment` | Owner changes affect routing/audit and must validate a CRM user. | `POST /users/crm/deals/{deal_id}/owner` |
| `participants_action` | Internal team changes are participant mutations, not scalar deal edits. | Participant APIs |
| `account_action` | Account fields are snapshots from the linked account/company. | `PATCH /users/crm/deals/{deal_id}/account` |
| `primary_contact_action` | Primary contact fields update the contact relationship snapshot. | `PATCH /users/crm/deals/{deal_id}/primary-contact` |
| `lifecycle_action` | Closed/won/lost fields must use close/reopen flows. | `POST /users/crm/deals/{deal_id}/close` or `/reopen` |
| `generated` | Generated from deal analysis. | Read-only |
| `system` | Computed or provider/system-managed value. | Read-only |
| `read_only` | Returned for display only. | Read-only |

The predefined field metadata currently contains these keys in order.
Top-level count metrics such as `associated_contact_count`,
`associated_company_count`, and `note_count` remain response fields but are not
repeated as Get Details metadata rows:

`name`, `deal_status`, `stage_name`, `pipeline_name`, `amount`, `currency`, `predicted_amount`, `probability`, `forecast_category`, `deal_health`, `deal_health_reason`, `owner_details`, `deal_team`, `company_name`, `company_domain`, `company_industry`, `company_website`, `company_linkedin_url`, `company_country`, `primary_contact_name`, `primary_contact_email`, `source_created_at`, `close_date`, `expected_close_date`, `closed_won_at`, `closed_lost_at`, `deal_age_days`, `last_activity_at`, `stage_entered_at`, `stage_updated_at`, `deal_type`, `priority`, `lead_source`, `closed_reason`, `crm_url`, `description`.

---

## 4.1 Get Deal Activity Feed

Cursor-paginated CRM activity cards linked to one visible deal.

### Endpoint

```http
GET /users/crm/deals/{deal_id}/activity-feed
```

### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | Deal whose linked activity cards should be returned |

### Authorization

The authenticated user must be allowed to view the deal under the normal deal
relationship policy (admin, owner, creator, or active participant). A missing,
deleted, or inaccessible deal returns `404` so the endpoint does not disclose
whether another user's deal exists.

### Query Parameters

| Parameter | Type | Default | Validation | Description |
|-----------|------|---------|------------|-------------|
| `limit` | integer | `20` | `1-100` | Maximum feed items per page |
| `cursor` | string | - | Opaque | Cursor returned by the previous response |
| `activity_types` | string | - | Comma-separated | Projected activity types to include, for example `EMAIL,MEETING,TASK` |
| `source_types` | string | - | Comma-separated | Source kinds to include, for example `EMAIL,MEETING,STAGE_CHANGE` |
| `start_date` | datetime | - | ISO 8601 with timezone | Include activities at or after this occurrence time |
| `end_date` | datetime | - | ISO 8601 with timezone; must be at or after `start_date` | Include activities at or before this occurrence time |
| `sort` | `asc` or `desc` | `desc` | Enum | Sort by linked activity occurrence time |

### Ordering and Timestamp Contract

- Each activity appears once even when it has multiple link roles to the deal.
- `occurred_at`, not `created_at` or `updated_at`, controls filtering, sorting,
  and cursor pagination.
- `occurred_at` normally represents the source event time. When the source has no
  usable event timestamp, it falls back to the association or projection time.
- A past meeting uses its actual `start_at`. A future meeting uses its association
  time for feed ordering; its scheduled time remains in `display_payload.start_at`.
- Email cards prefer association email date, then received/sent time. Notes prefer
  provider-created time. Task cards prefer due time/date.

### Navigation Contract

- `source_type` and `source_record_id` identify the canonical local email,
  calendar event, task, or note row used for detail hydration.
- `source_url` is the provider URL when one is available.
- `related_objects` groups CRM targets by `object_type` and `object_id`, with all
  applicable `link_roles`, so the frontend can open related deal, contact, and
  account pages.
- Association-backed email and meeting cards include `association_id` in
  `display_payload`.

### Deal-Created Card Contract

A deal creation is returned as one activity card with `source_type` set to
`DEAL_CREATED`. It records the stage and pipeline in which the deal was created;
it is not a stage-transition event.

The backend provides a display-ready `title`. The frontend should render it
verbatim instead of rebuilding it from `display_payload`:

| Available values | `title` format |
|---|---|
| `stage_name` and `deal_name` | `Deal created in {stage_name}: {deal_name}` |
| `stage_name` only | `Deal created in {stage_name}` |
| `deal_name` only | `Deal created: {deal_name}` |
| Neither | `Deal created` |

The creation card includes this snapshot in `display_payload`:

| Field | Type | Description |
|---|---|---|
| `deal_name` | `string \| null` | Deal name at projection time. |
| `pipeline_id` | `UUID string \| null` | Local ID of the pipeline in which the deal was created. |
| `pipeline_name` | `string \| null` | Display name of the creation pipeline. |
| `stage_id` | `UUID string \| null` | Local ID of the stage in which the deal was created. |
| `stage_name` | `string \| null` | Display name of the creation stage. |

Example creation card fields:

```json
{
  "activity_type": "EVENT",
  "source_type": "DEAL_CREATED",
  "title": "Deal created in Discovery: Acme renewal",
  "display_payload": {
    "deal_name": "Acme renewal",
    "pipeline_id": "04e49288-e72d-49de-bca1-3261c14f5af5",
    "pipeline_name": "Sales",
    "stage_id": "fd67a4ea-a511-4f90-9bd7-6a0a32ebfb25",
    "stage_name": "Discovery"
  }
}
```

Frontend behavior:

- Show this card with the **Created** label and the supplied `title`.
- Use the stage and pipeline fields for secondary UI such as chips, details, or
  navigation.
- Do not label `DEAL_CREATED` as **Stage changed** or infer a stage transition
  from this payload.
- Do not use the deal's current stage to describe its creation stage; the card's
  `display_payload` is the creation snapshot.
- A genuine later stage transition is a separate `STAGE_CHANGE` activity.

### Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Activity feed retrieved successfully",
  "data": {
    "object_type": "DEAL",
    "object_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "items": [
      {
        "activity_id": "9e41b693-a053-4ed4-bf4a-46e01251e118",
        "activity_type": "MEETING",
        "source_type": "MEETING",
        "source_record_id": "287ca162-2096-4e40-bca3-023291900904",
        "source_provider": "GOOGLE_CALENDAR",
        "source_integration_id": "70e0f73a-74ca-4084-ae20-d6c64d33362f",
        "source_url": "https://calendar.google.com/calendar/event?eid=example",
        "title": "Acme discovery call",
        "summary": "Discovery call with the buying committee",
        "preview": "Discovery call with the buying committee",
        "occurred_at": "2026-07-19T09:30:00Z",
        "actor_name": "Jane Seller",
        "actor_email": "jane@example.com",
        "display_payload": {
          "association_id": "9e41b693-a053-4ed4-bf4a-46e01251e118",
          "start_at": "2026-07-19T09:30:00Z",
          "end_at": "2026-07-19T10:00:00Z",
          "participants_preview": [],
          "participant_count": 4,
          "duration_minutes": 30,
          "meeting_status": "confirmed"
        },
        "link_role": "PRIMARY",
        "link_source": "SYSTEM",
        "confidence_score": null,
        "related_objects": [
          {
            "object_type": "DEAL",
            "object_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
            "link_roles": ["PRIMARY"]
          }
        ],
        "created_at": "2026-07-19T10:01:00Z",
        "updated_at": "2026-07-19T10:01:00Z"
      }
    ],
    "total_count": 1,
    "cursor": null
  }
}
```

`cursor` is `null` when no additional page exists. Pass a non-null returned
cursor unchanged in the next request.

### Error Responses

| HTTP Status | `message` | When |
|-------------|-----------|------|
| `400` | Validation message | Invalid cursor, timezone-free date, or reversed date range |
| `404` | `"Deal not found"` | Deal is missing, deleted, or not visible to the authenticated user |
| `422` | FastAPI validation response | Invalid UUID, limit, datetime, sort value, or query value shape |
| `429` | `"Too many requests ..."` | User read rate limit exceeded |
| `500` | `"An unexpected error occurred while processing your request"` | Unhandled server error |

---

## 4.2 Generic Deal Job Stream

Subscribe to any user-visible deal job returned by deal or stakeholder APIs.
This stream is used for stakeholder add processing jobs and stakeholder research
jobs returned from add/re-add or manual research triggers.

### Endpoint

```http
GET /users/crm/deals/{deal_id}/jobs/{job_id}/stream
```

### Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | Deal that owns the job |
| `job_id` | UUID | Yes | Job returned by a mutation or active job recovery response |

### Authorization

The job must belong to the authenticated user and to the requested `deal_id`.
If the user owns the job but requests it through a different deal URL, the API
returns `403`. If the job does not exist or cannot be loaded, the API returns
`404` instead of opening an idle stream.

### Event Types

| Event | Description |
|-------|-------------|
| `ready` | Stream connected and includes the `job_id` |
| `progress.update` | Job stage/progress update |
| `answer.replace` | Optional final result payload replayed for completed jobs |
| `finalize` | Terminal job frame; `reason` is `done` or `error` |
| `complete` | Job finished; close the stream and refresh affected data |
| `timeout` | Stream timed out; reconnect if the job is still active |
| `error` | Stream error; reconnect if appropriate |

Use `Last-Event-ID` on reconnect when the client has a previous stream event id.

---

## 5. Update Deal

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
Unknown fields are rejected with `422`; do not send lifecycle, owner, stage, account, primary-contact, generated, or system fields to this endpoint.
`name` must be a non-empty string when provided; omit it to keep the existing name.

| Field | Type | Validation | Description |
|-------|------|------------|-------------|
| `name` | string | 1-500 chars, non-empty | Deal name |
| `amount` | number | >= 0 | Deal value (can be null) |
| `currency` | string | 3-char ISO code (USD, INR) | Currency code |
| `source_created_at` | string | ISO 8601 datetime | Deal start / CRM created date |
| `expected_close_date` | string | ISO 8601 datetime | Forecasted close date |
| `deal_type` | string | Max 128 chars | "New Business", "Renewal", "Expansion", "Upsell" |
| `priority` | string | Max 64 chars | "High", "Medium", "Low" |
| `lead_source` | string | Max 128 chars | "Inbound", "Outbound", "Referral", etc. |
| `description` | string | - | Deal notes/description |
| `probability` | number | 0-100 | Win probability (%) |
| `forecast_category` | string | Max 64 chars | Forecast bucket (Pipeline / Best Case / Commit / Closed) |
| `custom_columns` | object | non-empty object; keys must exist in custom field definitions; omit instead of sending `null` or `{}` | Public custom field write payload keyed by custom field key. Use the exact key from `custom_column_definitions[].key` / `patch_field`; `custom_` prefix stripping is only a compatibility fallback. Send a field value as `null` to clear that stored custom value. |
| `custom_properties` | object | same as `custom_columns`; cannot conflict when both are sent | Deprecated alias for `custom_columns` kept for older clients |

**Note**: To update `pipeline_id`, `stage_id`, `owner_user_id`, or actual `close_date`, use the dedicated endpoints below.
`last_activity_at` and `crm_url` are read-only and returned by the deal detail API.
Use `POST /users/crm/deals/{deal_id}/close` to set close outcome and `closed_reason`.

**Custom field validation**:
- Unknown custom keys are rejected (`400`).
- Formula custom fields are read-only in this endpoint.
- Values are type-checked against field definitions.
- Keys must resolve to a non-empty custom field key. The backend first matches
  the exact active definition key, then falls back to the legacy prefix-stripped
  form for old clients.
- Duplicate normalized keys such as `our_revenue` and `custom_our_revenue` are
  rejected to avoid ambiguous writes.
- Omit `custom_columns` when not updating custom fields; explicit `null` and `{}` are rejected.
- Send `custom_columns.<key>: null` to clear a stored custom field value. Detail responses still show active custom keys with `null` when `include_empty_custom_columns=true`.
- `custom_properties` is still accepted as a deprecated alias, but the two fields must match if both are sent.
- Supported custom field types include `text`, `number`, `integer`, `boolean`, `date`, `datetime`, `currency`, and `percentage` (plus select/multiselect/formula where applicable).
- Numeric custom fields (`integer`, `number`, `currency`, `percentage`) reject JSON booleans; send numeric values or numeric strings instead.
- `integer` custom fields reject fractional values.
- `percentage` custom fields must resolve to a number from `0` through `100`.
- `boolean` custom fields accept JSON booleans, standard true/false strings (`true`, `false`, `yes`, `no`, `y`, `n`), and numeric `0` or `1`; other numeric values are rejected.

### Example Request

Update only amount and expected close date:
```json
{
  "amount": 75000,
  "expected_close_date": "2026-04-15T00:00:00Z"
}
```

Update all editable fields:
```json
{
  "name": "Acme Corp - Enterprise Plus",
  "amount": 75000,
  "currency": "USD",
  "source_created_at": "2026-01-22T10:00:00Z",
  "expected_close_date": "2026-04-30T00:00:00Z",
  "deal_type": "Expansion",
  "priority": "High",
  "lead_source": "Inbound",
  "probability": 65,
  "forecast_category": "Commit",
  "description": "Upgraded to enterprise plus tier with 200 seats"
}
```

### Success Response (200)

`data` uses the full `DealWriteResponse` contract above. The example below shows
the commonly inspected updated fields; nullable account/contact snapshot fields
are still present as defined by `DealWriteResponse`.

```json
{
  "status": "SUCCESS",
  "message": "Deal updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise Plus",
    "amount": 75000,
    "predicted_amount": 48750,
    "currency": "USD",
    "currency_symbol": "$",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "pipeline_name": "Default Pipeline",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "stage_name": "Qualification",
    "deal_status": "open",
    "probability": 65,
    "forecast_category": "Commit",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "owner_name": "John Doe",
    "owner_email": "john@example.com",
    "is_internal": true,
    "source_created_at": "2026-01-22T10:00:00Z",
    "close_date": "2026-04-15T00:00:00Z",
    "expected_close_date": "2026-04-30T00:00:00Z",
    "closed_won_at": null,
    "closed_lost_at": null,
    "closed_reason": null,
    "stage_entered_at": "2026-01-22T10:00:00Z",
    "stage_updated_at": "2026-01-22T10:00:00Z",
    "deal_type": "Expansion",
    "priority": "High",
    "lead_source": "Inbound",
    "description": "Upgraded to enterprise plus tier with 200 seats",
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
| 422 | Validation error | Field validation failed or request included unsupported fields such as `deal_status`, `stage_id`, `owner_user_id`, `close_date`, `closed_reason`, `crm_url`, or `last_activity_at` |
| 500 | Internal server error | Unexpected error |

---

## 6. Move Stage

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

`data` uses the full `DealWriteResponse` contract above. The example below shows
the stage/lifecycle fields that change during this action.

```json
{
  "status": "SUCCESS",
  "message": "Deal stage updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "9ba7b810-9dad-11d1-80b4-00c04fd430c9",
    "stage_name": "Proposal",
    "deal_status": "open",
    "closed_won_at": null,
    "closed_lost_at": null,
    "closed_reason": null,
    "stage_entered_at": "2026-01-22T15:45:00Z",
    "stage_updated_at": "2026-01-22T15:45:00Z",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T15:45:00Z"
  }
}
```

**Note**: Moving to a new stage automatically creates a stage history record with timestamp.
If the target stage has `stage_state: "won"` or `stage_state: "lost"`, the deal lifecycle
is closed and `deal_status` becomes `won` or `lost`. If the target stage has
`stage_state: "open"` and the deal was closed, the deal is reopened and `deal_status`
becomes `open`; `close_date`, `closed_won_at`, and `closed_lost_at` are cleared.
Moving a previously lost deal into a won stage also clears the old `closed_reason`
so a won deal does not retain stale loss-reason text.

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 400 | Missing stage_id | Request missing required field |
| 404 | Stage not found | Invalid stage_id or stage doesn't belong to deal's pipeline |
| 409 | External CRM deals are read-only | Tried to move a deal synced from HubSpot/Salesforce |
| 409 | Unusable pipeline | Deal's pipeline is missing at least one `open`, `won`, or `lost` stage |
| 500 | Internal server error | Unexpected error |

---

## 7. Close Deal

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
POST /users/crm/deals/{deal_id}/close
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `outcome` | enum | **Yes** | `won` or `lost`; values are trimmed and case-normalized |
| `closed_reason` | string | Conditionally required | Required when `outcome=lost`; ignored and cleared when `outcome=won` |
| `close_date` | string | No | ISO 8601 datetime; defaults to current time |
| `stage_id` | UUID | No | Optional explicit closed stage; must match outcome state |

### Example Request (Won)

```json
{
  "outcome": "won"
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

`data` uses the full `DealWriteResponse` contract above. The example below shows
the lifecycle fields that change during this action.

```json
{
  "status": "SUCCESS",
  "message": "Deal closed successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "9ba7b810-9dad-11d1-80b4-00c04fd430c9",
    "stage_name": "Closed Won",
    "deal_status": "won",
    "close_date": "2026-02-10T16:15:00Z",
    "closed_won_at": "2026-02-10T16:15:00Z",
    "closed_lost_at": null,
    "closed_reason": null,
    "stage_entered_at": "2026-02-10T16:15:00Z",
    "stage_updated_at": "2026-02-10T16:15:00Z",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-02-10T16:15:00Z"
  }
}
```

Won deals always return `closed_reason: null`. The field is for lost-deal reason
text only; closing a previously lost deal as won clears stale loss-reason text.

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 400 | Invalid close request | Invalid outcome or missing `closed_reason` for lost outcome |
| 404 | Deal/stage not found | Invalid deal_id or stage cannot be resolved |
| 409 | External CRM deals are read-only | Tried to close a synced external deal |
| 500 | Internal server error | Unexpected error |

---

## 8. Reopen Deal

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

`data` uses the full `DealWriteResponse` contract above. The example below shows
the reopened lifecycle fields.

```json
{
  "status": "SUCCESS",
  "message": "Deal reopened successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "stage_name": "Qualification",
    "deal_status": "open",
    "close_date": null,
    "closed_won_at": null,
    "closed_lost_at": null,
    "closed_reason": null,
    "stage_entered_at": "2026-02-10T16:20:00Z",
    "stage_updated_at": "2026-02-10T16:20:00Z",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-02-10T16:20:00Z"
  }
}
```

Reopening always clears `close_date`, `closed_won_at`, and `closed_lost_at`.
`closed_reason` is preserved unless `clear_closed_reason` is `true`.

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 404 | Deal/stage not found | Invalid deal_id or open stage not found |
| 409 | External CRM deals are read-only | Tried to reopen a synced external deal |
| 500 | Internal server error | Unexpected error |

---

## 9. Assign Owner

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

`data` uses the full `DealWriteResponse` contract above. The examples below show
the owner field being assigned or cleared.

Assigned:
```json
{
  "status": "SUCCESS",
  "message": "Deal owner updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "owner_name": "John Doe",
    "owner_email": "john@example.com",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-22T15:50:00Z"
  }
}
```

Cleared:
```json
{
  "status": "SUCCESS",
  "message": "Deal owner updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "owner_user_id": null,
    "owner_name": null,
    "owner_email": null,
    ...
  }
}
```

**Note**: On first owner assignment, if no CRM users exist, the system auto-syncs all org users to CRM (MVP behavior).
Owner assignment updates the deal owner snapshot (`owner_user_id`, `owner_name`, `owner_email`) and the internal `owner` participant projection in the same transaction. If the participant projection rejects the change, the endpoint returns that 4xx/409 validation status instead of a generic server error.

### Error Responses

| Status | Error | Description |
|--------|-------|-------------|
| 404 | User not found | Invalid owner_user_id or user not available in CRM |
| 409 | External CRM deals are read-only | Tried to assign owner to a deal synced from HubSpot/Salesforce |
| 500 | Internal server error | Unexpected error |

---

## 10. Update Deal Account

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
PATCH /users/crm/deals/{deal_id}/account
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `company_id` | UUID | No | Existing CRM company UUID. Links this deal to that account; external-provider accounts are copied into the internal CRM before linking. |
| `name` | string | No | Company/account name. Cannot be cleared with `null`; omit when unchanged. |
| `domain` | string | No | Company domain (recommended; used to create/link company). Cannot be cleared with `null`; omit when unchanged. |
| `website` | string \| null | No | Company website URL. Send `null` to clear it on the linked account. |
| `linkedin_url` | string \| null | No | Company LinkedIn URL. Send `null` to clear it on the linked account. |
| `industry` | string \| null | No | Company industry classification. Send `null` to clear it on the linked account. |
| `country` | string \| null | No | Company country/geography. Send `null` to clear it on the linked account. |

### Behavior

- At least one account field must be provided. Blank strings are rejected with `422`.
- If `domain` (or `website`) is provided, the system creates or links a company by domain
  and attaches it to the deal.
- If `company_id` is provided, the deal is linked to that existing company. If the selected
  company came from an external provider, the backend copies it into the internal CRM before linking.
- If only `name` is provided, the deal stores `company_name` without creating a company record.
- If the deal is already linked to an account, profile fields such as `website`,
  `linkedin_url`, `industry`, and `country` can be sent as `null` to clear them.
  Account identity fields `name` and `domain` must be non-empty strings when sent.
- Deal account-change mutation events include visible account snapshot changes:
  `company_id`, `company_name`, `company_domain`, `company_industry`, `company_website`,
  `company_linkedin_url`, and `company_country`.

Frontend field mapping from `detail_field_definitions`:

| Detail field | Send to this endpoint as |
|--------------|--------------------------|
| `company_name` | `name` |
| `company_domain` | `domain` |
| `company_website` | `website` |
| `company_linkedin_url` | `linkedin_url` |
| `company_industry` | `industry` |
| `company_country` | `country` |

### Example Request

```json
{
  "name": "Acme Corp",
  "domain": "acme.com",
  "linkedin_url": "https://www.linkedin.com/company/acme"
}
```

### Success Response (200)

`data` uses the full `DealWriteResponse` contract above. The example below shows
the account snapshot fields that change during this action.

```json
{
  "status": "SUCCESS",
  "message": "Deal account updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "amount": 50000,
    "currency": "USD",
    "currency_symbol": "$",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "pipeline_name": "Default Pipeline",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "stage_name": "Qualification",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "is_internal": true,
    "owner_name": "John Doe",
    "owner_email": "john@example.com",
    "company_id": "5f35a0b7-5f5f-4a0b-9e34-3b2b88a0e9a2",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "company_industry": "Manufacturing",
    "company_website": "https://acme.com",
    "company_linkedin_url": "https://www.linkedin.com/company/acme",
    "company_country": "United States",
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-01-23T11:00:00Z"
  }
}
```

---

## 11. Update Primary Contact

**⚠️ Internal CRM Only**: This endpoint only works for deals where `is_internal: true`.

### Endpoint

```
PATCH /users/crm/deals/{deal_id}/primary-contact
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `primary_contact_id` | UUID \| null | No | Preferred frontend field. Send a contact UUID to set the deal POC. Send `null` to clear the POC. |
| `primary_contact_name` | string \| null | No | Legacy/import-compatible snapshot write. Do not use for manual frontend POC edits. |
| `primary_contact_email` | string \| null | No | Legacy/import-compatible snapshot write. Do not use for manual frontend POC edits. |

Manual frontend POC edits should use `primary_contact_id` only.

Rules for `primary_contact_id`:
- The contact must be active and not archived.
- The contact must already be an active approved Buying Committee contact stakeholder on this deal.
- The contact must be reachable by at least one identity: primary email, additional email, phone, or mobile phone.
- Setting a new POC by contact ID demotes the previous primary contact but does not remove the previous person from the Buying Committee.
- `primary_contact_id` cannot be combined with `primary_contact_name` or `primary_contact_email` in the same request.

Legacy name/email mode:
- At least one of `primary_contact_name` or `primary_contact_email` must be provided with a non-empty value, unless both fields are explicitly sent as `null` to clear the primary contact.
- Blank strings are rejected.
- A single null legacy field without the other null field is rejected.
- When the backend cannot resolve the supplied name/email to one unique internal contact, it stores the provided name/email as the deal snapshot and clears the previous primary contact participant link so `primary_contact_id` and participant state do not point at an older contact.

Set primary contact by contact ID:
```json
{
  "primary_contact_id": "44ef8f41-c980-4b12-987e-3b7fd7ed8c3e"
}
```

Clear primary contact:
```json
{
  "primary_contact_id": null
}
```

Legacy clear payloads with both `primary_contact_name: null` and
`primary_contact_email: null` are still accepted for backward compatibility.

Recommended POC picker source:
- Use the deal's active approved Buying Committee contact stakeholders from
  `GET /users/crm/deals/{deal_id}/stakeholders`.
- Do not use all active People contacts for direct POC selection because the backend will reject unrelated contacts.
- If the current deal has no eligible stakeholder, frontend should prompt the user to add a Buying Committee stakeholder first, then set that stakeholder as POC.

Common validation responses:

| Status | Message | When |
|--------|---------|------|
| `400` | `Primary contact must have an email or phone number` | Selected stakeholder has no primary/additional email, phone, or mobile phone. |
| `400` | `Primary contact must be an active approved Buying Committee stakeholder on this deal` | Contact exists but is not an active approved stakeholder on this deal. |
| `404` | `Contact not found` | Contact does not exist in this organisation/integration or is archived. |
| `409` | Deal write conflict/read-only message | Deal is not an internal editable CRM deal. |
| `422` | Pydantic validation error | Payload mixes ID mode with legacy fields, has unknown fields, malformed UUID/email, or blank strings. |

### Success Response (200)

`data` uses the full `DealWriteResponse` contract above. The example below shows
the primary-contact and account context fields most relevant to this action.

```json
{
  "status": "SUCCESS",
  "message": "Deal primary contact updated successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
    "name": "Acme Corp - Enterprise License",
    "pipeline_id": "550e8400-e29b-41d4-a716-446655440000",
    "stage_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "deal_status": "open",
    "owner_user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "company_name": "Acme Corp",
    "company_domain": "acme.com",
    "primary_contact_id": "44ef8f41-c980-4b12-987e-3b7fd7ed8c3e",
    "primary_contact_name": "Jane Buyer",
    "primary_contact_email": "jane@acme.com",
    "primary_contact_details": [
      {
        "contact_id": "44ef8f41-c980-4b12-987e-3b7fd7ed8c3e",
        "name": "Jane Buyer",
        "email": "jane@acme.com",
        "phone_number": "+15551234567"
      }
    ],
    "created_at": "2026-01-22T10:00:00Z",
    "updated_at": "2026-02-10T10:30:00Z"
  }
}
```

After refetching `GET /users/crm/deals/{deal_id}`, detail responses also include
`primary_contact_details` with `phone_number` when the linked contact has a phone
or mobile phone.

---

## 12. Delete Deal

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

`data` uses the full `DealWriteResponse` contract above. The example below shows
the deletion status fields.

```json
{
  "status": "SUCCESS",
  "message": "Deal deleted successfully",
  "data": {
    "deal_id": "98a0c9d2-b680-4aa0-8c87-dc451fccbad5",
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

### Get CRM Schema Metadata

```
GET /users/crm/schema?entity=deal
```

Returns the backend-owned field schema for deal forms, import mapping, agents,
filters, and future table column metadata. The response does not require a
specific deal ID.

Standard deal fields are listed first using the same order as
`detail_field_definitions`. Active custom fields follow with source metadata:
internal custom fields can be writable through `custom_columns.<key>`, while
provider-backed fields are visible but read-only for internal CRM writes.

**Query Parameters**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `entity` | enum | `deal` | Entity schema to return. Currently only `deal` is supported. |

**Success Response (200)**

```json
{
  "status": "SUCCESS",
  "message": "CRM schema retrieved",
  "data": {
    "entity": "deal",
    "fields": [
      {
        "field_id": null,
        "entity": "deal",
        "key": "name",
        "label": "Deal name",
        "type": "string",
        "order": 1,
        "section": "core",
        "source": "standard",
        "ownership": "system",
        "integration_id": null,
        "provider_name": null,
        "provider_field_key": null,
        "required": false,
        "read_only": false,
        "nullable": true,
        "editable": true,
        "sortable": false,
        "filterable": false,
        "searchable": true,
        "input_control": "text",
        "options": {},
        "validation": {},
        "lifecycle_state": "active",
        "write_path": "name",
        "write_policy": "always",
        "read_only_reason": null,
        "internal_write": true,
        "internal_write_validation": {
          "required": false,
          "option_membership": false
        }
      },
      {
        "field_id": "c1d2e3f4-a5b6-7890-cdef-1234567890ab",
        "entity": "deal",
        "key": "procurement_stage",
        "label": "Procurement Stage",
        "type": "select",
        "order": 1000,
        "section": "custom",
        "source": "internal",
        "ownership": "internal",
        "integration_id": null,
        "provider_name": null,
        "provider_field_key": null,
        "required": true,
        "read_only": false,
        "nullable": false,
        "editable": true,
        "sortable": false,
        "filterable": false,
        "searchable": false,
        "input_control": "select",
        "options": {
          "values": [
            { "id": "opt_legal", "value": "legal", "label": "Legal" }
          ]
        },
        "validation": {},
        "lifecycle_state": "active",
        "write_path": "custom_columns.procurement_stage",
        "write_policy": "custom_columns",
        "read_only_reason": null,
        "internal_write": true,
        "internal_write_validation": {
          "required": true,
          "option_membership": true
        }
      },
      {
        "field_id": "dfecde7a-9c2b-4e8b-9479-188ca36a51f2",
        "entity": "deal",
        "key": "hubspot_score",
        "label": "Hubspot Score",
        "type": "number",
        "order": 1001,
        "section": "custom",
        "source": "provider",
        "ownership": "hubspot",
        "integration_id": "8d85a5d0-a75b-4741-bbde-df1f4e5a1c76",
        "provider_name": "hubspot",
        "provider_field_key": "hs_deal_score",
        "required": false,
        "read_only": true,
        "nullable": true,
        "editable": false,
        "sortable": false,
        "filterable": false,
        "searchable": false,
        "input_control": "number",
        "options": {},
        "validation": {},
        "lifecycle_state": "active",
        "write_path": null,
        "write_policy": "provider_read_only",
        "read_only_reason": "Provider-backed fields are not writable through internal CRM writes.",
        "internal_write": false,
        "internal_write_validation": {
          "required": false,
          "option_membership": false
        }
      }
    ]
  }
}
```

**Frontend rules**

- Use `source`, `ownership`, `provider_name`, and `provider_field_key` to
  distinguish internal fields from HubSpot/Salesforce mirrored fields.
- Only render direct create/edit inputs when `internal_write=true` and
  `editable=true`.
- For custom fields, submit values under `custom_columns.<key>` from
  `write_path`.
- Agent and voice `create_deal` callers use the same `custom_columns` key; when
  required custom fields are missing, the tool response points clients back to
  this schema endpoint with `required_custom_fields`.
- `internal_write_validation.required=true` means create and terminal lifecycle
  transitions require a non-empty value.
- `internal_write_validation.option_membership=true` means select/multiselect
  values must match an active configured option value.
- `custom_column_definitions` in deal detail remains for backward-compatible
  detail rendering. New non-detail consumers should use this schema endpoint.

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

`q` searches owner name, email, or owner UUID. Leading/trailing whitespace is
ignored, and blank search values are treated the same as no search term.

Optional `owner_ids` query values hydrate selected owners even if they do not match
the current search term or fall outside the first `limit` results:

```
GET /users/crm/deals/owners?q=nikhil&owner_ids={owner_id}
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

### Resolve Current User Deal Owners

```
GET /users/crm/deals/owners/me
```

Returns CRM deal-owner rows that match the authenticated app user by app user ID,
CRM user ID, or normalized email. Email matching is trimmed and case-insensitive,
so provider-padded owner emails still resolve. Frontend should use this endpoint
to preselect `owner_ids` on Deals page load when the product wants a personal
owner view by default.

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
   in one request. Company is linked when `account.company_id` is supplied, otherwise
   created/linked from `account.domain` when available.
9. **Owner filter options**: Use `/users/crm/deals/owners` and filter by `owner_ids`; do not derive options from the current page.
10. **Close flow**: Use `POST /users/crm/deals/{deal_id}/close` and
    `POST /users/crm/deals/{deal_id}/reopen` when you need explicit close/reopen
    controls. Stage picker moves through `POST /users/crm/deals/{deal_id}/stage`
    also update lifecycle when the selected stage has `stage_state` `won`, `lost`,
    or `open`.
11. **Deal list columns**: Use `user_preferences.crm.deals.list.columns` from login/session bootstrap for the table order. Do not hardcode the default deal list order in the frontend.
12. **Deal detail rendering**: Use `data.detail_field_definitions` from `GET /users/crm/deals/{deal_id}` for predefined Get Details order and inline editability. Do not hardcode the deal field order in the frontend.
13. **Schema-driven custom controls**: Use `GET /users/crm/schema?entity=deal` for create/edit/agent custom field metadata. Deal detail still returns `custom_column_definitions` as a compatibility projection for existing detail controls.
14. **CSV import**: Use the generic CRM CSV upload API at `/users/crm/imports/csv`; deal-scoped import reconnect/SSE fields are not part of the deal list response.
15. **Manual POC edits**: Use `PATCH /users/crm/deals/{deal_id}/primary-contact` with `primary_contact_id`. The picker should show active approved Buying Committee contact stakeholders from `GET /users/crm/deals/{deal_id}/stakeholders`. To clear, send `{ "primary_contact_id": null }`. If no eligible stakeholder exists, guide the user to add a Buying Committee stakeholder first.

---

## Related Documents

- `deal-workflow-specification.md` - Complete workflow specification
- `create-deal-fields-specification.md` - Field-level specification
- `internal-crm-frontend-stepwise-integration.md` - Integration guide

---

## Deal CSV Upload Import

Deal CSV import is handled by the generic CRM CSV upload API, not by deal-scoped routes.

Use `docs/API Documentation/deals/crm-imports-api.md` and the base path `/users/crm/imports/csv`.
The removed deal-scoped import endpoints are no longer registered.

---

## Changelog

### 2026-07-19

1. Added `GET /users/crm/deals/{deal_id}/activity-feed` with cursor pagination,
   activity/source filters, occurrence-date filtering, and ascending/descending
   ordering.
2. Documented deal visibility authorization, activity navigation fields, and
   source-specific `occurred_at` behavior.
3. Documented the `DEAL_CREATED` title rules and its initial stage/pipeline
   snapshot so clients can render creation separately from later stage changes.

### 2026-07-05

1. Added `GET /users/crm/schema?entity=deal` as the canonical deal field schema
   endpoint for standard fields, internal custom fields, and provider mirrored
   fields.
2. Documented provider-backed schema fields as read-only for internal CRM writes.
3. Clarified that deal-detail `custom_column_definitions` is a compatibility
   projection; new create/edit/import/agent consumers should use the schema
   endpoint.
4. Documented that standard schema fields return null custom/provider metadata
   fields (`field_id`, `integration_id`, `provider_name`, `provider_field_key`)
   to keep one stable response shape across standard, internal, and provider
   fields.

### 2026-07-01

1. Added `user_preferences.crm.deals.list.columns` as the backend-owned default
   deal list column source.
2. Removed `deal_health` and `deal_health_reason` from `DealListItem`; health
   remains available on deal detail only.
3. Added `primary_contact_id`, `primary_contact_name`, `primary_contact_email`,
   and `last_activity_at` to deal list rows so every default deal column has a
   list payload path.

### 2026-06-21

1. Documented that `POST /users/crm/deals/{deal_id}/stage` keeps `deal_status`
   in sync with target stage state for closed and reopened lifecycle transitions.

### 2026-04-18

1. Removed the previous deal-scoped CSV import implementation from the active API surface while the generic CRM CSV upload API was rebuilt separately.

### 2026-02-26

1. Added `company_country` to `GET /users/crm/deals/{deal_id}` response. Populated from linked CRM company record.
2. Added `industry` and `country` fields to `PATCH /users/crm/deals/{deal_id}/account` request. These are now editable via the account update endpoint.
3. Expanded `q` search parameter in `GET /users/crm/deals` to match against both deal name and company name (OR condition).

### 2026-02-10

1. Expanded `GET /users/crm/deals/{deal_id}` response to include broader deal, timeline, association, and system metadata.
2. Added `include_empty_custom_columns` query parameter behavior to detail docs.
3. Extended `PATCH /users/crm/deals/{deal_id}` contract with additional core fields and custom field writes (`custom_columns`; legacy alias `custom_properties`).
4. Added new endpoint: `PATCH /users/crm/deals/{deal_id}/primary-contact`.
5. Documented custom field update validation behavior for interactive deal edits.
6. Restricted `PATCH /users/crm/deals/{deal_id}` to disallow editing `last_activity_at` (read-only timeline field); `source_created_at` is accepted as the deal start/CRM-created date.
7. Added dedicated lifecycle endpoints: `POST /users/crm/deals/{deal_id}/close` and `POST /users/crm/deals/{deal_id}/reopen`.
8. Moved `closed_reason` handling out of generic update and into the dedicated close flow.
9. Added `currency_symbol` to deal list/detail responses so frontend can render currency icons without hardcoded defaults.
10. Documented `datetime` and `currency` as supported custom field types for interactive deal updates.
11. Added explicit frontend guidance to render `date`/`datetime`/`currency` custom field controls from backend `data_type`.
12. Added temporary deal-import reconnect metadata to the deal list response; this field was later removed when import moved to the generic CRM CSV upload API.
13. Added `custom_column_definitions` to deal detail response, including `data_type` and currency context (`currency_code`, `currency_symbol`) for custom field UI rendering.
14. Restricted `PATCH /users/crm/deals/{deal_id}` to disallow editing `crm_url` (read-only detail metadata).
