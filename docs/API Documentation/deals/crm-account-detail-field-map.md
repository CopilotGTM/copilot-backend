# CRM Account Detail Field Map

> Status: Implemented in API response metadata
> Endpoint: `GET /users/crm/accounts/{company_id}`
> Source of truth checked: `AccountDetail` in `src/api/schemas/crm_account_models.py` and `CRMCompany`.

This document defines the ordered frontend map for the account detail "Get Details" section. The API returns this metadata in `detail_field_definitions`; the frontend should use the returned `order`, `editable`, `patch_field`, `edit_control`, and `edit_policy` values instead of hardcoding edit behavior.

## Display Rules

- Main content should use `account_summary` when available.
- The right panel should show account details first, then compact contacts, then compact deals.
- Hide null/empty optional scalar fields by default.
- Do not mix contact/deal arrays into the scalar account detail rows.
- Account detail contacts and deals intentionally use compact shapes. Open the contact or deal detail page for deeper context.
- Inline editing should only be enabled when `detail_field_definitions[].editable` is `true`.
- Owner fields are not inline editable; account owner reassignment should be a dedicated action that validates an org user and updates owner ID/name/email together.

## Account Detail Fields

| Order | API field | Label | Data type | Frontend control/filter | Editable | Patch field | Edit policy | Source | Notes |
|---:|---|---|---|---|---|---|---|---|---|
| 1 | `name` | Account name | string | text/search | Yes | `name` | always | `crm_companies.name` | Primary title. |
| 2 | `domain` | Domain | string | domain/text | Yes | `domain` | always | `crm_companies.domain` | Normalised on PATCH; duplicate active domains return `409`. |
| 3 | `website` | Website | string URL | external link | Yes | `website` | always | `crm_companies.website` | If `domain` is omitted, PATCH derives domain from website when possible. |
| 4 | `industry` | Industry | string | select/text | Yes | `industry` | always | `crm_companies.industry` | Sales segmentation. |
| 5 | `lifecycle_stage` | Lifecycle stage | string | select/text | Yes | `lifecycle_stage` | always | `crm_companies.lifecycle_stage` | Account lifecycle. |
| 6 | `company_type` | Company type | string | select/text | Yes | `company_type` | always | `crm_companies.company_type` | Segment/type. |
| 7 | `description` | Description | string | long text | Yes | `description` | always | `crm_companies.description` | Account notes/description. |
| 8 | `is_active` | Active | boolean | boolean/badge | No | — | read_only | `crm_companies.is_active` | Provider/internal active flag. |

## Owner And Business Metrics

| Order | API field | Label | Data type | Frontend control/filter | Editable | Patch field | Edit policy | Source | Notes |
|---:|---|---|---|---|---|---|---|---|---|
| 9 | `owner.id` | Owner ID | UUID | link/filter key | No | — | assignment_action | `crm_companies.owner_id` | Do not show ID as text. |
| 10 | `owner.name` | Owner | string | people picker/filter | No | — | assignment_action | `crm_companies.owner_name` | Also available as legacy `owner_name`. |
| 11 | `owner.email` | Owner email | string email | email link/search | No | — | assignment_action | `crm_companies.owner_email` | Owner contact. |
| 12 | `number_of_employees` | Employees | integer | number/range | Yes | `number_of_employees` | always | `crm_companies.number_of_employees` | Firmographic. |
| 13 | `annual_revenue` | Annual revenue | number | currency/number range | Yes | `annual_revenue` | always | `crm_companies.annual_revenue` | No currency code is currently included in account detail. |

`contact_count`, `deal_count`, and `pipeline_value` stay as top-level account summary values in the detail response, but they are not Get Details rows. Use the compact `contacts` and `deals` arrays in the same response for the account relationship sections.

## Contact, Location, And Social Fields

| Order | API field | Label | Data type | Frontend control/filter | Editable | Patch field | Edit policy | Source | Notes |
|---:|---|---|---|---|---|---|---|---|---|
| 14 | `phone` | Phone | string phone | phone link/search | Yes | `phone` | always | `crm_companies.phone` | Company phone. |
| 15 | `linkedin_url` | LinkedIn | string URL | external link | Yes | `linkedin_url` | always | `crm_companies.linkedin_url` | Company page. |
| 16 | `address` | Address | string | text | Yes | `address` | always | `crm_companies.address` | Street address. |
| 17 | `city` | City | string | select/text | Yes | `city` | always | `crm_companies.city` | Location. |
| 18 | `state` | State | string | select/text | Yes | `state` | always | `crm_companies.state` | Region. |
| 19 | `postal_code` | Postal code | string | text | Yes | `postal_code` | always | `crm_companies.postal_code` | Postal/ZIP. |
| 20 | `country` | Country | string | select/text | Yes | `country` | always | `crm_companies.country` | Geography. |
| 21 | `timezone` | Timezone | string | select/text | Yes | `timezone` | always | `crm_companies.timezone` | Useful for outreach timing. |

## Account Summary

| Order | API field | Label | Data type | Frontend control/filter | Editable | Patch field | Edit policy | Source | Notes |
|---:|---|---|---|---|---|---|---|---|---|
| 22 | `account_summary.status` | Summary status | enum string | status/badge | No | — | generated | account research | Currently `not_available` unless research storage is wired. |
| 23 | `account_summary.content` | Account summary | string markdown/text or null | markdown | No | — | generated | account research | Main content section. |
| 24 | `account_summary.last_researched_at` | Last researched | datetime or null | date range | No | — | generated | account research | Show with summary. |

## Contacts Section

`contacts` should render as a compact related-contacts section after scalar account details.

| Field | Label | Data type | Source | Click/filter behavior |
|---|---|---|---|---|
| `contact_id` | Contact ID | UUID | `crm_contacts.contact_id` | Open contact detail. |
| `full_name` | Name | string or null | `crm_contacts.full_name` | Open contact detail. |
| `email` | Email | string email or null | `crm_contacts.email` | Mailto and search. |
| `job_title` | Job title | string or null | `crm_contacts.job_title` | Display/search. |

Do not add `deal_count` to account-detail contacts. That belongs to contact detail.

## Deals Section

`deals` should render as a compact related-opportunities section after contacts.

| Field | Label | Data type | Source | Click/filter behavior |
|---|---|---|---|---|
| `deal_id` | Deal ID | UUID | `crm_deals.deal_id` | Open deal detail. |
| `name` | Deal | string or null | `crm_deals.name` | Open deal detail. |
| `amount` | Amount | number or null | `crm_deals.amount` | Currency/number display. |
| `currency` | Currency | string or null | `crm_deals.currency` | Currency code. |
| `deal_status` | Status | enum string or null | `crm_deals.deal_status` | Filter by status. |
| `stage_name` | Stage | string or null | `crm_deals.stage_name` | Filter by stage. |
| `owner_name` | Owner | string or null | `crm_deals.owner_name` | Display/search. |

## System Fields

Keep these out of the primary detail rows:

| API field | Data type | Reason |
|---|---|---|
| `company_id` | UUID | Route/link identifier. |
| `owner_name` | string | Legacy convenience field; prefer nested `owner.name`. |
| `created_at`, `updated_at` | datetime | System timestamps. Use only in an advanced section if needed. |
