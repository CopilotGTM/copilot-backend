# Salesforce Opportunity Contact References as Stakeholders

## Problem

The stakeholder tools correctly read contact stakeholders from
`crm_deal_participants`. The missing-data bug is earlier in Salesforce ingestion:
we currently create those rows from `OpportunityContactRole`, but some organisations
store deal contacts in custom Opportunity lookup fields instead.

Exotel's HeroX opportunity demonstrates the gap:

- Salesforce Opportunity: `006T100000SHqq5IAD`
- `OpportunityContactRole` rows: none
- `Primary_Contact_Person__c`: `003S400000JVZIVIA5`
- `Primary_Budget_Owner__c`: `003S400000JVZIVIA5`
- The referenced contact already exists locally as Shekhar Gupta
- No matching `crm_deal_participants` row was created

This is not a stakeholder-tool query bug and should not be patched for Exotel alone.

## Production design

Use Salesforce's own field metadata as the contract. For every active Salesforce
deal-field definition:

1. Select fields whose metadata says `type=reference`.
2. Require `referenceTo` to contain `Contact`.
3. Ignore fields that Salesforce marks hidden or non-queryable.
4. Read populated Contact IDs from each Opportunity.
5. Deduplicate by `(deal, contact)` when multiple fields point to the same person.
6. Resolve the Contact through existing CRM source mappings.
7. Upsert through the existing `SalesforceDealParticipantWriter`.

No organisation-specific IDs, field names, configuration table, migration, or new
dependency is required. Organisations using only standard
`OpportunityContactRole` continue to work unchanged.

## Business mapping

Field names and labels provide bounded hints only:

- primary contact -> `is_primary_contact=true`
- decision maker, economic decision maker, executive sponsor -> `DECISION_MAKER`
- budget owner, economic buyer -> relationship role only; decision power remains
  `UNKNOWN` unless Salesforce supplies an explicit decision-maker signal
- technical buyer, evaluator -> `EVALUATOR`
- influencer -> `INFLUENCER`
- gatekeeper -> `GATEKEEPER`
- business user, end user -> `END_USER`
- champion -> `CHAMPION`
- supporter -> `SUPPORTER`
- detractor -> `DETRACTOR`
- blocker -> `BLOCKER`
- neutral -> `NEUTRAL`
- anything unrecognised -> `UNKNOWN`

The Salesforce role/field label is retained in `relationship_role` and exposed by
stakeholder reads, except primary-contact labels: primary status belongs only in
`is_primary_contact`, so a contact with no other relationship has a null role.
When several Contact lookup fields reference the same person, one deterministic
role is retained in this order: economic/budget authority, decision maker,
executive sponsor, technical evaluator, influencer, user, champion, primary
contact, then any other label. Standard
`OpportunityContactRole` data is applied after custom lookups and therefore wins
when Salesforce supplies an explicit role.

Salesforce can expose more than one primary-looking Contact field. All referenced
contacts remain participants and retain their own primary flags; the deal's
single primary-contact snapshot is selected deterministically by Salesforce API
field name so database row order cannot change the result between syncs.

Provider hints fill only `UNKNOWN` application fields. They never overwrite
stakeholder classifications edited in our application. CRM-owned relationship
roles can refresh from Salesforce, while manually owned roles and participant
ownership are preserved. A primary-contact flag never replaces a business role.
Every valid Contact lookup still becomes a stakeholder even when its label has no
known business meaning.

## Implementation steps

1. Discover Contact-reference Opportunity fields from mirrored Salesforce schema
   metadata in `crm_custom_field_definitions`.
2. Normalize and deduplicate populated references alongside standard contact roles.
3. Feed the resulting primary-contact and contact-count snapshots into deal
   normalization.
4. Persist participants during both:
   - full/page-based Salesforce deal sync
   - single-opportunity CDC persistence
5. Keep the existing participant writer's one-row-per-deal/contact identity and
   source mappings.
6. Run a normal Salesforce deal resync after deployment to backfill historical
   Opportunities. The upsert is idempotent, so no separate migration script is
   needed.

## Verification

Automated checks must prove:

- only Salesforce Contact-reference fields are discovered
- duplicate fields pointing to one Contact create one participant
- duplicate fields select the same canonical role deterministically
- primary-contact, decision-power, and stance mappings are bounded
- primary-contact markers never persist as relationship roles
- economic-buyer labels do not imply decision power
- unknown labels still create an `UNKNOWN` stakeholder
- existing non-`UNKNOWN` application classifications and manual roles are preserved
- stakeholder API/tool output includes `relationship_role`
- bulk sync and single-opportunity persistence both use the same extraction path
- existing `OpportunityContactRole` behavior remains green

Production rollout evidence after deployment:

1. Run Salesforce schema sync for the organisation if metadata is stale.
2. Run the standard Salesforce deal sync.
3. Verify HeroX has one contact participant for Shekhar Gupta.
4. Verify Mayank's stakeholder-list tool returns that participant.
5. Compare unresolved-contact counts and participant create/update counts before
   enabling the same release broadly.

## Rollback

Rollback is code-only. Existing participant rows are valid CRM-derived
relationships and should not be deleted automatically. If the feature is reverted,
standard contact-role ingestion and stakeholder reads continue unchanged.
