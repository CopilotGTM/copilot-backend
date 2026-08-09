# Agent Automation Tables Implementation Plan

> **Schema correction (2026-08-09):** `automation_triggers.delivery_config` is removed. Current
> output delivery belongs to `user_agents`, and each `agent_runs` row stores its own immutable
> activation-time snapshot. See `../specs/2026-08-09-agent-level-output-delivery.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add the three tenant-local PostgreSQL tables that persist user-owned agents, reusable automation triggers, and durable agent runs.

**Architecture:** Keep the agent definition, trigger definition, and execution record independent. Triggers use a polymorphic target identity so the same table can target workflows later, while run history uses restrictive foreign keys and immutable snapshots so edits and archives do not corrupt past executions.

**Tech Stack:** Python 3.11, SQLModel, SQLAlchemy, PostgreSQL JSONB, Alembic, pytest, Ruff.

---

## File map

- Create src/domain/models/organisation_tables/agent_models.py: enums and SQLModel mappings for all three tables.
- Modify src/domain/models/organisation_tables/__init__.py: stable package exports for the new enums and models.
- Create migrations/org_migrations/versions/3c7f79637721_078_agent_automation_tables.py: one reversible organisation migration, following head 3c7f79637722.
- Create tests/test_agent_automation_schema.py: focused model and migration contract.
- Preserve docs/superpowers/specs/2026-07-14-agent-automation-design.md as the authoritative product/schema rationale.

No router, service, worker, Redis scheduler, workflow table, or reminder change belongs in this plan.

### Task 1: Lock the schema contract with a failing test

**Files:**

- Create: tests/test_agent_automation_schema.py

- [ ] **Step 1: Add model metadata helpers and table-name coverage**

The test imports UserAgent, AutomationTrigger, and AgentRun from the organisation-table package,
then asserts:

~~~python
assert UserAgent.__tablename__ == "user_agents"
assert AutomationTrigger.__tablename__ == "automation_triggers"
assert AgentRun.__tablename__ == "agent_runs"
~~~

It reads SQLAlchemy table metadata rather than opening a database, keeping the test fast and
deterministic.

- [ ] **Step 2: Cover the complete contract**

The test must prove:

- user_agents has the definition, provenance, revision, tool/skill allowlist, schema, lifecycle,
  and timestamp columns from the design;
- automation_triggers has generic target_type/target_id identity, a pinned target snapshot,
  schedule/event fields, owner scope, config JSON, counters, and lifecycle fields;
- agent_runs has agent/optional-trigger references, immutable snapshots, idempotency, claim state,
  output/error/usage fields, and execution timestamps;
- target_id has no foreign key because the trigger target is intentionally polymorphic;
- agent_runs parent foreign keys use ON DELETE RESTRICT;
- the exact lean check/index sets are present, including duplicate, due-schedule, active-event,
  idempotency, pending-claim, owner-list, and history indexes;
- evolving JSON/lifecycle checks and speculative timestamp/provenance/lease indexes are absent;
- JSONB types and server defaults remain present on required JSON columns;
- the migration has revision 3c7f79637721, follows 3c7f79637722, creates only the three requested
  tables, and drops them in reverse dependency order.

- [ ] **Step 3: Run the test and observe RED**

Run:

~~~powershell
uv run pytest tests/test_agent_automation_schema.py -q
~~~

Expected: collection fails because the new organisation-table exports do not exist yet. This is the
intentional TDD failure.

### Task 2: Add the SQLModel table mappings

**Files:**

- Create: src/domain/models/organisation_tables/agent_models.py
- Modify: src/domain/models/organisation_tables/__init__.py
- Test: tests/test_agent_automation_schema.py

- [ ] **Step 1: Define persisted string enums**

Define str/Enum classes for these exact values:

- AgentSourceType: user, system
- AgentVisibility: private, organisation
- AgentStatus: active, paused, archived
- AgentOutputMode: text, json
- AutomationTargetType: agent, workflow
- AutomationTriggerType: schedule, event
- AutomationScopeType: owner
- AutomationTriggerStatus: active, paused, archived
- AgentRunStatus: pending, running, succeeded, failed, delivery_failed, validation_failed, cancelled

- [ ] **Step 2: Map user_agents**

Use SQLModel with explicit, unindexed timestamps and map this exact database contract:

| Group | Columns |
| --- | --- |
| Identity | agent_id UUID PK, organisation_id UUID, owner_user_id UUID |
| Provenance | source_type text default user, source_agent_id UUID nullable, source_version integer nullable |
| Version/lifecycle | revision integer default 1, visibility text default private, status text default active |
| Definition | agent_type text default general, name text, description text nullable, prompt text |
| Runtime | model_name text nullable, model_config JSONB object default {}, allowed_tools JSONB array default [], allowed_skills JSONB array default [], max_turns integer nullable |
| Contract | input_schema JSONB object nullable, output_mode text default text, output_schema JSONB object nullable |
| Time | created_at and updated_at timezone-aware with server now defaults |

Pydantic reserves the Python name model_config, so expose the Python attribute as model_settings
while mapping it to the physical model_config column.

Add only stable revision and persisted-state checks plus the owner/status/updated list index.
Service validation owns provenance, limits, JSON shapes, schemas, and catalog membership; do not
add standalone timestamp or source-provenance indexes.

- [ ] **Step 3: Map automation_triggers**

Use SQLModel with explicit, unindexed timestamps and map:

| Group | Columns |
| --- | --- |
| Identity | trigger_id UUID PK, organisation_id UUID, owner_user_id UUID, name text |
| Target | target_type text default agent, target_id UUID without FK, target_revision integer, target_snapshot JSONB object |
| Match | trigger_type text, source_domain text nullable, event_type text nullable |
| Configuration | config_schema_version integer default 1, trigger_config/filter_config/scope_config/task_config/delivery_config JSONB objects default {}, scope_type text default owner, config_hash text |
| Lifecycle | status text default active, next_trigger_at/last_triggered_at timestamptz nullable, trigger_count bigint default 0, consecutive_failures integer default 0, last_error text nullable |
| Time | created_at and updated_at timezone-aware with server now defaults |

Add only target/trigger/status values, positive versions, and nonnegative-counter checks. Service
validation owns scope policy, JSON shapes, and schedule/event combinations. Keep these proven
indexes:

- ix_automation_triggers_target
- ix_automation_triggers_due_schedule, partial for active due schedules
- ix_automation_triggers_active_event, partial for active events
- uq_automation_triggers_active_config, unique while status is not archived

- [ ] **Step 4: Map agent_runs**

Use SQLModel directly because a run has created_at but no mutable updated_at. Map:

| Group | Columns |
| --- | --- |
| Identity | run_id UUID PK, organisation_id UUID, owner_user_id UUID |
| Parents | agent_id UUID FK RESTRICT, trigger_id UUID nullable FK RESTRICT, agent_revision integer |
| Dispatch | status text default pending, idempotency_key/source_domain/event_type/source_event_id text nullable, scheduled_for timestamptz nullable |
| Evidence | trigger_snapshot JSONB object default {}, agent_snapshot JSONB object, input_snapshot JSONB object default {} |
| Result | output_text text nullable, output_json JSONB nullable, error_code/error_message text nullable |
| Claim/usage | attempt_count integer default 0, claimed_at timestamptz nullable, claimed_by/model_name text nullable, token_usage JSONB object nullable, cost numeric nullable, latency_ms integer nullable |
| Time | created_at timestamptz server default now, started_at/completed_at timestamptz nullable |

Add status, revision, and nonnegative numeric checks. Add owner history plus FK-leading agent and
trigger history indexes, the partial pending-claim index, and partial unique idempotency index.
Defer a running-lease index until its worker recovery query exists.

- [ ] **Step 5: Export the public model surface**

Import all nine enums plus UserAgent, AutomationTrigger, and AgentRun in
src/domain/models/organisation_tables/__init__.py and append their names to __all__.

- [ ] **Step 6: Run the focused test**

Run:

~~~powershell
uv run pytest tests/test_agent_automation_schema.py -q
~~~

Expected: model assertions pass; the migration assertion remains red until Task 3.

### Task 3: Create the organisation migration

**Files:**

- Create: migrations/org_migrations/versions/3c7f79637721_078_agent_automation_tables.py
- Test: tests/test_agent_automation_schema.py

- [ ] **Step 1: Create tables in dependency order**

Set:

~~~python
revision: str = "3c7f79637721"
down_revision: str | None = "3c7f79637722"
~~~

upgrade() creates user_agents, automation_triggers, then agent_runs. Use PostgreSQL JSONB,
timezone-aware DateTime, stable state/version/numeric checks, explicit server defaults, and
RESTRICT run-history foreign keys matching the model metadata. Do not encode evolving JSON or
lifecycle policy in this first migration.

- [ ] **Step 2: Create the exact indexes from Task 2**

Use op.create_index with PostgreSQL predicates. Descending timestamp expressions must remain
descending in both model metadata and DDL.

- [ ] **Step 3: Implement a reversible downgrade**

Drop agent-run indexes and agent_runs first, trigger indexes and automation_triggers second, then
user-agent indexes and user_agents last.

- [ ] **Step 4: Run the focused contract**

Run:

~~~powershell
uv run pytest tests/test_agent_automation_schema.py -q
~~~

Expected: all focused schema tests pass.

### Task 4: Verify the migration slice

**Files:**

- Verify all changed Python files and both design/plan documents.

- [ ] **Step 1: Check the Alembic graph**

~~~powershell
uv run alembic -c migrations/org_migrations/alembic.ini heads
~~~

Expected: exactly 3c7f79637721 (head).

- [ ] **Step 2: Run focused migration safety checks**

~~~powershell
uv run pytest tests/test_agent_automation_schema.py tests/migrations/test_org_migration_head_contract.py -q
uv run python scripts/run_org_migrations.py --all --precheck
~~~

Expected: focused tests pass and the precheck reports a valid single organisation-migration head.

- [ ] **Step 3: Run code-quality checks**

~~~powershell
uv run ruff check src/domain/models/organisation_tables/agent_models.py src/domain/models/organisation_tables/__init__.py migrations/org_migrations/versions/3c7f79637721_078_agent_automation_tables.py tests/test_agent_automation_schema.py
python .agents/skills/writing-python-comments/scripts/check_comment_smells.py src/domain/models/organisation_tables/agent_models.py migrations/org_migrations/versions/3c7f79637721_078_agent_automation_tables.py
git diff --check
~~~

Expected: every command exits zero.

- [ ] **Step 4: Audit scope**

Confirm git status contains the two design documents, this plan, the model module, package export,
migration, and focused test only. Do not commit, push, deploy, or alter reminders without a separate
user request.
