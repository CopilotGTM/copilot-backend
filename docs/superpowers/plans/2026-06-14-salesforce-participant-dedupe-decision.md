# Deal Participant Dedupe Decision Note

## Decision

Keep the implementation simple:

```text
participant identity = organisation + deal + resolved contact
schema migration = one corrected 060 migration
data cleanup = controlled data migration/backfill, not Alembic repair SQL
```

## What Failed

Prod startup failed while applying org migration `060` because the migration attempted to enforce
participant uniqueness on data that already had duplicate active `crm_deal_participants` rows and
valid email collisions.

The visible failure involved:

```text
organisation_id: d0a590bf-d364-4e0d-a4a1-4ad9d6525467
deal_id: 50542145-ca94-4311-91ee-e50f577851f0
normalized related_email: info@salesforce.com
```

That email collision is valid because different contacts can share a generic email address.
Therefore, email must not be the active participant uniqueness key for resolved contacts.

## Root Cause

Salesforce deal participant ingestion used role-bearing identity:

```text
deal + contact + relationship_role
```

That let one real contact become multiple active participant rows on the same deal, for example:

```text
same deal + same contact + Business User
same deal + same contact + primary_contact
```

The app-owned participant identity should be:

```text
organisation_id + deal_id + related_local_id
```

for resolved contact participants.

## Product Rule

`primary_contact` is a source relationship flag and is stored as:

```text
crm_deal_participants.is_primary_contact
```

Other Salesforce labels such as `Business User`, `Decision Maker`, `Economic Buyer`, or
`Influencer` are not app-owned buying committee classification. They must not create separate
participant rows and must not overwrite agent/product-owned participant classification fields.

## Migration Rule

Migration `060` should add schema only:

1. participant-backed stakeholder fields
2. contact research fields
3. non-unique lookup indexes
4. required job/insight ID cutover SQL

Migration `060` should not:

1. dedupe Salesforce participant rows
2. repoint source mappings for duplicate participant rows
3. soft-delete duplicate participant rows
4. add a blanket email unique index
5. add a local-id unique index before cleanup

## Rollout Rule

Because the bad `060` already ran in dev and some prod org schemas, rollout should be:

1. downgrade affected org schemas back to `059`
2. deploy corrected `060`
3. rerun org migrations
4. run the participant/stakeholder data migration
5. audit duplicate participants

This avoids a second migration whose only purpose is repairing a partially rolled-out bad
migration.
