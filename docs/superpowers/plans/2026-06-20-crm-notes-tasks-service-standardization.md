# CRM Notes And Tasks Service Standardization Implementation Plan

> Historical note: this plan was written before the final task/note source-table
> migration. Current runtime code should use `crm_tasks` / `CRMTask` and
> `crm_notes` / `CRMNote` as source rows, with `crm_activities` as projection.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Standardize CRM notes and tasks into canonical vertical service modules while preserving current API, agent, bot-form, reminder, provider-sync, and deal-insight behavior.

**Architecture:** Notes and tasks are both stored in `crm_activities`, but they are not the same product surface. Create `src/service/crm/notes/` and `src/service/crm/tasks/` as vertical modules with contracts, read services, write services, and small side-effect services where needed. Existing `DealNotesService`, `TasksAccessService`, and `TasksWriteService` remain as temporary compatibility facades until callers are migrated.

**Tech Stack:** Python 3.11, FastAPI, SQLModel, SQLAlchemy async sessions, PostgreSQL tenant schemas, Redis-backed reminders, Celery deal-worker, pytest, mypy, ruff.

---

## Starting Point

Current facts from this checkout:

- `src/service/crm/internal/deal_notes_service.py` exists and mixes:
  - deal-note reads
  - user-note create/update/delete
  - duplicate-note idempotency
  - participant mention lookup
  - note source resolution
  - deal insight job creation and Celery dispatch
- `src/api/routers/crm/deal_notes_handler.py` calls `DealNotesService` for reads and writes.
- `src/agents/tools/crm/add_deal_note.py` calls `DealNotesService` and then triggers deal insight work.
- `src/service/bots/form_handlers/add_deal_note_handler.py` creates notes through `DealNotesService`.
- `src/service/crm/access/tasks_access_service.py` owns task reads, global task filtering, assignee filter options, and current-user assignee resolution.
- `src/service/crm/internal/tasks_write_service.py` owns task create/update/delete, reminder lifecycle, owner resolution, external-assignee validation, and external assignment email sending.
- `src/api/routers/crm/tasks_handler.py` depends on `TasksAccessService` for reads and `TasksWriteService` for writes.
- `src/agents/tools/tasks/create_task.py` imports `TasksWriteService` lazily through `_load_tasks_write_service()`.
- `src/service/bots/form_handlers/confirm_task_handler.py` creates tasks through `TasksWriteService`.
- HubSpot note/task provider ingestion writes provider-owned `CRMActivity` rows under integration/webhook/sync services and must stay outside this internal user-write cleanup.

## Current Problem

Notes and tasks are activity rows, but their internal service ownership is still caller-oriented and side-effect-heavy.

Production risks:

- Agent/API/bot form writes depend on old internal/access services instead of canonical vertical modules.
- Note writes and deal-insight dispatch live in the same service, which makes transaction ownership unclear.
- Task writes also manage reminders and outbound external-assignment emails, so failures can blur core CRM write success versus side-effect success.
- Future workflows need a single internal service to create/update tasks, not separate paths for API, agent, and bot confirmations.
- Provider-synced notes/tasks are read-only in user APIs, but that rule is duplicated in task response mapping and write guards.
- Notes/tasks share `CRMActivity`, so a generic `ActivityWriteService` is tempting but too broad for this stage.

## Core Decision

Use vertical product modules first. Do not create a generic activity write service in this slice.

```text
src/service/crm/
  notes/
    __init__.py
    contracts.py
    read_service.py
    write_service.py
    insight_dispatch_service.py
  tasks/
    __init__.py
    contracts.py
    read_service.py
    write_service.py
    reminder_service.py
    notification_service.py
  internal/
    deal_notes_service.py       # temporary compatibility facade
    tasks_write_service.py      # temporary compatibility facade
  access/
    tasks_access_service.py     # temporary compatibility facade
```

Reason:

- Notes have note-specific behavior: deal mention participants, duplicate exact-note idempotency, user-created note editing rules, and deal memory refresh.
- Tasks have task-specific behavior: assignee resolution, due dates, reminders, external-assignment emails, task filters, and task options.
- Provider sync already owns provider ingestion for notes/tasks and should not be routed through internal user-write services.
- A generic activity layer can be considered later for read-only timeline aggregation, not for first write standardization.

## Target Boundaries

### Notes

Read callers converge here:

```text
API get deal notes       -> NotesReadService
future agent list notes  -> NotesReadService
timeline projection      -> later activity/timeline read layer
```

Write callers converge here:

```text
API create/update/delete note      -> NotesWriteService
agent add_deal_note                -> NotesWriteService
bot confirmation add_deal_note     -> NotesWriteService
future workflow note writes        -> NotesWriteService
```

Post-commit side effects converge here:

```text
note created and committed -> NotesInsightDispatchService
```

Temporary compatibility:

```text
DealNotesService -> delegates to NotesReadService + NotesWriteService + NotesInsightDispatchService
```

### Tasks

Read callers converge here:

```text
global Tasks page          -> TaskReadService
deal-scoped task list      -> TaskReadService
task assignee filters      -> TaskReadService
task detail                -> TaskReadService
```

Write callers converge here:

```text
API task create/update/delete      -> TaskWriteService
agent create_task                  -> TaskWriteService
bot confirm_task                   -> TaskWriteService
future workflow task create/update -> TaskWriteService
```

Side-effect collaborators:

```text
TaskReminderService      -> create/update/cancel UserReminder and Redis schedule
TaskNotificationService  -> external-assignment email notifications
```

Temporary compatibility:

```text
TasksAccessService -> delegates to TaskReadService
TasksWriteService  -> delegates to TaskWriteService
```

## Non-Negotiable Rules

- Implement notes first, then tasks.
- Do not modify provider sync/webhook note/task writers in this slice.
- Do not add migrations in this slice.
- Do not add workflow dispatch tables or workers in this slice.
- Do not add CRM mutation events for notes/tasks in this slice.
- Do not create a generic `ActivityWriteService` in this slice.
- New note code must live under `src/service/crm/notes/`.
- New task code must live under `src/service/crm/tasks/`.
- Internal user-created notes/tasks have `integration_id = None`.
- Provider-synced notes/tasks have `integration_id` set and remain read-only through internal user-write services.
- Read services must not construct `CRMActivity(...)`.
- Core write services must not commit transactions.
- Routers, tools, bot handlers, and future workflows own commit/rollback.
- External dispatch must happen after commit.
- Reminder Redis scheduling and external-assignee email failures must not make a committed task disappear.
- Every query and write must filter or set `organisation_id`.

## Loopholes To Close

### Loophole 1: Note Write And Insight Dispatch Share One Transaction Boundary

Risk:

```text
note is created
service commits inside trigger_deal_insight_pipeline
caller already committed or wants to rollback
```

Required behavior:

```text
NotesWriteService creates/updates/deletes and flushes only
caller commits
NotesInsightDispatchService runs only after commit
if dispatch fails after commit, note remains created and caller gets current success behavior
```

### Loophole 2: Generic Activity Service Becomes A Dumping Ground

Risk:

```text
notes, tasks, emails, meetings, calls, provider sync, timeline, and workflows all land in one service
```

Required behavior:

```text
notes module owns note product behavior
tasks module owns task product behavior
provider sync keeps owning provider ingestion
timeline/activity aggregation is planned after writes are clean
```

### Loophole 3: Provider-Owned Rows Become Editable

Risk:

```text
integration_id is ignored and user API edits HubSpot/Salesforce notes or tasks
```

Required behavior:

```text
NotesWriteService._load_user_note rejects integration_id is not None
TaskWriteService._load_user_task rejects integration_id is not None
read DTOs keep is_read_only based on integration_id
```

### Loophole 4: Task Core Write Is Coupled To Redis And Email

Risk:

```text
task row is valid but reminder scheduling or external email fails and caller sees inconsistent write behavior
```

Required behavior:

```text
TaskWriteService owns durable CRMActivity mutation
TaskReminderService owns UserReminder and Redis schedule lifecycle
TaskNotificationService owns assignment emails
side effects stay best-effort unless existing behavior currently rejects before task creation
```

### Loophole 5: Compatibility Facades Silently Change Commit Behavior

Risk:

```text
old caller still imports DealNotesService or TasksWriteService and assumes old side effects
```

Required behavior:

```text
first migration either updates the caller in the same task or preserves facade behavior for that caller
facades delegate to new services but keep old method names and return shapes
```

### Loophole 6: Workflows Later Need Task Creation But Get API-Specific Rules

Risk:

```text
workflow-created task goes through a different path than API/agent-created task
```

Required behavior:

```text
TaskWriteService accepts TaskWriteContext(source='workflow', actor_user_id=workflow_actor_id)
API/agent/bot/workflow all use the same create/update/delete service
workflow dispatcher integration is documented as later work
```

## Flow Examples

### Flow A: API Creates A Deal Note

```text
POST /users/crm/deals/{deal_id}/notes
  -> NotesWriteService.create_user_note()
  -> validate deal belongs to organisation
  -> check exact active duplicate for same deal/content/subject
  -> create CRMActivity(activity_type=NOTE, integration_id=NULL)
  -> flush and refresh
router commits
router maps response
if created_new:
  -> NotesInsightDispatchService.trigger_deal_insight_pipeline()
  -> create/get deal memory job
  -> commit job transaction if needed by existing job helper
  -> dispatch Celery deal insight pipeline
frontend receives note response
```

### Flow B: Agent Creates A Deal Note

```text
agent add_deal_note tool
  -> validate organisation_id, user_id, deal_id, content length, subject length
  -> NotesWriteService.create_user_note(created_via='agent_add_deal_note')
  -> commit session
  -> NotesInsightDispatchService.trigger_deal_insight_pipeline() only when created_new
  -> return note details and optional insight_job_id
```

### Flow C: API Creates A Task With Reminder

```text
POST /users/crm/tasks
  -> TaskWriteService.create_task()
  -> validate task type/status/priority
  -> validate optional deal belongs to organisation
  -> resolve owner within organisation
  -> create CRMActivity(activity_type=TASK/TODO, integration_id=NULL)
  -> TaskReminderService.create_or_update_reminder() when reminder enabled
  -> TaskNotificationService.send_external_assignment_email() when external assignee exists
  -> flush and refresh
router commits
router refreshes task and enriches response
```

### Flow D: Future Workflow Creates A Task

```text
workflow run step
  -> TaskWriteService.create_task(context.source='workflow')
  -> same validation and owner/deal rules as API/agent
  -> caller commits
  -> workflow records its own run/step output outside crm_activities
```

## Task 0: Add Notes Module And Boundary Tests

**Files:**

- Create: `src/service/crm/notes/__init__.py`
- Create: `src/service/crm/notes/contracts.py`
- Create: `tests/service/crm/test_notes_service_boundaries.py`

- [ ] **Step 1: Create boundary tests**

Create `tests/service/crm/test_notes_service_boundaries.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path


def test_notes_vertical_module_exists() -> None:
    assert Path("src/service/crm/notes/__init__.py").exists()
    assert Path("src/service/crm/notes/contracts.py").exists()
    assert Path("src/service/crm/notes/read_service.py").exists()
    assert Path("src/service/crm/notes/write_service.py").exists()
    assert Path("src/service/crm/notes/insight_dispatch_service.py").exists()


def test_notes_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/notes/read_service.py").read_text()

    assert "async def create_user_note" not in source
    assert "async def update_user_note" not in source
    assert "async def delete_user_note" not in source
    assert "CRMActivity(" not in source


def test_notes_write_service_does_not_dispatch_insights() -> None:
    source = Path("src/service/crm/notes/write_service.py").read_text()

    assert "dispatch_deal_insight_pipeline" not in source
    assert "get_or_create_deal_memory_job" not in source


def test_deal_notes_service_is_compatibility_facade_after_migration() -> None:
    source = Path("src/service/crm/internal/deal_notes_service.py").read_text()

    assert "NotesReadService" in source
    assert "NotesWriteService" in source
    assert "NotesInsightDispatchService" in source
    assert "CRMActivity(" not in source
```

- [ ] **Step 2: Run boundary tests and verify they fail**

Run:

```bash
uv run pytest tests/service/crm/test_notes_service_boundaries.py -q
```

Expected: FAIL because `src/service/crm/notes/` does not exist yet.

- [ ] **Step 3: Create notes package and contracts**

Create `src/service/crm/notes/__init__.py`:

```python
"""Deal note read, write, and post-commit dispatch services."""
```

Create `src/service/crm/notes/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm import CRMActivity


@dataclass(frozen=True)
class ParticipantInfo:
    """Mention target returned with deal notes."""

    email: str
    name: str | None


@dataclass(frozen=True)
class CreateUserNoteResult:
    """User note creation result with exact-duplicate idempotency semantics."""

    note: CRMActivity
    created_new: bool


@dataclass(frozen=True)
class NoteWriteContext:
    """Caller metadata for internal notes and future note mutation events."""

    source: str
    actor_user_id: UUID
    actor_name: str | None = None
    actor_email: str | None = None
    created_via: str = "api"


class NotesServiceError(Exception):
    """HTTP-shaped notes service error for routers, agents, and bot handlers."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
```

## Task 1: Extract NotesReadService

**Files:**

- Create: `src/service/crm/notes/read_service.py`
- Modify later: `src/service/crm/internal/deal_notes_service.py`
- Test: `tests/service/crm/test_notes_service_boundaries.py`

- [ ] **Step 1: Move read-only note behavior**

Create `src/service/crm/notes/read_service.py` by moving these methods from `DealNotesService` without changing behavior:

```text
close
session property
get_deal_notes
_get_deal_participants
_verify_deal_access
determine_note_source
```

Use this constructor:

```python
class NotesReadService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Import the shared contracts:

```python
from src.service.crm.notes.contracts import NotesServiceError, ParticipantInfo
```

- [ ] **Step 2: Verify read boundary**

Run:

```bash
uv run pytest tests/service/crm/test_notes_service_boundaries.py::test_notes_read_service_does_not_expose_writes -q
```

Expected: PASS.

## Task 2: Extract NotesWriteService

**Files:**

- Create: `src/service/crm/notes/write_service.py`
- Test: `tests/service/crm/test_notes_write_service.py`

- [ ] **Step 1: Create focused note write tests**

Create `tests/service/crm/test_notes_write_service.py` with tests named:

- `test_create_user_note_creates_internal_note_with_integration_id_null`
- `test_create_user_note_reuses_exact_active_duplicate`
- `test_update_user_note_rejects_provider_synced_note`
- `test_delete_user_note_soft_deletes_user_created_note`
- `test_note_write_service_does_not_commit`

The assertions must match current `DealNotesService` behavior:

```text
activity_type = NOTE
integration_id = None
source = MANUAL
properties.is_user_created = True
properties.created_via = context.created_via
exact duplicate means same deal_id, body, subject, active, integration_id NULL
```

- [ ] **Step 2: Move write behavior**

Create `src/service/crm/notes/write_service.py` by moving these methods from `DealNotesService`:

```text
close
session property
create_user_note
update_user_note
delete_user_note
_verify_deal_access
_load_user_note
_find_existing_user_note
```

Use this constructor:

```python
class NotesWriteService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Use this public create signature:

```python
async def create_user_note(
    self,
    deal_id: UUID,
    content: str,
    subject: str | None,
    context: NoteWriteContext,
) -> CreateUserNoteResult:
```

The service must call `flush()` and `refresh()`, but must not call `commit()`.

- [ ] **Step 3: Run note write tests**

Run:

```bash
uv run pytest tests/service/crm/test_notes_write_service.py -q
```

Expected: PASS.

## Task 3: Extract NotesInsightDispatchService

**Files:**

- Create: `src/service/crm/notes/insight_dispatch_service.py`
- Modify later: `src/api/routers/crm/deal_notes_handler.py`
- Modify later: `src/agents/tools/crm/add_deal_note.py`
- Test: `tests/service/crm/test_notes_insight_dispatch_service.py`

- [ ] **Step 1: Create dispatch tests**

Create `tests/service/crm/test_notes_insight_dispatch_service.py` with tests named:

- `test_trigger_deal_insight_pipeline_returns_job_id_on_success`
- `test_trigger_deal_insight_pipeline_returns_none_when_dispatch_fails`

Keep the current contract: dispatch failures are logged and return `None`, because note creation should remain successful after commit.

- [ ] **Step 2: Move dispatch behavior**

Create `src/service/crm/notes/insight_dispatch_service.py` and move `trigger_deal_insight_pipeline()` from `DealNotesService`.

Use this constructor:

```python
class NotesInsightDispatchService:
    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id
```

Do not call this service before the note write caller commits.

- [ ] **Step 3: Run dispatch tests**

Run:

```bash
uv run pytest tests/service/crm/test_notes_insight_dispatch_service.py -q
```

Expected: PASS.

## Task 4: Convert DealNotesService Into Compatibility Facade

**Files:**

- Modify: `src/service/crm/internal/deal_notes_service.py`
- Test: `tests/service/crm/test_deal_notes_service_participants.py`
- Test: `tests/service/crm/test_internal_crm_external_id_cleanup.py`
- Test: `tests/service/crm/test_notes_service_boundaries.py`

- [ ] **Step 1: Replace implementation with delegation**

Keep the public class and error name:

```python
DealNotesServiceError = NotesServiceError


class DealNotesService:
    """Compatibility facade; use NotesReadService, NotesWriteService, or NotesInsightDispatchService."""
```

Delegate:

```text
get_deal_notes -> NotesReadService.get_deal_notes
determine_note_source -> NotesReadService.determine_note_source
create_user_note -> NotesWriteService.create_user_note
update_user_note -> NotesWriteService.update_user_note
delete_user_note -> NotesWriteService.delete_user_note
trigger_deal_insight_pipeline -> NotesInsightDispatchService.trigger_deal_insight_pipeline
```

For old callers, keep the current `create_user_note(...)` signature and convert it into `NoteWriteContext`.

- [ ] **Step 2: Verify notes facade**

Run:

```bash
uv run pytest tests/service/crm/test_deal_notes_service_participants.py tests/service/crm/test_internal_crm_external_id_cleanup.py tests/service/crm/test_notes_service_boundaries.py -q
```

Expected: PASS.

## Task 5: Route Notes API, Agent Tool, And Bot Handler To New Services

**Files:**

- Modify: `src/api/routers/crm/deal_notes_handler.py`
- Modify: `src/agents/tools/crm/add_deal_note.py`
- Modify: `src/service/bots/form_handlers/add_deal_note_handler.py`
- Test: `tests/agents/tools/crm/test_add_deal_note_direct.py`
- Test: `tests/service/bots/form_handlers/test_add_deal_note_handler_nonce.py`

- [ ] **Step 1: Route API dependencies**

Use `NotesReadService` for GET and `NotesWriteService` for POST/PATCH/DELETE.

Use `NotesInsightDispatchService` only after successful commit and only when `created_new` is true.

Create notes with:

```python
NoteWriteContext(
    source="api",
    actor_user_id=user_ids.user_id,
    actor_name=user_ids.user_name,
    actor_email=user_ids.user_email,
    created_via="api",
)
```

- [ ] **Step 2: Route agent direct note creation**

Replace direct `DealNotesService` usage in `src/agents/tools/crm/add_deal_note.py` with:

```text
NotesWriteService for create_user_note
NotesInsightDispatchService after session.commit()
```

Use:

```python
NoteWriteContext(
    source="agent",
    actor_user_id=user_id,
    actor_name=user_name,
    actor_email=context.user_email,
    created_via="agent_add_deal_note",
)
```

- [ ] **Step 3: Route bot confirmation handler**

Replace direct `DealNotesService` usage in `src/service/bots/form_handlers/add_deal_note_handler.py` with `NotesWriteService`.

Keep existing security behavior:

```text
resolve user from bot activity
trust resolved user's organisation_id
log form organisation mismatch
commit after successful write
```

- [ ] **Step 4: Verify notes caller behavior**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_add_deal_note_direct.py tests/service/bots/form_handlers/test_add_deal_note_handler_nonce.py -q
```

Expected: PASS.

## Task 6: Add Tasks Module And Boundary Tests

**Files:**

- Create: `src/service/crm/tasks/__init__.py`
- Create: `src/service/crm/tasks/contracts.py`
- Create: `tests/service/crm/test_task_service_boundaries.py`

- [ ] **Step 1: Create boundary tests**

Create `tests/service/crm/test_task_service_boundaries.py`:

```python
from __future__ import annotations

from pathlib import Path


def test_tasks_vertical_module_exists() -> None:
    assert Path("src/service/crm/tasks/__init__.py").exists()
    assert Path("src/service/crm/tasks/contracts.py").exists()
    assert Path("src/service/crm/tasks/read_service.py").exists()
    assert Path("src/service/crm/tasks/write_service.py").exists()
    assert Path("src/service/crm/tasks/reminder_service.py").exists()
    assert Path("src/service/crm/tasks/notification_service.py").exists()


def test_task_read_service_does_not_expose_writes() -> None:
    source = Path("src/service/crm/tasks/read_service.py").read_text()

    assert "async def create_task" not in source
    assert "async def update_task" not in source
    assert "async def delete_task" not in source
    assert "CRMActivity(" not in source


def test_task_write_service_does_not_import_router_or_agent_layers() -> None:
    source = Path("src/service/crm/tasks/write_service.py").read_text()

    assert "src.api." not in source
    assert "src.agents." not in source


def test_legacy_task_services_are_facades_after_migration() -> None:
    access_source = Path("src/service/crm/access/tasks_access_service.py").read_text()
    write_source = Path("src/service/crm/internal/tasks_write_service.py").read_text()

    assert "TaskReadService" in access_source
    assert "TaskWriteService" in write_source
```

- [ ] **Step 2: Run boundary tests and verify they fail**

Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py -q
```

Expected: FAIL because `src/service/crm/tasks/` does not exist yet.

- [ ] **Step 3: Create task package and contracts**

Create `src/service/crm/tasks/__init__.py`:

```python
"""Task read, write, reminder, and notification services for CRM activities."""
```

Create `src/service/crm/tasks/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.domain.models.organisation_tables.sync.crm import CRMActivity


@dataclass(frozen=True)
class TaskWriteContext:
    """Caller metadata for internal task writes and future task mutation events."""

    source: str
    actor_user_id: UUID
    created_via: str = "api"
    user_timezone: str | None = None


@dataclass(frozen=True)
class TaskWriteResult:
    """Canonical task write result returned to API, agents, bot forms, and workflows."""

    task: CRMActivity


class TaskReadServiceError(Exception):
    """HTTP-shaped task read error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TaskWriteServiceError(Exception):
    """HTTP-shaped task write error."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
```

## Task 7: Extract TaskReadService

**Files:**

- Create: `src/service/crm/tasks/read_service.py`
- Modify later: `src/service/crm/access/tasks_access_service.py`
- Test: `tests/api/routers/crm/test_tasks_handler.py`

- [ ] **Step 1: Move read-only behavior**

Create `src/service/crm/tasks/read_service.py` from `src/service/crm/access/tasks_access_service.py`.

Move these functions/classes without behavior changes:

```text
_normalize_email
_task_assignee_filter_key
TaskReadService
list_tasks_by_deal
list_tasks
resolve_current_user_task_assignees
list_task_assignees
get_task_by_id
_assigned_to_key_condition
_has_no_external_assignee_condition
_current_user_assignee_condition
_resolve_current_user_crm_user_ids
_count_tasks_for_condition
```

Use:

```python
from src.service.crm.tasks.contracts import TaskReadServiceError
```

- [ ] **Step 2: Verify task read behavior through existing API tests**

Run:

```bash
uv run pytest tests/api/routers/crm/test_tasks_handler.py -q
```

Expected: PASS after the compatibility facade is wired in Task 10. During extraction before facade wiring, boundary tests are enough.

## Task 8: Extract TaskReminderService And TaskNotificationService

**Files:**

- Create: `src/service/crm/tasks/reminder_service.py`
- Create: `src/service/crm/tasks/notification_service.py`
- Modify later: `src/service/crm/tasks/write_service.py`
- Test: existing task write/reminder tests under `tests/service/crm/` and `tests/agents/tools/tasks/`

- [ ] **Step 1: Move reminder lifecycle**

Create `src/service/crm/tasks/reminder_service.py` and move these methods from old `TasksWriteService`:

```text
_create_task_reminder
_update_task_reminder
_cancel_task_reminders
_get_active_reminder
_resolve_task_reminder_target
_resolve_delivery_target_for_channel
_normalize_delivery_context
_delivery_context_is_ready
_safe_schedule
_safe_unschedule
```

Use this constructor:

```python
class TaskReminderService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
```

- [ ] **Step 2: Move external assignment notifications**

Create `src/service/crm/tasks/notification_service.py` and move:

```text
_validate_external_assignee_domain
_send_external_assignment_email
```

Use this constructor:

```python
class TaskNotificationService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, actor_user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
```

Preserve current behavior: external domain validation can reject before write; email send failures are logged and do not fail task creation/update.

## Task 9: Extract TaskWriteService

**Files:**

- Create: `src/service/crm/tasks/write_service.py`
- Test: `tests/service/crm/test_internal_crm_external_id_cleanup.py`
- Test: `tests/agents/tools/tasks/test_create_task.py`

- [ ] **Step 1: Move core task write behavior**

Create `src/service/crm/tasks/write_service.py` from old `TasksWriteService`.

Move:

```text
create_task
update_task
delete_task
_load_deal
_load_user_task
_normalize_due_payload
_resolve_owner
_normalize_enum
```

Use collaborators:

```python
self._reminders = TaskReminderService(session, organisation_id, actor_user_id)
self._notifications = TaskNotificationService(session, organisation_id, actor_user_id)
```

Use the class name:

```python
class TaskWriteService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
        *,
        user_timezone: str | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.user_timezone = resolve_timezone_name(user_timezone)
        self._reminders = TaskReminderService(session, organisation_id, actor_user_id)
        self._notifications = TaskNotificationService(session, organisation_id, actor_user_id)
```

- [ ] **Step 2: Preserve read-only provider guard**

Keep the authoritative predicate:

```python
if task.integration_id is not None:
    raise TaskWriteServiceError("External CRM tasks are read-only", 409)
```

- [ ] **Step 3: Keep transaction ownership outside the service**

`TaskWriteService` and its collaborators may call `flush()` and `refresh()`.

They must not call:

```python
await self.session.commit()
```

- [ ] **Step 4: Verify task write behavior**

Run:

```bash
uv run pytest tests/service/crm/test_internal_crm_external_id_cleanup.py tests/agents/tools/tasks/test_create_task.py -q
```

Expected: PASS after compatibility wiring in Task 10 and caller routing in Task 11.

## Task 10: Convert Legacy Task Services Into Compatibility Facades

**Files:**

- Modify: `src/service/crm/access/tasks_access_service.py`
- Modify: `src/service/crm/internal/tasks_write_service.py`
- Test: `tests/service/crm/test_task_service_boundaries.py`

- [ ] **Step 1: Convert access service**

Keep:

```python
TasksAccessServiceError = TaskReadServiceError


class TasksAccessService:
    """Compatibility facade; use TaskReadService for new code."""
```

Delegate all read methods to `TaskReadService`.

- [ ] **Step 2: Convert write service**

Keep:

```python
TasksWriteServiceError = TaskWriteServiceError


class TasksWriteService:
    """Compatibility facade; use TaskWriteService for new code."""
```

Delegate:

```text
create_task -> TaskWriteService.create_task
update_task -> TaskWriteService.update_task
delete_task -> TaskWriteService.delete_task
```

Keep the same constructor and return shape so current callers keep working during migration.

- [ ] **Step 3: Verify task facades**

Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py -q
```

Expected: PASS.

## Task 11: Route Task API, Agent Tool, And Bot Handler To New Services

**Files:**

- Modify: `src/api/routers/crm/tasks_handler.py`
- Modify: `src/agents/tools/tasks/create_task.py`
- Modify: `src/service/bots/form_handlers/confirm_task_handler.py`
- Test: `tests/api/routers/crm/test_tasks_handler.py`
- Test: `tests/agents/tools/tasks/test_create_task.py`
- Test: `tests/service/bots/form_handlers/test_confirm_task_handler.py`

- [ ] **Step 1: Route API reads and writes**

Use:

```python
from src.service.crm.tasks.read_service import TaskReadService
from src.service.crm.tasks.write_service import TaskWriteService
from src.service.crm.tasks.contracts import TaskReadServiceError, TaskWriteServiceError
```

Reads use `TaskReadService`.

Writes use `TaskWriteService`.

Router keeps:

```text
commit/rollback
response enrichment
rate limits
CRM write-scope guard
```

- [ ] **Step 2: Route agent task tool**

Update `_load_tasks_write_service()` in `src/agents/tools/tasks/create_task.py` to load:

```python
from src.service.crm.tasks.write_service import TaskWriteService
from src.service.crm.tasks.contracts import TaskWriteServiceError
```

Keep the tool-level validation and date/deal/user resolution unchanged.

- [ ] **Step 3: Route bot confirmation handler**

Replace `src.service.crm.internal.tasks_write_service.TasksWriteService` import with:

```python
from src.service.crm.tasks.write_service import TaskWriteService
```

Keep the existing security behavior:

```text
resolve user from bot identity
validate payload organisation_id against resolved user organisation
create task
commit
```

- [ ] **Step 4: Verify task callers**

Run:

```bash
uv run pytest tests/api/routers/crm/test_tasks_handler.py tests/agents/tools/tasks/test_create_task.py tests/service/bots/form_handlers/test_confirm_task_handler.py -q
```

Expected: PASS.

## Task 12: Focused Verification

**Files:**

- No code files unless verification exposes a bug.

- [ ] **Step 1: Verify notes boundaries and behavior**

Run:

```bash
uv run pytest tests/service/crm/test_notes_service_boundaries.py tests/service/crm/test_notes_write_service.py tests/service/crm/test_notes_insight_dispatch_service.py -q
```

Expected: PASS.

- [ ] **Step 2: Verify notes API/agent/bot behavior**

Run:

```bash
uv run pytest tests/agents/tools/crm/test_add_deal_note_direct.py tests/agents/tools/crm/test_add_deal_note_confirmation.py tests/service/bots/form_handlers/test_add_deal_note_handler_nonce.py -q
```

Expected: PASS.

- [ ] **Step 3: Verify task boundaries and behavior**

Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py tests/service/crm/test_internal_crm_external_id_cleanup.py tests/agents/tools/tasks/test_create_task.py -q
```

Expected: PASS.

- [ ] **Step 4: Verify task API and bot behavior**

Run:

```bash
uv run pytest tests/api/routers/crm/test_tasks_handler.py tests/service/bots/form_handlers/test_confirm_task_handler.py -q
```

Expected: PASS.

- [ ] **Step 5: Focused lint and type checks before PR**

Run only before pushing or opening a PR:

```bash
uv run ruff check src/service/crm/notes src/service/crm/tasks src/service/crm/internal/deal_notes_service.py src/service/crm/internal/tasks_write_service.py src/service/crm/access/tasks_access_service.py src/api/routers/crm/deal_notes_handler.py src/api/routers/crm/tasks_handler.py src/agents/tools/crm/add_deal_note.py src/agents/tools/tasks/create_task.py src/service/bots/form_handlers/add_deal_note_handler.py src/service/bots/form_handlers/confirm_task_handler.py tests/service/crm/test_notes_service_boundaries.py tests/service/crm/test_task_service_boundaries.py
```

Expected: PASS.

Run:

```bash
uv run mypy --no-incremental src/service/crm/notes src/service/crm/tasks src/service/crm/internal/deal_notes_service.py src/service/crm/internal/tasks_write_service.py src/service/crm/access/tasks_access_service.py src/api/routers/crm/deal_notes_handler.py src/api/routers/crm/tasks_handler.py src/agents/tools/crm/add_deal_note.py src/agents/tools/tasks/create_task.py src/service/bots/form_handlers/add_deal_note_handler.py src/service/bots/form_handlers/confirm_task_handler.py
```

Expected: PASS.

## Later Cleanup Phase

Do this only after notes/tasks modules are merged and production imports are migrated.

1. Search legacy imports:

   ```bash
   rg -n "DealNotesService|TasksAccessService|TasksWriteService|deal_notes_service|tasks_access_service|tasks_write_service" src tests
   ```

2. If only compatibility tests remain, move or delete:

   ```text
   src/service/crm/internal/deal_notes_service.py
   src/service/crm/internal/tasks_write_service.py
   src/service/crm/access/tasks_access_service.py
   ```

3. Plan the timeline/activity read layer:

   ```text
   ActivityTimelineReadService
   combines notes, tasks, emails, meetings, calls
   read/projection only
   no internal writes
   ```

4. Plan workflow integration:

   ```text
   workflow action "create task" -> TaskWriteService
   workflow trigger "note created" or "task completed" -> separate workflow dispatch layer
   no workflow runtime columns on crm_activities
   ```

5. Plan notes/tasks mutation events only after workflow dispatcher design is ready.

## Rollout Notes

- Notes and tasks are planned together but should be implemented one by one.
- Notes should be implemented first because the service is smaller and proves the post-commit dispatch boundary.
- Tasks should be implemented second because reminder/email side effects make it riskier.
- Provider sync/webhook writers remain untouched.
- Existing API paths remain unchanged.
- Existing agent tool names remain unchanged.
- Existing bot confirmation verbs remain unchanged.
- Compatibility facades keep old imports working during migration.
- No database migrations are part of this plan.
- No workflow dispatcher implementation is part of this plan.

## Success Criteria

- `src/service/crm/notes/` exists with contracts, read service, write service, and insight dispatch service.
- `src/service/crm/tasks/` exists with contracts, read service, write service, reminder service, and notification service.
- `DealNotesService` is a compatibility facade.
- `TasksAccessService` is a compatibility facade.
- `TasksWriteService` is a compatibility facade.
- Notes API, agent tool, and bot confirmation use notes services directly.
- Tasks API, agent tool, and bot confirmation use task services directly.
- Provider-synced notes/tasks remain read-only in user write paths.
- Core write services do not commit transactions.
- External dispatch runs after commit.
- Focused notes/tasks service, API, agent, bot, ruff, and mypy checks pass before PR.

## Self-Review

Spec coverage:

- Accounts/contact/participant vertical-module pattern is mirrored.
- Notes and tasks are planned as separate product modules over shared `crm_activities`.
- Provider sync is explicitly excluded.
- Workflow dispatcher and CRM mutation events are explicitly deferred.
- Side effects are separated from core writes.
- Compatibility facades protect current imports during rollout.

Placeholder scan:

- The plan contains no open implementation placeholders.
- Each task names files, expected services, behavior, commands, and expected results.

Type consistency:

- Notes use `NotesServiceError`, `NotesReadService`, `NotesWriteService`, `NotesInsightDispatchService`, `NoteWriteContext`, and `CreateUserNoteResult`.
- Tasks use `TaskReadServiceError`, `TaskWriteServiceError`, `TaskReadService`, `TaskWriteService`, `TaskReminderService`, `TaskNotificationService`, and `TaskWriteContext`.
