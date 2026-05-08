# CRM Deal Participant Normalization - Design

**Status:** Proposed - hardened after implementation-risk review
**Context:** HubSpot initial sync now materializes deal participants and generic
associations. Salesforce deal sync already has partial participant extraction
for opportunity contact roles and opportunity account links. This design locks
the cross-provider boundary before adding more Salesforce and HubSpot webhook
coverage.

## Summary

`crm_deal_participants` is the app-facing stakeholder and team roster for a
deal. It should contain only CRM entities that can reasonably participate in a
deal conversation or deal team:

- `contact`
- `company`
- `user`

Everything else is a provider relationship edge, not a participant. Line items,
notes, tasks, emails, meetings, calls, products, quotes, tickets, custom object
links, and activity links belong in `crm_object_associations`.

This keeps the application from having to special-case every CRM provider while
still preserving the full provider graph.

## Current State

The repo already has the right two-table shape:

- `crm_deal_participants` stores deal roster rows with `participant_type`,
  `relationship_role`, related external/local IDs, display name/email, and raw
  payload.
- `crm_object_associations` stores a generic object graph with source object,
  target object, provider association type, label, role, primary flag, and raw
  payload.

Current HubSpot deal sync emits:

- Contact participants from `associations.contacts.results`.
- Company participants from `associations.companies.results`.
- User participants from `hs_all_collaborator_owner_ids` and
  `hs_all_deal_split_owner_ids`.
- Generic association rows for contact, company, and line item edges.

Current Salesforce deal sync emits:

- Contact participants from `OpportunityContactRole`.
- Company participants from `Opportunity.AccountId`.
- Generic association rows for those same opportunity-contact and
  opportunity-account relationships.

The gap is that provider-specific role strings are stored directly in
`relationship_role`, so consumers must know HubSpot strings like
`deal_to_company_unlabeled` and Salesforce objects like
`OpportunityContactRole`. Also, Salesforce `OpportunityTeamMember` is not yet
materialized as `user` participants.

## Provider Research

HubSpot associations are a generic CRM graph. Official HubSpot docs define deal
association types to contacts, companies, tickets, calls, emails, meetings,
notes, tasks, communications, line items, invoices, orders, payments, quotes,
subscriptions, appointments, courses, listings, services, feedback submissions,
cart/project-style commerce or workspace objects as those APIs evolve, and
custom labels. HubSpot also returns both unlabeled/default and primary
association types for company links; a record can have an unlabeled company
association and a primary company association. HubSpot's official deal
association table does not define a default "primary contact" association in the
same way it defines primary company, so HubSpot contact primary status must only
be set when the actual payload exposes a primary/custom label we explicitly
recognize.

Salesforce has several separate relationship concepts:

- `OpportunityContactRole` links contacts to opportunities with `Role` and
  `IsPrimary`.
- `Opportunity.AccountId` links the opportunity to its account/company.
- `OpportunityTeamMember` links users to opportunities with `TeamMemberRole`
  and opportunity access.
- `Task.WhoId` / `WhatId` and similar activity references are activity edges,
  not deal participants.

Sources:

- HubSpot Associations v4:
  https://developers.hubspot.com/docs/guides/api/crm/associations/associations-v4
- HubSpot Associations overview:
  https://developers.hubspot.com/docs/api-reference/latest/crm/associations/overview
- Salesforce Opportunity Contact Role mapping:
  https://developer.salesforce.com/docs/data/data-cloud-dmo-mapping/guide/c360dm-crm-opportunity-contact-role.html
- Salesforce Opportunity Teams:
  https://help.salesforce.com/s/articleView?id=sales.teamselling.htm&language=en_US&type=5

## Decision

Keep participants narrow and normalize roles.

### Participant Boundary

`crm_deal_participants` should only contain:

| Participant type | Meaning |
| --- | --- |
| `contact` | External person involved in the deal |
| `company` | Organization/account involved in the deal |
| `user` | Internal or partner CRM user collaborating on the deal |

Do not write non-roster objects to `crm_deal_participants`. Preserve them in
`crm_object_associations`.

### Role Boundary

Add normalized role fields instead of replacing provider roles:

- `canonical_relationship_role`: app-facing role used by CopilotGTM logic.
- `participant_role_key`: stable identity key for participant de-duplication.
- `is_primary`: boolean primary marker.

Keep the existing `relationship_role` as the provider/source role for debugging,
replay, and provider-specific UI.

`participant_role_key` is required because the current table and writers key
rows by provider `relationship_role`. That is incompatible with collapsing
provider-specific labels like `deal_to_company` and `deal_to_company_unlabeled`
into one app-facing participant while still retaining provider labels elsewhere.
The role key is the identity role for the participant row:

- For standard/default roles, it equals the canonical role
  (`associated_company`, `primary_company`, etc.).
- For provider custom labels, it includes the provider label/type so two
  distinct custom roles on the same contact do not overwrite each other.
- For unmapped roles, it is `other:<provider-role-or-type-id>`.

Role keys must be deterministic and length-safe. If a provider label/type would
exceed the column size, store a compact key with a stable hash suffix while
keeping the full provider value in `relationship_role` and
`crm_object_associations.data_payload`.

Canonical roles:

| Canonical role | Meaning |
| --- | --- |
| `primary_contact` | Main external person for the deal |
| `associated_contact` | External contact linked without a stronger normalized role |
| `primary_company` | Main company/account for the deal |
| `associated_company` | Company linked without a stronger normalized role |
| `internal_collaborator` | Internal user collaborating on the deal |
| `revenue_split` | User with split/revenue credit |
| `opportunity_team_member` | Salesforce opportunity team user |
| `custom_contact_role` | Provider/custom contact role preserved in `relationship_role` |
| `custom_company_role` | Provider/custom company role preserved in `relationship_role` |
| `other` | Known participant type but unmapped role |

## Mapping Rules

### HubSpot

| Source | Participant type | Canonical role | Primary |
| --- | --- | --- | --- |
| explicitly recognized HubSpot contact primary label/type, if present | `contact` | `primary_contact` | true |
| `deal_to_contact` | `contact` | `associated_contact` | false |
| custom contact association labels | `contact` | `custom_contact_role` | false unless HubSpot marks primary |
| `deal_to_company_primary` | `company` | `primary_company` | true |
| `deal_to_company` / `deal_to_company_unlabeled` | `company` | `associated_company` | false |
| custom company association labels | `company` | `custom_company_role` | false unless HubSpot marks primary |
| `hs_all_collaborator_owner_ids` | `user` | `internal_collaborator` | false |
| `hs_all_deal_split_owner_ids` | `user` | `revenue_split` | false |

When HubSpot returns multiple default/unlabeled labels for the same
deal/company or deal/contact, `crm_deal_participants` should collapse duplicate
app-level rows by `(deal, participant_type, related_external_id,
participant_role_key)`. `crm_object_associations` should still preserve each
provider edge. The mapper must support both the existing inline `type` string
shape and association v4-style fields such as `associationCategory`,
`associationTypeId`, and `label`, because webhook and full-sync payloads can
arrive through different provider surfaces.

### Salesforce

| Source | Participant type | Canonical role | Primary |
| --- | --- | --- | --- |
| `OpportunityContactRole.Role` present | `contact` | `custom_contact_role` | value of `IsPrimary` |
| `OpportunityContactRole.IsPrimary=true` and no role | `contact` | `primary_contact` | true |
| `OpportunityContactRole` without role | `contact` | `associated_contact` | value of `IsPrimary` |
| `Opportunity.AccountId` | `company` | `primary_company` | true |
| `OpportunityTeamMember` | `user` | `opportunity_team_member` | false |

For `OpportunityTeamMember`, preserve `TeamMemberRole` in `relationship_role`
and the raw row in `data_payload`. Fetch `Id`, `OpportunityId`, `UserId`,
`TeamMemberRole`, `OpportunityAccessLevel`, and `LastModifiedDate` when the
object is queryable. If the org does not expose Opportunity Teams or the object
is not queryable for the connected user, skip only the team-member slice with a
typed warning/result count; do not fail the entire deal sync.

Primary contact is represented by `is_primary=true`. Do not emit a second
synthetic `primary_contact` participant for the same
`OpportunityContactRole.Role` row. That would create duplicate contact
participants and make stakeholder sync ambiguous.

## Confidence Review Loop

The first pass was not deployment-safe. These vulnerabilities must be fixed
before implementation:

1. **Participant identity drift.** Existing uniqueness and synthetic
   `external_id` values use provider `relationship_role`, but the new behavior
   collapses by canonical app role.
   Fix: add `participant_role_key`, migrate/rewrite synthetic IDs, and de-dupe
   existing rows before adding the new uniqueness contract.

2. **Nullable canonical role weakens the API contract.** A nullable
   `canonical_relationship_role` forces every consumer to keep fallback logic
   forever.
   Fix: add with a temporary default, backfill, then enforce `NOT NULL` with
   server default `other` or a mapped canonical role.

3. **Custom label overwrite risk.** Collapsing only by canonical role would
   merge distinct custom roles for the same person.
   Fix: custom and unmapped roles include provider label/type in
   `participant_role_key`; default/unlabeled provider variants collapse to one
   app role.

4. **HubSpot primary-contact assumption.** Official HubSpot deal associations
   define primary company, but not a standard primary contact in the same shape.
   Fix: map HubSpot contacts to `associated_contact` unless the concrete
   payload has an explicitly recognized primary/custom label.

5. **Salesforce contact-role ambiguity.** The original table allowed both
   `IsPrimary=true` and `Role` present to create separate participant rows.
   Fix: one row per source contact-role edge; preserve the role in
   `relationship_role`, set `canonical_relationship_role=custom_contact_role`,
   and set `is_primary=true`.

6. **Opportunity Team availability.** `OpportunityTeamMember` can be disabled or
   inaccessible by permissions.
   Fix: add describe/queryability gating and partial-result accounting.

7. **Webhook payload parity.** Webhook payloads may not include enough
   association detail to update participants in place.
   Fix: association-change webhooks enqueue a deal-level reconciliation/full
   association refresh using the same normalizer and writer path as initial
   sync.

8. **Backfill duplicates can break deployment.** Existing rows can already
   contain both `deal_to_company` and `deal_to_company_unlabeled`.
   Fix: migration backfills all rows, chooses one survivor per new identity key,
   merges safest display/raw fields, soft-deletes duplicates, then creates the
   new partial unique index.

9. **Nullable related IDs bypass uniqueness.** PostgreSQL unique indexes treat
   `NULL` values as distinct, and this table allows internal-only participant
   rows with no `related_external_id`.
   Fix: provider-synced participant uniqueness applies only to active rows with
   `related_external_id IS NOT NULL`; internal-only rows need a separate future
   contract using `related_local_id` if that surface is expanded. Do not rely on
   a nullable unique key to enforce internal-only de-duplication.

## Implementation Plan

1. Add a migration for `crm_deal_participants`:
   - `canonical_relationship_role VARCHAR(64) NOT NULL DEFAULT 'other'`
   - `participant_role_key VARCHAR(128) NOT NULL DEFAULT 'other'`
   - `is_primary BOOLEAN NOT NULL DEFAULT false`
   - Backfill existing rows from `relationship_role` using the mapping rules
     above.
   - Rewrite synthetic `external_id` values from
     `{deal_external_id}:{participant_type}:{related_external_id}:{relationship_role}`
     to
     `{deal_external_id}:{participant_type}:{related_external_id}:{participant_role_key}`
     for provider-synced rows where the old shape is detected.
   - Soft-delete duplicate active rows after backfill by the new identity:
     `(deal_id, participant_type, related_external_id, participant_role_key)`,
     scoped to active provider-synced rows where `related_external_id IS NOT
     NULL`.
   - Replace or supplement the old uniqueness contract with a partial unique
     index on active rows for
     `(deal_id, participant_type, related_external_id, participant_role_key)`
     where `related_external_id IS NOT NULL`.
   - Keep existing `CRMParticipantType` enum values for backward compatibility;
     this change stops new non-roster writes but does not remove historical enum
     values in the same deployment.

2. Add a shared normalizer helper for participant role mapping:
   - Inputs: provider, participant type, source role/type string, raw payload.
   - Outputs: `canonical_relationship_role`, `participant_role_key`,
     `is_primary`.
   - Keep provider-specific mapping tables close to CRM record sync code, not in
     API handlers.
   - Make the helper pure and cover both HubSpot inline association strings and
     v4-style association metadata.

3. Update HubSpot deal participant extraction:
   - Populate canonical role and primary flag.
   - Collapse duplicate app-level participants caused by
     `deal_to_company` + `deal_to_company_unlabeled`.
   - Keep all raw association rows in `crm_object_associations`.

4. Update Salesforce deal sync:
   - Add `OpportunityTeamMember` fetcher.
   - Gate it with describe/queryability checks and partial-result accounting.
   - Emit user participants for opportunity team members.
   - Preserve `TeamMemberRole` as `relationship_role`.
   - Persist canonical role, participant role key, and primary flag.
   - Do not emit a separate synthetic primary row when a primary
     `OpportunityContactRole` already has a provider role.

5. Update participant writers:
   - Write the new fields on create and update.
   - Generate synthetic external IDs with `participant_role_key`, not provider
     `relationship_role`.
   - Load/update existing rows by the new external ID and by the new active-row
     identity so migrated rows remain idempotent.

6. Update app-facing reads:
   - Consumers should use `participant_type` plus
     `canonical_relationship_role` for cross-provider behavior.
   - Use `relationship_role` only for provider-specific display/debugging.
   - Update `src/api/schemas/crm_participant_models.py` and
     `src/api/routers/crm/deal_participants_handler.py` so API consumers can see
     canonical role and primary status.

7. Add webhook reconciliation:
   - HubSpot association or relevant deal property webhooks should enqueue a
     deal-level participant/association reconciliation task.
   - The webhook path must call the same normalizer and writer logic used by
     initial sync; do not create a parallel direct-update path.
   - Webhook events remain idempotent by provider event ID plus the normalized
     participant identity.

## Test Plan

- HubSpot: a company with both `deal_to_company` and
  `deal_to_company_unlabeled` creates one `associated_company` participant and
  two generic association rows.
- HubSpot: primary company sets `is_primary=true` and
  `canonical_relationship_role=primary_company`.
- HubSpot: contacts default to `associated_contact` unless a real payload
  fixture contains an explicitly recognized primary contact label/type.
- HubSpot: collaborator and split-owner properties become `user` participants
  with `internal_collaborator` and `revenue_split`.
- Salesforce: `OpportunityContactRole` with `Role="Decision Maker"` and
  `IsPrimary=true` creates one `custom_contact_role` participant with
  `is_primary=true`, not two rows.
- Salesforce: `OpportunityContactRole` with `IsPrimary=true` and no role
  creates a `primary_contact` participant.
- Salesforce: `OpportunityContactRole.Role="Decision Maker"` creates or updates
  a `custom_contact_role` participant while preserving `relationship_role`.
- Salesforce: `Opportunity.AccountId` creates a `primary_company` participant.
- Salesforce: `OpportunityTeamMember` creates `user` participants with
  `opportunity_team_member`.
- Salesforce: `OpportunityTeamMember` unavailable/query denied records a
  partial skip and does not fail the deal sync.
- Regression: stakeholder sync still processes only contact participants.
- Regression: meeting and email matchers still use user participants as internal
  team emails.
- Migration: existing rows are backfilled without deleting
  `relationship_role` or raw payloads.
- Migration: duplicate default/unlabeled rows are soft-deleted or collapsed
  before the new unique index is created.
- Writer idempotency: re-running sync after migration updates the migrated row
  instead of creating a second row with the new synthetic external ID.
- Webhook: an association-change event triggers reconciliation and produces the
  same rows as initial sync for the same provider payload.

## Assumptions

- `crm_deal_participants` is the app-facing deal roster, not the complete CRM
  graph.
- `crm_object_associations` remains the complete provider relationship graph.
- Deal owner stays on `crm_deals.owner_id` and owner columns. Do not duplicate
  the owner into participants unless the provider also exposes the user as a
  collaborator/team member.
- Provider-specific labels are valuable and must be retained even when the app
  uses canonical roles.
