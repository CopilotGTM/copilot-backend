# CRM Deal Service Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a vertical `src/service/crm/deals/` module so API, agents, workflows, and bot handlers use the same production-grade deal read/write rules.

**Architecture:** Deals are the CRM aggregate root for internal deal state, but they should not remain one large service file. Extract the current read and write behavior into focused deal services while keeping old `access` and `internal` files as compatibility facades until callers are migrated. Deal mutation events stay tied to committed deal writes, but workflow dispatch remains out of scope for this service cleanup.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, pytest, mypy, ruff.

---

## Starting Point

This plan is based on the current checkout on 2026-06-20.

Current facts:

- `src/service/crm/deals/` does not exist.
- Deal reads live in `src/service/crm/access/deals_access_service.py`.
- Internal deal writes live in `src/service/crm/internal/deal_write_service.py`.
- `src/service/crm/internal/deal_write_service.py` is large and owns:
  - deal create/update/delete
  - custom field validation/coercion
  - stage movement and stage history
  - close/reopen lifecycle writes
  - owner assignment
  - account linking and company participant replacement
  - primary contact upsert/linking
  - team member and contact participant linking
  - internal CRM setup validation
- `src/api/routers/crm/deals_handler.py` imports both `DealsAccessService` and `DealWriteService`.
- Agent tools and bot form handlers still import `src.service.crm.internal.deal_write_service.DealWriteService`.
- The account/contact/participant standardization work has already established the vertical module pattern:

```text
src/service/crm/accounts/
src/service/crm/contacts/
src/service/crm/participants/
```

- CRM mutation event table/model work exists in the current working tree, but it is not a clean baseline assumption for this plan unless those exact files are included in the same PR or merged first.
- Workflow dispatcher execution is not implemented yet and remains out of scope.

## Current Problem

Deal behavior is centralized in name only. In practice, the current write service mixes too many responsibilities.

Production risks:

- A stage update, lifecycle update, field update, account update, owner update, and participant update all pass through the same large file.
- Future workflow/agent/API write paths have to import an internal service whose boundary is unclear.
- Core deal field validation and custom field validation are coupled to stage/lifecycle/account/contact orchestration.
- A single multi-field update needs one mutation event, but the current service shape makes it easy to emit duplicate events from sub-functions.
- The current compatibility imports make it hard to migrate callers safely without breaking agents and bot handlers.
- Provider sync writers should not be routed through internal CRM write rules, but the current naming does not make that distinction obvious.

## Core Decision

Create a vertical deal module:

```text
src/service/crm/deals/
  __init__.py
  contracts.py
  read_service.py
  write_service.py
  custom_field_service.py
  stage_service.py
  lifecycle_service.py
  owner_service.py
  relationship_service.py
  mutation_event_service.py
```

Temporary compatibility surfaces stay:

```text
src/service/crm/access/deals_access_service.py
src/service/crm/internal/deal_write_service.py
```

The new services must own the actual implementation. The old files become facades only after their behavior is extracted.

## Target Boundaries

### `src/service/crm/deals/contracts.py`

Owns deal service DTOs and exceptions:

- `DealReadServiceError`
- `DealWriteServiceError`
- `DealWriteContext`
- `DealWriteResult`
- `DealMutationIntent`
- `DealRelationshipChangeResult`
- `DealStageChangeResult`
- `DealLifecycleChangeResult`
- `DealOwnerChangeResult`

### `src/service/crm/deals/read_service.py`

Owns read-only deal behavior:

- `list_deals()`
- `get_all_deals()`
- `get_deal_by_id()`
- `get_deal_stage_history()`
- `get_pipeline_stages()`
- `get_company_by_id()`
- `get_deals_stats()`
- `get_deal_team_members_batch()`
- `list_deal_owners()`
- `resolve_current_user_deal_owners()`

It must not create or update `CRMDeal`.

### `src/service/crm/deals/write_service.py`

Owns core internal deal state writes:

- `create_deal()`
- `update_deal()`
- `delete_deal()` after lifecycle extraction is complete only if the facade contract still needs it
- internal deal loading and internal CRM validation

It may orchestrate other deal services, but should not own the internal implementation of stage, owner, lifecycle, custom fields, account/contact relationship logic after those slices are extracted.

### `src/service/crm/deals/custom_field_service.py`

Owns deal custom field validation and coercion:

- normalize custom field keys
- load active internal deal custom field definitions
- reject unknown custom fields
- reject formula fields for writes
- coerce text, integer, number, boolean, date, datetime, currency, select, multiselect

This service should be reusable by API, agents, import assistant, and future workflows.

### `src/service/crm/deals/stage_service.py`

Owns stage movement:

- load stage
- validate pipeline/integration
- move deal to stage
- update stage timestamps
- write `CRMDealStageHistory`
- no-op when requested stage is already current

### `src/service/crm/deals/lifecycle_service.py`

Owns lifecycle writes:

- `close_deal()`
- `reopen_deal()`
- state-specific stage resolution
- close timestamps
- closed reason clearing/preservation
- no-op lifecycle guard

### `src/service/crm/deals/owner_service.py`

Owns owner assignment:

- `assign_owner()`
- load/ensure CRM user through `InternalCRMUserService`
- clear owner
- no-op when same owner is requested

### `src/service/crm/deals/relationship_service.py`

Owns deal relationship orchestration that is not pure deal scalar data:

- account payload applied to deal
- company participant replace/sync
- primary contact upsert/link
- selected contact participant materialization/linking
- team member participant linking

This service should delegate durable account/contact/participant writes to canonical services:

```text
AccountWriteService
ContactWriteService or ContactDealLinkService where available
ParticipantWriteService
```

It must not become a new account/contact write service.

### `src/service/crm/deals/mutation_event_service.py`

Owns CRM mutation event row creation for internal deal writes:

- one user/API/agent/workflow action creates one mutation event
- changed fields are stored together
- semantic event types are derived from changed fields
- source and actor metadata come from `DealWriteContext`
- no workflow dispatcher runtime state is stored here

Workflow dispatch is deliberately deferred. The mutation event row is the historical/audit and future workflow-trigger source, not a workflow run ledger.

## Non-Negotiable Rules

- Do not change provider sync deal writers in this plan.
- Do not move HubSpot/Salesforce deal ingestion into internal deal write services.
- Do not delete old deal access/internal services in the first implementation slice.
- New deal code must live under `src/service/crm/deals/`.
- New internal deal reads should use `DealReadService`.
- New internal deal writes should use `src.service.crm.deals.write_service.DealWriteService`.
- Old `DealsAccessService` and old internal `DealWriteService` must become compatibility facades, not duplicate implementations.
- New write services must not commit transactions. Routers, tools, workers, and bot handlers own commit/rollback.
- Every query/write must filter or set `organisation_id`.
- Internal write services must reject provider-synced external CRM deals as read-only.
- One user/API action that changes five fields creates one mutation event, not five unrelated mutation events.
- Stage, owner, lifecycle, account, and primary-contact sub-services return change results to the orchestrator; they should not emit duplicate mutation events on their own unless they are called as standalone public write operations.
- Do not add workflow dispatcher tables or workers in this slice.
- Do not create a new migration in this service-standardization slice.
- If the mutation-event model/migration files are not already merged into the target branch, either include the existing mutation-event model/migration files as an explicit prerequisite commit in this PR or pause Task 10 until that migration slice lands. Do not silently invent a second migration.

## Recommended Implementation Order

The fastest safe path is not to split the whole file at once. Use these phases:

```text
Phase 1: module skeleton, contracts, read service, compatibility facade
Phase 2: custom field service and core deal create/update writer
Phase 3: stage service
Phase 4: lifecycle service
Phase 5: owner service
Phase 6: relationship service for account/contact/team participant orchestration
Phase 7: mutation event service integration
Phase 8: route API, agents, bot handlers to new services
Phase 9: old facade cleanup after imports are migrated
```

## Loopholes To Close

### Loophole 0: Mutation Event Baseline Is Ambiguous

Risk:

```text
Task 10 imports CRMDealMutationEvent because the current working tree has local
model/migration files, but the branch target does not. The service PR then
either fails on a clean checkout or accidentally publishes unrelated WIP.
```

Required behavior:

```text
Before implementing Task 10, verify whether CRMDealMutationEvent is already in
the target branch. If not, choose exactly one of these paths:

1. Include the existing mutation-event model/migration files in the deal-service
   branch as an explicit prerequisite slice.
2. Stop before Task 10 and leave mutation recording deferred until the
   mutation-event migration PR is merged.

Do not create a second mutation-event migration in this plan.
```

Boundary check:

```bash
git diff --name-only -- src/domain/models/organisation_tables/sync/crm/deals.py src/domain/models/organisation_tables/sync/crm/__init__.py migrations/org_migrations/versions
```

Expected: any mutation-event model/migration files are either intentionally included in the implementation branch or absent because Task 10 is deferred.

### Loophole 1: Hollow Services

Risk:

```text
src/service/crm/deals/write_service.py exists, but it delegates back to
src/service/crm/internal/deal_write_service.py.
```

Required behavior:

```text
Move implementation into the new deal module.
The old internal file may delegate to the new service.
The new service must not delegate to the old internal file.
```

Boundary check:

```bash
rg -n "internal\\.deal_write_service|from src.service.crm.internal.deal_write_service" src/service/crm/deals
```

Expected: no output.

### Loophole 2: One User Save Creates Multiple Mutation Events

Risk:

```text
update_deal() changes amount, expected_close_date, stage, and priority.
StageService emits one event, WriteService emits another event, and custom field code emits a third event.
```

Required behavior:

```text
The orchestrating write method collects changed_fields once.
MutationEventService writes one crm.deal.updated event with semantic_event_types.
Sub-services return field changes but do not independently write events for the same action.
```

Example:

```json
{
  "event_type": "crm.deal.updated",
  "semantic_event_types": [
    "crm.deal.stage_changed",
    "crm.deal.amount_changed"
  ],
  "changed_fields": {
    "stage_id": {"old": "stage_discovery", "new": "stage_proposal"},
    "expected_close_date": {"old": "2026-07-01", "new": "2026-08-01"},
    "amount": {"old": 50000, "new": 65000},
    "priority": {"old": "medium", "new": "high"}
  }
}
```

### Loophole 3: Facade Transaction Behavior Changes Silently

Risk:

```text
Old DealWriteService method used by an agent no longer commits or starts returning a new DTO,
breaking an unmigrated caller.
```

Required behavior:

```text
Either migrate the caller in the same task, or preserve the facade's old return shape.
Do not silently change an old public method's behavior while production callers remain on it.
Until the explicit return-shape migration task, public create/update/stage/lifecycle/
owner/relationship methods must return CRMDeal to legacy callers.
Internal helper methods may return DealWriteResult or change-result DTOs only when
the public facade unwraps them back to CRMDeal.
```

### Loophole 4: Provider Sync Gets Mixed With Internal Writes

Risk:

```text
Salesforce or HubSpot sync starts using internal DealWriteService and emits internal mutation events
for provider-owned changes before we have a provider-sync mutation policy.
```

Required behavior:

```text
Provider sync writers stay in src/service/integrations/crm/records/*/deals/.
Internal CRM write services only handle app-owned internal CRM deals in this plan.
Provider-origin mutation events require a later provider-sync design.
```

### Loophole 5: Relationship Service Becomes Another Account/Contact Writer

Risk:

```text
DealRelationshipService starts constructing CRMCompany and CRMContact directly.
```

Required behavior:

```text
DealRelationshipService orchestrates deal relationships.
AccountWriteService owns company construction.
ContactWriteService owns contact construction.
ParticipantWriteService owns participant construction.
Exception: copied-source contact materialization may temporarily remain inside
DealRelationshipService only if ContactWriteService cannot yet preserve the
provider-source copy contract. That exception must have a code comment and a
focused regression test so it is not mistaken for the long-term boundary.
```

Boundary check:

```bash
rg -n "CRMCompany\\(|CRMContact\\(|CRMDealParticipant\\(" src/service/crm/deals/relationship_service.py
```

Expected: no direct construction except type annotations or imports required for returned model types. If a direct construction remains, document why in the code and add a focused test.

### Loophole 6: Custom Field Logic Stays Trapped In Deal Writer

Risk:

```text
API writes validate custom fields, but agent/workflow writes later skip validation.
```

Required behavior:

```text
DealCustomFieldWriteService is reusable by API, agent tools, imports, and future workflow writers.
All internal custom field writes route through it.
```

### Loophole 7: Stage History And Lifecycle Drift Apart

Risk:

```text
close_deal(stage_id=target_stage_id) updates deal.stage_id but does not close the previous stage history row.
move_stage() does it correctly.
```

Required behavior:

```text
LifecycleService uses StageService when changing stage.
There is one implementation for stage timestamp and history mutation.
```

### Loophole 8: No-Op Writes Pollute History

Risk:

```text
assign_owner() called with the same owner still updates timestamps and emits a mutation event.
move_stage() called with the current stage still creates stage history.
```

Required behavior:

```text
No-op writes return the current deal without mutation event and without new stage history.
If the API needs a success response, it can still return 200 with the current deal.
```

### Loophole 10: Mutation Event Idempotency Conflicts Are Undefined

Risk:

```text
A caller retries with the same idempotency_key. The unique index rejects the
insert, but the service does not know whether this is a safe retry or a conflicting
payload reuse.
```

Required behavior:

```text
DealMutationEventService must treat idempotency_key as tenant-scoped:

- same idempotency_key + same payload_hash returns the existing event without
  inserting a duplicate row
- same idempotency_key + different payload_hash raises DealWriteServiceError
  with status_code=409
- no idempotency_key means every non-empty mutation writes a fresh event

The payload_hash must be computed before insert and used for both conflict
checking and storage.
```

### Loophole 11: Public Services Double-Emit When Composed

Risk:

```text
close_deal() calls public move_stage(), so closing with a target stage creates a
stage event and a lifecycle event for one user action.
```

Required behavior:

```text
Each service that can be called both standalone and from another deal service
must expose an internal helper that accepts emit_mutation_event=False or an
equivalent private method that does not record events.

Public methods may emit mutation events for standalone calls.
Composed writes must call the non-emitting helper and let the top-level user
action record exactly one event.
```

### Loophole 9: Router Response Mapping Remains Too Coupled

Risk:

```text
Deal service extraction succeeds, but response shaping and currency/custom-column mapping
remain scattered across router helpers.
```

Required behavior:

```text
Do not move response mapping in the first service split.
After write boundaries stabilize, create a separate response-mapper plan if router size remains a problem.
```

This keeps the first deal slice focused on service correctness, not API presentation refactor.

## Flow Examples

### Flow A: Create Deal From API

```text
POST /users/crm/deals
  -> router validates request schema
  -> DealWriteService.create_deal(payload, context)
  -> load internal CRM integration
  -> resolve default pipeline/stage if omitted
  -> create CRMDeal
  -> StageService creates initial CRMDealStageHistory
  -> RelationshipService applies account/team/contact participants
  -> MutationEventService writes crm.deal.created
  -> service flushes and returns DealWriteResult
router commits
router refreshes deal
router dispatches post-commit stakeholder research for created participant IDs
frontend receives DealWriteResponse
```

### Flow B: Update Five Deal Fields In One API Request

```text
PATCH /users/crm/deals/{deal_id}
  -> router passes one payload to DealWriteService.update_deal()
  -> service loads internal deal with organisation_id filter
  -> custom scalar fields are compared old vs new
  -> DealCustomFieldWriteService validates custom_properties
  -> StageService is called only if stage_id is part of this endpoint/payload in a later combined API
  -> changed_fields is accumulated once
  -> MutationEventService writes one crm.deal.updated row
  -> service flushes and returns DealWriteResult
router commits
future workflow dispatcher can later evaluate mutation event semantic_event_types
```

### Flow C: Move Stage

```text
POST /users/crm/deals/{deal_id}/stage
  -> DealStageService.move_stage()
  -> validate stage belongs to same org, integration, and pipeline
  -> if same stage, return current deal with changed=false
  -> close previous CRMDealStageHistory row
  -> create new CRMDealStageHistory row
  -> update deal.stage_id, stage_name, timestamps
  -> MutationEventService writes crm.deal.stage_changed if changed
router commits
```

### Flow D: Close Deal

```text
POST /users/crm/deals/{deal_id}/close
  -> DealLifecycleService.close_deal()
  -> validate outcome won/lost/closed
  -> optionally resolve close stage
  -> call DealStageService if stage changes
  -> set deal_status, close_date, closed_won_at or closed_lost_at, closed_reason
  -> MutationEventService writes crm.deal.closed
router commits
```

## File Responsibilities

### New Files

```text
src/service/crm/deals/__init__.py
src/service/crm/deals/contracts.py
src/service/crm/deals/read_service.py
src/service/crm/deals/write_service.py
src/service/crm/deals/custom_field_service.py
src/service/crm/deals/stage_service.py
src/service/crm/deals/lifecycle_service.py
src/service/crm/deals/owner_service.py
src/service/crm/deals/relationship_service.py
src/service/crm/deals/mutation_event_service.py
tests/service/crm/test_deal_service_boundaries.py
```

### Modified Files

```text
src/service/crm/access/deals_access_service.py
src/service/crm/internal/deal_write_service.py
src/api/routers/crm/deals_handler.py
src/agents/tools/crm/create_deal.py
src/agents/tools/crm/update_deal_fields.py
src/agents/tools/crm/update_deal_account.py
src/agents/tools/crm/update_deal_lifecycle.py
src/service/bots/form_handlers/create_deal_handler.py
src/service/bots/form_handlers/update_deal_stage_handler.py
src/service/crm/access/leads_access_service.py
src/service/crm/__init__.py
```

Do not modify provider sync writers in this plan:

```text
src/service/integrations/crm/records/hubspot/deals/
src/service/integrations/crm/records/salesforce/deals/
```

## Task 0: Create Deal Module And Boundary Tests

**Files:**

- Create: `src/service/crm/deals/__init__.py`
- Create: `src/service/crm/deals/contracts.py`
- Create: `tests/service/crm/test_deal_service_boundaries.py`

- [ ] **Step 1: Create the failing boundary tests**

Create `tests/service/crm/test_deal_service_boundaries.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path


DEAL_MODULE_FILES = [
    "src/service/crm/deals/__init__.py",
    "src/service/crm/deals/contracts.py",
    "src/service/crm/deals/read_service.py",
    "src/service/crm/deals/write_service.py",
    "src/service/crm/deals/custom_field_service.py",
    "src/service/crm/deals/stage_service.py",
    "src/service/crm/deals/lifecycle_service.py",
    "src/service/crm/deals/owner_service.py",
    "src/service/crm/deals/relationship_service.py",
    "src/service/crm/deals/mutation_event_service.py",
]


def test_deal_vertical_module_exists() -> None:
    for filename in DEAL_MODULE_FILES:
        assert Path(filename).exists(), filename


def test_deal_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/deals/read_service.py").read_text()

    assert "async def create_deal" not in source
    assert "async def update_deal" not in source
    assert "async def move_stage" not in source
    assert "CRMDeal(" not in source


def test_new_deal_services_do_not_import_old_internal_writer() -> None:
    result = subprocess.run(
        [
            "rg",
            "-n",
            "internal\\.deal_write_service|from src\\.service\\.crm\\.internal\\.deal_write_service",
            "src/service/crm/deals",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == ""


def test_provider_deal_sync_writers_are_out_of_scope() -> None:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--",
            "src/service/integrations/crm/records/hubspot/deals",
            "src/service/integrations/crm/records/salesforce/deals",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == ""
```

- [ ] **Step 2: Run the boundary test and verify it fails**

Run:

```bash
uv run pytest tests/service/crm/test_deal_service_boundaries.py -q
```

Expected: FAIL because `src/service/crm/deals/` does not exist yet.

- [ ] **Step 3: Create the deal package**

Create `src/service/crm/deals/__init__.py`:

```python
"""Deal read, write, lifecycle, stage, owner, and relationship services."""
```

- [ ] **Step 4: Create deal contracts**

Create `src/service/crm/deals/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal

DealWriteSource = Literal["api", "agent", "workflow", "bot_form", "import_assistant"]


@dataclass(frozen=True)
class DealWriteContext:
    """Caller metadata used for audit, mutation events, and future workflow triggers."""

    source: DealWriteSource
    actor_user_id: UUID | None
    source_label: str | None = None
    approval_state_id: UUID | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class DealWriteResult:
    """Canonical result for internal CRM deal writes."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    semantic_event_types: list[str] = field(default_factory=list)
    mutation_event_id: UUID | None = None
    changed: bool = True


@dataclass(frozen=True)
class DealStageChangeResult:
    """Stage mutation result returned to write/lifecycle orchestration."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    changed: bool


@dataclass(frozen=True)
class DealLifecycleChangeResult:
    """Lifecycle mutation result returned by close/reopen operations."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    semantic_event_type: str | None
    changed: bool


@dataclass(frozen=True)
class DealOwnerChangeResult:
    """Owner mutation result returned by owner assignment operations."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    changed: bool


@dataclass(frozen=True)
class DealRelationshipChangeResult:
    """Relationship mutation result for account/contact/team participant orchestration."""

    deal: CRMDeal
    changed_fields: dict[str, dict[str, Any]]
    created_contact_participant_ids: list[UUID] = field(default_factory=list)
    changed: bool = False


class DealReadServiceError(Exception):
    """HTTP-shaped deal read error for routers and tools."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealWriteServiceError(Exception):
    """HTTP-shaped deal write error for routers and tools."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
```

- [ ] **Step 5: Run the module existence test**

Run:

```bash
uv run pytest tests/service/crm/test_deal_service_boundaries.py::test_deal_vertical_module_exists -q
```

Expected: FAIL until the remaining service files are created in later tasks.

## Task 1: Extract DealReadService

**Files:**

- Create: `src/service/crm/deals/read_service.py`
- Modify later: `src/service/crm/access/deals_access_service.py`
- Test: `tests/service/crm/test_deals_access_service_team_members.py`
- Test: `tests/service/crm/test_deal_service_boundaries.py`

- [ ] **Step 1: Create `DealReadService` from current access service**

Create `src/service/crm/deals/read_service.py` by moving read-only behavior from `src/service/crm/access/deals_access_service.py`.

The class must be:

```python
class DealReadService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Move these methods without behavior changes:

```text
close
session property
_deal_status_to_db_value
list_deals
get_all_deals
get_deal_by_id
get_deal_stage_history
get_pipeline_stages
get_company_by_id
get_deals_stats
get_deal_team_members_batch
list_deal_owners
resolve_current_user_deal_owners
extract_deal_health
extract_deal_health_reason
```

Replace `DealsAccessServiceError` with `DealReadServiceError`.

- [ ] **Step 2: Keep read service write-free**

`src/service/crm/deals/read_service.py` must not contain:

```text
CRMDeal(
CRMDealStageHistory(
async def create_deal
async def update_deal
async def delete_deal
async def move_stage
```

- [ ] **Step 3: Run focused read tests**

Run:

```bash
uv run pytest tests/service/crm/test_deals_access_service_team_members.py tests/service/crm/test_deal_service_boundaries.py::test_deal_read_service_does_not_expose_writes -q
```

Expected: PASS after the read service exists and the old access facade still preserves behavior.

## Task 2: Convert DealsAccessService To Read Facade

**Files:**

- Modify: `src/service/crm/access/deals_access_service.py`
- Test: `tests/service/crm/test_deals_access_service_team_members.py`

- [ ] **Step 1: Replace implementation with delegation**

Keep the public class:

```python
class DealsAccessService:
    """Compatibility facade; use DealReadService for new code."""
```

Construct:

```python
self._reader = DealReadService(org_session, organisation_id)
```

Delegate read methods to `_reader`.

- [ ] **Step 2: Preserve exception compatibility**

Keep:

```python
class DealsAccessServiceError(DealReadServiceError):
    """Compatibility alias for old deal read callers."""
```

If subclassing causes test or typing issues, use direct aliasing:

```python
DealsAccessServiceError = DealReadServiceError
```

- [ ] **Step 3: Verify access facade still works**

Run:

```bash
uv run pytest tests/service/crm/test_deals_access_service_team_members.py -q
```

Expected: PASS.

## Task 3: Extract Deal Custom Field Write Service

**Files:**

- Create: `src/service/crm/deals/custom_field_service.py`
- Modify later: `src/service/crm/deals/write_service.py`
- Test: `tests/service/crm/test_custom_field_source_isolation.py`
- Test: `tests/service/crm/test_deal_write_service_utils.py`

- [ ] **Step 1: Create custom field service**

Create `src/service/crm/deals/custom_field_service.py`:

```python
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)
from src.service.crm.deals.contracts import DealWriteServiceError


class DealCustomFieldWriteService:
    """Validates and coerces internal deal custom field writes."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
```

Move these methods from old `DealWriteService`:

```text
_validate_custom_properties -> validate_custom_properties
_coerce_custom_value -> coerce_custom_value
_parse_boolean -> parse_boolean
_parse_date -> parse_date
_parse_datetime -> parse_datetime
_parse_currency -> parse_currency
_normalize_custom_key -> normalize_custom_key
```

- [ ] **Step 2: Keep the old static helpers available through compatibility**

Old tests currently import helper methods from `src.service.crm.internal.deal_write_service.DealWriteService`. The old facade can keep static wrappers:

```python
@staticmethod
def _parse_datetime(value: Any) -> str:
    return DealCustomFieldWriteService.parse_datetime(value)
```

Repeat wrappers for `_parse_currency`, `_normalize_custom_key`, and other utility methods that tests or callers import.

- [ ] **Step 3: Run custom field tests**

Run:

```bash
uv run pytest tests/service/crm/test_custom_field_source_isolation.py tests/service/crm/test_deal_write_service_utils.py -q
```

Expected: PASS.

## Task 4: Create Core DealWriteService In New Module

**Files:**

- Create: `src/service/crm/deals/write_service.py`
- Modify later: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_deal_write_service_stage_history.py`
- Test: `tests/service/crm/test_deal_write_service_account_updates.py`

- [ ] **Step 1: Create the new write service class**

Create `src/service/crm/deals/write_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.service.crm.deals.contracts import DealWriteContext, DealWriteResult, DealWriteServiceError
from src.service.crm.deals.custom_field_service import DealCustomFieldWriteService
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService


class DealWriteService:
    """Canonical internal CRM deal write orchestrator."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
    ):
        self.session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self._custom_fields = DealCustomFieldWriteService(self.session, self.organisation_id)
```

- [ ] **Step 2: Move core create/update code**

Move these methods from old `DealWriteService` into new `DealWriteService`:

```text
close
create_deal
update_deal
_load_internal_deal
_load_pipeline
_load_stage
_get_default_pipeline
_get_first_stage
_clean_string
_normalize_contact_name
_build_contact_display_name
_normalize_email
```

During this task, keep relationship helper methods in the old file only if moving them would make the slice too large. If the new `create_deal()` still needs them, move only the called helper methods required for create/update to work.

- [ ] **Step 3: Route custom properties through DealCustomFieldWriteService**

Replace:

```python
validated_custom = (
    custom_props
    if skip_custom_validation
    else await self._validate_custom_properties(custom_props)
)
```

with:

```python
validated_custom = (
    custom_props
    if skip_custom_validation
    else await self._custom_fields.validate_custom_properties(custom_props)
)
```

- [ ] **Step 4: Preserve current return shape for this slice**

Keep new `create_deal()` and `update_deal()` returning `CRMDeal` initially:

```python
async def create_deal(self, payload: dict[str, Any]) -> CRMDeal:
    return await self._create_deal_row_and_relationships(payload)

async def update_deal(
    self,
    deal_id: UUID,
    payload: dict[str, Any],
    *,
    skip_custom_validation: bool = False,
) -> CRMDeal:
    return await self._update_deal_scalar_fields(
        deal_id=deal_id,
        payload=payload,
        skip_custom_validation=skip_custom_validation,
    )
```

Do not switch callers to `DealWriteResult` until mutation event integration. This avoids a large router/agent regression in the first slice.

- [ ] **Step 5: Verify focused deal write tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py tests/service/crm/test_deal_write_service_account_updates.py -q
```

Expected: PASS with old imports still working through the compatibility facade.

## Task 5: Convert Old Internal DealWriteService To Facade

**Files:**

- Modify: `src/service/crm/internal/deal_write_service.py`
- Test: `tests/service/crm/test_deal_write_service_stage_history.py`
- Test: `tests/service/crm/test_deal_write_service_account_updates.py`
- Test: `tests/service/crm/test_deal_write_service_account_copy.py`

- [ ] **Step 1: Replace old class implementation with delegation**

Keep:

```python
class DealWriteService:
    """Compatibility facade; use src.service.crm.deals.write_service.DealWriteService."""
```

The facade should construct:

```python
from src.service.crm.deals.write_service import DealWriteService as CanonicalDealWriteService

self._writer = CanonicalDealWriteService(
    org_session=org_session,
    master_session=master_session,
    organisation_id=organisation_id,
    actor_user_id=actor_user_id,
)
```

Delegate public methods:

```text
create_deal
update_deal
delete_deal
move_stage
close_deal
reopen_deal
assign_owner
update_account
update_primary_contact
close
```

Keep static helper wrappers required by tests:

```text
_normalize_company_name
_normalize_contact_name
_normalize_email
_parse_datetime
_parse_currency
```

- [ ] **Step 2: Preserve error compatibility**

Old imports must keep working:

```python
from src.service.crm.deals.contracts import DealWriteServiceError
```

The facade can re-export this name.

- [ ] **Step 3: Verify old import behavior**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py tests/service/crm/test_deal_write_service_account_updates.py tests/service/crm/test_deal_write_service_account_copy.py tests/service/crm/test_deal_write_service_utils.py -q
```

Expected: PASS.

## Task 6: Extract DealStageService

**Files:**

- Create: `src/service/crm/deals/stage_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Test: `tests/service/crm/test_deal_write_service_stage_history.py`

- [ ] **Step 1: Create stage service**

Create `src/service/crm/deals/stage_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMDealStageHistory,
)
from src.service.crm.deals.contracts import DealStageChangeResult, DealWriteServiceError


class DealStageService:
    """Owns internal deal stage transitions and stage history rows."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
```

Move these methods from the current deal writer:

```text
move_stage
_apply_stage_update
_record_stage_history
_normalize_stage_state
_load_stage
```

- [ ] **Step 2: Make stage no-op explicit**

When requested stage is already current:

```python
return DealStageChangeResult(deal=deal, changed_fields={}, changed=False)
```

No new `CRMDealStageHistory` row should be added.

- [ ] **Step 3: Route write service stage calls through DealStageService**

In `src/service/crm/deals/write_service.py` constructor:

```python
self._stage_service = DealStageService(self.session, self.organisation_id)
```

Public `move_stage()` on `DealWriteService` should call stage service and return `result.deal` until callers migrate to `DealWriteResult`.

- [ ] **Step 4: Verify stage history tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py -q
```

Expected: PASS.

## Task 7: Extract DealLifecycleService

**Files:**

- Create: `src/service/crm/deals/lifecycle_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Test: focused lifecycle tests under `tests/service/crm/`

- [ ] **Step 1: Add lifecycle tests if missing**

If there is no focused lifecycle test file, create `tests/service/crm/test_deal_lifecycle_service.py` with tests covering these assertions:

```text
test_close_deal_sets_won_fields_together:
  assert deal_status == won
  assert closed_won_at is not None
  assert closed_lost_at is None
  assert changed_fields contains deal_status, closed_won_at, close_date

test_close_deal_sets_lost_fields_together:
  assert deal_status == lost
  assert closed_lost_at is not None
  assert closed_won_at is None
  assert changed_fields contains deal_status, closed_lost_at, closed_reason

test_reopen_deal_clears_closed_timestamps_when_requested:
  assert deal_status == open
  assert closed_won_at is None
  assert closed_lost_at is None
  assert closed_reason is None

test_close_deal_noops_when_same_closed_state_is_requested:
  assert result.changed is False
  assert result.changed_fields == {}
  assert no new mutation event is added
```

- [ ] **Step 2: Create lifecycle service**

Create `src/service/crm/deals/lifecycle_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStatus
from src.service.crm.deals.contracts import DealLifecycleChangeResult, DealWriteServiceError
from src.service.crm.deals.stage_service import DealStageService


class DealLifecycleService:
    """Owns close/reopen business rules for internal CRM deals."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        stage_service: DealStageService,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self._stage_service = stage_service
```

Move:

```text
close_deal
reopen_deal
_resolve_stage_for_state
```

- [ ] **Step 3: Ensure lifecycle stage changes use stage service**

If `close_deal()` or `reopen_deal()` changes `stage_id`, it must call:

```python
await self._stage_service.move_stage(deal=deal, stage_id=stage_id, source="api")
```

Use the exact stage service signature implemented in Task 6.

- [ ] **Step 4: Verify lifecycle tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_lifecycle_service.py -q
```

Expected: PASS.

## Task 8: Extract DealOwnerService

**Files:**

- Create: `src/service/crm/deals/owner_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Test: focused owner tests under `tests/service/crm/`

- [ ] **Step 1: Add owner tests if missing**

Create `tests/service/crm/test_deal_owner_service.py` if there is no focused owner coverage, with these assertions:

```text
test_assign_owner_sets_owner_snapshot:
  assert owner_id is the CRM user id
  assert owner_name is the CRM user full name
  assert owner_email is the CRM user email
  assert changed_fields contains owner_id, owner_name, owner_email

test_assign_owner_clears_owner_snapshot:
  assert owner_id is None
  assert owner_name is None
  assert owner_email is None

test_assign_owner_noops_when_same_owner_requested:
  assert result.changed is False
  assert result.changed_fields == {}
```

- [ ] **Step 2: Create owner service**

Create `src/service/crm/deals/owner_service.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.deals.contracts import DealOwnerChangeResult
from src.service.crm.internal.internal_crm_user_service import InternalCRMUserService


class DealOwnerService:
    """Owns owner assignment and owner snapshot updates for internal deals."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
    ):
        self.session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
```

Move:

```text
assign_owner
```

- [ ] **Step 3: Verify owner tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_owner_service.py -q
```

Expected: PASS.

## Task 9: Extract DealRelationshipService

**Files:**

- Create: `src/service/crm/deals/relationship_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Test: `tests/service/crm/test_deal_write_service_account_copy.py`
- Test: `tests/service/crm/test_deal_write_service_account_updates.py`

- [ ] **Step 1: Create relationship service**

Create `src/service/crm/deals/relationship_service.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.accounts.write_service import AccountWriteService
from src.service.crm.deals.contracts import DealRelationshipChangeResult, DealWriteServiceError


class DealRelationshipService:
    """Orchestrates account, contact, team, and participant links for internal deals."""

    def __init__(
        self,
        org_session: AsyncSession,
        master_session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
    ):
        self.session = org_session
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self._account_writer = AccountWriteService(self.session, self.organisation_id)
```

Move relationship helpers from deal writer:

```text
update_account
update_primary_contact
_apply_account_and_team
_upsert_contact_from_primary
_add_company_participant
_replace_company_participant
_has_active_company_participant
_sync_deal_company_snapshot
_replace_primary_contact_participant
_apply_fallback_company_name
_add_team_participants
_add_contact_participants
_load_org_contact
_ensure_internal_contact_from_source
_add_contact_participant
_get_current_company
```

- [ ] **Step 2: Delegate durable writes to canonical services**

Keep account materialization delegated:

```python
company = await self._account_writer.upsert_company_from_account(
    account_payload=account_payload,
    integration_id=deal.integration_id,
    now=now,
)
```

When contact services are stable enough, route contact construction through `ContactWriteService`. If current contact service does not yet support the exact copied-source behavior, keep the moved helper in `DealRelationshipService` for this slice and document it in a code comment:

```python
# Deal creation still supports copying provider contacts into the internal CRM.
# Keep this here until ContactWriteService exposes a copied-source contract.
```

If that exception is used, add or preserve a focused assertion in
`tests/service/crm/test_deal_write_service_account_copy.py`:

```text
test_create_deal_copies_provider_primary_contact_into_internal_contact:
  assert the copied contact is internal CRM owned
  assert the deal primary_contact_id points at the copied contact
  assert the participant links to the copied internal contact, not the provider contact
```

- [ ] **Step 3: Verify account/contact relationship tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_account_copy.py tests/service/crm/test_deal_write_service_account_updates.py -q
```

Expected: PASS.

## Task 10: Add DealMutationEventService

**Files:**

- Create: `src/service/crm/deals/mutation_event_service.py`
- Modify: `src/service/crm/deals/write_service.py`
- Modify: `src/service/crm/deals/stage_service.py`
- Modify: `src/service/crm/deals/lifecycle_service.py`
- Modify: `src/service/crm/deals/owner_service.py`
- Test: focused mutation event tests under `tests/service/crm/`

- [ ] **Step 0: Confirm mutation-event model availability**

Before importing `CRMDealMutationEvent`, run:

```bash
rg -n "class CRMDealMutationEvent" src/domain/models/organisation_tables/sync/crm/deals.py
```

Expected: one match.

If there is no match, stop this task. Either include the already-planned mutation-event model/migration files as an explicit prerequisite slice in this branch, or defer Task 10 until that prerequisite lands. Do not create a second migration from this plan.

- [ ] **Step 1: Create mutation event service**

Create `src/service/crm/deals/mutation_event_service.py`:

```python
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealMutationEvent
from src.service.crm.deals.contracts import DealWriteContext, DealWriteServiceError


class DealMutationEventService:
    """Writes immutable CRM mutation events for committed internal deal changes."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
```

Add method:

```python
async def record_event(
    self,
    *,
    deal: CRMDeal,
    event_type: str,
    operation: str,
    changed_fields: dict[str, dict[str, Any]],
    semantic_event_types: list[str],
    context: DealWriteContext,
    summary: str | None,
) -> CRMDealMutationEvent | None:
    if not changed_fields:
        return None

    payload = {
        "event_type": event_type,
        "deal_id": str(deal.deal_id),
        "changed_fields": changed_fields,
        "semantic_event_types": semantic_event_types,
    }
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    if context.idempotency_key:
        stmt = select(CRMDealMutationEvent).where(
            col(CRMDealMutationEvent.organisation_id) == self.organisation_id,
            col(CRMDealMutationEvent.idempotency_key) == context.idempotency_key,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            if existing.payload_hash == payload_hash:
                return existing
            raise DealWriteServiceError(
                "Idempotency key was already used for a different deal mutation",
                status_code=409,
            )

    event = CRMDealMutationEvent(
        event_id=uuid4(),
        organisation_id=self.organisation_id,
        event_type=event_type,
        semantic_event_types=semantic_event_types,
        mutation_group_id=uuid4(),
        entity_type="deal",
        entity_id=deal.deal_id,
        deal_id=deal.deal_id,
        operation=operation,
        occurred_at=datetime.now(UTC),
        changed_fields=changed_fields,
        summary=summary,
        integration_id=deal.integration_id,
        provider=None,
        source_object_mapping_id=None,
        actor_user_id=context.actor_user_id,
        source=context.source,
        source_label=context.source_label,
        approval_state_id=context.approval_state_id,
        correlation_id=context.correlation_id,
        idempotency_key=context.idempotency_key,
        payload_hash=payload_hash,
    )
    self.session.add(event)
    await self.session.flush()
    return event
```

Use only fields present on `CRMDealMutationEvent`. Do not add synthetic `account_id`,
`contact_id`, workflow runtime, dispatch, or attempt fields in this service slice.

- [ ] **Step 2: Add changed-field helper**

In `mutation_event_service.py`, add:

```python
def changed_field(old: Any, new: Any) -> dict[str, Any] | None:
    if old == new:
        return None
    return {"old": old, "new": new}
```

Callers should only add non-`None` field changes.

- [ ] **Step 3: Add mutation tests**

Create `tests/service/crm/test_deal_mutation_event_service.py` with assertions equivalent to:

```text
test_record_event_skips_empty_changed_fields:
  assert recorded_event is None
  assert no CRMDealMutationEvent object is added to the session

test_record_event_persists_one_event_for_multi_field_update:
  assert event.event_type == "crm.deal.updated"
  assert event.changed_fields contains amount and stage_id
  assert event.semantic_event_types contains crm.deal.amount_changed and crm.deal.stage_changed

test_record_event_does_not_store_workflow_runtime_state:
  assert the event object has no workflow_run_id attribute
  assert the event object has no dispatch_status attribute
  assert the event object has no attempt_count attribute

test_record_event_returns_existing_event_for_same_idempotency_payload:
  call record_event twice with the same idempotency_key and same changed_fields
  assert the same event_id is returned
  assert only one CRMDealMutationEvent object is added to the session

test_record_event_rejects_same_idempotency_key_with_different_payload:
  call record_event once with amount changed to 100
  call record_event again with the same idempotency_key and amount changed to 200
  assert DealWriteServiceError.status_code == 409
```

- [ ] **Step 4: Integrate mutation recording into write orchestration**

Only the public orchestrator method should call `record_event()` for a user action:

```text
create_deal -> crm.deal.created
update_deal -> crm.deal.updated
move_stage -> crm.deal.stage_changed
close_deal -> crm.deal.closed
reopen_deal -> crm.deal.reopened
assign_owner -> crm.deal.owner_changed
update_account -> crm.deal.account_changed
update_primary_contact -> crm.deal.primary_contact_changed
delete_deal -> crm.deal.deleted
```

When one public method delegates to another public method internally, avoid double event writes by using an internal helper method that accepts `emit_mutation_event=False`.

Required composition rule:

```text
Public write methods may emit mutation events when called directly.
Internal orchestration must call private/non-emitting helpers and then emit one
event at the top-level user action.
```

- [ ] **Step 5: Verify mutation tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_mutation_event_service.py -q
```

Expected: PASS.

## Task 11: Route Deals API To New Services

**Files:**

- Modify: `src/api/routers/crm/deals_handler.py`
- Test: API deal router tests under `tests/api/routers/crm/`

- [ ] **Step 1: Update imports**

Replace:

```python
from src.service.crm.access.deals_access_service import DealsAccessService
from src.service.crm.internal.deal_write_service import DealWriteService
```

with:

```python
from src.service.crm.deals.read_service import DealReadService
from src.service.crm.deals.write_service import DealWriteService
```

Keep old error aliases only if tests still depend on them:

```python
from src.service.crm.deals.contracts import DealReadServiceError, DealWriteServiceError
```

- [ ] **Step 2: Split dependencies by read/write service names**

Use:

```python
async def get_deal_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    service = DealReadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        await service.close()
```

and keep:

```python
async def get_deal_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    service = DealWriteService(
        org_session=org_session,
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        actor_user_id=user_ids.user_id,
    )
    try:
        yield service
    finally:
        await service.close()
```

For compatibility with existing test dependency overrides, leave `get_deals_access_service` as an alias dependency until tests and callers migrate:

```python
get_deals_access_service = get_deal_read_service
```

- [ ] **Step 3: Preserve router-owned commits**

Write endpoints must keep:

```python
await service.session.commit()
```

on success and:

```python
await service.session.rollback()
```

on failure.

- [ ] **Step 4: Run API tests**

Run:

```bash
uv run pytest tests/api/routers/crm -q
```

Expected: PASS for the CRM router tests already present in the checkout.

## Task 12: Route Agent Tools And Bot Handlers

**Files:**

- Modify: `src/agents/tools/crm/create_deal.py`
- Modify: `src/agents/tools/crm/update_deal_fields.py`
- Modify: `src/agents/tools/crm/update_deal_account.py`
- Modify: `src/agents/tools/crm/update_deal_lifecycle.py`
- Modify: `src/service/bots/form_handlers/create_deal_handler.py`
- Modify: `src/service/bots/form_handlers/update_deal_stage_handler.py`
- Modify: `src/service/crm/access/leads_access_service.py`
- Test: `tests/agents/tools/crm/test_create_deal_direct.py`
- Test: focused agent/tool tests if present

- [ ] **Step 1: Update imports**

Replace old imports:

```python
from src.service.crm.internal.deal_write_service import DealWriteService, DealWriteServiceError
```

with:

```python
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
```

- [ ] **Step 2: Preserve caller transaction behavior**

Each migrated caller must still explicitly commit after success:

```python
await org_session.commit()
```

and rollback on service error:

```python
await org_session.rollback()
```

- [ ] **Step 3: Run agent create-deal tests**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_create_deal_direct.py -q
```

Expected: PASS.

## Task 13: Focused Verification

**Files:**

- No code files unless verification exposes a bug.

- [ ] **Step 1: Verify deal boundary tests**

Run:

```bash
uv run pytest tests/service/crm/test_deal_service_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 2: Verify core deal write behavior**

Run:

```bash
uv run pytest tests/service/crm/test_deal_write_service_stage_history.py tests/service/crm/test_deal_write_service_account_updates.py tests/service/crm/test_deal_write_service_account_copy.py tests/service/crm/test_custom_field_source_isolation.py tests/service/crm/test_deal_write_service_utils.py -q
```

Expected: PASS.

- [ ] **Step 3: Verify deal read behavior**

Run:

```bash
uv run pytest tests/service/crm/test_deals_access_service_team_members.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify agent and bot-facing deal writes**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_create_deal_direct.py -q
```

Expected: PASS.

- [ ] **Step 5: Verify provider sync is untouched**

Run:

```bash
git diff --name-only -- src/service/integrations/crm/records/hubspot/deals src/service/integrations/crm/records/salesforce/deals
```

Expected: no output.

- [ ] **Step 6: Run focused lint and type checks before PR**

Run:

```bash
uv run ruff check src/service/crm/deals src/service/crm/access/deals_access_service.py src/service/crm/internal/deal_write_service.py src/api/routers/crm/deals_handler.py src/agents/tools/crm/create_deal.py src/agents/tools/crm/update_deal_fields.py src/agents/tools/crm/update_deal_account.py src/agents/tools/crm/update_deal_lifecycle.py tests/service/crm/test_deal_service_boundaries.py
```

Expected: PASS.

Run:

```bash
uv run mypy --no-incremental src/service/crm/deals src/service/crm/access/deals_access_service.py src/service/crm/internal/deal_write_service.py src/api/routers/crm/deals_handler.py src/agents/tools/crm/create_deal.py src/agents/tools/crm/update_deal_fields.py src/agents/tools/crm/update_deal_account.py src/agents/tools/crm/update_deal_lifecycle.py
```

Expected: PASS.

## Later Cleanup Phase

Only after new deal services are merged and callers are stable:

1. Search old imports:

   ```bash
   rg -n "src\\.service\\.crm\\.internal\\.deal_write_service|src\\.service\\.crm\\.access\\.deals_access_service|DealWriteService|DealsAccessService" src tests
   ```

2. If old imports are only compatibility tests, move or delete:

   ```text
   src/service/crm/internal/deal_write_service.py
   src/service/crm/access/deals_access_service.py
   ```

3. Review router mapping separately:

   ```text
   src/api/routers/crm/deals_handler.py
   ```

   Do not combine service extraction with response-schema cleanup unless tests show it is necessary.

4. Design provider-origin mutation events separately:

   ```text
   HubSpot/Salesforce sync update -> provider mutation event policy
   ```

5. Design workflow dispatcher separately:

   ```text
   crm_mutation_events -> workflow trigger matcher -> workflow runs
   ```

## Rollout Notes

- This plan is deals-only.
- It follows the existing account/contact/participant vertical-module pattern.
- It keeps provider sync writers out of scope.
- It keeps old services temporarily as compatibility facades.
- It does not add workflow dispatch workers.
- It does not change public API paths.
- It does not require a database migration for the service split.
- It preserves router/tool-owned commits.
- It separates mutation-event recording from workflow execution.

## Success Criteria

- `src/service/crm/deals/` exists with focused deal services.
- `DealReadService` owns read-only deal behavior.
- `DealWriteService` owns core internal deal create/update orchestration.
- `DealCustomFieldWriteService` owns custom field validation/coercion.
- `DealStageService` owns stage transitions and stage history.
- `DealLifecycleService` owns close/reopen behavior.
- `DealOwnerService` owns owner assignment.
- `DealRelationshipService` owns account/contact/team participant orchestration without becoming an account/contact writer.
- `DealMutationEventService` writes one mutation event per user/API/agent action.
- Old access/internal deal services are facades, not competing implementations.
- API, agent, bot, and lead conversion callers can migrate to the new module incrementally.
- Focused deal read/write/API/agent tests pass.
- Focused ruff and mypy checks pass before PR.
