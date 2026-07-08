# CRM CSV Imports API

Authenticated CRM users can upload one CSV and import accounts, contacts, deals, or rows that link those records together. The legacy deal-specific import route remains disabled; deal imports use this generic CRM CSV import API.

Base path: `/users/crm/imports/csv`

## Flow

1. `POST /presign`
   - Body: `{ "filename": "crm-import.csv", "mode": "account_contact_deal", "update_existing": false }`
   - Returns `import_id`, `upload_url`, and `storage_path`.
2. Browser uploads the CSV directly to the returned SAS `upload_url`.
3. `POST /{import_id}/finalize`
   - Verifies the blob and queues async analysis.
4. `GET /{import_id}`
   - Poll until `crm_import_status` is `ready_for_mapping`.
   - Response includes headers, mapping suggestions, counts, percent complete, and confidence summary.
5. `PATCH /{import_id}/mapping`
   - Body: `{ "mapping": { "Deal Name": "deal.name", "Email": "contact.email" }, "update_existing": false }`
   - Saves deterministic frontend-reviewed mapping.
6. `POST /{import_id}/start`
   - Starts async import processing.
7. `GET /{import_id}`
   - Poll progress until completed or failed.
8. `GET /{import_id}/rows?cursor=0&limit=100`
   - Cursor-paginates row outcomes by CSV `row_number`.
   - Frontend should call this after `completed` or `failed` to render the import result table, not only for failed rows.
9. `GET /{import_id}/errors.csv`
   - Downloads failed/skipped row details with formula-looking cells sanitized.

## Frontend Result Contract

`GET /{import_id}` returns aggregate import counts in `data.counts`:

```json
{
  "counts": {
    "total_rows": 2,
    "processed_rows": 2,
    "created_accounts": 1,
    "created_contacts": 1,
    "created_deals": 1,
    "skipped_duplicates": 0,
    "failed_rows": 0
  },
  "percent_complete": 100,
  "crm_import_status": "completed"
}
```

The completion screen should show these counts prominently. If `processed_rows > 0` and all created counts are `0`, show a warning that the import finished but no CRM records were created.

`GET /{import_id}/rows` returns row-level outcomes for the result table:

```json
{
  "items": [
    {
      "row_number": 1,
      "status": "created",
      "target_type": "account_contact_deal",
      "account_id": "00000000-0000-0000-0000-000000000001",
      "contact_id": "00000000-0000-0000-0000-000000000002",
      "deal_id": "00000000-0000-0000-0000-000000000003",
      "error_code": null,
      "error_message": null,
      "raw_values": {
        "Company": "Acme",
        "Email": "buyer@acme.com",
        "Deal Name": "Acme Expansion"
      },
      "normalized_values": {}
    },
    {
      "row_number": 2,
      "status": "skipped_duplicate",
      "target_type": null,
      "account_id": null,
      "contact_id": null,
      "deal_id": null,
      "error_code": "duplicate",
      "error_message": "A contact with email 'buyer@acme.com' already exists",
      "raw_values": {
        "Email": "buyer@acme.com"
      },
      "normalized_values": {}
    }
  ],
  "next_cursor": null,
  "has_more": false
}
```

Frontend should render:

- aggregate cards from `counts`: accounts, contacts, deals, skipped, failed;
- a row table from `/rows`: row number, status, created record links/IDs, and error/skipped reason;
- `/errors.csv` as a download action for failed or skipped rows.

Do not use `/rows?status=failed` for the completion view; that hides created and skipped outcomes.

## Supported Target Fields

Account fields:

- `account.name`
- `account.domain`
- `account.website`
- `account.industry`
- `account.phone`
- `account.city`
- `account.state`
- `account.country`
- `account.address`
- `account.postal_code`
- `account.linkedin_url`

Contact fields:

- `contact.name`
- `contact.first_name`
- `contact.last_name`
- `contact.email`
- `contact.job_title`
- `contact.phone`
- `contact.mobile_phone`
- `contact.website`
- `contact.address`
- `contact.city`
- `contact.state`
- `contact.country`
- `contact.postal_code`
- `contact.linkedin_url`

Deal fields:

- `deal.name`
- `deal.amount`
- `deal.currency`
- `deal.source_created_at`
- `deal.expected_close_date`
- `deal.description`
- `deal.deal_type`
- `deal.priority`
- `deal.lead_source`
- `deal.probability`
- `deal.forecast_category`

Common source aliases include `Company`, `Account Name`, `Website`, `Domain`, account/contact phone and location fields, `First Name`, `Last Name`, `Full Name`, `Email`, `Title`, `LinkedIn`, `Phone`, `Mobile Phone`, `Company LinkedIn`, `Account LinkedIn`, `Deal Name`, `Opportunity Name`, `Amount`, `Deal Value`, `Currency`, `Created Date`, `Expected Close Date`, `Deal Type`, `Priority`, `Lead Source`, `Probability`, `Forecast Category`, and `Description`.

## Limits

- Maximum file size: 10 MB.
- Maximum rows: 25,000 non-blank data rows.
- Maximum columns: 200.
- One CSV per import.
- UTF-8 with optional BOM is supported.

## Dedupe Rules

- Existing accounts and contacts are skipped by default through the existing CRM write services.
- Deals are created through the existing internal `DealWriteService`; V1 does not attempt deal duplicate detection.
- `update_existing=true` is present in the request contract but rejected in V1.
- Contact-plus-account-plus-deal rows create or resolve the account first, create the contact with that account id, then create the deal with account/contact relationship payloads.
- Row-level duplicate outcomes are stored as `skipped_duplicate`.

## Row Statuses

- `preview`: row parsed and available for preview.
- `ready`: row is ready for processing.
- `created`: row created at least one CRM record successfully. Row responses can include `account_id`, `contact_id`, and `deal_id`.
- `skipped_duplicate`: existing CRM data prevented creation.
- `failed`: validation or unexpected row-level failure.

## Unsupported In V1

- Leads, tasks, notes, custom fields, deal owner assignment, pipeline/stage selection by CSV, rollback UI, multi-file matching, formulas, update-existing semantics, and large streaming imports.
- The legacy `/users/crm/deals/import/...` route remains disabled and returns the existing compatibility response. Do not use it for CSV import.

## Example Frontend Sequence

```ts
const presign = await api.post("/users/crm/imports/csv/presign", {
  filename: file.name,
  mode: "account_contact_deal",
  update_existing: false,
});

await fetch(presign.data.upload_url, { method: "PUT", body: file });
await api.post(`/users/crm/imports/csv/${presign.data.import_id}/finalize`);

const status = await poll(`/users/crm/imports/csv/${presign.data.import_id}`);
await api.patch(`/users/crm/imports/csv/${presign.data.import_id}/mapping`, {
  mapping: status.data.mapping_suggestions.reduce(toReviewedMapping, {}),
  update_existing: false,
});

await api.post(`/users/crm/imports/csv/${presign.data.import_id}/start`);
const result = await poll(`/users/crm/imports/csv/${presign.data.import_id}`);
const resultRows = await api.get(
  `/users/crm/imports/csv/${presign.data.import_id}/rows`,
  { params: { limit: 100 } },
);
```
