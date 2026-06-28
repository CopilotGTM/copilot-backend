# CRM Contact Detail Field Map

> Status: Draft for frontend approval
> Endpoint: `GET /users/crm/contacts/{contact_id}`
> Source of truth checked: `ContactDetail` in `src/api/schemas/crm_contact_models.py` and `CRMContact`.

This document defines the ordered frontend map for the contact detail "Get Details" section. The API returns this metadata in `detail_field_definitions`.

## Display Rules

- Main content should lead with `research_summary` when present.
- The right panel "Details" section should use the ordered fields below.
- Hide null/empty optional fields by default.
- Keep `deal_involvement` as its own related-deals section, not mixed into scalar contact details.
- Keep `deal_count` out of `detail_field_definitions`; the same response already includes `deal_involvement`, so the count should be shown as related-deals summary context.
- Do not add `pipeline_value` to contact detail metadata. Contact detail does not expose that value; pipeline value belongs to account/deal rollups.
- `profile_enrichment.data` is structured enrichment metadata. Do not flatten all keys into the primary panel unless a later product spec names those fields.
- Keep display/filter/edit metadata together. `filter_control` is not editability; the API also returns explicit `editable`, `patch_field`, `edit_control`, `edit_policy`, and `locked_reason` fields on each metadata object.

## Editability Rules

Returned metadata shape:

```json
{
  "key": "linkedin_url",
  "label": "LinkedIn",
  "data_type": "url",
  "order": 7,
  "section": "contact",
  "filter_control": "link",
  "editable": true,
  "patch_field": "linkedin_url",
  "edit_control": "url",
  "edit_policy": "set_if_empty",
  "locked_reason": "Existing LinkedIn URL is treated as verified profile identity."
}
```

Sales-rep policy:

- Let reps fix high-value gaps: missing name, title, phone, mobile phone, LinkedIn URL, and missing account link.
- Lock identity fields after set: email and account linkage are used for duplicate detection, stakeholder matching, and account routing.
- Treat existing LinkedIn URLs as verified identity links. Allow adding when missing; require a future explicit correction/override flow before replacing an existing value.
- Edit related account fields on the account record, not through contact detail.
- Keep research, enrichment, activity, owner, and system fields read-only until a dedicated workflow exists.

| API field | Editable in normal UI? | PATCH field | Edit policy | Notes |
|---|---|---|---|---|
| `full_name` | Yes | `full_name` | `always` | Primary display name can be corrected by a rep. |
| `first_name`, `last_name` | Not in metadata yet | `first_name`, `last_name` | `always` later | Not primary detail rows; add to metadata only if an expanded name editor needs them. |
| `job_title` | Yes | `job_title` | `always` | Common sales-rep correction. |
| `email` | Only when empty | `email` | `set_if_empty` | Backend already rejects changing an existing email. |
| `additional_emails` | No | none | `read_only` | Needs a dedicated secondary-email workflow. |
| `phone` | Yes | `phone` | `always` | Low-risk outreach field. |
| `mobile_phone` | Yes | `mobile_phone` | `always` | Low-risk outreach field. |
| `linkedin_url` | Only when empty | `linkedin_url` | `set_if_empty` | Backend rejects changing or clearing an existing LinkedIn URL. |
| `website` | No | none | `read_only` | Prefer account website. |
| `lifecycle_stage`, `lead_status`, `marketing_status` | No | none | `read_only` | Needs governed lifecycle/status workflows. |
| `company.id` | Only when no stored account link exists | `company_id` | `set_once` | Backend already rejects unlink/change after set. |
| `company.*` display fields | No | none | `related_entity` | Edit through account APIs. |
| `owner.*` | No | none | `read_only` | Requires owner assignment workflow. |
| `address`, `city`, `state`, `postal_code`, `country`, `timezone` | No | none in metadata | `read_only` | Backend can accept these if sent, but normal sales UI should not expose them yet. |
| `last_contacted_at`, `last_activity_at`, `source_created_at`, `source_modified_at` | No | none | `read_only` | Derived activity/sync timestamps. |
| `research_summary.*`, `profile_enrichment.*` | No | none | `read_only` | Generated/provider data; manual edits should go to first-class contact fields. |
| `deal_involvement[]` | No | none | `related_entity` | Manage through add-to-deal and stakeholder APIs. |

## Contact Detail Fields

| Order | API field | Label | Data type | Frontend control/filter | Source | Notes |
|---:|---|---|---|---|---|---|
| 1 | `full_name` | Name | string | text/search | `crm_contacts.full_name` | Primary title. |
| 2 | `job_title` | Job title | string | text/search | `crm_contacts.job_title` | Sales context. |
| 3 | `email` | Email | string email | email link/search | `crm_contacts.email` | Primary contact channel. |
| 4 | `additional_emails` | Additional emails | array<string email> | chips/search | `crm_contacts.additional_emails` | Hide when empty. |
| 5 | `phone` | Phone | string phone | phone link/search | `crm_contacts.phone` | Primary phone. |
| 6 | `mobile_phone` | Mobile | string phone | phone link/search | `crm_contacts.mobile_phone` | Mobile phone. |
| 7 | `linkedin_url` | LinkedIn | string URL | external link | `crm_contacts.linkedin_url` | Profile link. |
| 8 | `website` | Website | string URL | external link | `crm_contacts.website` | Contact-associated website. |
| 9 | `lifecycle_stage` | Lifecycle stage | string | select/text | `crm_contacts.lifecycle_stage` | CRM lifecycle. |
| 10 | `lead_status` | Lead status | string | select/text | `crm_contacts.lead_status` | Sales qualification. |
| 11 | `marketing_status` | Marketing status | string | select/text | `crm_contacts.marketing_status` | Email/marketing status. |
| 12 | `source` | Source | string | select/badge | sync mapping/default | Defaults to `crm_sync` in API schema. |

## Account And Owner Fields

| Order | API field | Label | Data type | Frontend control/filter | Source | Notes |
|---:|---|---|---|---|---|---|
| 13 | `company.id` | Account ID | UUID | link key | `crm_contacts.company_id` or domain match | Do not show ID as text. Use for account link. |
| 14 | `company.name` | Account | string | account link/search | joined `crm_companies.name` | Link when `company.id` is present. |
| 15 | `company.domain` | Account domain | string | domain/text | joined `crm_companies.domain` | Useful for account matching. |
| 16 | `company.website` | Account website | string URL | external link | joined `crm_companies.website` | Open externally. |
| 17 | `company.industry` | Account industry | string | select/text | joined `crm_companies.industry` | Account context. |
| 18 | `company.linkedin_url` | Account LinkedIn | string URL | external link | joined `crm_companies.linkedin_url` | Open externally. |
| 19 | `company.country` | Account country | string | select/text | joined `crm_companies.country` | Geography. |
| 20 | `owner.id` | Owner ID | UUID | link/filter key | `crm_contacts.owner_id` | Do not show ID as text. |
| 21 | `owner.name` | Owner | string | people picker/filter | `crm_contacts.owner_name` | Sales rep owner. |
| 22 | `owner.email` | Owner email | string email | email link/search | `crm_contacts.owner_email` | Owner contact. |

## Location And Activity Fields

| Order | API field | Label | Data type | Frontend control/filter | Source | Notes |
|---:|---|---|---|---|---|---|
| 23 | `address` | Address | string | text | `crm_contacts.address` | Street address. |
| 24 | `city` | City | string | select/text | `crm_contacts.city` | Location. |
| 25 | `state` | State | string | select/text | `crm_contacts.state` | Region. |
| 26 | `postal_code` | Postal code | string | text | `crm_contacts.postal_code` | Postal/ZIP. |
| 27 | `country` | Country | string | select/text | `crm_contacts.country` | Location. |
| 28 | `timezone` | Timezone | string | select/text | `crm_contacts.timezone` | Useful for outreach timing. |
| 29 | `last_contacted_at` | Last contacted | datetime | date range | `crm_contacts.last_contacted_at` | Last logged touch. |
| 30 | `last_activity_at` | Last activity | datetime | date range | `crm_contacts.last_activity_at` | Latest activity. |
| 31 | `source_created_at` | CRM created date | datetime | date range | `crm_contacts.source_created_at` | Provider creation time. |
| 32 | `source_modified_at` | CRM modified date | datetime | date range | `crm_contacts.source_modified_at` | Provider modified time. |

## Research And Enrichment Fields

| Order | API field | Label | Data type | Frontend control/filter | Source | Notes |
|---:|---|---|---|---|---|---|
| 33 | `research_summary.summary` | Research summary | string markdown/text | markdown | `crm_contacts.contact_research` | Main content, not just a small detail row. |
| 34 | `research_summary.last_researched_at` | Last researched | datetime | date range | `crm_contacts.last_researched_at` | Show with research summary. |
| 35 | `profile_enrichment.is_enriched` | Profile enriched | boolean | boolean/filter | derived from enrichment fields | Badge or status. |
| 36 | `profile_enrichment.source` | Enrichment source | string | select/text | `crm_contacts.profile_enrichment_source` | Provider/workflow name. |
| 37 | `profile_enrichment.enriched_at` | Enriched at | datetime | date range | `crm_contacts.profile_enriched_at` | Last enrichment time. |
| 38 | `profile_enrichment.data` | Enrichment data | object | structured advanced panel | `crm_contacts.profile_enrichment` | Keep collapsed unless product names specific fields. |

## Deal Involvement Section

`deal_involvement` should render after contact details as a related-deals section. It is not part of the scalar Get Details list.

| Field | Label | Data type | Source | Click/filter behavior |
|---|---|---|---|---|
| `deal_id` | Deal ID | UUID | `crm_deals.deal_id` | Open deal detail. |
| `deal_name` | Deal | string | `crm_deals.name` | Open deal detail. |
| `deal_status` | Status | enum string | `crm_deals.deal_status` | Filter by status. |
| `stage_name` | Stage | string | `crm_deals.stage_name` | Filter by stage. |
| `last_activity_at` | Last activity | datetime | `crm_deals.last_activity_at` | Sort/date filter. |
| `participant_id` | Participant ID | UUID | `crm_deal_participants.participant_id` | Open stakeholder detail for that deal when needed. |
| `relationship_role` | Role | string or null | `crm_deal_participants.relationship_role` | Filter/select. |

## System Fields

Keep these out of the primary detail section:

| API field | Data type | Reason |
|---|---|---|
| `contact_id` | UUID | Route/link identifier. |
| `first_name`, `last_name` | string | Use `full_name` for display unless editing individual names. |
| `created_at`, `updated_at`, `synced_at` | datetime | Internal/sync metadata. |
| `deal_count` | integer | Summary metric derived from `deal_involvement`; show near the related-deals header, not as a scalar detail row. |
| `pipeline_value` | currency | Not returned by contact detail. Use account/deal rollups when pipeline value is needed. |
