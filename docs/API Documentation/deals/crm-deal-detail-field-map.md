# CRM Deal Detail Field Map

> Status: Implemented in API response metadata
> Endpoint: `GET /users/crm/deals/{deal_id}`
> Source of truth: `detail_field_definitions` on `DealDetail` in `src/api/schemas/crm_models.py`

This document defines the ordered frontend map for the deal detail "Get Details" section. The API returns this metadata in `detail_field_definitions`; the frontend should use the returned `order`, `editable`, `patch_field`, `edit_control`, and `edit_policy` values instead of hardcoding edit behavior.

## Display Rules

- Render predefined fields from `detail_field_definitions` ordered by `order`.
- Hide null or empty optional scalar values by default unless the view has an "all fields" mode.
- Render internal custom fields from `custom_column_definitions`, not from `detail_field_definitions`. Use each custom definition's `editable`, `patch_field`, `edit_control`, and `edit_policy` values for custom-field editing.
- `editable: true` means the field is accepted by `PATCH /users/crm/deals/{deal_id}` when `is_internal` is also `true`. External CRM deals stay read-only.
- Fields with action policies such as `owner_assignment`, `stage_action`, `account_action`, `primary_contact_action`, and `lifecycle_action` require their dedicated endpoint.
- `active_deal_jobs` and `deal_summary_data` are intentionally not detail rows. They are full objects used for reconnect banners and AI summary rendering.
- `company_industry`, `company_website`, `company_linkedin_url`, and `company_country` are hydrated from the linked account/company row. They are shown on deal detail for convenience, but the durable source is `CRMCompany`, not `CRMDeal`.

## Returned Metadata

| Order | Key | Label | Data type | Section | Control | Editable | Patch field | Edit policy |
|---:|---|---|---|---|---|---|---|---|
| 1 | `name` | Deal name | string | core | text | Yes | `name` | always |
| 2 | `deal_status` | Status | enum | core | select | No | - | stage_action |
| 3 | `stage_name` | Stage | string | core | select | No | - | stage_action |
| 4 | `pipeline_name` | Pipeline | string | core | select | No | - | stage_action |
| 5 | `amount` | Amount | number | value | currency | Yes | `amount` | always |
| 6 | `currency` | Currency | string | value | select | Yes | `currency` | always |
| 7 | `predicted_amount` | Predicted amount | number | value | currency | No | - | system |
| 8 | `probability` | Probability | number | value | percent | Yes | `probability` | always |
| 9 | `forecast_category` | Forecast category | string | value | select | Yes | `forecast_category` | always |
| 10 | `deal_health` | Deal health | enum | ai | select | No | - | generated |
| 11 | `deal_health_reason` | Health reason | string | ai | long_text | No | - | generated |
| 12 | `owner_details` | Owner | array | ownership | people | No | - | owner_assignment |
| 13 | `deal_team` | Internal team | array | ownership | people | No | - | participants_action |
| 14 | `company_name` | Account | string | account | account | No | - | account_action |
| 15 | `company_domain` | Account domain | string | account | domain | No | - | account_action |
| 16 | `company_industry` | Industry | string | account | select | No | - | account_action |
| 17 | `company_website` | Website | url | account | link | No | - | account_action |
| 18 | `company_linkedin_url` | Company LinkedIn | url | account | link | No | - | account_action |
| 19 | `company_country` | Account country | string | account | select | No | - | account_action |
| 20 | `primary_contact_name` | Primary contact | string | contact | contact | No | - | primary_contact_action |
| 21 | `primary_contact_email` | Primary contact email | email | contact | email | No | - | primary_contact_action |
| 22 | `source_created_at` | CRM created date | datetime | timeline | date_range | Yes | `source_created_at` | always |
| 23 | `close_date` | Close date | datetime | timeline | date_range | No | - | lifecycle_action |
| 24 | `expected_close_date` | Expected close date | datetime | timeline | date_range | Yes | `expected_close_date` | always |
| 25 | `closed_won_at` | Won date | datetime | timeline | date_range | No | - | lifecycle_action |
| 26 | `closed_lost_at` | Lost date | datetime | timeline | date_range | No | - | lifecycle_action |
| 27 | `deal_age_days` | Deal age | integer | timeline | number | No | - | system |
| 28 | `last_activity_at` | Last activity | datetime | timeline | date_range | No | - | system |
| 29 | `stage_entered_at` | Stage entered | datetime | timeline | date_range | No | - | system |
| 30 | `stage_updated_at` | Stage updated | datetime | timeline | date_range | No | - | system |
| 31 | `deal_type` | Deal type | string | process | select | Yes | `deal_type` | always |
| 32 | `priority` | Priority | string | process | select | Yes | `priority` | always |
| 33 | `lead_source` | Lead source | string | process | select | Yes | `lead_source` | always |
| 34 | `closed_reason` | Closed reason | string | process | long_text | No | - | lifecycle_action |
| 35 | `crm_url` | CRM link | url | process | link | No | - | system |
| 36 | `description` | Description | string | process | long_text | Yes | `description` | always |

## Editability Policy

Inline editing through `PATCH /users/crm/deals/{deal_id}` is allowed for:

`name`, `amount`, `currency`, `probability`, `forecast_category`, `source_created_at`, `expected_close_date`, `deal_type`, `priority`, `lead_source`, and `description`.

Dedicated actions:

- Owner: `POST /users/crm/deals/{deal_id}/owner`
- Stage/status: `POST /users/crm/deals/{deal_id}/stage`, `POST /users/crm/deals/{deal_id}/close`, `POST /users/crm/deals/{deal_id}/reopen`
- Account snapshot: `PATCH /users/crm/deals/{deal_id}/account`
- Primary contact snapshot: `PATCH /users/crm/deals/{deal_id}/primary-contact`
- Custom fields: `PATCH /users/crm/deals/{deal_id}` with `custom_columns`; render individual controls from `custom_column_definitions`. Formula custom fields return `edit_policy: "formula"` and are read-only.

Read-only/generated fields:

- AI health fields are generated from deal analysis.
- Top-level count metrics (`associated_contact_count`, `associated_company_count`, `note_count`) are system-managed response fields, not detail rows.
- Account profile fields (`company_industry`, `company_website`, `company_linkedin_url`, `company_country`) come from the linked account/company. To change them from a deal screen, use the deal account action, which updates/links the account and returns refreshed deal context.
- Stage timestamps are system-managed.
- `crm_url` is provider/system metadata.
- `active_deal_jobs` is an operational reconnect object, not a detail row.
