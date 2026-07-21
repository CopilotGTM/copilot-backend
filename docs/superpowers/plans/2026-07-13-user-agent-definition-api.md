# Agent Definition CRUD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add production user-owned agent definition CRUD with catalog validation, strict owner isolation, atomic revision checks, and a versioned FastAPI surface.

**Architecture:** A thin `definitions_handler.py` binds authenticated organisation/user identity and owns transactions. `AgentDefinitionService` orchestrates an owner-scoped access module, a code-catalog/JSON validator, and response serializers; triggers and runtime remain absent.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLModel/SQLAlchemy async, PostgreSQL JSONB, jsonschema Draft 2020-12, pytest, Ruff.

---

### Task 1: Lock API and validation contracts

**Files:**
- Create: `src/api/schemas/agent_models.py`
- Create: `tests/api/routers/agents/test_agent_definition_contracts.py`
- Create: `tests/service/agents/test_agent_definition_validator.py`

- [ ] Write failing tests for trimmed text, forbidden ownership/provenance fields, positive
  `expected_revision`, unique capability lists, JSON output requiring an output schema, and
  active/paused-only replacement status.
- [ ] Run
  `uv run pytest tests/api/routers/agents/test_agent_definition_contracts.py tests/service/agents/test_agent_definition_validator.py -q`.
  Confirm failure is caused by missing agent contracts/validator.
- [ ] Add `AgentDefinitionBase`, `AgentCreateRequest`, `AgentUpdateRequest`, `AgentSummary`,
  `AgentDetail`, and `AgentListResponse` with `ConfigDict(extra="forbid")` and bounded fields.
- [ ] Re-run the focused tests; contract assertions must pass.

### Task 2: Add catalog and JSON validation

**Files:**
- Create: `src/service/agents/__init__.py`
- Create: `src/service/agents/errors.py`
- Create: `src/service/agents/validator.py`
- Test: `tests/service/agents/test_agent_definition_validator.py`

- [ ] Add failing tests using one real `ToolName` and skill for the happy path, plus independent
  failures for unknown tools, unknown skills, invalid Draft 2020-12 schemas, credential-bearing
  nested model settings, oversized JSON, and JSON output without a schema.
- [ ] Run `uv run pytest tests/service/agents/test_agent_definition_validator.py -q` and confirm
  the expected RED state.
- [ ] Implement `AgentDefinitionValidator`: discover skill folders containing `SKILL.md`, check
  schemas through `Draft202012Validator.check_schema`, measure canonical JSON bytes, reject
  credential-like keys recursively, and raise `AgentServiceError` with status 400.
- [ ] Re-run the validator tests and confirm GREEN.

### Task 3: Add owner-scoped persistence

**Files:**
- Create: `src/service/agents/access.py`
- Create: `tests/service/agents/test_agent_definition_access.py`

- [ ] Write failing tests that capture and PostgreSQL-compile SQLAlchemy statements. Assert every
  list/get/update/archive statement includes both tenant and owner predicates; normal reads exclude
  archived rows; writes include `revision = expected_revision`; no DELETE statement is used.
- [ ] Run `uv run pytest tests/service/agents/test_agent_definition_access.py -q` and confirm RED.
- [ ] Implement `AgentDefinitionAccess` with list/get/create/replace/archive. Conditional writes
  atomically increment revision and return `None` when the predicate does not match.
- [ ] Re-run the access tests and confirm GREEN.

### Task 4: Add service orchestration and serializers

**Files:**
- Create: `src/service/agents/serializers.py`
- Create: `src/service/agents/definition_service.py`
- Modify: `src/service/agents/__init__.py`
- Create: `tests/service/agents/test_agent_definition_service.py`

- [ ] Write failing service tests proving create injects authenticated organisation/owner and
  server defaults; replace validates and increments one revision; stale replace returns 409;
  archive is soft; cross-owner/archived lookups return 404; service code never commits.
- [ ] Run `uv run pytest tests/service/agents/test_agent_definition_service.py -q` and confirm RED.
- [ ] Implement the service with `(session, organisation_id, owner_user_id)`, map public
  `agent_model_config` to `UserAgent.model_settings`, serialize response DTOs, and use one
  owner-scoped follow-up read to resolve 404 versus 409 after a failed conditional write.
- [ ] Run `uv run pytest tests/service/agents -q` and confirm GREEN.

### Task 5: Add versioned FastAPI endpoints

**Files:**
- Create: `src/api/routers/agents/__init__.py`
- Create: `src/api/routers/agents/definitions_handler.py`
- Modify: `src/api/fastapi_app.py`
- Create: `tests/api/routers/agents/test_agent_definitions_handler.py`
- Modify: `tests/api/routers/test_external_api_versioning.py`

- [ ] Write failing tests for create 201 + `Location` + commit, reads without commit, replacement
  commit, archive 204, rollback on 409/500, and generated OpenAPI paths under `/users/v1/agents`.
- [ ] Run
  `uv run pytest tests/api/routers/agents tests/api/routers/test_external_api_versioning.py -q`
  and confirm RED.
- [ ] Implement a service dependency from `get_current_user` and `get_org_async_session`; apply
  read/write rate limits; commit only successful writes; roll back write failures; sanitize 500s.
- [ ] Re-run the route tests and confirm GREEN.

### Task 6: Document and verify

**Files:**
- Create: `docs/API Documentation/agents/v1-agent-definitions-api.md`

- [ ] Document concrete requests/responses, pagination, expected revisions, archive behavior,
  server-owned fields, and the separate legacy `/users/agents` route.
- [ ] Run:

  ```text
  uv run pytest tests/test_agent_automation_schema.py tests/service/agents tests/api/routers/agents tests/api/routers/test_external_api_versioning.py -q
  uv run ruff check src/service/agents src/api/routers/agents src/api/schemas/agent_models.py tests/service/agents tests/api/routers/agents
  uv run ruff format --check src/service/agents src/api/routers/agents src/api/schemas/agent_models.py tests/service/agents tests/api/routers/agents
  uv run python .agents/skills/writing-python-comments/scripts/check_comment_smells.py src/service/agents src/api/routers/agents src/api/schemas/agent_models.py
  git diff --check
  ```

- [ ] Confirm no trigger service, worker, runtime, reminder mutation, system-template
  materialization, new dependency, or database constraint was added.
