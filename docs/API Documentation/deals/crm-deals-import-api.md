# CRM Deals Import API (CSV)

> Version: 1.0  
> Base URL: `/users/crm/deals`  
> Last Updated: 2026-02-10

---

## Overview

This document describes the **CSV mass‑upload endpoints** for Internal CRM deals.
It covers how to **download a template**, **upload a filled CSV**, **start processing**,
and **stream job status via SSE**.

**Storage split:**
- Template download → **master/application storage**
- CSV upload → **customer/org storage**

---

## 1) Download Template

```
GET /users/crm/deals/import/template?file_type=csv
```

### Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Template ready",
  "data": {
    "file_type": "csv",
    "download_url": "https://<storage>.blob.core.windows.net/gtm-app/crm_templates/deal_import_template.csv?<sas>",
    "expires_in_seconds": 3600,
    "storage_path": "crm_templates/deal_import_template.csv"
  }
}
```

---

## 2) Create Upload URL

```
POST /users/crm/deals/import/upload-url
```

### Request Body

```json
{
  "filename": "deals_import.csv"
}
```

### Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Upload URL created",
  "data": {
    "upload_url": "https://<storage>.blob.core.windows.net/<org-container>/crm_imports/deals/<user>/<id>/deals_import.csv?<sas>",
    "storage_path": "crm_imports/deals/<user>/<id>/deals_import.csv",
    "expires_in_seconds": 900
  }
}
```

---

## 3) Process Uploaded CSV

```
POST /users/crm/deals/import/process
```

### Request Body

```json
{
  "storage_path": "crm_imports/deals/<user>/<id>/deals_import.csv",
  "import_mode": "upsert",
  "emit_row_events": true,
  "strict_validation": false,
  "custom_column_types": {
    "custom_steel_target": "integer",
    "custom_partner_details": "text",
    "custom_contract_signed_at": "datetime",
    "custom_project_budget": "currency"
  },
  "custom_column_labels": {
    "custom_steel_target": "Steel Target"
  }
}
```

### Process Options

- `import_mode` (optional): `create_only | update_only | upsert` (default: `upsert`)
- `emit_row_events` (optional): emit per-deal SSE frames (default: `true`)
- `strict_validation` (optional): reserve flag for stricter row validation policy (default: `false`)
- `custom_column_types` (optional): required only when **new** `custom_*` columns are found.
  Allowed values: `text | number | integer | date | datetime | currency | boolean`
  Frontend rendering guidance: `date` -> date picker, `datetime` -> date-time picker, `currency` -> numeric input with currency symbol/code.
- `custom_column_labels` (optional): friendly labels for newly defined custom columns.

### Response (202)

```json
{
  "status": "SUCCESS",
  "message": "Deal import processing started",
  "data": {
    "job_id": "9f2c1b62-9d8a-4f1c-bac4-7f5b2a8f5b0b",
    "job_type": "deal_import",
    "status": "pending",
    "stream_url": "/users/crm/deals/import/jobs/9f2c1b62-9d8a-4f1c-bac4-7f5b2a8f5b0b/events"
  }
}
```

### Response (409) - Custom Column Mapping Required

When uploaded CSV contains unknown `custom_*` columns and mapping is not provided:

```json
{
  "status": "ERROR",
  "message": "Custom column type mapping required before import can continue",
  "data": {
    "status": "mapping_required",
    "requires_custom_column_mapping": true,
    "unknown_custom_columns": ["steel_target", "partner_details"],
    "suggested_types": {
      "steel_target": "integer",
      "partner_details": "text"
    }
  }
}
```

---

## 4) SSE: Stream Import Job Events

```
GET /users/crm/deals/import/jobs/{job_id}/events
```

This uses the standard deal-job SSE stream. Events include:

- `progress.update` → progress status updates
- `import.deal.started` → per-deal row processing started
- `import.deal.progress` → per-deal step updates
- `import.deal.completed` → per-deal success result
- `import.deal.failed` → per-deal failure result
- `import.summary.tick` → running totals while file is processing
- `answer.replace` → final job result payload
- `error` → job errors
- `finalize` → completion signal

**Final result payload (answer.replace)**

```json
{
  "total_rows": 10,
  "created_count": 8,
  "failed_count": 2,
  "warning_count": 1,
  "created": [
    {"row_number": 2, "deal_id": "....", "deal_name": "Acme - Enterprise"}
  ],
  "failed": [
    {"row_number": 4, "deal_name": "Beta", "error": "Owner not found: ops@beta.com"}
  ],
  "warnings": [
    {"row_number": 7, "email": "se@acme.com", "warning": "team_member_not_found"}
  ]
}
```

---

## CSV Columns (Human‑Readable)

No IDs required.

```
deal_name
deal_amount
deal_currency
deal_status
deal_probability
deal_close_date
deal_expected_close_date
deal_closed_won_at
deal_closed_lost_at
deal_closed_reason
deal_stage_name
deal_forecast_category
deal_type
deal_priority
deal_lead_source
deal_owner_email
company_name
company_domain
deal_primary_contact_name
deal_primary_contact_email
deal_source_created_at
deal_last_activity_at
deal_description
company_website
company_linkedin
company_industry
company_country
deal_team_member_emails
```

**Notes**
- `deal_owner_email` is recommended (email in your org users table).
- `deal_team_member_emails` can be comma‑separated.
- `deal_stage_name` must match an existing stage name/label in internal CRM.
- `company_domain` is the primary identity for account creation/linking.
- If `company_domain` is missing but `company_name` is present, import uses a name-only account strategy:
  - reuses an existing exact name match when unique,
  - otherwise creates a provisional account (`is_provisional=true`) for later merge,
  - emits `ambiguous_account_name` warning when multiple account name matches exist.
- Primary contact handling:
  - if `deal_primary_contact_email` is present, contact upsert is email-first.
  - if only `deal_primary_contact_name` is present, import uses a name-only contact strategy:
    - reuses an existing exact name match when unique,
    - otherwise creates a provisional contact (`is_provisional=true`) for later merge.
  - emits `contact_email_missing` warning for name-only contacts and `ambiguous_contact_name` when multiple contact name matches exist.
- If `deal_name` is empty, we fall back to `company_name` or `company_domain`.
- `deal_owner_email` must exist in your organisation users.
- `pipeline` is currently fixed to the org default during import.
- `custom_*` columns are not part of the standard template, but are supported in upload files.
- New custom columns trigger a mapping gate (`409`) unless type mapping is supplied.
- Once mapped, definitions are stored and reused in future imports.
- `datetime` and `currency` custom column types are supported for mapping and type validation.

---

## Test Flow (Example)

### Step 1: Download template

```bash
curl -H "Authorization: Bearer <token>" \
  "https://<api-host>/users/crm/deals/import/template?file_type=csv"
```

Download the CSV from `download_url`.

### Step 2: Fill CSV

Fill the template and save as `deals_import.csv`.

### Step 3: Get upload URL

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"filename": "deals_import.csv"}' \
  "https://<api-host>/users/crm/deals/import/upload-url"
```

### Step 4: Upload to Azure Blob

```bash
curl -X PUT \
  -H "x-ms-blob-type: BlockBlob" \
  --upload-file "deals_import.csv" \
  "<upload_url>"
```

### Step 5: Start Processing

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"storage_path": "crm_imports/deals/<user>/<id>/deals_import.csv"}' \
  "https://<api-host>/users/crm/deals/import/process"
```

### Step 6: Listen to SSE

```bash
curl -N \
  -H "Authorization: Bearer <token>" \
  "https://<api-host>/users/crm/deals/import/jobs/<job_id>/events"
```

---

## Changelog

### 2026-02-10

- Added custom column mapping support in process request: `custom_column_types`, `custom_column_labels`.
- Documented `409` mapping-required response for unknown `custom_*` columns.
- Clarified that custom column definitions are persisted and reused in later imports.
- Updated SSE section to reflect deal-level import progress events and final summary payload.
- Refined CSV guidance and notes for account/contact fallback handling during import.
- Extended custom mapping types with `datetime` and `currency`.
- Added frontend rendering guidance for `date`/`datetime`/`currency` custom type mapping controls.
