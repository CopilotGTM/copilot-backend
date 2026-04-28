"""Regression tests for deal-scoped task automation tools.

These tests protect the safety contract around immutable context, task identity,
and read-only signals before task writes happen. They use a lightweight fake
service to verify tool wiring independently of database backends.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.tasks.complete_deal_task import create_complete_deal_task
from src.agents.tools.tasks.create_deal_task import create_create_deal_task
from src.agents.tools.tasks.list_deal_tasks import create_list_deal_tasks


@dataclass
class _FakeTask:
    """Minimal in-memory task shape for tool-level assertions."""

    activity_id: UUID = field(default_factory=uuid4)
    subject: str = "Send proposal"
    body: str | None = "Send the revised proposal."
    status: str = "NOT_STARTED"
    priority: str = "HIGH"
    due_date: datetime | None = None
    owner_name: str | None = "Ravi Kumar"
    owner_email: str | None = "ravi@example.com"
    source: str | None = "AI_MEETING"
    integration_id: UUID | None = None


class _FakeService:
    """Fake DealTaskAutomationService adapter used by all tool regressions."""

    instances: list[_FakeService] = []

    def __init__(self, session: object, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.created_calls: list[dict] = []
        self.completed_calls: list[dict] = []
        _FakeService.instances.append(self)

    async def list_deal_tasks(self, deal_id: UUID) -> list[_FakeTask]:
        """Return two sample tasks for read-only contract assertions."""
        self.listed_deal_id = deal_id
        return [
            _FakeTask(subject="Internal task"),
            _FakeTask(subject="CRM task", integration_id=uuid4()),
        ]

    async def create_task(self, **kwargs):
        """Capture create inputs and return a successful TaskWriteResult-like object."""
        self.created_calls.append(kwargs)
        task = _FakeTask(activity_id=uuid4(), subject=kwargs["subject"])
        return type("TaskWriteResult", (), {"task": task, "created": True, "reason": None})()

    async def complete_task(self, task_id: UUID, **kwargs):
        """Capture complete inputs and return a completed task payload."""
        self.completed_calls.append({"task_id": task_id, **kwargs})
        return _FakeTask(activity_id=task_id, status="COMPLETED")


@pytest.fixture(autouse=True)
def reset_fake_service() -> None:
    """Clear fake service instances so each test starts from a clean registry."""
    _FakeService.instances.clear()


@pytest.fixture
def context() -> AgentContext:
    """Return an AgentContext with immutable org/deal IDs and source IDs."""
    ctx = AgentContext(
        organisation_id=uuid4(),
        deal_id=uuid4(),
        metadata={
            "source_event_id": str(uuid4()),
            "source_association_id": str(uuid4()),
            "source_summary_job_id": str(uuid4()),
        },
    )

    @asynccontextmanager
    async def acquire_session():
        """Return a placeholder async session object for context injection tests."""
        yield object()

    ctx.acquire_session = acquire_session  # type: ignore[method-assign]
    return ctx


@pytest.mark.asyncio
async def test_create_deal_task_tool_uses_context_deal_id(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create tool must pass the immutable context deal_id through to service call."""
    # Regression lock: this is the tenant isolation surface for write-path tool calls.
    monkeypatch.setattr(
        "src.agents.tools.tasks.create_deal_task.DealTaskAutomationService",
        _FakeService,
    )
    tool = create_create_deal_task(context, lambda: None)

    result = await tool(title="Send proposal", assignee="seller@example.com")

    assert result["status"] == "created"
    assert result["deal_id"] == str(context.deal_id)
    service = _FakeService.instances[-1]
    assert service.created_calls[0]["deal_id"] == context.deal_id
    assert service.created_calls[0]["assignee_name_or_email"] == "seller@example.com"
    assert service.created_calls[0]["source_event_id"] == UUID(context.metadata["source_event_id"])


@pytest.mark.asyncio
async def test_create_deal_task_tool_requires_title(context: AgentContext) -> None:
    """Create tool must reject blank titles before any service call."""
    # Blank input here is a malformed model extraction; returning a clean error
    # protects downstream code from creating meaningless tasks.
    tool = create_create_deal_task(context, lambda: None)

    result = await tool(title="   ")

    assert result["status"] == "error"
    assert "title" in result["message"].lower()


@pytest.mark.asyncio
async def test_complete_deal_task_tool_requires_task_id(context: AgentContext) -> None:
    """Complete tool should fail early on missing/blank task identifiers."""
    tool = create_complete_deal_task(context, lambda: None)

    result = await tool(task_id="", reason="Done in the meeting")

    assert result["status"] == "error"
    assert "task_id" in result["message"]


@pytest.mark.asyncio
async def test_complete_deal_task_tool_uses_context_deal_id(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete tool must target the immutable context deal_id when calling service."""
    # This prevents a tool call from completing a task that belongs to another
    # active deal while still in the same agent session.
    monkeypatch.setattr(
        "src.agents.tools.tasks.complete_deal_task.DealTaskAutomationService",
        _FakeService,
    )
    tool = create_complete_deal_task(context, lambda: None)
    task_id = uuid4()

    result = await tool(task_id=str(task_id), reason="Done in the meeting")

    assert result["status"] == "completed"
    service = _FakeService.instances[-1]
    assert service.completed_calls[0]["task_id"] == task_id
    assert service.completed_calls[0]["deal_id"] == context.deal_id


@pytest.mark.asyncio
async def test_list_deal_tasks_marks_crm_synced_tasks_read_only(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List tool should expose read-only for CRM-synced rows."""
    # Automation depends on this contract to avoid sending completion attempts for
    # external tasks that must remain immutable from this flow.
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks.DealTaskAutomationService",
        _FakeService,
    )
    tool = create_list_deal_tasks(context, lambda: None)

    result = await tool()

    assert result["status"] == "ok"
    assert result["deal_id"] == str(context.deal_id)
    assert [task["read_only"] for task in result["tasks"]] == [False, True]
