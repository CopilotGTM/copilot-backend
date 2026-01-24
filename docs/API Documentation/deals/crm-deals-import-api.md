# CRM Deals Import API (CSV)

> Version: 1.0  
> Base URL: `/users/crm/deals`  
> Last Updated: 2026-01-23

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
  "storage_path": "crm_imports/deals/<user>/<id>/deals_import.csv"
}
```

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

---

## 4) SSE: Stream Import Job Events

```
GET /users/crm/deals/import/jobs/{job_id}/events
```

This uses the standard deal-job SSE stream. Events include:

- `progress.update` → progress status updates
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
owner_email
pipeline_name
stage_name
amount
currency
close_date
expected_close_date
deal_type
priority
lead_source
description
company_domain
company_name
company_website
company_linkedin
team_member_emails
```

**Notes**
- `owner_email` is recommended (email in your org users table).
- `team_member_emails` can be comma‑separated.
- `pipeline_name` + `stage_name` must match existing pipeline/stage names.
- `company_domain` is the primary identity for account creation/linking.
- If `deal_name` is empty, we fall back to `company_name` or `company_domain`.
- `owner_email` must exist in your organisation users.
- If `pipeline_name` is omitted, the default pipeline is used.

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
