# Automation Trigger API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add user-owned schedule and CRM deal-stage trigger catalog/CRUD that pins executable
agent snapshots without dispatching runs.

**Architecture:** A generic automation trigger package owns catalogs, validation, owner-scoped SQL,
hashing, and lifecycle orchestration. FastAPI exposes the catalog globally under `/users/v1` and
trigger resources beneath an agent; handlers own commits while the service only flushes.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLModel/SQLAlchemy async, PostgreSQL JSONB,
`zoneinfo`, pytest, Ruff, mypy.

---

### Task 1: Lock HTTP And Catalog Contracts

**Files:**
- Create: `src/api/schemas/automation_trigger_models.py`
- Create: `tests/api/routers/automation/test_trigger_contracts.py`
- Create: `tests/service/automation/triggers/test_catalog.py`

- [ ] Write failing tests proving request bodies reject server-owned target/snapshot/hash/counter
  fields, trim names/goals, accept only active/paused writes, and require complete PUT payloads plus
  a timezone-aware `expected_updated_at` concurrency token.
- [ ] Write a failing catalog test requiring exactly schedule and CRM deal-stage entries, including
  backend defaults, frontend field definitions, and organization pipeline options.
- [ ] Run the two tests and verify RED from missing contracts/catalog.
- [ ] Implement bounded Pydantic request/response/catalog models with `extra="forbid"`.
- [ ] Implement the static catalog builder and rerun until GREEN.

### Task 2: Share JSON Trust-Boundary Helpers

**Files:**
- Create: `src/service/automation/__init__.py`
- Create: `src/service/automation/config_validation.py`
- Modify: `src/service/agents/validator.py`
- Test: `tests/service/agents/test_agent_definition_validator.py`
- Create: `tests/service/automation/test_config_validation.py`

- [ ] Write failing tests for recursive provider-prefixed credential keys and canonical JSON byte
  limits in trigger configs.
- [ ] Extract `validate_json_document` and `contains_credentials` from the agent validator into the
  automation helper without changing error messages or limits.
- [ ] Keep `AgentDefinitionValidator` delegating to the shared helper and run both old and new tests.

### Task 3: Implement Schedule And CRM Validation

**Files:**
- Create: `src/service/automation/triggers/__init__.py`
- Create: `src/service/automation/triggers/options.py`
- Create: `src/service/automation/triggers/validator.py`
- Create: `tests/service/automation/triggers/test_validator.py`

- [ ] Write failing schedule tests for valid IANA zones, timezone-aware starts, recurrence choices,
  deterministic next occurrence, one-time past rejection, exact JSON fields, deal-owner scope, and
  credential rejection.
- [ ] Write failing CRM tests for catalog identity, exact schema key, any/outcome/specific-stage
  mode invariants, unknown fields, org-owned pipeline/stage IDs, and selected-stage membership.
- [ ] Implement small internal Pydantic config models, recurrence projection, and the reference
  worktree's deal-stage validator using org-scoped option queries.
- [ ] Return normalized configs plus `next_trigger_at` from validation and rerun until GREEN.

### Task 4: Implement Owner-Scoped Persistence And Snapshots

**Files:**
- Create: `src/service/automation/triggers/access.py`
- Modify: `src/service/agents/serializers.py`
- Create: `src/service/automation/triggers/serializers.py`
- Create: `tests/service/automation/triggers/test_access.py`
- Create: `tests/service/automation/triggers/test_serializers.py`

- [ ] Write failing SQL compilation tests proving every trigger operation includes organization,
  owner, target type, target id, and archived-row boundaries.
- [ ] Write a failing snapshot test requiring prompt/model/tool/skill/schema/provenance/revision data
  and excluding operational agent fields.
- [ ] Implement access methods for owned target resolution, list/count, get, duplicate lookup,
  create, timestamp-conditional replace/refresh, and soft archive; no SQL DELETE is allowed.
- [ ] Implement agent execution snapshots and trigger response mapping; rerun until GREEN.

### Task 5: Implement Trigger Lifecycle Service

**Files:**
- Create: `src/service/automation/triggers/service.py`
- Create: `tests/service/automation/triggers/test_service.py`

- [ ] Write failing tests proving create injects owner/target/schema/delivery defaults, computes the
  canonical hash, pins the current agent, rejects active-on-paused and duplicates, and never commits.
- [ ] Add tests proving PUT preserves the existing target pin, refresh-target explicitly repins,
  list/detail expose update availability, stale timestamps return 409, and archive remains a soft
  delete.
- [ ] Implement `AutomationTriggerService` and map integrity races to stable `409` errors.
- [ ] Run the service tests until GREEN.

### Task 6: Add Versioned FastAPI Routes

**Files:**
- Create: `src/api/routers/automation/__init__.py`
- Create: `src/api/routers/automation/triggers_handler.py`
- Modify: `src/api/fastapi_app.py`
- Create: `tests/api/routers/automation/test_triggers_handler.py`
- Modify: `tests/api/routers/test_external_api_versioning.py`

- [ ] Write failing handler tests for authenticated service binding, read-only catalog/list/detail,
  201 + Location create, PUT/refresh commits, DELETE 204, expected-error rollback, sanitized 500,
  and OpenAPI response contracts.
- [ ] Implement one request-scoped service dependency, read/write rate limits, transaction ownership,
  and routes from the approved design.
- [ ] Run handler/versioning tests until GREEN.

### Task 7: Document And Verify

**Files:**
- Create: `docs/API Documentation/automation/v1-agent-triggers-api.md`
- Modify: `docs/superpowers/specs/2026-07-14-agent-automation-design.md`

- [ ] Document catalog, request/response examples, schedule projection, CRM filters, target pinning,
  refresh behavior, errors, and the explicit no-execution boundary.
- [ ] Run the focused schema, agent-definition, trigger service, route, and OpenAPI tests.
- [ ] Run Ruff check/format, mypy, the Python comment checker, Alembic heads, and
  `git diff HEAD --check`.
- [ ] Confirm the diff contains no dispatcher, `agent_runs` creation, Redis, worker, reminder,
  provider-ingestion, workflow, live migration, commit, or publication change.

## Self-Review

- Spec coverage: catalog, schedule, CRM event config, ownership, snapshots, duplicates, lifecycle,
  APIs, docs, and verification each have an implementation task.
- YAGNI: email/calendar/Twitter producers, dispatch, run creation, delivery, workflows, and admin
  scope are deliberately absent.
- Type consistency: public contracts use existing automation enums; source-specific JSON is parsed
  into internal strict models and normalized before hashing.
- Security: target and pipeline/stage resolution always include authenticated organization/owner
  boundaries; clients never provide snapshots, principals, or credentials.
