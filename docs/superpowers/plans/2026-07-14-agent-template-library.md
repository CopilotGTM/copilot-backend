# Code-Owned Agent Template Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship base agents as backend metadata and let users copy an exact metadata version into their normal owned agents.

**Architecture:** `template_metadata.py` is the temporary source of truth for platform base agents. Catalog reads are database-free; `Use Template` passes the selected metadata through the existing definition validator and persists a normal `user_agents` row with `source_type=system`, `source_agent_id=<template_id>`, and `source_version=<version>`. Triggers and runs continue to target only the copied user agent.

**Tech Stack:** Python, FastAPI, Pydantic v2, SQLModel, pytest

---

## V1 boundary

- No template table or migration.
- No admin create/update endpoints.
- No community publishing or automatic updates.
- One current metadata version per template.
- Existing user-agent schema, editor, triggers, runtime, and run history are reused unchanged.
- Moving metadata into tables later changes the catalog repository, not the user-agent execution flow.

## Metadata contract

Each template contains:

- stable UUID, slug, and positive version;
- display name, description, and publication time;
- agent type, prompt, model/configuration, limits, tools, and skills;
- input/output schemas and output mode.

The first template is `Deal Stage Health Review` version 1. It uses the existing
`deal-health-check` skill and read-only deal, stakeholder, risk, email, note, and task tools. It
returns structured health, confidence, risks, and recommended actions. It cannot mutate CRM data
or send communications.

## API contract

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/users/v1/agent-templates` | List current code-owned template cards |
| `GET` | `/users/v1/agent-templates/{template_id}` | Get the current complete metadata definition |
| `POST` | `/users/v1/agent-templates/{template_id}/use` | Copy the selected exact version into the user's agents |

Use request:

```json
{
  "version": 1
}
```

Use returns `201 Created`, the normal `AgentDetail`, and a `Location` header pointing to the new
`/users/v1/agents/{agent_id}` resource. Unknown IDs or versions return `404`; the backend never
substitutes another version.

## Implementation tasks

### Task 1: Code-owned metadata

**Files:**

- `src/api/schemas/agent_models.py`
- `src/service/agents/template_metadata.py`
- `tests/service/agents/test_agent_template_metadata.py`

- [x] Add bounded template summary/detail/use schemas.
- [x] Define stable `Deal Stage Health Review` metadata.
- [x] Return defensive copies from list/detail lookup functions.
- [x] Convert metadata into the existing `AgentCreateRequest` contract.
- [x] Validate the example against the real tool, skill, and JSON Schema catalogs.
- [x] Prove the example has no CRM-write or outbound communication capabilities.

### Task 2: Materialise a user-owned agent

**Files:**

- `src/service/agents/definition_service.py`
- `tests/service/agents/test_agent_definition_service.py`

- [x] Reuse one validated user-agent constructor.
- [x] Resolve the exact template ID/version.
- [x] Copy every execution field into a private active user agent at revision 1.
- [x] Store `source_type=system`, template ID in `source_agent_id`, and metadata version.
- [x] Return `404` without creating a row for unknown versions.

### Task 3: Catalog and use API

**Files:**

- `src/api/routers/agents/templates_handler.py`
- `src/api/routers/agents/__init__.py`
- `src/api/fastapi_app.py`
- `tests/api/routers/agents/test_agent_templates_handler.py`
- `tests/api/routers/test_external_api_versioning.py`

- [x] Add authenticated catalog and detail reads without database access.
- [x] Add a transaction-owning use endpoint through `AgentDefinitionService`.
- [x] Commit only after materialisation and roll back expected/unexpected failures.
- [x] Mount and document versioned routes and the `201 Location` header.

### Task 4: Documentation and verification

**Files:**

- `docs/API Documentation/agents/v1-agent-templates-api.md`
- `docs/API Documentation/agents/v1-agent-definitions-api.md`
- `docs/superpowers/specs/2026-07-14-agent-automation-design.md`

- [x] Replace the table proposal with the metadata-first boundary.
- [x] Run focused tests, Ruff, mypy, comment checks, and `git diff --check`.
- [x] Do not apply migrations, start workers, execute an LLM, commit, or push.

## Future database cutover

When the admin UI is required, add template/version persistence and make the catalog service read
from it. Keep template IDs and version numbers stable, preserve the three user endpoints, and keep
`Use Template` materialising the same `UserAgent` fields. Existing copied agents require no data
migration because they already contain complete executable definitions and provenance.
