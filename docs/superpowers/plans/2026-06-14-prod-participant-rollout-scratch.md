# Production Participant Migration Scratch Runbook

> Scratch file for this production run only. Do not commit this file.

**Goal:** safely rerun org migration `060` in production, run the participant/stakeholder data migration, and remove already-created non-canonical/business-user participant rows plus their source mappings.

**Core rule:** no blind production writes. Every write step has a read-only precheck/audit immediately before it.

## Current Assumptions

- Dev is already deployed and validated with corrected migration/code.
- Production database connection has been pointed from local environment.
- Production code is not yet deployed, so some prod org schemas may have already applied the earlier/bad `060` and some may still be at `059`.
- Correct org migration:
  - `060`: `3c7f79637739`
  - `059`: `3c7f79637740`
- Data migration script:
  - `uv run python -m scripts.backfill_participant_stakeholder_fields --all --apply`
- Duplicate audit script:
  - `uv run python -m scripts.audit_duplicate_deal_contact_participants --all --fail-on-duplicates`
- Canonical participant types are `contact`, `company`, and `user`.
- Manual cleanup must target only non-canonical/leaked business-user participant rows. Do not delete canonical `user` rows unless a separate audit proves they are wrong.

## Phase 0: Preflight

- [ ] Confirm current Kubernetes context before touching pods:

```powershell
kubectl config current-context
```

- [ ] Confirm local git branch and no unexpected code changes except scratch docs:

```powershell
git status --short
```

- [ ] Confirm local environment is pointed to production before local migration commands:

```powershell
uv run python -m scripts.run_org_migrations --all --precheck
```

Expected:
- Production org schemas are listed.
- Some schemas may show current `3c7f79637739`.
- Some schemas may show current `3c7f79637740`.
- Stop if any schema shows an unexpected revision above `3c7f79637739`.

## Phase 1: Downgrade Already-Upgraded Prod Schemas From Kubernetes

Reason: if a schema already has revision `3c7f79637739`, Alembic will not rerun corrected `060`. It must be downgraded to `059` first.

- [ ] Find the production backend/startup pod:

```powershell
kubectl get pods -A
```

Look for the production backend/startup pod that has the app image and environment used for org migrations.

- [ ] From the pod, run a read-only precheck:

```powershell
kubectl exec -n <namespace> <pod-name> -- uv run python -m scripts.run_org_migrations --all --target-version 3c7f79637740 --precheck
```

- [ ] Copy the precheck output into the rollout notes.

Decision:
- For schemas already at `3c7f79637739`, downgrade to `3c7f79637740`.
- For schemas already at `3c7f79637740`, do nothing.
- For schemas at any other revision, stop and inspect before proceeding.

- [ ] Downgrade only org IDs currently at `3c7f79637739`:

```powershell
kubectl exec -n <namespace> <pod-name> -- uv run python -m scripts.run_org_migrations --target-version 3c7f79637740 --fail-fast <org-id-1> <org-id-2>
```

- [ ] Re-run precheck from the pod:

```powershell
kubectl exec -n <namespace> <pod-name> -- uv run python -m scripts.run_org_migrations --all --target-version 3c7f79637740 --precheck
```

Expected:
- Previously upgraded orgs now show current `3c7f79637740`.
- No schema remains at `3c7f79637739`.

## Phase 2: Upgrade Prod Schemas From Local Corrected Code

Reason: local code now has corrected `060`; production DB is pointed locally.

- [ ] Local precheck to head:

```powershell
uv run python -m scripts.run_org_migrations --all --precheck
```

- [ ] Upgrade all approved org schemas to head:

```powershell
uv run python -m scripts.run_org_migrations --all --fail-fast
```

- [ ] Verify all org schemas are at `3c7f79637739`:

```powershell
uv run python -m scripts.run_org_migrations --all --precheck
```

Expected:
- Every approved org schema current revision is `3c7f79637739`.

## Phase 3: Audit Existing Non-Canonical/Business-User Participant Rows

Reason: runtime code now normalizes provider participants into our canonical structure, but bad rows may already exist in production.

- [ ] For each affected org schema, audit active participant type counts before deleting anything:

```sql
SELECT participant_type, count(*) AS row_count
FROM crm_deal_participants
WHERE is_deleted = false
GROUP BY participant_type
ORDER BY participant_type;
```

- [ ] Audit rows that are not canonical participant types:

```sql
SELECT participant_id, deal_id, participant_type, relationship_role, related_local_id, related_email
FROM crm_deal_participants
WHERE is_deleted = false
  AND participant_type NOT IN ('contact', 'company', 'user')
ORDER BY participant_type, deal_id, participant_id
LIMIT 100;
```

- [ ] Audit known leaked business-user spellings:

```sql
SELECT participant_id, deal_id, participant_type, relationship_role, related_local_id, related_email
FROM crm_deal_participants
WHERE is_deleted = false
  AND lower(btrim(participant_type)) IN (
    'business_user',
    'business user',
    'business-user',
    'businessuser'
  )
ORDER BY deal_id, participant_id
LIMIT 100;
```

Decision:
- If both audits return zero rows, skip Phase 4.
- If rows are returned, save counts and sample IDs before cleanup.
- Do not delete rows where `participant_type = 'user'` in this runbook.

## Phase 4: Manual Cleanup Of Non-Canonical/Business-User Rows

Run this per org schema only after Phase 3 identifies rows to remove.

- [ ] Start a transaction and materialize exact cleanup IDs:

```sql
BEGIN;

CREATE TEMP TABLE participant_cleanup_ids AS
SELECT participant_id
FROM crm_deal_participants
WHERE is_deleted = false
  AND (
    participant_type NOT IN ('contact', 'company', 'user')
    OR lower(btrim(participant_type)) IN (
      'business_user',
      'business user',
      'business-user',
      'businessuser'
    )
  );

SELECT count(*) AS participants_to_cleanup
FROM participant_cleanup_ids;

SELECT count(*) AS source_mappings_to_cleanup
FROM crm_source_object_mappings
WHERE local_object_type = 'deal_participant'
  AND local_object_id IN (SELECT participant_id FROM participant_cleanup_ids)
  AND is_deleted = false;
```

- [ ] If counts match the Phase 3 audit expectation, soft-delete source mappings and participant rows:

```sql
UPDATE crm_source_object_mappings
SET is_deleted = true,
    updated_at = now()
WHERE local_object_type = 'deal_participant'
  AND local_object_id IN (SELECT participant_id FROM participant_cleanup_ids)
  AND is_deleted = false;

UPDATE crm_deal_participants
SET is_deleted = true,
    updated_at = now()
WHERE participant_id IN (SELECT participant_id FROM participant_cleanup_ids)
  AND is_deleted = false;
```

- [ ] Verify cleanup inside the same transaction:

```sql
SELECT participant_type, count(*) AS remaining_active_rows
FROM crm_deal_participants
WHERE is_deleted = false
  AND (
    participant_type NOT IN ('contact', 'company', 'user')
    OR lower(btrim(participant_type)) IN (
      'business_user',
      'business user',
      'business-user',
      'businessuser'
    )
  )
GROUP BY participant_type;
```

- [ ] Commit only if the verification query returns no rows:

```sql
COMMIT;
```

- [ ] If verification is unexpected:

```sql
ROLLBACK;
```

## Phase 5: Run Participant/Stakeholder Data Migration

- [ ] Dry-run first:

```powershell
uv run python -m scripts.backfill_participant_stakeholder_fields --all --dry-run
```

Review output for:
- `conflicts=0` expected.
- primary-contact updates are reasonable.
- duplicate rows pointed to canonical are expected, not exploding unexpectedly.

- [ ] Apply:

```powershell
uv run python -m scripts.backfill_participant_stakeholder_fields --all --apply
```

- [ ] Save per-org output.

## Phase 6: Post-Migration Audits

- [ ] Audit duplicate active deal contact participants:

```powershell
uv run python -m scripts.audit_duplicate_deal_contact_participants --all --fail-on-duplicates
```

Expected:
- No active duplicate participant groups by `organisation_id + deal_id + related_local_id`.
- Email collisions may still be reported, but should not fail if they are not duplicate local contact IDs.

- [ ] For any high-value org IDs, validate legacy stakeholder coverage:

```powershell
uv run python -m scripts.validate_deal_stakeholder_participant_cutover --organisation-id <org-id> --only-mismatches --fail-on-mismatch
```

- [ ] Spot-check primary contact flags in production for key orgs:

```sql
SELECT count(*) AS active_primary_contact_participants
FROM crm_deal_participants
WHERE is_deleted = false
  AND participant_type = 'contact'
  AND is_primary_contact = true;
```

## Phase 7: Final Gate Before Production Code Rollout

- [ ] Confirm org migration head:

```powershell
uv run python -m scripts.run_org_migrations --all --precheck
```

- [ ] Confirm no non-canonical active participant types remain:

```sql
SELECT participant_type, count(*) AS row_count
FROM crm_deal_participants
WHERE is_deleted = false
GROUP BY participant_type
ORDER BY participant_type;
```

Expected only:
- `contact`
- `company`
- `user`

- [ ] Confirm no active mappings point to cleaned participant IDs if Phase 4 was used:

```sql
SELECT count(*) AS active_mappings_to_deleted_participants
FROM crm_source_object_mappings AS mappings
JOIN crm_deal_participants AS participants
  ON participants.participant_id = mappings.local_object_id
WHERE mappings.local_object_type = 'deal_participant'
  AND mappings.is_deleted = false
  AND participants.is_deleted = true;
```

Expected:
- `0`

## Stop Conditions

Stop and do not continue if:

- Any prod schema is above `3c7f79637739`.
- Downgrade precheck shows a schema that is neither `3c7f79637739` nor `3c7f79637740`.
- Manual cleanup audit includes canonical `user` rows only; this runbook does not delete them.
- Data migration dry-run reports conflicts.
- Duplicate audit fails after apply.
- Source mappings still point to deleted participant rows after cleanup.
