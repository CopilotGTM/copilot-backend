# Task Handler Boundary Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move non-HTTP task response and reminder logic out of `tasks_handler.py` without changing the public Tasks API contract.

**Architecture:** `TaskReadService` owns database reads and read-side enrichment, including reminder existence/details. A new API presenter owns conversion from task read records to `TaskListItem`, `TaskDetail`, and assignee options. `tasks_handler.py` remains responsible for FastAPI dependencies, request parameters, service calls, response envelopes, and error mapping.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy async sessions, Pydantic response models, pytest, ruff, mypy.

---

## Boundary Decision

The current `src/api/routers/crm/tasks_handler.py` mixes three responsibilities:

- HTTP orchestration: routes, `Depends`, query params, response envelopes, write guards.
- Read-side data access: direct `UserReminder` SQL queries.
- API DTO construction: assignee mapping, due metadata, overdue status, source fields, detail/list model assembly.

This violates the repo architecture guideline that API endpoints should use services for database work. It also creates a future maintenance loophole: every source-table field added to `crm_tasks` risks growing the router again.

The better pattern is:

- `src/service/crm/tasks/read_service.py`: owns all SQL and org-scoped read enrichment.
- `src/service/crm/tasks/contracts.py`: exposes API-neutral read records with task, deal name, reminder summary, and active-reminder flag.
- `src/api/presenters/crm/task_presenter.py`: owns API schema construction.
- `src/api/routers/crm/tasks_handler.py`: stays thin and does not import SQLAlchemy reminder models.

## Loopholes To Close Before Implementation

- Reminder reads must be organisation-scoped. The current router helper checks `activity_id` and status only; the service method must also filter by `organisation_id`.
- The service layer must not import API schemas. `TaskReadService` returns plain dataclasses/dicts; presenter imports `TaskListItem`, `TaskDetail`, and `TaskAssignee`.
- The presenter must not run SQL. It should be synchronous and deterministic over `TaskReadRecord` or `CRMTask`.
- Write endpoints hydrate records after commit through `TaskWriteService.read_task_record()`. That method must return the same enriched record shape as `TaskReadService.get_task_by_id()`.
- Delete currently returns a raw `CRMTask`; the presenter must support a raw task record without requiring DB reads after soft delete.
- Response JSON must not change: field names, due metadata, `has_reminder`, `reminder`, `source`, and all-day behavior remain stable.
- No schema migrations, no new task columns, no table-level constraints.

## File Structure

- Create `src/api/presenters/crm/__init__.py`
  - Makes the presenter package explicit.
- Create `src/api/presenters/crm/task_presenter.py`
  - Builds task API response models from source rows and read records.
- Modify `src/service/crm/tasks/contracts.py`
  - Add `TaskReminderSummary` and reminder fields to `TaskReadRecord`.
- Modify `src/service/crm/tasks/read_service.py`
  - Add organisation-scoped reminder summary loading and attach it to read records.
- Modify `src/service/crm/tasks/write_service.py`
  - Ensure post-write `read_task_record()` returns the enriched read record.
- Modify `src/api/routers/crm/tasks_handler.py`
  - Remove presenter helpers and direct reminder SQL imports.
- Modify `tests/api/routers/crm/test_tasks_handler.py`
  - Import presenter for direct mapping tests instead of router internals.
- Modify `tests/api/routers/crm/conftest.py`
  - Keep fake task service returning enriched `TaskReadRecord` shape.
- Modify `tests/service/crm/test_task_service_boundaries.py`
  - Add boundary tests that prevent SQL/reminder models from returning to the router.

## Tasks

### Task 1: Add Boundary Tests

**Files:**
- Modify: `tests/service/crm/test_task_service_boundaries.py`
- Modify: `tests/api/routers/crm/test_tasks_handler.py`

- [ ] Add a service boundary test that reads `src/api/routers/crm/tasks_handler.py` and asserts it does not import or reference `UserReminder`, `ReminderStatus`, `select(`, `func`, `col`, or `AsyncSession`.
- [ ] Add a presenter package existence assertion for `src/api/presenters/crm/task_presenter.py`.
- [ ] Change `test_task_response_reads_all_day_due_metadata` to import `present_task_detail` from the presenter and call it synchronously.
- [ ] Run:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py::test_task_handler_does_not_own_task_presentation_or_reminder_sql tests/api/routers/crm/test_tasks_handler.py::test_task_response_reads_all_day_due_metadata -q
```

Expected: fail because the presenter does not exist and the router still owns SQL/presentation helpers.

### Task 2: Create Presenter

**Files:**
- Create: `src/api/presenters/crm/__init__.py`
- Create: `src/api/presenters/crm/task_presenter.py`
- Modify: `src/api/routers/crm/tasks_handler.py`

- [ ] Move these helpers into `task_presenter.py`:
  - `_compute_is_overdue`
  - `_normalize_email`
  - `_parse_uuid`
  - `_task_due_datetime`
  - `_task_source`
  - `_build_task_assignee`
  - `_build_task_due_metadata`
  - `_enrich_task_list_item`
  - `_enrich_task_detail`
- [ ] Rename public presenter functions:
  - `present_task_list_item(record: TaskReadRecord | CRMTask) -> TaskListItem`
  - `present_task_detail(record: TaskReadRecord | CRMTask) -> TaskDetail`
  - `present_task_assignee(task: Any) -> TaskAssignee | None`
- [ ] Keep presenter synchronous and free of SQLAlchemy imports.
- [ ] Replace router calls to `_enrich_task_list_item` and `_enrich_task_detail` with presenter calls.

### Task 3: Move Reminder Reads Into TaskReadService

**Files:**
- Modify: `src/service/crm/tasks/contracts.py`
- Modify: `src/service/crm/tasks/read_service.py`
- Modify: `src/service/crm/tasks/write_service.py`

- [ ] Add:

```python
@dataclass(frozen=True)
class TaskReminderSummary:
    reminder_id: UUID
    status: str
    trigger_time: datetime | None
    recurrence: dict | None
    delivery_channel: str | None
```

- [ ] Extend `TaskReadRecord`:

```python
task: CRMTask
deal_name: str | None = None
has_active_reminder: bool = False
reminder: TaskReminderSummary | None = None
```

- [ ] In `TaskReadService`, add an org-scoped batch reminder loader for task ids returned by list/detail queries.
- [ ] Attach reminder summaries in `_read_records(...)` via an async enrichment helper.
- [ ] Keep `TaskWriteService.read_task_record()` delegating to `TaskReadService.get_task_by_id()` so post-write responses get the same enrichment.

### Task 4: Thin Router

**Files:**
- Modify: `src/api/routers/crm/tasks_handler.py`

- [ ] Remove imports for `datetime`, `ZoneInfo`, `ZoneInfoNotFoundError`, `select`, `func`, `col`, `UserReminder`, `ReminderStatus`, `CRMTask`, `TaskReadRecord`, `TaskAssignee`, `TaskDetail`, and `TaskListItem` when no longer needed.
- [ ] Keep option constants and route functions in the router.
- [ ] Use `present_task_list_item(...)`, `present_task_detail(...)`, and `present_task_assignee(...)`.
- [ ] Keep error handling and response envelopes unchanged.

### Task 5: Verify

**Files:**
- No production edits unless verification exposes a bug.

- [ ] Run focused tests:

```bash
uv run pytest tests/service/crm/test_task_service_boundaries.py tests/api/routers/crm/test_tasks_handler.py::test_task_response_reads_all_day_due_metadata tests/api/routers/crm/test_tasks_handler.py::test_get_task_success tests/api/routers/crm/test_tasks_handler.py::test_list_tasks_is_overdue_computed -q
```

- [ ] Run lint/type checks for touched files:

```bash
uv run ruff check src/api/routers/crm/tasks_handler.py src/api/presenters/crm/task_presenter.py src/service/crm/tasks/contracts.py src/service/crm/tasks/read_service.py src/service/crm/tasks/write_service.py tests/service/crm/test_task_service_boundaries.py tests/api/routers/crm/test_tasks_handler.py
uv run mypy --no-incremental src/api/routers/crm/tasks_handler.py src/api/presenters/crm/task_presenter.py src/service/crm/tasks/contracts.py src/service/crm/tasks/read_service.py src/service/crm/tasks/write_service.py
```

- [ ] Confirm with `rg`:

```bash
rg -n "UserReminder|ReminderStatus|select\\(|func|col\\(|_enrich_task|_has_active_reminder|_get_reminder_dict" src/api/routers/crm/tasks_handler.py
```

Expected: no matches for direct reminder SQL or old private enrichment helpers.
