# Deal Task Agent Implementation Plan

> Historical note: this plan predates the task/note source-table migration.
> Current runtime code should use `crm_tasks` / `CRMTask` for task source rows
> and treat `crm_activities` / `CRMActivity` as the feed projection only.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a post-meeting `DealTaskAgent` that creates and completes deal-scoped CRM tasks from meeting summaries after the existing deal insight pipeline.

**Architecture:** Add a deal-scoped Agno workflow with direct-write tools backed by a deterministic automation service. Wire a new Celery job after deal memory, risk/blocker extraction, and stakeholder analysis so task creation uses the updated deal context. Keep notifications out of this slice but return enough write metadata for a later notifier.

**Tech Stack:** Python 3.11, FastAPI service layer, SQLModel/SQLAlchemy async sessions, Agno agents/tools, Celery deal worker, PostgreSQL JSONB task provenance, Ruff/Pytest.

---

## File Structure

- Create `src/service/crm/internal/deal_task_automation_service.py`
  - Deterministic task create/complete/list operations for unattended AI meeting tasks.
- Create `src/agents/tools/tasks/list_deal_tasks.py`
  - Agent tool for current deal task context.
- Create `src/agents/tools/tasks/create_deal_task.py`
  - Agent tool for creating AI meeting tasks.
- Create `src/agents/tools/tasks/complete_deal_task.py`
  - Agent tool for completing existing editable deal tasks.
- Modify `src/agents/tools/tasks/__init__.py`
  - Export new task tool factories.
- Modify `src/agents/tools/registry.py`
  - Register new tool names and map them to `DealTaskAgent`.
- Create `src/agents/workflows/deal_tasks/models.py`
  - Pydantic contracts for agent result and tool result shape.
- Create `src/agents/workflows/deal_tasks/prompts.py`
  - Strict task extraction/completion prompt.
- Create `src/agents/workflows/deal_tasks/agent.py`
  - Agno agent wrapper using the direct-write tools.
- Create `src/agents/workflows/deal_tasks/__init__.py`
  - Export `DealTaskAgent` and result models.
- Modify `src/infra/config/agent_config.py`
  - Add `deal_task_agent` model config and env overrides.
- Modify `src/domain/models/organisation_tables/job_models.py`
  - Add explicit `DEAL_TASK_SYNC` job type.
- Create `src/workers/deal_worker/tasks/deal_insight_tasks/deal_task_sync_task.py`
  - Celery task that runs the agent and records job status/costing.
- Modify `src/workers/deal_worker/tasks/deal_insight_tasks/__init__.py`
  - Import/export the new Celery task so worker discovery loads it.
- Modify `src/workers/deal_worker/config.py`
  - Route `deal_tasks.sync_from_summary` to `deal_jobs`.
- Modify `src/workers/deal_worker/utils/pipeline_dispatch.py`
  - Create a task-sync job and append the task-sync signature after the existing insight group.
- Add tests under `tests/service/crm/internal/`, `tests/agents/tools/tasks/`,
  `tests/agents/workflows/deal_tasks/`, and `tests/workers/deal_worker/`.

---

### Task 1: Add The Automation Service

**Files:**
- Create: `src/service/crm/internal/deal_task_automation_service.py`
- Test: `tests/service/crm/internal/test_deal_task_automation_service.py`

- [ ] **Step 1: Write failing tests for task creation, missing assignee fallback, dedupe, and completion guards**

Create `tests/service/crm/internal/test_deal_task_automation_service.py` with focused async tests. Use existing CRM task test fixtures if available in `tests/api/routers/crm/conftest.py`; otherwise create local SQLModel objects with an async session fixture already used in service tests.

Test names and expected behavior:

```python
async def test_create_ai_meeting_task_with_resolved_owner(org_session, org_id, user):
    service = DealTaskAutomationService(org_session, org_id)
    result = await service.create_task(
        deal_id=deal.deal_id,
        subject="Send pricing proposal",
        body="Follow up with the revised commercial proposal.",
        assignee_name_or_email=str(user.email),
        source_event_id=event_id,
        source_association_id=association_id,
        source_summary_job_id=job_id,
    )

    assert result.created is True
    assert result.task.subject == "Send pricing proposal"
    assert result.task.source == CRMActivitySource.AI_MEETING.value
    assert result.task.owner_external_id == str(user.user_id)
    assert result.task.properties["created_via"] == "deal_task_agent"
    assert result.task.properties["source_association_id"] == str(association_id)
```

```python
async def test_create_ai_meeting_task_without_assignee_still_creates_deal_task(org_session, org_id):
    result = await service.create_task(
        deal_id=deal.deal_id,
        subject="Confirm integration timeline",
        body=None,
        assignee_name_or_email=None,
        source_event_id=event_id,
        source_association_id=association_id,
        source_summary_job_id=job_id,
    )

    assert result.created is True
    assert result.task.deal_id == deal.deal_id
    assert result.task.owner_external_id is None
    assert result.task.properties["assignee_resolution"]["status"] == "not_provided"
```

```python
async def test_create_ai_meeting_task_is_idempotent_for_same_summary_source(org_session, org_id):
    first = await service.create_task(...)
    second = await service.create_task(...)

    assert first.created is True
    assert second.created is False
    assert second.task.activity_id == first.task.activity_id
    assert second.reason == "duplicate"
```

```python
async def test_complete_task_refuses_crm_synced_activity(org_session, org_id):
    crm_task.integration_id = uuid4()
    with pytest.raises(DealTaskAutomationError) as exc:
        await service.complete_task(crm_task.activity_id, completion_reason="Summary says it was sent")

    assert exc.value.status_code == 409
```

- [ ] **Step 2: Run the new service tests and verify they fail**

Run:

```bash
uv run pytest tests/service/crm/internal/test_deal_task_automation_service.py -v
```

Expected: import failures for `DealTaskAutomationService`.

- [ ] **Step 3: Implement `DealTaskAutomationService`**

Create `src/service/crm/internal/deal_task_automation_service.py` with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityObjectType,
    CRMActivityPriority,
    CRMActivitySource,
    CRMActivityStatus,
    CRMActivityType,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.infra.database.sessions import master_async_session_scope


class DealTaskAutomationError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass
class TaskWriteResult:
    task: CRMActivity
    created: bool
    reason: str | None = None


class DealTaskAutomationService:
    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def list_deal_tasks(self, deal_id: UUID) -> list[CRMActivity]:
        c = CRMActivity.__table__.c
        stmt = (
            select(CRMActivity)
            .where(
                c.organisation_id == self.organisation_id,
                c.deal_id == deal_id,
                c.activity_type.in_([CRMActivityType.TASK.value, CRMActivityType.TODO.value]),
                or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
            )
            .order_by(c.created_at.desc(), c.activity_id.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create_task(
        self,
        *,
        deal_id: UUID,
        subject: str,
        body: str | None,
        assignee_name_or_email: str | None,
        source_event_id: UUID | None,
        source_association_id: UUID | None,
        source_summary_job_id: UUID | None,
        priority: str = CRMActivityPriority.NONE.value,
        due_date: datetime | None = None,
    ) -> TaskWriteResult:
        deal = await self._load_deal(deal_id)
        normalized_subject = self._normalize_subject(subject)
        idempotency_key = self._build_idempotency_key(
            deal_id=deal_id,
            source_event_id=source_event_id,
            source_association_id=source_association_id,
            normalized_subject=normalized_subject,
        )
        existing = await self._find_existing(idempotency_key)
        if existing:
            return TaskWriteResult(task=existing, created=False, reason="duplicate")

        owner_user_id, owner_name, owner_email, resolution = await self._resolve_assignee(
            assignee_name_or_email
        )
        now = datetime.now(UTC)
        task_id = uuid4()
        task = CRMActivity(
            activity_id=task_id,
            organisation_id=self.organisation_id,
            integration_id=None,
            external_id=str(task_id),
            activity_type=CRMActivityType.TASK.value,
            subject=subject.strip(),
            body=body.strip() if body else None,
            status=CRMActivityStatus.NOT_STARTED.value,
            priority=priority,
            due_date=due_date,
            deal_id=deal.deal_id,
            deal_external_id=deal.external_id,
            related_object_type=CRMActivityObjectType.DEAL.value,
            related_external_id=deal.external_id,
            related_local_id=deal.deal_id,
            related_name=deal.name,
            owner_external_id=str(owner_user_id) if owner_user_id else None,
            owner_name=owner_name,
            owner_email=owner_email,
            source=CRMActivitySource.AI_MEETING.value,
            created_by_user_id=None,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                "is_user_created": False,
                "created_via": "deal_task_agent",
                "source_event_id": str(source_event_id) if source_event_id else None,
                "source_association_id": str(source_association_id)
                if source_association_id
                else None,
                "source_summary_job_id": str(source_summary_job_id)
                if source_summary_job_id
                else None,
                "idempotency_key": idempotency_key,
                "assignee_resolution": resolution,
            },
        )
        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)
        return TaskWriteResult(task=task, created=True)

    async def complete_task(self, task_id: UUID, *, completion_reason: str) -> CRMActivity:
        task = await self.session.get(CRMActivity, task_id)
        if not task or task.organisation_id != self.organisation_id or task.is_deleted:
            raise DealTaskAutomationError("Task not found", 404)
        if task.integration_id is not None:
            raise DealTaskAutomationError("External CRM tasks are read-only", 409)
        now = datetime.now(UTC)
        task.status = CRMActivityStatus.COMPLETED.value
        task.completed_date = now
        task.source_modified_at = now
        task.synced_at = now
        task.last_seen_at = now
        properties = task.properties or {}
        properties["completed_via"] = "deal_task_agent"
        properties["completion_reason"] = completion_reason
        task.properties = properties
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def _load_deal(self, deal_id: UUID) -> CRMDeal:
        deal = await self.session.get(CRMDeal, deal_id)
        if not deal or deal.organisation_id != self.organisation_id or deal.is_deleted:
            raise DealTaskAutomationError("Deal not found", 404)
        return deal

    async def _find_existing(self, idempotency_key: str) -> CRMActivity | None:
        c = CRMActivity.__table__.c
        stmt = select(CRMActivity).where(
            c.organisation_id == self.organisation_id,
            c.properties["idempotency_key"].astext == idempotency_key,
            or_(c.is_deleted.is_(False), c.is_deleted.is_(None)),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _resolve_assignee(
        self, value: str | None
    ) -> tuple[UUID | None, str | None, str | None, dict[str, Any]]:
        if not value or not value.strip():
            return None, None, None, {"status": "not_provided"}
        lookup = value.strip()
        async with master_async_session_scope() as master_session:
            user = await self._resolve_user(master_session, lookup)
        if not user:
            return None, None, None, {"status": "unresolved", "input": lookup}
        display = " ".join(
            part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part
        ) or str(user.email)
        return user.user_id, display, str(user.email), {"status": "resolved", "input": lookup}

    async def _resolve_user(self, master_session: AsyncSession, lookup: str) -> User | None:
        if "@" in lookup:
            result = await master_session.execute(
                select(User).where(
                    col(User.organisation_id) == self.organisation_id,
                    func.lower(col(User.email)) == lookup.lower(),
                )
            )
            return result.scalar_one_or_none()
        normalized = " ".join(lookup.lower().split())
        result = await master_session.execute(
            select(User).where(col(User.organisation_id) == self.organisation_id)
        )
        matches = [
            user
            for user in result.scalars().all()
            if " ".join(f"{user.first_name} {user.last_name}".lower().split()) == normalized
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    @staticmethod
    def _normalize_subject(subject: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", subject.lower()).strip()

    def _build_idempotency_key(
        self,
        *,
        deal_id: UUID,
        source_event_id: UUID | None,
        source_association_id: UUID | None,
        normalized_subject: str,
    ) -> str:
        source_id = source_association_id or source_event_id or "deal"
        return f"{self.organisation_id}:{deal_id}:{source_id}:{normalized_subject}"
```

- [ ] **Step 4: Run service tests and fix fixture-level issues only**

Run:

```bash
uv run pytest tests/service/crm/internal/test_deal_task_automation_service.py -v
```

Expected: service tests pass after adapting imports/fixtures to the repo's existing test helpers.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/service/crm/internal/deal_task_automation_service.py tests/service/crm/internal/test_deal_task_automation_service.py
git commit -m "feat: add automated deal task write service"
```

---

### Task 2: Add Direct-Write Task Tools

**Files:**
- Create: `src/agents/tools/tasks/list_deal_tasks.py`
- Create: `src/agents/tools/tasks/create_deal_task.py`
- Create: `src/agents/tools/tasks/complete_deal_task.py`
- Modify: `src/agents/tools/tasks/__init__.py`
- Test: `tests/agents/tools/tasks/test_deal_task_tools.py`

- [ ] **Step 1: Write failing tool tests**

Create tests that build an `AgentContext(organisation_id=org_id, deal_id=deal_id)` and call each
factory. Assertions:

```python
async def test_create_deal_task_tool_uses_context_deal_id(context):
    tool = create_create_deal_task(context, lambda: None)
    result = await tool(title="Send proposal", assignee="seller@example.com")
    assert result["status"] in {"created", "duplicate"}
    assert result["deal_id"] == str(context.deal_id)
```

```python
async def test_complete_deal_task_tool_requires_task_id(context):
    tool = create_complete_deal_task(context, lambda: None)
    result = await tool(task_id="", reason="Done in the meeting")
    assert result["status"] == "error"
```

- [ ] **Step 2: Run tool tests and verify they fail**

```bash
uv run pytest tests/agents/tools/tasks/test_deal_task_tools.py -v
```

Expected: imports fail because tool modules do not exist.

- [ ] **Step 3: Implement `list_deal_tasks` tool**

Create `src/agents/tools/tasks/list_deal_tasks.py`:

```python
from __future__ import annotations

from typing import Any, Callable

from src.agents.common.capabilities import AgentCapabilities
from src.agents.context import AgentContext
from src.infra.database.sessions import acquire_org_async_session
from src.service.crm.internal.deal_task_automation_service import DealTaskAutomationService


def create_list_deal_tasks(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    async def list_deal_tasks_tool() -> dict[str, Any]:
        if not context.organisation_id or not context.deal_id:
            return {"status": "error", "message": "Deal context is required."}
        async for session in acquire_org_async_session(context.organisation_id):
            service = DealTaskAutomationService(session, context.organisation_id)
            tasks = await service.list_deal_tasks(context.deal_id)
            return {
                "status": "ok",
                "tasks": [
                    {
                        "task_id": str(task.activity_id),
                        "subject": task.subject,
                        "body": task.body,
                        "status": task.status,
                        "priority": task.priority,
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                        "owner_name": task.owner_name,
                        "owner_email": task.owner_email,
                        "source": task.source,
                        "read_only": task.integration_id is not None,
                    }
                    for task in tasks
                ],
            }
        return {"status": "error", "message": "Could not acquire organisation session."}

    return list_deal_tasks_tool
```

- [ ] **Step 4: Implement `create_deal_task` tool**

Create `src/agents/tools/tasks/create_deal_task.py` with `create_create_deal_task(...)`. It should:

- require `title`,
- use `context.deal_id`,
- pass optional assignee, body, source IDs from context metadata,
- return `created` or `duplicate`.

- [ ] **Step 5: Implement `complete_deal_task` tool**

Create `src/agents/tools/tasks/complete_deal_task.py` with `create_complete_deal_task(...)`. It should:

- validate `task_id` is a UUID,
- require a non-empty reason,
- call `DealTaskAutomationService.complete_task`,
- return task id and status.

- [ ] **Step 6: Export tools**

Modify `src/agents/tools/tasks/__init__.py`:

```python
from src.agents.tools.tasks.complete_deal_task import create_complete_deal_task
from src.agents.tools.tasks.create_deal_task import create_create_deal_task
from src.agents.tools.tasks.create_task import create_create_task
from src.agents.tools.tasks.list_deal_tasks import create_list_deal_tasks

__all__ = [
    "create_complete_deal_task",
    "create_create_deal_task",
    "create_create_task",
    "create_list_deal_tasks",
]
```

- [ ] **Step 7: Run tool tests**

```bash
uv run pytest tests/agents/tools/tasks/test_deal_task_tools.py -v
```

Expected: tests pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/agents/tools/tasks tests/agents/tools/tasks/test_deal_task_tools.py
git commit -m "feat: add deal task agent tools"
```

---

### Task 3: Register Agent Tools

**Files:**
- Modify: `src/agents/tools/registry.py`
- Test: `tests/agents/tools/test_registry_deal_task_agent.py`

- [ ] **Step 1: Write registry tests**

Create `tests/agents/tools/test_registry_deal_task_agent.py`:

```python
def test_deal_task_agent_tools_are_registered():
    names = AGENT_TOOLS["DealTaskAgent"]
    assert ToolName.LIST_DEAL_TASKS in names
    assert ToolName.CREATE_DEAL_TASK in names
    assert ToolName.COMPLETE_DEAL_TASK in names
    assert ToolName.CREATE_TASK not in names
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run pytest tests/agents/tools/test_registry_deal_task_agent.py -v
```

Expected: `ToolName.LIST_DEAL_TASKS` does not exist.

- [ ] **Step 3: Add tool names and mapping**

Modify `ToolName`:

```python
LIST_DEAL_TASKS = "list_deal_tasks"
CREATE_DEAL_TASK = "create_deal_task"
COMPLETE_DEAL_TASK = "complete_deal_task"
```

Add:

```python
"DealTaskAgent": {
    ToolName.GET_DEAL_MEMORY,
    ToolName.GET_MEETING_SUMMARY,
    ToolName.LIST_DEAL_TASKS,
    ToolName.CREATE_DEAL_TASK,
    ToolName.COMPLETE_DEAL_TASK,
}
```

Register factories in `ToolRegistry.__init__`:

```python
from src.agents.tools.tasks import (
    create_complete_deal_task,
    create_create_deal_task,
    create_list_deal_tasks,
)

self._tool_factories[ToolName.LIST_DEAL_TASKS] = lambda: create_list_deal_tasks(
    self.context, self.capabilities_getter
)
self._tool_factories[ToolName.CREATE_DEAL_TASK] = lambda: create_create_deal_task(
    self.context, self.capabilities_getter
)
self._tool_factories[ToolName.COMPLETE_DEAL_TASK] = lambda: create_complete_deal_task(
    self.context, self.capabilities_getter
)
```

- [ ] **Step 4: Run registry tests**

```bash
uv run pytest tests/agents/tools/test_registry_deal_task_agent.py -v
```

Expected: pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/agents/tools/registry.py tests/agents/tools/test_registry_deal_task_agent.py
git commit -m "feat: register deal task agent tools"
```

---

### Task 4: Add DealTaskAgent Workflow

**Files:**
- Create: `src/agents/workflows/deal_tasks/models.py`
- Create: `src/agents/workflows/deal_tasks/prompts.py`
- Create: `src/agents/workflows/deal_tasks/agent.py`
- Create: `src/agents/workflows/deal_tasks/__init__.py`
- Modify: `src/infra/config/agent_config.py`
- Test: `tests/agents/workflows/deal_tasks/test_deal_task_agent.py`

- [ ] **Step 1: Write failing agent construction test**

```python
def test_deal_task_agent_builds_with_scoped_context(org_id, deal_id):
    agent = DealTaskAgent(organisation_id=org_id, deal_id=deal_id, org_name="Acme")
    assert agent.context.organisation_id == org_id
    assert agent.context.deal_id == deal_id
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run pytest tests/agents/workflows/deal_tasks/test_deal_task_agent.py -v
```

Expected: import failure.

- [ ] **Step 3: Add models**

`src/agents/workflows/deal_tasks/models.py`:

```python
from __future__ import annotations

from pydantic import BaseModel, Field


class DealTaskItemResult(BaseModel):
    task_id: str | None = None
    title: str
    action: str
    status: str
    reason: str | None = None


class DealTaskSyncResult(BaseModel):
    created_tasks: list[DealTaskItemResult] = Field(default_factory=list)
    completed_tasks: list[DealTaskItemResult] = Field(default_factory=list)
    skipped_items: list[DealTaskItemResult] = Field(default_factory=list)
    failed_items: list[DealTaskItemResult] = Field(default_factory=list)
    created_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
```

- [ ] **Step 4: Add prompt**

`src/agents/workflows/deal_tasks/prompts.py` should require:

```python
DEAL_TASK_SYSTEM_PROMPT = """
You create and complete CRM tasks for one deal after meeting analysis.

Rules:
- First call list_deal_tasks.
- Use meeting summary and deal memory as evidence.
- Create concrete follow-up tasks even if no owner or assignee is known.
- Do not create vague tasks such as "follow up" without the actual work.
- Complete an existing task only when the summary explicitly states it was done.
- Never complete read-only CRM-synced tasks.
- Use create_deal_task and complete_deal_task for writes.
- Return structured counts after tool work is finished.
"""
```

- [ ] **Step 5: Add agent**

Follow the pattern in `src/agents/workflows/risk_blocker_extraction/agent.py`, using
`agent_config.get_config("deal_task_agent")`, `output_schema=DealTaskSyncResult`, and
`get_tools_for_agent("DealTaskAgent", self.context, lambda: None)`.

- [ ] **Step 6: Add agent config**

Modify `src/infra/config/agent_config.py`:

```python
DEAL_TASK_MODEL: str = os.getenv("AGENT_DEAL_TASK_MODEL", "gpt-5.2")
DEAL_TASK_DEBUG: bool = os.getenv("AGENT_DEAL_TASK_DEBUG", "false").lower() == "true"
DEAL_TASK_TIMEOUT: int = int(os.getenv("AGENT_DEAL_TASK_TIMEOUT", "600"))
```

Add to `_CONFIG_MAP`:

```python
"deal_task_agent": ("DEAL_TASK_MODEL", "DEAL_TASK_DEBUG", "DEAL_TASK_TIMEOUT"),
```

- [ ] **Step 7: Run agent tests**

```bash
uv run pytest tests/agents/workflows/deal_tasks/test_deal_task_agent.py -v
```

Expected: pass without calling Azure OpenAI.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/agents/workflows/deal_tasks src/infra/config/agent_config.py tests/agents/workflows/deal_tasks/test_deal_task_agent.py
git commit -m "feat: add deal task agent workflow"
```

---

### Task 5: Add Celery Task And Job Type

**Files:**
- Modify: `src/domain/models/organisation_tables/job_models.py`
- Create: `src/workers/deal_worker/tasks/deal_insight_tasks/deal_task_sync_task.py`
- Modify: `src/workers/deal_worker/tasks/deal_insight_tasks/__init__.py`
- Modify: `src/workers/deal_worker/config.py`
- Test: `tests/workers/deal_worker/test_deal_task_sync_task.py`

- [ ] **Step 1: Write failing tests**

Assertions:

```python
def test_deal_task_sync_job_type_exists():
    assert DealJobType.DEAL_TASK_SYNC.value == "deal_task_sync"
```

```python
def test_deal_worker_routes_deal_task_sync_to_deal_jobs():
    config = DealWorkerConfig()
    route = config.task_routes["deal_tasks.sync_from_summary"]
    assert route["queue"] == "deal_jobs"
```

- [ ] **Step 2: Run and verify failure**

```bash
uv run pytest tests/workers/deal_worker/test_deal_task_sync_task.py -v
```

Expected: enum or route missing.

- [ ] **Step 3: Add job type**

In `DealJobType`:

```python
DEAL_TASK_SYNC = "deal_task_sync"
```

- [ ] **Step 4: Add Celery task**

Create `deal_task_sync_task.py` with the same fresh-session pattern as risk and stakeholder tasks:

- load job and mark `IN_PROGRESS`,
- instantiate `DealTaskAgent`,
- call `sync_from_summary(...)`,
- save result and token usage,
- mark `COMPLETED`,
- retry on unexpected exceptions.

- [ ] **Step 5: Export and route task**

In `deal_insight_tasks/__init__.py`, import/export `sync_deal_tasks_from_summary`.

In `DealWorkerConfig.deal_jobs_queue_tasks`, add:

```python
"deal_tasks.sync_from_summary",
```

- [ ] **Step 6: Run worker tests**

```bash
uv run pytest tests/workers/deal_worker/test_deal_task_sync_task.py -v
```

Expected: pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add src/domain/models/organisation_tables/job_models.py src/workers/deal_worker/tasks/deal_insight_tasks src/workers/deal_worker/config.py tests/workers/deal_worker/test_deal_task_sync_task.py
git commit -m "feat: add deal task sync celery job"
```

---

### Task 6: Wire Task Sync After The Insight Pipeline

**Files:**
- Modify: `src/workers/deal_worker/utils/pipeline_dispatch.py`
- Test: `tests/workers/deal_worker/test_pipeline_dispatch_deal_tasks.py`

- [ ] **Step 1: Write failing pipeline tests**

Test that `dispatch_deal_insight_pipeline_async` creates a `DEAL_TASK_SYNC` job and includes the
signature `deal_tasks.sync_from_summary` after the existing risk/stakeholder group.

- [ ] **Step 2: Run and verify failure**

```bash
uv run pytest tests/workers/deal_worker/test_pipeline_dispatch_deal_tasks.py -v
```

Expected: no task-sync job is created.

- [ ] **Step 3: Create task-sync job with risk/stakeholder jobs**

Update `_create_pipeline_jobs` and `_create_pipeline_jobs_async` to return:

```python
tuple[str, str, str]
```

where the third value is `deal_task_job_id`.

Create the job:

```python
task_job = DealJobs(
    organisation_id=org_uuid,
    user_id=AUTOMATED_PIPELINE_USER_ID,
    job_type=DealJobType.DEAL_TASK_SYNC,
    deal_id=deal_uuid,
    status=JobStatus.PENDING,
    job_metadata={"source": "automated_pipeline", "chained_from": "deal_insights"},
)
```

- [ ] **Step 4: Append final task sync signature**

Update `_build_deal_insight_canvas(...)` signature to accept `task_job_id`.

Add:

```python
deal_task_sig = celery_app.signature(
    "deal_tasks.sync_from_summary",
    kwargs={
        "organisation_id": organisation_id,
        "deal_id": deal_id,
        "job_id": task_job_id,
    },
    queue="deal_jobs",
    immutable=True,
    headers=correlation_headers(),
)
```

Return:

```python
return chain(deal_memory_sig, group(risk_blocker_sig, stakeholder_sig), deal_task_sig)
```

- [ ] **Step 5: Run pipeline tests**

```bash
uv run pytest tests/workers/deal_worker/test_pipeline_dispatch_deal_tasks.py -v
```

Expected: pass.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/workers/deal_worker/utils/pipeline_dispatch.py tests/workers/deal_worker/test_pipeline_dispatch_deal_tasks.py
git commit -m "feat: run deal task sync after insights"
```

---

### Task 7: End-To-End Regression Tests

**Files:**
- Test: `tests/workers/deal_worker/test_deal_task_agent_pipeline_integration.py`

- [ ] **Step 1: Add integration tests with mocked LLM/tool calls**

Cover:

- meeting summary pipeline creates final task-sync job,
- task sync creates a task with no assignee,
- task sync creates a task with resolved assignee,
- repeated task sync does not duplicate task,
- task sync completes an existing internal task,
- task sync skips CRM-synced task completion.

- [ ] **Step 2: Run focused suites**

```bash
uv run pytest tests/service/crm/internal/test_deal_task_automation_service.py tests/agents/tools/tasks/test_deal_task_tools.py tests/agents/workflows/deal_tasks/test_deal_task_agent.py tests/workers/deal_worker/test_deal_task_sync_task.py tests/workers/deal_worker/test_pipeline_dispatch_deal_tasks.py tests/workers/deal_worker/test_deal_task_agent_pipeline_integration.py -v
```

Expected: all focused tests pass.

- [ ] **Step 3: Run lint for touched backend files**

```bash
uv run ruff check src/agents/tools/tasks src/agents/workflows/deal_tasks src/service/crm/internal/deal_task_automation_service.py src/workers/deal_worker/tasks/deal_insight_tasks src/workers/deal_worker/utils/pipeline_dispatch.py tests/service/crm/internal tests/agents tests/workers/deal_worker
```

Expected: no Ruff errors.

- [ ] **Step 4: Commit Task 7**

```bash
git add tests src
git commit -m "test: cover deal task agent pipeline"
```

---

### Task 8: Documentation And Final Verification

**Files:**
- Modify: `docs/API Documentation/deals/deal-tasks-api.md` only if response fields or source semantics need documentation.
- Create or modify: `docs/Technical Documentation/deal/deal-task-agent.md`

- [ ] **Step 1: Add technical documentation**

Document:

- pipeline placement,
- agent tools,
- idempotency key,
- completion rules,
- notification deferral,
- how to re-run focused tests.

- [ ] **Step 2: Run final verification**

```bash
uv run pytest tests/service/crm/internal/test_deal_task_automation_service.py tests/agents/tools/tasks/test_deal_task_tools.py tests/agents/workflows/deal_tasks/test_deal_task_agent.py tests/workers/deal_worker/test_deal_task_sync_task.py tests/workers/deal_worker/test_pipeline_dispatch_deal_tasks.py tests/workers/deal_worker/test_deal_task_agent_pipeline_integration.py -v
uv run ruff check src/ tests/
uv run mypy src/
git diff --check
```

Expected:

- focused tests pass,
- Ruff passes,
- MyPy passes or only reports pre-existing unrelated errors that are captured in the handoff,
- `git diff --check` passes.

- [ ] **Step 3: Commit docs**

```bash
git add docs
git commit -m "docs: document deal task agent"
```

---

## Self-Review

- Spec coverage: the plan covers direct-write tools, post-insight pipeline placement, missing-assignee fallback, same-deal completion, idempotency, job tracking, and notification deferral.
- Placeholder scan: no task contains TBD/TODO placeholders.
- Type consistency: service, tool, agent, and worker names are consistent across tasks.
- Scope check: Teams/Slack/WhatsApp notification delivery is intentionally deferred because recipient rules are not finalized.
