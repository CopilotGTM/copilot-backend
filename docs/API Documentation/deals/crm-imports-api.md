# Audited CSV Deal Import API — Frontend Handoff

This is the implemented frontend contract for the three-step flow:

`Upload → Map & Import → Results`

Every successful CSV row creates one new Deal linked to one resolved-or-created Account and one resolved-or-created Contact. Matched Accounts and Contacts are preserved; CSV values never overwrite them.

Base path: `/users/crm/imports/csv`

All endpoints require the normal authenticated CRM user session. Presign, finalize, mapping save, and start require all three create permissions: `accounts:create`, `contacts:create`, and `deals:create`.

## Frontend Checklist

1. Select an internal CRM Pipeline, default stage, and optional default owner.
2. Load the backend-owned target catalog with `GET /targets`.
3. Presign, upload the CSV directly to storage, and finalize it.
4. Poll status until `ready_for_mapping` or `failed`.
5. Present every CSV column as mapped or explicitly ignored.
6. Save the reviewed mapping, then start the import.
7. Poll until `completed` or `failed`.
8. Render aggregate cards and cursor-paginated row results.
9. If rows failed, offer the error CSV and failed-row retry.

Do not maintain a hard-coded frontend target list. The target catalog includes active Deal custom fields and changes with the selected Pipeline.

## Import State Machine

| `crm_import_status` | Frontend behavior |
|---|---|
| `uploading` | Upload bytes to the SAS URL, then call finalize. |
| `analyzing` | Poll the status endpoint. Mapping is not ready. |
| `ready_for_mapping` | Render mapping review or enable the Import action after a mapping has been saved. |
| `importing` | Poll status and display `counts`/`percent_complete`. |
| `completed` | Stop polling and load result rows. This state may include failed rows. |
| `failed` | Stop polling and show a fatal import error state. |

The top-level `status` is the generic job status. Drive this UI from `crm_import_status`.

Fatal job text is not currently exposed by the status response. For `failed`, show a generic import-failed message and a restart/close action; `latest_errors` is for row failures, not fatal analysis or worker failures.

Recommended polling interval: start at 1 second and back off to 3–5 seconds. Finalize and start are retry-safe.

## 1. Load Mapping Targets

```http
GET /users/crm/imports/csv/targets?pipeline_id={pipeline_uuid}
```

Call this after Pipeline selection and whenever the Pipeline changes.

```json
{
  "data": {
    "pipeline_id": "uuid",
    "pipeline_name": "Enterprise Sales",
    "stages": [
      {
        "stage_id": "uuid",
        "stage_key": "proposal",
        "name": "Proposal",
        "label": "Proposal",
        "stage_state": "open",
        "probability": 50
      }
    ],
    "targets": [
      {
        "target_field": "deal.name",
        "label": "Deal name",
        "group": "deal",
        "value_type": "text",
        "required": true,
        "read_only": false,
        "custom_field_id": null,
        "custom_field_key": null,
        "options": {},
        "validation": {}
      },
      {
        "target_field": "deal.custom.region",
        "label": "Region",
        "group": "deal",
        "value_type": "select",
        "required": false,
        "read_only": false,
        "custom_field_id": "uuid",
        "custom_field_key": "region",
        "options": {},
        "validation": {}
      }
    ]
  }
}
```

When Pipeline changes, clear the selected default stage locally, reload this endpoint, and require a stage from the new response before presign or mapping save.

## 2. Presign and Upload

```http
POST /users/crm/imports/csv/presign
Content-Type: application/json
```

All keys below are required. `default_owner_user_id` is required but nullable.

```json
{
  "filename": "Sales_Deals_June2026.csv",
  "mode": "account_contact_deal",
  "update_existing": false,
  "pipeline_id": "uuid",
  "default_stage_id": "uuid",
  "default_owner_user_id": null
}
```

Response: `201 Created`

```json
{
  "data": {
    "import_id": "uuid",
    "upload_url": "https://storage.example/...sas...",
    "storage_path": "crm_imports/.../Sales_Deals_June2026.csv"
  }
}
```

Upload the original file directly to Azure:

```ts
await fetch(uploadUrl, {
  method: "PUT",
  headers: {
    "x-ms-blob-type": "BlockBlob",
    "Content-Type": "text/csv",
  },
  body: file,
});
```

Only call finalize after the PUT succeeds.

## 3. Finalize and Analyze

```http
POST /users/crm/imports/csv/{import_id}/finalize
```

Response: `202 Accepted`. Poll `GET /{import_id}` while the state is `analyzing`.

Analysis is ready when the state becomes `ready_for_mapping`:

```json
{
  "data": {
    "import_id": "uuid",
    "status": "pending",
    "crm_import_status": "ready_for_mapping",
    "filename": "Sales_Deals_June2026.csv",
    "pipeline_id": "uuid",
    "default_stage_id": "uuid",
    "default_owner_user_id": null,
    "analysis": {
      "headers": ["Deal Name", "Company", "Email", "Stage"],
      "total_rows": 250,
      "sampled_rows": 20,
      "sample_rows": [
        {
          "Deal Name": "ABC Expansion",
          "Company": "ABC Ltd",
          "Email": "buyer@abc.com",
          "Stage": "Proposal"
        }
      ],
      "ambiguous_numeric_date_headers": []
    },
    "mapping_suggestions": [
      {
        "header": "Deal Name",
        "target_field": "deal.name",
        "confidence": "high",
        "reason_code": "alias_and_sample"
      }
    ],
    "mapping_confidence_summary": {
      "high": 1,
      "medium": 0,
      "low": 0
    }
  }
}
```

Suggestions are deterministic hints, not final mappings. A suggestion may have `target_field: null` and `confidence: "unmapped"`.

## 4. Save Mapping

```http
PATCH /users/crm/imports/csv/{import_id}/mapping
Content-Type: application/json
```

```json
{
  "mapping": {
    "Deal Name": "deal.name",
    "Company": "account.name",
    "Email": "contact.email",
    "Stage": "deal.stage",
    "Salesperson": "deal.owner",
    "Region": "deal.custom.region"
  },
  "ignored_columns": ["Internal Notes"],
  "column_formats": {
    "Expected Close Date": "DMY"
  },
  "pipeline_id": "uuid",
  "default_stage_id": "uuid",
  "default_owner_user_id": null,
  "update_existing": false,
  "retry_failed_rows": false
}
```

### Authoritative Mapping Rules

- Every analyzed header must appear in `mapping` or `ignored_columns`.
- One target can be mapped only once.
- `deal.name` is required.
- Account identity requires at least one mapped target from `account.name`, `account.domain`, `account.website`, or `account.linkedin_url`.
- Contact identity requires `contact.email` or `contact.linkedin_url`.
- Every active required Deal custom field must be mapped.
- Custom fields must come from the latest target catalog.
- `column_formats` accepts only `DMY` or `MDY`, only for mapped date/datetime columns.
- Headers listed in `analysis.ambiguous_numeric_date_headers` require an explicit format when mapped to a date target.
- `update_existing` must remain `false` in V1.

The frontend should implement the same checks for immediate feedback, but backend validation remains authoritative. Display the backend error `message` and field arrays from `data`, such as `missing_fields`, `unresolved_columns`, `unsupported_fields`, or `duplicate_target_fields`.

## 5. Start Import

```http
POST /users/crm/imports/csv/{import_id}/start
```

Response: `202 Accepted`, with `crm_import_status: "importing"`.

The backend revalidates the current Pipeline, stage, owner, mapping, and custom-field definitions before processing. Poll status until `completed` or `failed`.

## 6. Status and Aggregate Results

```http
GET /users/crm/imports/csv/{import_id}
```

```json
{
  "data": {
    "crm_import_status": "completed",
    "percent_complete": 100,
    "counts": {
      "total_rows": 250,
      "processed_rows": 250,
      "successful_rows": 243,
      "created_accounts": 32,
      "matched_accounts": 211,
      "created_contacts": 45,
      "matched_contacts": 198,
      "created_deals": 243,
      "failed_rows": 7,
      "skipped_rows": 0
    },
    "latest_errors": [
      {
        "row_number": 23,
        "status": "failed",
        "error_code": "invalid_stage",
        "error_field": "deal.stage",
        "error_message": "Unknown stage: Evaluation"
      }
    ]
  }
}
```

`completed` means processing finished; it does not mean every row succeeded. Use `failed_rows` to show partial success.

## 7. Paginate Result Rows

```http
GET /users/crm/imports/csv/{import_id}/rows?cursor={row_number}&limit=100&status=failed&q=acme
```

Query parameters:

| Parameter | Contract |
|---|---|
| `cursor` | Exclusive CSV row number. Omit for the first page. |
| `limit` | `1–500`; default `100`. |
| `status` | Optional: `preview`, `ready`, `created`, `skipped_duplicate`, or `failed`. |
| `q` | Optional literal search, `1–200` characters. |

```json
{
  "data": {
    "items": [
      {
        "row_number": 1,
        "status": "created",
        "deal_name": "ABC Expansion",
        "account_name": "ABC Ltd",
        "contact_name": "John Doe",
        "contact_email": "buyer@abc.com",
        "owner_name": "Rahul Sharma",
        "stage_name": "Proposal",
        "target_type": "deal",
        "account_id": "uuid",
        "contact_id": "uuid",
        "deal_id": "uuid",
        "owner_user_id": "uuid",
        "stage_id": "uuid",
        "account_outcome": "matched",
        "contact_outcome": "created",
        "attempt_count": 1,
        "error_code": null,
        "error_field": null,
        "error_message": null,
        "raw_values": {},
        "normalized_values": {}
      }
    ],
    "next_cursor": 1,
    "has_more": true
  }
}
```

Fetch all statuses for the default Results table. Use the status filter only for a selected tab. `normalized_values` never contains backend-only audit metadata.

Use the returned IDs for detail actions:

- Deal: `GET /users/crm/deals/{deal_id}`
- Account: `GET /users/crm/accounts/{account_id}`
- Contact: `GET /users/crm/contacts/{contact_id}`

## 8. Download Failed Rows

```http
GET /users/crm/imports/csv/{import_id}/errors.csv
```

The response is `text/csv` and includes the original CSV columns plus:

- `row_number`
- `status`
- `error_code`
- `error_field`
- `error_message`

Formula-looking values are escaped for spreadsheet safety. Enable this action when `failed_rows > 0` or `skipped_rows > 0`.

## 9. Retry Failed Rows

Failed-row retry reuses the same `import_id` and uploaded data:

1. Let the user correct mappings, Pipeline defaults, or owner defaults.
2. Call `PATCH /{import_id}/mapping` with the full mapping and `retry_failed_rows: true`.
3. The backend resets only failed rows and returns `ready_for_mapping`.
4. Call `POST /{import_id}/start` again.
5. Poll normally. Previously successful rows and their Deal IDs are preserved.

## Supported Targets

The target catalog is authoritative. Standard targets currently include:

- Account: name, domain, website, industry, phone, address, city, state, country, postal code, LinkedIn URL.
- Contact: name, first/last name, email, job title, phone, mobile phone, website, address, city, state, country, postal code, LinkedIn URL.
- Deal: name, amount, currency, created date, expected close date, description, type, priority, lead source, probability, forecast category, owner, and stage.
- Deal custom fields: every active, writable internal field returned by `/targets`.

## Value Formats

- Amount/currency: numeric values, currency symbols/codes, comma thousands separators, and accounting parentheses.
- Probability/percentage: number with optional `%`, constrained to `0–100`.
- Date/datetime: ISO or named-month formats such as `30-Jun-2026`; ambiguous numeric dates require DMY/MDY.
- Boolean custom fields: `true/false`, `yes/no`, or `1/0`.
- Multiselect custom fields: JSON arrays or semicolon-separated configured option values.
- Invalid values fail only that row with a field-specific error.

## Resolution Semantics

- Accounts match by normalized domain, Account LinkedIn URL, then unique case-insensitive name; otherwise they are created.
- Contacts match by normalized email, then Contact LinkedIn URL; otherwise they are created.
- Conflicting or ambiguous identities fail the row instead of guessing.
- Matched records are not updated.
- Empty stage values use the default stage. Non-empty unknown stages fail without fallback.
- Empty or unmatched owner values use the optional default owner. Without a default, unresolved owners fail.
- Deals are always created; V1 does not update or upsert Deals.

## Limits and Errors

- File type: UTF-8 CSV, optional BOM.
- Maximum file size: 10 MB.
- Maximum non-blank rows: 25,000.
- Maximum columns: 200.
- Maximum header length: 512 characters.
- Maximum mapping, ignored-column, and format entries: 200 each.

Expected HTTP handling:

- `400`: invalid configuration, mapping, value, or CSV limit.
- `403`: missing CRM create scope.
- `404`: import, Pipeline, upload, or stage resource not found.
- `409`: invalid state transition, ambiguous identity, or unusable Pipeline.
- `422`: request shape validation error.
- `429`: rate limited; retry with backoff.
- `500`: unexpected server/worker error.

## TypeScript Sequence

```ts
const catalog = await api.get("/users/crm/imports/csv/targets", {
  params: { pipeline_id: pipelineId },
});

const presign = await api.post("/users/crm/imports/csv/presign", {
  filename: file.name,
  mode: "account_contact_deal",
  update_existing: false,
  pipeline_id: pipelineId,
  default_stage_id: defaultStageId,
  default_owner_user_id: defaultOwnerId ?? null,
});

await fetch(presign.data.upload_url, {
  method: "PUT",
  headers: { "x-ms-blob-type": "BlockBlob", "Content-Type": "text/csv" },
  body: file,
});

await api.post(`/users/crm/imports/csv/${presign.data.import_id}/finalize`);
const analyzed = await pollUntilTerminalOr(
  `/users/crm/imports/csv/${presign.data.import_id}`,
  "ready_for_mapping",
);

await api.patch(`/users/crm/imports/csv/${presign.data.import_id}/mapping`, {
  mapping: reviewedMapping,
  ignored_columns: ignoredColumns,
  column_formats: dateFormats,
  pipeline_id: pipelineId,
  default_stage_id: defaultStageId,
  default_owner_user_id: defaultOwnerId ?? null,
  update_existing: false,
  retry_failed_rows: false,
});

await api.post(`/users/crm/imports/csv/${presign.data.import_id}/start`);
const result = await pollUntilTerminal(
  `/users/crm/imports/csv/${presign.data.import_id}`,
  ["completed", "failed"],
);

const rows = await api.get(
  `/users/crm/imports/csv/${presign.data.import_id}/rows`,
  { params: { limit: 100 } },
);
```

## V1 Exclusions

- Lead creation or Lead mappings.
- Account or Contact custom fields.
- Deal updates/upserts.
- Automatic custom-field or organization-user creation.
- External enrichment during import.
- Files larger than the documented limits.
- The removed Deal-scoped import routes; use only this generic CRM import API.
