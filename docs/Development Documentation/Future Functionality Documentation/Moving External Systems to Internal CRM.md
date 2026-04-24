# Moving External Systems to Internal CRM

## Purpose
This document captures the working rules for moving HubSpot-driven deal data into our internal CRM model without letting HubSpot naming or HubSpot-only behavior become the product contract.

The goal is simple:
- sync everything we need from HubSpot
- store provider data safely
- expose only the fields that matter for deals
- progressively make the internal CRM the source of truth

## Current Live HubSpot Findings
For organisation `3ed09720-971e-4fb1-a1d9-bd5e57b432f1` and integration `d2a1b7a7-60f6-4273-9717-ace90b6475cb`:

- `204` HubSpot deal properties were fetched
- `18` were treated as canonical at sync time
- `186` were persisted as provider-backed deal fields
- `0` of the `186` are HubSpot-custom; all are standard HubSpot fields
- `28` of the `186` are user-editable in HubSpot
- `158` of the `186` are HubSpot-driven or read-only

This means most of the deal schema HubSpot gives us is provider operational metadata, calculated values, stage analytics, team/source internals, or other fields we should not expose by default.

## What We Already Decided
These HubSpot fields now map to existing canonical deal columns:

- `num_associated_contacts -> associated_contact_count`
- `num_notes -> note_count`
- `closed_won_reason -> closed_reason`
- `closed_lost_reason -> closed_reason`

These HubSpot fields are now explicitly ignored for schema-sync exposure:

- `closed_won_date`
- `closed_lost_date`

These fields were reviewed but are not canonical yet:

- `hs_next_step`
- `notes_next_activity_date`
- `notes_last_contacted`
- `hs_acv`
- `hs_arr`
- `hs_mrr`
- `hs_tcv`

## Locked HubSpot Deal Field Policy
This is the locked policy for the current HubSpot deal integration until we deliberately revise it.

### Bucket 1: Canonical Deal Fields
These fields belong in `CRMDeal` and should not appear as deal custom values:

- `closed_won_reason -> closed_reason`
- `closed_lost_reason -> closed_reason`
- `num_associated_contacts -> associated_contact_count`
- `num_notes -> note_count`

### Bucket 2: Explicitly Ignored
These fields should be ignored from canonical mapping and custom-field exposure:

- `closed_won_date`
- `closed_lost_date`

### Bucket 3: Approved HubSpot Custom-Value Fields
These are non-canonical fields that survive stage-1 filtering and are approved to be stored in deal custom values:

- `hs_next_step -> next_step`

### Bucket 4: HubSpot Provider-Custom Fields
Any HubSpot field that the provider itself marks as custom should be stored in deal custom values automatically.

Rule:
- if `is_provider_custom = true`, keep it in `CRMDeal.properties`
- keep using an internal key, not the raw provider key, for product/UI exposure
- this rule is additive to the explicit allowlist for standard HubSpot fields

Current live note:
- for the current integration, `0` deal fields are marked provider-custom by HubSpot
- so the live approved custom-value list is still only `next_step`

### Stage-1 Mechanical Filters
For HubSpot deal fields, remove any field that is:

- canonical
- explicitly ignored
- provider-custom fields are exempt from the remaining suppressions and should be kept
- hidden in HubSpot
- read-only in HubSpot
- provider-noise (analytics, team/share metadata, line-item config, source internals, forecast mechanics, provider operational fields)

### Current Live Result
For the live HubSpot deal registry (`186` provider-backed fields), stage-1 filtering leaves exactly one approved deal custom-value field:

- `next_step`

That means the current product-facing HubSpot deal model is:

- canonical values from `CRMDeal`
- one approved extra custom value: `next_step`
- everything else stored raw but hidden

### Internal Naming Rule
Even when a field originates from HubSpot, product naming must stay internal:

- store/show `next_step`
- do not expose `hs_next_step`

The same rule applies to any future promoted field: provider key remains adapter metadata, internal key remains the product contract.

## What Should Be Read In Product
Deal details should be built from two layers only:

1. Canonical `CRMDeal` columns
2. A filtered provider-backed custom-field layer

The app should not read directly from raw HubSpot property names.

The canonical deal table should continue to hold:
- core deal identity and value
- sales process state
- close information
- ownership
- primary company/contact snapshot
- core activity timestamps
- counts and business-safe metrics we intentionally promote

## What Should Stay Stored But Hidden
We should keep raw provider-backed fields for audit, replay, and future reconsideration, but not expose them in normal frontend flows unless explicitly allowed.

These categories should stay hidden for deals:

- HubSpot object IDs and source internals
- merged IDs and system flags
- team IDs, owner/team sharing internals, business-unit IDs
- analytics/source drilldowns
- stage timing mechanics such as `hs_v2_*`
- line-item recurring billing configuration fields
- predictive or provider-calculated telemetry
- marketing attribution details that are not part of day-to-day deal work

Examples:
- `hubspot_team_id`
- `hs_object_source`
- `hs_analytics_*`
- `hs_v2_*`
- `hs_line_item_*`
- `hs_deal_score`
- `hubspot_owner_assigneddate`

## What Should Still Be Debated
The only remaining deal fields worth serious product discussion are:

- `hs_next_step`
- `notes_next_activity_date`
- `notes_last_contacted`
- recurring-revenue bundle only if product direction needs it:
  - `hs_acv`
  - `hs_arr`
  - `hs_mrr`
  - `hs_tcv`

Current recommendation:
- do not promote them automatically
- expose only if they are broadly useful for deal workflows across teams

## Hidden and Editable in HubSpot
HubSpot gives two different signals that matter:

- `readOnlyValue`
  - `false` means users can edit the value in HubSpot
  - `true` means HubSpot controls it or treats it as read-only
- `hidden`
  - `true` means HubSpot hides it from normal/default UI exposure
  - it does not necessarily mean read-only
  - it does not necessarily mean unusable through API

So a field can be editable and still hidden. For our product, `hidden=true` should be a strong signal to keep it out of the frontend unless we deliberately expose it.

## Service-Layer Exposure Rule
We should not show all provider-backed fields.

Best rule:
- store all synced provider fields
- show only canonical fields plus an explicit allowlist of provider-backed custom fields

Default behavior for provider-backed fields:
- hidden unless explicitly allowed

Good candidates for exposure:
- editable
- not hidden by HubSpot
- understandable by a sales rep
- important for deal execution
- not provider-internal or ID-like

Bad candidates for exposure:
- read-only operational metadata
- IDs, team/user/source internals
- analytics drilldowns
- line-item config
- provider-only workflow or AI internals

## Internal Vocabulary Rule
Product, API, UI, filters, and exports must use internal names, not HubSpot names.

That means:
- provider keys stay adapter metadata only
- promoted fields keep the same internal meaning even if storage changes
- we do not let `hs_*` names leak into the product contract

Important note: forms are not part of the current deal-model scope. A field such as `hs_form_id` may exist in HubSpot, but for our current deal-focused CRM it should remain suppressed unless the product later decides it has real deal-level business value.

## Ten-Point Operating Summary
1. Keep syncing and storing all provider fields raw for audit/replay, but do not expose all of them to the product.
2. Read deal details from two layers only: canonical `CRMDeal` columns first, plus a filtered custom-field layer second.
3. Do not use “all custom fields”; use an `exposed` service-layer allowlist per org/integration.
4. Default rule for provider-backed fields: `hidden unless explicitly allowed`.
5. Auto-hide anything that is read-only, hidden by HubSpot, ID-like, team/user/internal-source metadata, analytics drilldowns, line-item config, or provider operational fields.
6. Consider exposing only editable, non-hidden, business-meaningful fields that a sales rep would actually understand and update.
7. Good exposed custom examples: `hs_next_step`; maybe a few revenue/business fields if the team actually uses them.
8. Good hidden examples: `hubspot_team_id`, object/source IDs, owner/team IDs, merged IDs, analytics/source internals, calculated rollups we do not act on.
9. Make this org-specific: each org gets the same raw sync, but its own exposure rules for which custom fields appear in frontend.
10. So the best model is: `store everything`, `show very little`, `canonical first`, `custom only by explicit exposure policy`.

## Recommended Next Steps
1. Keep the current canonical mappings already added.
2. Add a service-layer suppressed-field set for HubSpot deals.
3. Add a service-layer exposed-custom allowlist for deal UI.
4. Decide the remaining short list:
   - `hs_next_step`
   - `notes_next_activity_date`
   - `notes_last_contacted`
   - revenue bundle only if product direction requires it
5. Keep everything else provider-backed and hidden by default.
