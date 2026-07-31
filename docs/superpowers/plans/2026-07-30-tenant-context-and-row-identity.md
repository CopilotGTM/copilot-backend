# Tenant Context and Row Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete rollout Tasks 2 and 3 by binding a validated organisation
UUID to every organisation transaction and adding non-null tenant identity to
the nine tables identified by the readiness audit.

**Architecture:** Reuse the existing master/org session split. Master sessions
remain unchanged. Sync and async organisation providers validate their existing
`organisation_id` argument, store the UUID on the SQLAlchemy session, and the
existing transaction-begin listener writes it to PostgreSQL with
transaction-local `set_config`. The organisation migration adds
`organisation_id` only to the audit-reported tables, backfills the one populated
source schema from an explicit master organisation UUID after validating its
canonical schema name, and installs the transaction context as the permanent
shared-schema default for future inserts.

**Tech Stack:** Python 3.11, SQLModel/SQLAlchemy, PostgreSQL 16, Alembic,
pytest.

---

## Scope and Invariants

Task 1 currently reports these nine tables:

```text
calendar_event_attendees
calendar_event_user_states
chat_message_attachment_link
chat_message_jobs
chat_messages
integration_sync_history
knowledge_base
knowledge_processing_jobs
transcript_segments
```

This slice does not harden every unique constraint or foreign key; those are
Tasks 4 and 5. It does not change schema routing, master sessions, master
migrations, provider credentials, or organisation provisioning.

The implementation must preserve these invariants:

1. `master_session_scope()` and `master_async_session_scope()` never receive
   tenant context.
2. Every org provider validates its existing argument as a UUID before engine
   or secret lookup.
3. Tenant identity is set with a bound parameter, never SQL interpolation.
4. `set_config(..., true)` makes identity transaction-local for PgBouncer
   transaction pooling.
5. A missing tenant context cannot create one of the nine rows because its
   server default becomes `NULL` and the column is `NOT NULL`.
6. A populated `org_*` schema is accepted only when an explicit organisation
   UUID maps back to its exact canonical name.
7. `tenant_data` is accepted only when all nine tables are empty during this
   additive migration; mixed-tenant backfill is forbidden.
8. Existing model constructors do not need new tenant plumbing; PostgreSQL
   obtains the value from the trusted transaction context.

## File Map

### Task 2

- Modify: `src/infra/database/tenant_search_path.py`
- Modify: `src/infra/database/provider_sync.py`
- Modify: `src/infra/database/provider_async.py`
- Modify: `tests/infra/test_tenant_search_path.py`
- Modify: `tests/infra/test_sync_db_provider.py`
- Modify: `tests/infra/test_async_db_provider.py`

### Task 3

- Modify: `src/domain/models/organisation_tables/sync/calendar.py`
- Modify: `src/domain/models/organisation_tables/chat_message_models.py`
- Modify: `src/domain/models/organisation_tables/job_models.py`
- Modify: `src/domain/models/organisation_tables/sync/integration_models.py`
- Modify: `src/domain/models/organisation_tables/knowledge_models.py`
- Modify:
  `src/domain/models/organisation_tables/sync/meeting_intelligence.py`
- Create:
  `migrations/org_migrations/versions/3c7f79637717_082_add_missing_organisation_identity.py`
- Create: `migrations/org_migrations/tenant_identity.py`
- Modify: `migrations/migration_manager.py`
- Modify: `scripts/run_org_migrations.py`
- Modify: `src/service/orgnisation/organization_management.py`
- Create:
  `tests/migrations/org/test_082_add_missing_organisation_identity.py`

## Task 2: Bind Tenant Identity to Existing Organisation Sessions

- [x] **Step 1: Write failing transaction-hook tests**

Add tests that store a UUID under
`TENANT_ORGANISATION_ID_SESSION_KEY` and require:

```python
connection.execute(
    text(
        "SELECT set_config("
        "'app.current_organisation_id', "
        ":organisation_id, true)"
    ),
    {"organisation_id": str(organisation_id)},
)
```

Also require a malformed value to raise `ValueError` before SQL is executed and
master sessions without org metadata to remain untouched.

- [x] **Step 2: Write failing provider-boundary tests**

For sync and async providers, prove:

```python
session.info[TENANT_ORGANISATION_ID_SESSION_KEY] == organisation_id
```

Passing the UUID as a string must normalize to the same UUID. A malformed value
must fail before `_get_tenant_engine` is called.

- [x] **Step 3: Verify red**

Run:

```powershell
uv run pytest `
  tests/infra/test_tenant_search_path.py `
  tests/infra/test_sync_db_provider.py `
  tests/infra/test_async_db_provider.py -q
```

Expected: the new tests fail because the key, parser, and `set_config` call do
not exist.

- [x] **Step 4: Implement the minimum shared behavior**

In `tenant_search_path.py`, add:

```python
TENANT_ORGANISATION_ID_SESSION_KEY = "tenant_organisation_id"


def parse_tenant_organisation_id(value: UUID | str) -> UUID:
    """Return the canonical UUID accepted by the org session boundary."""

    return value if isinstance(value, UUID) else UUID(str(value))
```

The existing `after_begin` listener continues to own transaction setup. After
setting `search_path`, it validates the stored ID and executes parameterized
transaction-local `set_config`.

At the start of each provider's `tenant_session`, normalize the ID before
calling `_get_tenant_engine`, then store the UUID on the sync session metadata.
Do not change master providers or add another listener.

- [x] **Step 5: Verify green**

Run the same focused command. Expected: all selected tests pass.

**Task 2 exit gate:** Sync and async org sessions carry one validated,
transaction-local organisation identity and master sessions remain unchanged.

## Task 3: Add Missing Organisation Identity

- [x] **Step 1: Write the failing model and migration contract**

The test must assert:

- the Alembic head advances from `3c7f79637718` to `3c7f79637717`
- the migration inventory equals the nine Task 1 findings exactly
- every listed model table exposes a non-null PostgreSQL UUID
  `organisation_id`
- every listed table has an organisation-leading index
- every model column uses this server default:

  ```sql
  NULLIF(current_setting('app.current_organisation_id', true), '')::uuid
  ```

- an explicit UUID matching canonical `org_<uuid>` returns that UUID
- missing, malformed, or schema-mismatched source identity fails
- `tenant_data` produces no backfill identity
- the migration adds nullable columns before backfill, proves no nulls, then
  enforces `NOT NULL`

- [x] **Step 2: Verify red**

Run:

```powershell
uv run pytest tests/migrations/org/test_082_add_missing_organisation_identity.py -q
```

Expected: fail because revision `082` and the nine model columns do not exist.

- [x] **Step 3: Declare the permanent model default**

Each of the nine model tables receives the same direct SQLModel field:

```python
organisation_id: UUID = Field(
    foreign_key="public.organisations.organisation_id",
    nullable=False,
    index=True,
    sa_column_kwargs={
        "server_default": text(
            "NULLIF(current_setting("
            "'app.current_organisation_id', true), '')::uuid"
        )
    },
)
```

Use each file's existing SQLAlchemy imports. Do not add a model helper for nine
identical declarations; the database expression is locked by the migration
contract test. Existing business callers remain unchanged because PostgreSQL
owns the default.

- [x] **Step 4: Implement revision 082**

The revision must:

1. read the exact target schema and optional organisation UUID from Alembic
   `-x` arguments
2. accept `tenant_data` as an empty target
3. accept a source schema only when its explicit UUID maps to that canonical
   name
4. add each column nullable with no default
5. backfill source rows with one bound UUID parameter
6. fail if `tenant_data` contains any null row
7. add and validate a temporary not-null check
8. set `NOT NULL`
9. install the transaction-context server default
10. create `ix_<table>_organisation_id`
11. remove the temporary check

`run_org_migrations.py` and `OrganizationManagementService` pass the UUID
already loaded from the master organisation record into
`ProductionMigrationManager`; the manager forwards it to Alembic. All table
names come from a fixed tuple in the migration. User input is never used as an
identifier.

Downgrade removes each index and column in reverse order. It does not attempt
to infer or rewrite business data.

- [x] **Step 5: Verify Task 3**

Run:

```powershell
uv run pytest `
  tests/migrations/org/test_082_add_missing_organisation_identity.py `
  tests/migrations/test_org_migration_head_contract.py `
  tests/scripts/maintenance/test_audit_shared_tenant_readiness.py -q

uv run python -m scripts.maintenance.audit_shared_tenant_readiness --models --json
```

Expected:

```text
missing_organisation_id = []
nullable_organisation_id = []
missing_tenant_indexes = []
```

Other audit findings remain for Tasks 4 and 5.

**Task 3 exit gate:** All 74 organisation models have non-null row identity,
and new rows obtain identity only from the trusted org transaction.

## Final Verification

Run:

```powershell
uv run pytest `
  tests/infra/test_tenant_search_path.py `
  tests/infra/test_sync_db_provider.py `
  tests/infra/test_async_db_provider.py `
  tests/migrations/org/test_082_add_missing_organisation_identity.py `
  tests/migrations/test_org_migration_head_contract.py `
  tests/scripts/maintenance/test_audit_shared_tenant_readiness.py -q

uv run python .agents/skills/writing-python-comments/scripts/check_comment_smells.py `
  src/infra/database/tenant_search_path.py `
  src/infra/database/provider_sync.py `
  src/infra/database/provider_async.py `
  migrations/org_migrations/versions/3c7f79637717_082_add_missing_organisation_identity.py

git diff --check
```

Completion requires fresh green output and a final diff review proving no
master-session, routing, credential, provisioning, or unrelated model behavior
changed.
