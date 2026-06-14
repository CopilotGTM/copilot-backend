# Deal Participant Dedupe Production Plan

**Goal:** unblock the participant-backed stakeholder rollout without adding a second repair
migration or embedding large dedupe SQL in Alembic.

## Current Decision

Use one org migration only:

```text
060 = schema change for participant-backed stakeholder fields
```

Do not add `061` for participant dedupe repair.
Do not add large Salesforce-specific dedupe SQL to the migration file.
Do not create a blanket active-contact email unique index.
Do not create a local-id unique index until the data is clean and the product contract needs it.

Existing participant duplicate cleanup happens through the controlled data migration flow after
schema migration is corrected and rerun.

## Why This Is Simpler

The failed prod data has two separate facts:

- true duplicate participants by `deal_id + related_local_id`
- valid email collisions where different contacts share generic emails such as
  `info@salesforce.com`

Alembic should not be responsible for deciding which participant row to keep, repointing source
mappings, and soft-deleting duplicates. The data migration/backfill handles only duplicates that
share the same resolved contact ID on the same deal. Email-only collisions are not deleted.

## Migration Shape

Migration `060` should only:

1. Add the new participant/contact columns.
2. Add the non-unique lookup index used by participant-backed stakeholder reads.
3. Update stakeholder-linked job/insight references to participant IDs where that is part of the
   stakeholder-to-participant cutover.
4. Avoid uniqueness rules that can fail on existing dirty participant data.

Migration `060` should not:

1. Merge participant rows.
2. Repoint `crm_source_object_mappings`.
3. Soft-delete duplicates.
4. Create `ux_crm_deal_participant_active_contact_email`.
5. Create `ux_crm_deal_participant_active_contact_local_id`.

## Runtime Fix

The runtime writer fix remains required:

1. Salesforce participant identity ignores `relationship_role`.
2. Same deal/contact writes to one participant row.
3. `primary_contact` updates `crm_deal_participants.is_primary_contact`.
4. Non-primary Salesforce role labels do not become app-owned participant classification.
5. Internal/manual participant writes reuse the active resolved deal/contact participant.

This prevents the duplicate rows from being recreated after the data migration.

## Deployment Runbook

1. Deploy the corrected code and corrected `060`.
2. Downgrade org schemas that already applied the bad `060` back to revision `059`
   (`3c7f79637740`) using the org migration runner.
3. Rerun org migrations so every org applies the corrected `060`.
4. Run the participant/stakeholder data migration:

```bash
uv run python -m scripts.backfill_participant_stakeholder_fields --all --apply
```

This copies stakeholder fields, repoints source mappings from duplicate participant IDs to the
selected canonical participant, and soft-deletes duplicate active contact participants only when
they share `organisation_id + deal_id + related_local_id`.

It also backfills `crm_deal_participants.is_primary_contact` from two sources:

1. Explicit `crm_deals.primary_contact_id`.
2. Deals with exactly one active CRM-sourced contact participant and no primary contact already.

5. Run the duplicate audit:

```bash
uv run python -m scripts.audit_duplicate_deal_contact_participants --all --fail-on-duplicates
```

Expected after data migration:

```text
no active duplicate participant groups by organisation_id + deal_id + related_local_id
valid email collisions may remain and are reported without failing --fail-on-duplicates
```

6. Run focused API/service verification.
7. Rerun the startup/Helm flow and confirm the hook completes.

## Verification

Local focused checks:

```bash
uv run pytest tests/service/integrations/crm/records/salesforce/deals/test_deal_child_source_mappings.py tests/service/crm/test_deal_participant_service_internal_ids.py tests/migrations/org/test_060_add_stakeholder_fields_to_deal_participants.py tests/scripts/test_audit_duplicate_deal_contact_participants.py tests/scripts/test_backfill_participant_stakeholder_fields.py tests/migrations/test_org_migration_head_contract.py -q
uv run ruff check migrations/org_migrations/versions/3c7f79637739_060_add_stakeholder_fields_to_deal_participants.py src/domain/models/organisation_tables/sync/crm/deals.py src/service/integrations/crm/records/salesforce/deals/participant_writer.py src/service/crm/internal/deal_participant_service.py src/service/crm/internal/deal_write_service.py scripts/backfill_participant_stakeholder_fields.py scripts/audit_duplicate_deal_contact_participants.py tests/service/integrations/crm/records/salesforce/deals/test_deal_child_source_mappings.py tests/service/crm/test_deal_participant_service_internal_ids.py tests/migrations/org/test_060_add_stakeholder_fields_to_deal_participants.py tests/scripts/test_audit_duplicate_deal_contact_participants.py tests/scripts/test_backfill_participant_stakeholder_fields.py
git diff --check
```

## Open Operational Note

Because the bad `060` already ran in some environments, the release must include the explicit
downgrade/rerun step. If we do not downgrade those schemas, Alembic will not rerun the corrected
`060` for them.
