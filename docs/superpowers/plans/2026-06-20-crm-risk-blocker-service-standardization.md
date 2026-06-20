# CRM Risk And Blocker Service Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a canonical risk/blocker service module so API, agent tools, and AI extraction all use the same read/write/lifecycle rules.

**Architecture:** Keep `deal_risks_blockers` as the source-of-truth table. Add `src/service/crm/risks/` as the vertical module with read, write, lifecycle, and DTO mapping services. Convert the current `src/service/crm/insights/risk_blocker_service.py` into a compatibility facade, then route API, agent tools, and extraction persistence through the new services.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, Celery deal-worker, pytest, mypy, ruff.

---

## Starting Point

Current facts from this checkout:

- Table/model already exists: `src/domain/models/organisation_tables/sync/risk_blocker_models.py`.
- API route exists: `src/api/routers/crm/risks_blockers_handler.py`.
- API schemas exist: `src/api/schemas/risk_blocker_models.py`.
- Current API service exists: `src/service/crm/insights/risk_blocker_service.py`.
- AI extraction service exists: `src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py`.
- Agent lifecycle tools exist: `src/agents/tools/crm/manage_risks_blockers.py`.
- Agent read tool exists: `src/agents/tools/crm/get_risks_blockers.py`.
- Source resolver exists: `src/service/crm/insights/risk_blocker_source_resolver.py`.
- Existing tests cover API behavior and extraction record creation:
  - `tests/api/routers/crm/test_risks_blockers_handler.py`
  - `tests/workers/deal_worker/test_risk_blocker_extraction_service.py`

## Current Problem

Risk/blocker writes are scattered across three places:

```text
Manual API writes       -> RiskBlockerService
AI extraction creates   -> RiskBlockerExtractionService.create_risk_blocker_from_extracted()
AI lifecycle tools      -> manage_risks_blockers.py direct SQL/model mutation
```

Production risks:

- API, extraction, and agent tools can drift on audit-trail shape.
- API and AI paths both create timeline events directly.
- Current API service commits internally, unlike the newer CRM module pattern.
- Agent tools bypass service rules and mutate `DealRiskBlocker` directly.
- Future workflows would have to choose between API service, extraction helper, or direct SQL.
- `RiskBlockerService` lives under `insights`, but this is now a core CRM write surface.

## Core Decision

Create a vertical module:

```text
src/service/crm/risks/
  __init__.py
  contracts.py
  mapper.py
  read_service.py
  write_service.py
  lifecycle_service.py
  source_resolver.py
```

Temporary compatibility:

```text
src/service/crm/insights/risk_blocker_service.py          # facade
src/service/crm/insights/risk_blocker_source_resolver.py  # re-export/facade
```

Do not change the table in this slice.

Do not rename public API routes in this slice.

## Target Boundaries

Read callers converge here:

```text
API list/detail                         -> RiskBlockerReadService
agent get_existing_risks_blockers        -> RiskBlockerReadService
deal detail tool risk section            -> RiskBlockerReadService later
pipeline/deal-health analysis tools       -> RiskBlockerReadService later
```

Write callers converge here:

```text
manual API create/update/delete          -> RiskBlockerWriteService
AI extraction new item creation          -> RiskBlockerWriteService.create_from_extraction()
future workflow create/update            -> RiskBlockerWriteService
```

Lifecycle callers converge here:

```text
manual API dismiss/resolve/reopen        -> RiskBlockerLifecycleService
AI tools dismiss/resolve/update          -> RiskBlockerLifecycleService + RiskBlockerWriteService
future workflow lifecycle actions        -> RiskBlockerLifecycleService
```

Mapping converges here:

```text
DealRiskBlocker -> RiskBlockerListItem / RiskBlockerDetail -> RiskBlockerMapper
```

## Non-Negotiable Rules

- Do not add migrations in this slice.
- Do not change `deal_risks_blockers` table shape in this slice.
- Do not rename public API routes.
- Do not remove `RiskBlockerService` immediately; make it a compatibility facade.
- New code must live under `src/service/crm/risks/`.
- New production reads must use `RiskBlockerReadService`.
- New production writes must use `RiskBlockerWriteService`.
- New production lifecycle actions must use `RiskBlockerLifecycleService`.
- Write/lifecycle services must not commit transactions after migration.
- Routers, agent tools, worker save phases, and future workflows own commit/rollback.
- Every query and write must filter or set `organisation_id`.
- AI extraction must keep the automated pipeline sentinel user:
  `00000000-0000-0000-0000-000000000001`.
- Manual user actions must keep human actor attribution.
- `is_ai_generated` means original creation provenance and must not flip when AI later updates a user-created item.
- Timeline events remain created with the risk/blocker write/lifecycle mutation, but they are flushed with the same caller-owned transaction.
- No workflow dispatcher integration in this slice.
- No CRM mutation events in this slice.

## Loopholes To Close

### Loophole 1: Different Audit Trail Shapes

Risk:

```text
API uses action=updated with user actor
AI tool uses action=updated with system actor
AI extraction uses action=opened
future workflow invents another shape
```

Required behavior:

```text
RiskBlockerWriteService and RiskBlockerLifecycleService own audit entry creation
actor_type, actor_id, actor_name, details, and unique_modifiers are built in one place
```

### Loophole 2: Service-Owned Commits Block Orchestration

Risk:

```text
service commits risk row
router/tool later fails while creating related side effect
caller cannot rollback cleanly
```

Required behavior:

```text
new services flush/refresh only
caller commits
compatibility facade can preserve old behavior until callers migrate
```

### Loophole 3: AI Tools Bypass Business Rules

Risk:

```text
manage_risks_blockers.py directly mutates SQL rows and diverges from API lifecycle rules
```

Required behavior:

```text
agent tools call canonical lifecycle/write services
tool-specific response shape stays in the tool layer
```

### Loophole 4: Timeline Events Drift

Risk:

```text
manual create emits timeline but AI update/dismiss may not
```

Required behavior:

```text
write/lifecycle services decide the timeline event per action
create risk -> RISK_FLAGGED
create blocker -> BLOCKER_ADDED
resolve -> BLOCKER_RESOLVED
dismiss -> BLOCKER_DISMISSED
reopen/update timeline behavior remains explicitly documented and tested
```

### Loophole 5: AI Updates User-Created Risks Incorrectly

Risk:

```text
AI modifies a human-created item and flips is_ai_generated to true
```

Required behavior:

```text
is_ai_generated is creation provenance only
last_modified_by_* changes to automated pipeline actor for AI mutations
created_by_* remains original creator
```

### Loophole 6: Workflow-Ready Surface Is Missing

Risk:

```text
future workflow action "resolve blocker" calls an API-shaped service that commits internally
```

Required behavior:

```text
workflow later calls RiskBlockerWriteService/RiskBlockerLifecycleService with actor context
workflow run metadata can be added to context later without changing service ownership
```

## Flow Examples

### Flow A: User Creates A Risk From API

```text
POST /users/crm/deals/{deal_id}/risks-blockers
  -> RiskBlockerWriteService.create_manual()
  -> validate deal belongs to organisation
  -> create DealRiskBlocker(status=OPEN, source_type=MANUAL, is_ai_generated=false)
  -> add opened audit entry with actor_type=user
  -> add timeline event RISK_FLAGGED or BLOCKER_ADDED
  -> flush and refresh
router commits
router returns RiskBlockerDetail
```

### Flow B: AI Extraction Creates New Risks

```text
risk_blocker.extract_deal_risks_blockers_task
  -> RiskBlockerExtractionService runs agent
  -> for each non-duplicate extracted item:
     -> RiskBlockerWriteService.create_from_extraction()
     -> create DealRiskBlocker(status=OPEN, is_ai_generated=true)
     -> actor is automated pipeline sentinel
     -> add timeline event
  -> worker save phase commits once
```

### Flow C: AI Tool Resolves Existing Risk

```text
RiskBlockerExtractionAgent calls resolve_risk_blocker tool
  -> RiskBlockerLifecycleService.resolve()
  -> verifies organisation and item id
  -> skips if already RESOLVED/DISMISSED according to current tool contract
  -> sets status=RESOLVED, resolution_notes, closed_at, closed_by_*
  -> adds system audit entry
  -> adds timeline event
  -> tool commits
  -> tool returns compact status payload
```

## Task 0: Add Risks Module And Contracts

**Files:**

- Create: `src/service/crm/risks/__init__.py`
- Create: `src/service/crm/risks/contracts.py`
- Create: `tests/service/crm/test_risk_blocker_service_boundaries.py`

- [ ] **Step 1: Create boundary tests**

Create `tests/service/crm/test_risk_blocker_service_boundaries.py`:

```python
from __future__ import annotations

from pathlib import Path


def test_risks_vertical_module_exists() -> None:
    assert Path("src/service/crm/risks/__init__.py").exists()
    assert Path("src/service/crm/risks/contracts.py").exists()
    assert Path("src/service/crm/risks/mapper.py").exists()
    assert Path("src/service/crm/risks/read_service.py").exists()
    assert Path("src/service/crm/risks/write_service.py").exists()
    assert Path("src/service/crm/risks/lifecycle_service.py").exists()
    assert Path("src/service/crm/risks/source_resolver.py").exists()


def test_risk_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/risks/read_service.py").read_text()

    assert "async def create" not in source
    assert "async def update" not in source
    assert "async def dismiss" not in source
    assert "async def resolve" not in source
    assert "DealRiskBlocker(" not in source


def test_risk_write_service_does_not_commit() -> None:
    source = Path("src/service/crm/risks/write_service.py").read_text()
    lifecycle = Path("src/service/crm/risks/lifecycle_service.py").read_text()

    assert ".commit(" not in source
    assert ".commit(" not in lifecycle


def test_legacy_risk_service_is_facade_after_migration() -> None:
    source = Path("src/service/crm/insights/risk_blocker_service.py").read_text()

    assert "RiskBlockerReadService" in source
    assert "RiskBlockerWriteService" in source
    assert "RiskBlockerLifecycleService" in source
```

- [ ] **Step 2: Run boundary tests and verify they fail**

Run:

```bash
uv run pytest tests/service/crm/test_risk_blocker_service_boundaries.py -q
```

Expected: FAIL because `src/service/crm/risks/` does not exist yet.

- [ ] **Step 3: Create package and contracts**

Create `src/service/crm/risks/__init__.py`:

```python
"""Risk and blocker read, write, lifecycle, and mapping services."""
```

Create `src/service/crm/risks/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

RiskBlockerActorType = Literal["user", "system", "workflow"]


@dataclass(frozen=True)
class RiskBlockerActor:
    """Actor metadata for audit trail and provenance fields."""

    actor_type: RiskBlockerActorType
    actor_id: UUID
    actor_name: str | None


@dataclass(frozen=True)
class RiskBlockerWriteContext:
    """Caller metadata for risk/blocker writes and future mutation events."""

    source: str
    actor: RiskBlockerActor


class RiskBlockerServiceError(Exception):
    """HTTP-shaped risk/blocker error for routers, agents, and workers."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class RiskBlockerNotFoundError(RiskBlockerServiceError):
    """Raised when a risk/blocker is not found in the organisation boundary."""

    def __init__(self, risk_blocker_id: UUID):
        super().__init__(f"Risk/blocker {risk_blocker_id} not found", status_code=404)


class InvalidStatusTransitionError(RiskBlockerServiceError):
    """Raised when lifecycle transition is not allowed."""

    def __init__(self, current: str, target: str):
        super().__init__(f"Cannot transition from {current} to {target}", status_code=400)
```

## Task 1: Create RiskBlockerMapper

**Files:**

- Create: `src/service/crm/risks/mapper.py`
- Test: `tests/service/crm/test_risk_blocker_mapper.py`

- [ ] **Step 1: Move DTO mapping**

Create `src/service/crm/risks/mapper.py` and move the current `_to_list_item()` and `_to_detail()` behavior from `RiskBlockerService`.

Expose these methods:

```text
RiskBlockerMapper.to_list_item(item: DealRiskBlocker) -> RiskBlockerListItem
RiskBlockerMapper.to_detail(item: DealRiskBlocker) -> RiskBlockerDetail
```

- [ ] **Step 2: Add mapper tests**

Create tests for:

- list item includes identity, type, severity, status, title, quote, source, owner, and AI flag
- detail includes audit trail, modification count, and unique modifier count
- missing `risk_blocker_data` returns empty audit fields

Run:

```bash
uv run pytest tests/service/crm/test_risk_blocker_mapper.py -q
```

Expected: PASS.

## Task 2: Extract RiskBlockerReadService

**Files:**

- Create: `src/service/crm/risks/read_service.py`
- Test: `tests/api/routers/crm/test_risks_blockers_handler.py`

- [ ] **Step 1: Move read behavior**

Create `src/service/crm/risks/read_service.py` with:

```python
class RiskBlockerReadService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._mapper = RiskBlockerMapper()
```

Move:

```text
list_for_deal
_get_summary
get_by_id
```

Use `RiskBlockerServiceError` from contracts.

- [ ] **Step 2: Keep org isolation**

Every query must include:

```python
DealRiskBlocker.organisation_id == self.organisation_id
```

- [ ] **Step 3: Verify read API behavior after facade wiring**

Run after Task 5:

```bash
uv run pytest tests/api/routers/crm/test_risks_blockers_handler.py -q
```

Expected: PASS.

## Task 3: Extract RiskBlockerWriteService

**Files:**

- Create: `src/service/crm/risks/write_service.py`
- Modify later: `src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py`
- Test: `tests/service/crm/test_risk_blocker_write_service.py`
- Test: `tests/workers/deal_worker/test_risk_blocker_extraction_service.py`

- [ ] **Step 1: Create write-service tests**

Create `tests/service/crm/test_risk_blocker_write_service.py` covering:

- manual create sets `source_type=MANUAL`, `status=OPEN`, `is_ai_generated=False`
- manual create uses user actor in `created_by_*` and audit trail
- extraction create sets `is_ai_generated=True`
- extraction create uses automated pipeline sentinel actor
- create emits `RISK_FLAGGED` for risks and `BLOCKER_ADDED` for blockers
- write service does not commit

- [ ] **Step 2: Implement manual create**

Create:

```python
class RiskBlockerWriteService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._mapper = RiskBlockerMapper()
```

Add:

```python
async def create_manual(
    self,
    deal_id: UUID,
    item_type: RiskBlockerType,
    title: str,
    context: RiskBlockerWriteContext,
    description: str | None = None,
    category: RiskBlockerCategory = RiskBlockerCategory.OTHER,
    severity: RiskBlockerSeverity = RiskBlockerSeverity.MEDIUM,
    owner_name: str | None = None,
    owner_email: str | None = None,
    assigned_team: str | None = None,
) -> RiskBlockerDetail:
```

It must:

```text
create DealRiskBlocker
seed opened audit trail
add timeline event
flush
refresh
return RiskBlockerDetail
```

- [ ] **Step 3: Implement extraction create**

Add:

```python
async def create_from_extraction(
    self,
    deal_id: UUID,
    extracted: ExtractedRiskBlocker,
    context: RiskBlockerWriteContext,
    source_type: RiskBlockerSource,
    source_record_id: str | None = None,
) -> DealRiskBlocker:
```

This may import `ExtractedRiskBlocker` under `TYPE_CHECKING` or accept a small protocol if direct import creates cycles.

It must preserve current extraction behavior exactly:

```text
status=OPEN
is_ai_generated=True
ai_confidence=extracted.confidence
ai_reasoning=extracted.reasoning
owner_name=extracted.suggested_owner
created_by_user_id=automated sentinel
last_modified_by_user_id=automated sentinel
opened audit entry actor_type=system
timeline event by item_type
```

- [ ] **Step 4: Implement update fields**

Move the current `RiskBlockerService.update()` field-change logic into:

```python
async def update_fields(
    self,
    risk_blocker_id: UUID,
    updates: dict[str, Any],
    context: RiskBlockerWriteContext,
) -> RiskBlockerDetail | None:
```

This handles non-status field updates and audit entries.

Status transitions should be delegated to `RiskBlockerLifecycleService`.

- [ ] **Step 5: Verify write behavior**

Run:

```bash
uv run pytest tests/service/crm/test_risk_blocker_write_service.py tests/workers/deal_worker/test_risk_blocker_extraction_service.py -q
```

Expected: PASS.

## Task 4: Extract RiskBlockerLifecycleService

**Files:**

- Create: `src/service/crm/risks/lifecycle_service.py`
- Test: `tests/service/crm/test_risk_blocker_lifecycle_service.py`

- [ ] **Step 1: Create lifecycle tests**

Create `tests/service/crm/test_risk_blocker_lifecycle_service.py` covering:

- dismiss sets `status=DISMISSED`, closure fields, audit entry, and timeline event
- resolve sets `status=RESOLVED`, resolution notes, closure fields, audit entry, and timeline event
- reopen from RESOLVED or DISMISSED clears closure fields and resolution notes
- reopen from OPEN or IN_PROGRESS raises `InvalidStatusTransitionError`
- lifecycle service does not commit
- AI lifecycle context updates last modified actor without changing created actor or `is_ai_generated`

- [ ] **Step 2: Implement lifecycle service**

Create:

```python
class RiskBlockerLifecycleService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self._mapper = RiskBlockerMapper()
```

Move:

```text
dismiss
resolve
reopen
```

Use:

```python
RiskBlockerWriteContext
RiskBlockerNotFoundError
InvalidStatusTransitionError
```

- [ ] **Step 3: Preserve existing idempotent AI tool semantics**

For API facade methods, keep current strict behavior.

For agent tools, the tool layer can translate already `RESOLVED` or `DISMISSED` into:

```text
status=skipped
```

The lifecycle service should expose enough information or exception behavior for the tool to preserve its current response contract.

- [ ] **Step 4: Run lifecycle tests**

Run:

```bash
uv run pytest tests/service/crm/test_risk_blocker_lifecycle_service.py -q
```

Expected: PASS.

## Task 5: Convert Existing RiskBlockerService Into Facade

**Files:**

- Modify: `src/service/crm/insights/risk_blocker_service.py`
- Test: `tests/api/routers/crm/test_risks_blockers_handler.py`
- Test: `tests/service/crm/test_risk_blocker_service_boundaries.py`

- [ ] **Step 1: Replace implementation with delegation**

Keep compatibility names:

```python
RiskBlockerServiceError = contracts.RiskBlockerServiceError
RiskBlockerNotFoundError = contracts.RiskBlockerNotFoundError
InvalidStatusTransitionError = contracts.InvalidStatusTransitionError


class RiskBlockerService:
    """Compatibility facade; use CRM risk read/write/lifecycle services for new code."""
```

Construct:

```python
self._reader = RiskBlockerReadService(session, organisation_id)
self._writer = RiskBlockerWriteService(session, organisation_id)
self._lifecycle = RiskBlockerLifecycleService(session, organisation_id)
```

- [ ] **Step 2: Preserve facade commit behavior until router migrates**

Because current API service commits internally, facade methods may keep commit behavior until Task 6 routes the API directly.

After API routing is complete, facade methods should either:

```text
delegate without commit only for tests/new code
or remain old-compatible but be marked deprecated
```

Do not silently change old imports before call sites are audited.

- [ ] **Step 3: Verify API tests**

Run:

```bash
uv run pytest tests/api/routers/crm/test_risks_blockers_handler.py tests/service/crm/test_risk_blocker_service_boundaries.py -q
```

Expected: PASS.

## Task 6: Route Risk/Blocker API To New Services

**Files:**

- Modify: `src/api/routers/crm/risks_blockers_handler.py`
- Test: `tests/api/routers/crm/test_risks_blockers_handler.py`

- [ ] **Step 1: Split dependencies**

Add:

```python
get_risk_blocker_read_service
get_risk_blocker_write_service
get_risk_blocker_lifecycle_service
```

Reads use `RiskBlockerReadService`.

Create/update/delete use `RiskBlockerWriteService`.

Dismiss/resolve/reopen use `RiskBlockerLifecycleService`.

- [ ] **Step 2: Move commit ownership to router**

For write/lifecycle endpoints:

```text
call service
commit
return response
rollback on service/error exception
```

- [ ] **Step 3: Build manual actor context**

Use:

```python
RiskBlockerWriteContext(
    source="api",
    actor=RiskBlockerActor(
        actor_type="user",
        actor_id=user_ids.user_id,
        actor_name=user_ids.user_name,
    ),
)
```

- [ ] **Step 4: Verify API behavior**

Run:

```bash
uv run pytest tests/api/routers/crm/test_risks_blockers_handler.py -q
```

Expected: PASS.

## Task 7: Route AI Extraction Persistence To RiskBlockerWriteService

**Files:**

- Modify: `src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py`
- Test: `tests/workers/deal_worker/test_risk_blocker_extraction_service.py`

- [ ] **Step 1: Replace local constructor helper**

Replace `create_risk_blocker_from_extracted()` implementation with delegation to:

```python
RiskBlockerWriteService.create_from_extraction()
```

Keep the public method temporarily if tests or callers still use it, but it should be a thin wrapper.

- [ ] **Step 2: Use automated actor context**

Use:

```python
RiskBlockerWriteContext(
    source="ai_extraction",
    actor=RiskBlockerActor(
        actor_type="system",
        actor_id=AUTOMATED_PIPELINE_USER_ID,
        actor_name=AI_EXTRACTION_ACTOR_NAME,
    ),
)
```

- [ ] **Step 3: Preserve worker transaction boundary**

The worker save phase should still commit once after all non-duplicate extracted items are added.

- [ ] **Step 4: Verify extraction behavior**

Run:

```bash
uv run pytest tests/workers/deal_worker/test_risk_blocker_extraction_service.py -q
```

Expected: PASS.

## Task 8: Route Agent Risk/Blocker Tools To New Services

**Files:**

- Modify: `src/agents/tools/crm/get_risks_blockers.py`
- Modify: `src/agents/tools/crm/manage_risks_blockers.py`
- Test/Add: `tests/agents/tools/crm/test_risk_blocker_tools.py`

- [ ] **Step 1: Route read tool**

Replace direct SQL in `get_risks_blockers.py` with `RiskBlockerReadService`.

Keep output shape exactly:

```text
risk_blocker_id
item_type
category
severity
status
title
description
quote
source_type
source_record_id
is_ai_generated
ai_reasoning
created_at
```

- [ ] **Step 2: Route lifecycle tools**

Replace direct SQL/model mutation in `manage_risks_blockers.py` with:

```text
RiskBlockerWriteService.update_fields()
RiskBlockerLifecycleService.dismiss()
RiskBlockerLifecycleService.resolve()
```

Use automated actor context:

```python
RiskBlockerWriteContext(
    source="ai_extraction_tool",
    actor=RiskBlockerActor(
        actor_type="system",
        actor_id=AUTOMATED_PIPELINE_USER_ID,
        actor_name=AI_EXTRACTION_ACTOR_NAME,
    ),
)
```

- [ ] **Step 3: Preserve tool response contract**

The tools must still return:

```text
updated / no_changes / dismissed / resolved / skipped / not_found / error
```

Do not leak service exception names or internal DTOs into agent-visible responses.

- [ ] **Step 4: Verify tools**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_risk_blocker_tools.py -q
```

Expected: PASS. If this file does not exist yet, create focused tests for read, update, dismiss, resolve, skipped, and not_found behavior.

## Task 9: Move Source Resolver

**Files:**

- Create: `src/service/crm/risks/source_resolver.py`
- Modify: `src/service/crm/insights/risk_blocker_source_resolver.py`

- [ ] **Step 1: Move resolver implementation**

Move the implementation from:

```text
src/service/crm/insights/risk_blocker_source_resolver.py
```

to:

```text
src/service/crm/risks/source_resolver.py
```

- [ ] **Step 2: Keep old import compatibility**

Replace old file contents with re-exports:

```python
from src.service.crm.risks.source_resolver import (
    resolve_email_risk_blocker_source,
    resolve_meeting_risk_blocker_source,
)
```

## Task 10: Focused Verification

**Files:**

- No code files unless verification exposes a bug.

- [ ] **Step 1: Verify service boundaries**

Run:

```bash
uv run pytest tests/service/crm/test_risk_blocker_service_boundaries.py -q
```

Expected: PASS.

- [ ] **Step 2: Verify service behavior**

Run:

```bash
uv run pytest tests/service/crm/test_risk_blocker_mapper.py tests/service/crm/test_risk_blocker_write_service.py tests/service/crm/test_risk_blocker_lifecycle_service.py -q
```

Expected: PASS.

- [ ] **Step 3: Verify API behavior**

Run:

```bash
uv run pytest tests/api/routers/crm/test_risks_blockers_handler.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify worker extraction behavior**

Run:

```bash
uv run pytest tests/workers/deal_worker/test_risk_blocker_extraction_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Verify agent tool behavior**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_risk_blocker_tools.py -q
```

Expected: PASS if the file exists or was created in Task 8.

- [ ] **Step 6: Focused lint and type checks before PR**

Run only before pushing or opening a PR:

```bash
uv run ruff check src/service/crm/risks src/service/crm/insights/risk_blocker_service.py src/service/crm/insights/risk_blocker_source_resolver.py src/api/routers/crm/risks_blockers_handler.py src/agents/tools/crm/get_risks_blockers.py src/agents/tools/crm/manage_risks_blockers.py src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py tests/service/crm/test_risk_blocker_service_boundaries.py
```

Expected: PASS.

Run:

```bash
uv run mypy --no-incremental src/service/crm/risks src/service/crm/insights/risk_blocker_service.py src/service/crm/insights/risk_blocker_source_resolver.py src/api/routers/crm/risks_blockers_handler.py src/agents/tools/crm/get_risks_blockers.py src/agents/tools/crm/manage_risks_blockers.py src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py
```

Expected: PASS.

## Later Cleanup Phase

Only after this module is merged and production imports are migrated:

1. Search old imports:

   ```bash
   rg -n "RiskBlockerService|risk_blocker_service|risk_blocker_source_resolver" src tests
   ```

2. If only compatibility tests remain, move or delete:

   ```text
   src/service/crm/insights/risk_blocker_service.py
   src/service/crm/insights/risk_blocker_source_resolver.py
   ```

3. Decide whether risk/blocker writes should emit CRM mutation events.

4. Decide whether workflow triggers need these event types:

   ```text
   crm.risk_blocker.created
   crm.risk_blocker.updated
   crm.risk_blocker.resolved
   crm.risk_blocker.dismissed
   crm.risk_blocker.reopened
   ```

5. Review delete semantics. Permanent delete exists today, but long-term production CRM may prefer soft delete or dismiss to preserve audit history.

## Rollout Notes

- This plan is risk/blocker-only.
- This does not change notes/tasks/deals/account/contact/participant modules.
- This does not change the table.
- This does not add workflow dispatcher behavior.
- This does not add CRM mutation events.
- Existing API routes and response schemas remain unchanged.
- Compatibility facade keeps existing imports working during migration.
- New services should be used by future workflows and agent tools.

## Success Criteria

- `src/service/crm/risks/` exists with contracts, mapper, read, write, lifecycle, and source resolver services.
- `RiskBlockerService` becomes a compatibility facade.
- API reads use `RiskBlockerReadService`.
- API writes use `RiskBlockerWriteService`.
- API lifecycle actions use `RiskBlockerLifecycleService`.
- AI extraction creates records through `RiskBlockerWriteService`.
- AI lifecycle tools use canonical services instead of direct SQL/model mutation.
- Write/lifecycle services do not commit.
- Audit trail shape is centralized.
- Timeline event creation is centralized.
- Existing API, worker, and agent behavior is preserved.
- Focused risk/blocker service, API, worker, agent, ruff, and mypy checks pass before PR.

## Self-Review

Spec coverage:

- Current API, worker, and agent paths are all covered.
- Existing table and public API routes are preserved.
- Service boundaries match the account/contact/participant/notes/tasks pattern.
- Workflow and mutation events are deferred explicitly.
- Compatibility facades are included to avoid big-bang removal.

Placeholder scan:

- The plan contains no open implementation placeholders.
- Each task names files, behavior, verification commands, and expected results.

Type consistency:

- Services consistently use `RiskBlockerReadService`, `RiskBlockerWriteService`, `RiskBlockerLifecycleService`, `RiskBlockerMapper`, `RiskBlockerWriteContext`, and `RiskBlockerActor`.
