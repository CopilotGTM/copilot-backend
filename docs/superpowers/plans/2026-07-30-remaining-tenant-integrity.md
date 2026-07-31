# Remaining Tenant Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete rollout Task 5 so every non-CRM organisation table has
tenant-qualified relationships and correctly classified uniqueness rules.

**Architecture:** Keep globally unique UUID primary keys and the existing
runtime/service APIs. Replace the remaining single-column tenant foreign keys
with explicit `(organisation_id, id)` constraints, add the candidate keys
PostgreSQL requires on referenced parents, and prepend `organisation_id` to
business/provider uniqueness. Preserve global uniqueness for shared blob
storage paths because those keys identify physical resources outside the
tenant table boundary.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy, PostgreSQL 16, Alembic,
pytest.

---

## Current Evidence and Scope

The model audit after Task 4 reports:

- 76 non-CRM single-column foreign keys to tenant tables
- 16 referenced parent identities without an
  `(organisation_id, id)` candidate key
- 28 unscoped uniqueness findings
- 73 foreign keys already backed by a usable local-ID index
- 3 foreign keys without a usable child index:
  - `chat_message_attachment_link.attachment_id`
  - `chat_messages.parent_message_id`
  - `deal_assets.kb_source_id`

Of the 28 uniqueness findings, these three remain intentionally global:

```text
attachments.storage_path
deal_assets.storage_path
organisation_sources.storage_path
```

Blob storage is shared outside PostgreSQL. Scoping those rules would allow two
tenant rows to claim the same physical object key, so the audit must classify
them explicitly rather than weakening the database rule.

The remaining 25 business/provider uniqueness rules become tenant-scoped.

## Invariants

1. Every non-CRM tenant relationship maps
   `(child.organisation_id, child.reference_id)` to
   `(parent.organisation_id, parent.id)`.
2. Every referenced parent pair is a primary/candidate key.
3. Every child foreign key has a usable index. Existing local-ID indexes count
   because referenced UUID identities remain globally unique; Task 5 adds only
   the three missing indexes.
4. The only tenant-table unique rules allowed without `organisation_id` are
   the three reviewed blob `storage_path` rules.
5. Existing `ON DELETE` behavior is preserved. The only remaining non-null
   actions are `RESTRICT` on the two `agent_runs` relationships and `CASCADE`
   on `integration_sync_states.integration_id`.
6. Nullable references remain nullable and use PostgreSQL `MATCH SIMPLE`.
7. New foreign keys are added `NOT VALID`, validated, and only then replace
   their legacy single-column constraints.
8. Tenant-scoped unique replacements are created before legacy rules are
   removed.
9. Existing globally unique UUID primary keys, runtime routing, services, and
   schema selection do not change.
10. Revision 084 accepts the same validated Alembic `schema` argument as
    revisions 082 and 083.

## File Map

### Shared declaration and audit contract

- Create:
  `src/domain/models/organisation_tables/tenant_constraints.py`
- Modify:
  `src/domain/models/organisation_tables/sync/crm/base.py`
- Modify:
  `src/domain/models/organisation_tables/sync/base.py`
- Modify:
  `scripts/maintenance/audit_shared_tenant_readiness.py`
- Modify:
  `tests/scripts/maintenance/test_audit_shared_tenant_readiness.py`

### Remaining model metadata

- Modify:
  `src/domain/models/organisation_tables/agent_models.py`
- Modify:
  `src/domain/models/organisation_tables/chat_message_models.py`
- Modify:
  `src/domain/models/organisation_tables/chat_session_access_models.py`
- Modify:
  `src/domain/models/organisation_tables/chat_session_models.py`
- Modify:
  `src/domain/models/organisation_tables/deal_asset_models.py`
- Modify:
  `src/domain/models/organisation_tables/job_models.py`
- Modify:
  `src/domain/models/organisation_tables/knowledge_models.py`
- Modify:
  `src/domain/models/organisation_tables/reminder_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/calendar.py`
- Modify:
  `src/domain/models/organisation_tables/sync/conversation_intelligence.py`
- Modify:
  `src/domain/models/organisation_tables/sync/deal_memory_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/email_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/integration_activity_request.py`
- Modify:
  `src/domain/models/organisation_tables/sync/integration_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/meeting_intelligence.py`
- Modify:
  `src/domain/models/organisation_tables/sync/meeting_recording.py`
- Modify:
  `src/domain/models/organisation_tables/sync/messaging_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/participant_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/risk_blocker_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/stakeholder_insight_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/timeline_models.py`

### Migration contract

- Create:
  `tests/migrations/org/test_084_harden_remaining_tenant_integrity.py`
- Create:
  `migrations/org_migrations/versions/3c7f79637715_084_harden_remaining_tenant_integrity.py`

## Task 5.1: Classify Shared Blob Uniqueness

- [x] **Step 1: Add a failing audit behavior test**

Create a tenant table with a globally unique `storage_path`. Prove it remains a
finding without an explicit classification and disappears only when the exact
`(table, columns)` rule is supplied as an intentional global unique rule.

- [x] **Step 2: Verify red**

Run:

```powershell
uv run pytest `
  tests/scripts/maintenance/test_audit_shared_tenant_readiness.py -q
```

Expected: fail because `audit_metadata()` cannot yet classify an intentional
global unique rule.

- [x] **Step 3: Add the narrow audit classification**

Add:

```python
INTENTIONAL_GLOBAL_UNIQUE_RULES = {
    ("attachments", ("storage_path",)),
    ("deal_assets", ("storage_path",)),
    ("organisation_sources", ("storage_path",)),
}
```

`audit_metadata()` accepts an optional set of exact rules and excludes only
those entries. `build_model_report()` passes the production constant.

- [x] **Step 4: Verify green**

Run the audit contract and require all tests to pass.

## Task 5.2: Lock the Remaining Model-Derived Contract

- [x] **Step 1: Add the failing Task 5 contract**

The test loads organisation model metadata and revision 084. It must prove:

- the audit has zero `unscoped_tenant_foreign_keys`
- the audit has zero `unscoped_unique_rules`
- exactly 76 non-CRM named composite relationships exist
- migration `REMAINING_TENANT_FOREIGN_KEYS` exactly matches those relationships
- all referenced `(organisation_id, id)` pairs are unique
- migration `REMAINING_CANDIDATE_KEYS` exactly matches the 16 Task 5 candidate
  constraints declared by models
- every child reference has an existing local-ID or tenant-leading index
- migration `REMAINING_FOREIGN_KEY_INDEXES` contains exactly the three new
  model indexes
- all 25 scoped uniqueness replacements match model columns, predicates, and
  constraint/index kind
- the three blob `storage_path` rules remain globally unique

- [x] **Step 2: Verify red**

Run:

```powershell
uv run pytest `
  tests/migrations/org/test_084_harden_remaining_tenant_integrity.py -q
```

Expected: fail because revision 084 and the remaining composite declarations
do not exist.

## Task 5.3: Declare Remaining Tenant Integrity

- [x] **Step 1: Move the existing helper to the shared model boundary**

Create `tenant_constraints.py` with the existing
`tenant_foreign_key(...) -> ForeignKeyConstraint` implementation. Make the CRM
module import and re-export it so Task 4 declarations do not change.

- [x] **Step 2: Remove the generic inherited integration FK**

Remove only `foreign_key="knowledge_integrations.integration_id"` from
`SyncedRecordMixin` and `FlexibleSyncedRecordMixin`. Each concrete synced table
then declares its composite integration constraint explicitly; no routing or
field type changes.

- [x] **Step 3: Replace all 76 remaining relationships**

For each current audit finding, remove the single-column `foreign_key` or
`sa.ForeignKey` declaration and add:

```python
tenant_foreign_key(
    "<child_table>",
    "<local_column>",
    "<parent_table>",
    "<remote_column>",
    ondelete="<existing action or omitted>",
)
```

Preserve nullability and the three existing delete actions exactly.

- [x] **Step 4: Add the 16 candidate keys**

Add a named
`UniqueConstraint("organisation_id", "<identity>", ...)` to:

```text
attachments.attachment_id
automation_triggers.trigger_id
calendar_event_attendees.attendee_id
calendar_events.event_id
chat_messages.message_id
chat_sessions.session_id
ci_call_speakers.speaker_id
ci_calls.call_id
email_folders.folder_id
integration_participants.participant_id
meetings.meeting_id
messaging_channels.channel_id
messaging_messages.message_id
organisation_sources.source_id
user_agents.agent_id
user_reminders.reminder_id
```

- [x] **Step 5: Add only three missing child indexes**

Add tenant-leading indexes for:

```text
chat_message_attachment_link(organisation_id, attachment_id)
chat_messages(organisation_id, parent_message_id)
deal_assets(organisation_id, kb_source_id)
```

Do not duplicate the 73 usable local-ID indexes already present.

- [x] **Step 6: Scope 25 business/provider unique rules**

Prepend `organisation_id`, use deterministic `_org` names, preserve the one
partial predicate on `chat_message_jobs`, and retain the original
constraint-versus-index kind. Do not change the three global blob-path rules.

- [x] **Step 7: Verify metadata green**

Run the Task 5 contract and model audit. Both structural finding lists must be
empty.

## Task 5.4: Add Revision 084

- [x] **Step 1: Define immutable migration inventories**

Revision `3c7f79637715` follows `3c7f79637716` and contains:

```text
REMAINING_CANDIDATE_KEYS: 16
REMAINING_TENANT_FOREIGN_KEYS: 76
REMAINING_FOREIGN_KEY_INDEXES: 3
REMAINING_SCOPED_UNIQUE_RULES: 25
```

The uniqueness inventory records whether each rule is a constraint or index,
its old/new name, columns, and optional PostgreSQL predicate.

- [x] **Step 2: Build supporting objects safely**

Build candidate unique indexes and the three child indexes concurrently.
Attach candidate indexes as unique constraints without a second scan.

- [x] **Step 3: Install and validate composite foreign keys**

Add each composite FK `NOT VALID`, validate it, discover the exact legacy
single-column FK through `pg_catalog`, and only then drop the legacy rule.

- [x] **Step 4: Replace scoped uniqueness safely**

Create the new tenant-scoped unique index before dropping the old rule. Attach
non-partial constraint replacements with `UNIQUE USING INDEX`; leave index
rules as indexes. Discover unnamed legacy constraints by their exact ordered
columns.

- [x] **Step 5: Implement dependency-safe downgrade**

Recreate legacy uniqueness and single-column foreign keys before removing the
tenant-qualified replacements. Drop the three child indexes and candidate
constraints last.

- [x] **Step 6: Verify migration ordering and head**

Run:

```powershell
uv run pytest `
  tests/migrations/org/test_084_harden_remaining_tenant_integrity.py `
  tests/migrations/org/test_083_harden_crm_tenant_integrity.py `
  tests/migrations/org/test_082_add_missing_organisation_identity.py `
  tests/migrations/test_org_migration_head_contract.py -q

uv run alembic -c migrations/org_migrations/alembic.ini heads
```

Expected head:

```text
3c7f79637715 (head)
```

## Final Verification

- [x] Run the focused audit and migration contracts.
- [x] Run affected chat, calendar, email, messaging, meeting, knowledge,
  integration, agent, reminder, job, and deal service/model tests.
- [x] Run the model readiness audit and prove all structural lists are empty.
- [x] Configure SQLAlchemy mappers with warnings treated as errors.
- [x] Compile every affected table with the PostgreSQL dialect.
- [x] Verify deterministic identifiers fit PostgreSQL's 63-byte limit.
- [x] Run Ruff on every Task 5 Python file.
- [x] Run the Python comment-smell scanner on new/changed Task 5 contract code.
- [x] Run `git diff --check`.
- [x] Confirm Task 1-3 staged state and Task 4/5 unstaged state remain
  preserved.

**Task 5 exit gate:** Every organisation model satisfies the shared-row
structural integrity contract, while physical blob keys remain globally unique
and no redundant index set is introduced.
