"""Regression tests for deal-scoped task automation tools.

These tests protect the safety contract around immutable context, task identity,
and read-only signals before task writes happen. They use a lightweight fake
service to verify tool wiring independently of database backends.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.access import AgentCRMAccessError
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
    assigned_to_user_id: UUID | None = None
    assigned_to_email: str | None = None
    task_metadata: dict = field(default_factory=dict)
    source: str | None = "AI_MEETING"
    integration_id: UUID | None = None


class _FakeService:
    """Fake DealTaskAutomationService adapter used by all tool regressions."""

    instances: list[_FakeService] = []
    assignee_public_user_id: UUID | None = None
    reminder_at: datetime | None = None
    listed_task_count = 2
    listed_tasks: list[_FakeTask] | None = None

    def __init__(self, session: object, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.created_calls: list[dict] = []
        self.completed_calls: list[dict] = []
        _FakeService.instances.append(self)

    async def list_deal_tasks(
        self, deal_id: UUID, *, include_completed: bool = False
    ) -> list[_FakeTask]:
        """Return two sample tasks for read-only contract assertions."""
        self.listed_deal_id = deal_id
        self.include_completed = include_completed
        if self.listed_tasks is not None:
            return self.listed_tasks
        return [
            _FakeTask(
                subject=f"Task {index}",
                integration_id=uuid4() if index == 1 else None,
            )
            for index in range(self.listed_task_count)
        ]

    async def create_task(self, **kwargs):
        """Capture create inputs and return a successful TaskWriteResult-like object."""
        self.created_calls.append(kwargs)
        task = _FakeTask(activity_id=uuid4(), subject=kwargs["subject"])
        return type(
            "TaskWriteResult",
            (),
            {
                "task": task,
                "created": True,
                "reason": None,
                "assignee_public_user_id": self.assignee_public_user_id,
                "assignee_timezone": "UTC",
                "reminder_at": self.reminder_at,
            },
        )()

    async def complete_task(self, task_id: UUID, **kwargs):
        """Capture complete inputs and return a completed task payload."""
        self.completed_calls.append({"task_id": task_id, **kwargs})
        return _FakeTask(activity_id=task_id, status="COMPLETED")


@pytest.fixture(autouse=True)
def reset_fake_service() -> None:
    """Clear fake service instances so each test starts from a clean registry."""
    _FakeService.instances.clear()
    _FakeService.assignee_public_user_id = None
    _FakeService.reminder_at = None
    _FakeService.listed_task_count = 2
    _FakeService.listed_tasks = None


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
            "deal_task_list_checked": True,
            "deal_task_list_truncated": False,
        },
    )

    class FakeSession:
        """Session double that records the explicit tool-owned commit."""

        committed = False

        async def commit(self) -> None:
            """Record that the agent tool committed its owned transaction."""
            self.committed = True

    @asynccontextmanager
    async def acquire_session():
        """Return a placeholder async session object for context injection tests."""
        yield FakeSession()

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
    assert service.created_calls[0]["fallback_assignee_name_or_emails"] is None
    assert service.created_calls[0]["source_event_id"] == UUID(context.metadata["source_event_id"])


@pytest.mark.asyncio
async def test_create_deal_task_links_one_nonrecurring_reminder(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved task owner gets one linked reminder through channel fallback."""
    owner_id = uuid4()
    trigger_time = datetime.now(UTC) + timedelta(days=1)
    _FakeService.assignee_public_user_id = owner_id
    _FakeService.reminder_at = trigger_time
    reminder_calls: list[dict] = []

    class FakeReminderService:
        """Capture the one-reminder contract without provider or database calls."""

        def __init__(self, _session, organisation_id, actor_user_id):
            assert organisation_id == context.organisation_id
            assert actor_user_id == owner_id

        async def get_active_reminder(self, _task_id):
            """Model a task with no existing linked reminder."""
            return None

        async def create_task_reminder(self, task, cfg):
            """Capture the reminder payload returned to the agent tool."""
            reminder_calls.append({"task": task, "cfg": cfg})
            return SimpleNamespace(
                reminder_id=uuid4(),
                delivery_channel="teams",
            )

    monkeypatch.setattr(
        "src.agents.tools.tasks.create_deal_task.DealTaskAutomationService",
        _FakeService,
    )
    monkeypatch.setattr(
        "src.agents.tools.tasks.create_deal_task.TaskReminderService",
        FakeReminderService,
    )

    result = await create_create_deal_task(context, lambda: None)(
        title="Send proposal",
        assignee="seller@example.com",
        fallback_assignees=["deal-owner@example.com", "organiser@example.com"],
    )

    assert result["status"] == "created"
    assert result["reminder_status"] == "created"
    assert result["reminder_channel"] == "teams"
    assert reminder_calls[0]["cfg"] == {
        "trigger_time": trigger_time,
        "recurrence": "NONE",
        "timezone": "UTC",
    }
    assert _FakeService.instances[-1].created_calls[0]["fallback_assignee_name_or_emails"] == [
        "deal-owner@example.com",
        "organiser@example.com",
    ]


@pytest.mark.asyncio
async def test_create_deal_task_caps_one_meeting_at_ten_tasks(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compromised transcript cannot make more than ten create calls in one run."""
    monkeypatch.setattr(
        "src.agents.tools.tasks.create_deal_task.DealTaskAutomationService",
        _FakeService,
    )
    tool = create_create_deal_task(context, lambda: None)

    results = [await tool(title=f"Task {index}") for index in range(11)]

    assert all(result["status"] == "created" for result in results[:10])
    assert results[10]["status"] == "error"
    assert "Maximum 10" in results[10]["message"]


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
async def test_create_deal_task_requires_full_read_before_write(context: AgentContext) -> None:
    """The write tool must fail closed when the current task list was not inspected."""
    context.metadata.pop("deal_task_list_checked")

    result = await create_create_deal_task(context, lambda: None)(title="Send proposal")

    assert result["status"] == "error"
    assert "ListDealTasks" in result["message"]


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


@pytest.mark.asyncio
async def test_list_deal_tasks_accepts_visible_explicit_deal_id(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human-facing agent may list a searched deal without page deal context."""
    explicit_deal_id = uuid4()
    context.deal_id = None
    context.user_id = uuid4()
    access_check = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks.DealTaskAutomationService",
        _FakeService,
    )
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks._require_human_deal_task_access",
        access_check,
        raising=False,
    )

    result = await create_list_deal_tasks(context, lambda: None)(deal_id=str(explicit_deal_id))

    assert result["status"] == "ok"
    assert result["deal_id"] == str(explicit_deal_id)
    assert _FakeService.instances[-1].listed_deal_id == explicit_deal_id
    access_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_human_deal_task_read_uses_context_relationship_guard(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Human reads use the task-specific admin-or-relationship policy."""
    import src.agents.tools.tasks.list_deal_tasks as module

    context.user_id = uuid4()
    relationship_check = AsyncMock()
    broad_access_check = AsyncMock()
    monkeypatch.setattr(
        module, "_require_human_deal_task_access", relationship_check, raising=False
    )
    monkeypatch.setattr(module, "require_agent_deal_access", broad_access_check)
    monkeypatch.setattr(module, "DealTaskAutomationService", _FakeService)

    result = await create_list_deal_tasks(context, lambda: None)()

    assert result["status"] == "ok"
    relationship_check.assert_awaited_once()
    broad_access_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_deal_task_access_uses_context_user_as_member(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The immutable context user must retain member relationship checks."""
    import src.agents.tools.tasks.list_deal_tasks as module
    from src.domain.models.master_tables.user_models import UserAppRole
    from src.service.crm.access.record_access_policy import CRMAccessActor

    context.user_id = uuid4()
    context.user_email = "member@example.com"
    member = CRMAccessActor(context.user_id, UserAppRole.MEMBER, context.user_email)
    captured: dict[str, object] = {}

    class FakeDealAccessPolicy:
        """Capture the role-neutral actor passed to the canonical deal policy."""

        def __init__(self, session, organisation_id, actor):
            captured["session"] = session
            captured["organisation_id"] = organisation_id
            captured["actor"] = actor

        async def require_deal(self, deal_id, level):
            """Return an owner decision while recording the exact policy inputs."""
            captured["deal_id"] = deal_id
            captured["level"] = level
            return object(), SimpleNamespace(is_owner=True, is_participant=False)

    role_resolver = AsyncMock(return_value=member)
    monkeypatch.setattr(module, "resolve_agent_access_actor", role_resolver)
    monkeypatch.setattr(module, "DealAccessPolicy", FakeDealAccessPolicy, raising=False)

    actor = await module._require_human_deal_task_access(context, object(), context.deal_id)

    assert captured["organisation_id"] == context.organisation_id
    assert captured["deal_id"] == context.deal_id
    assert captured["level"] == "view"
    assert actor.user_id == context.user_id
    assert actor.normalized_email == context.user_email
    assert actor.is_admin is False
    role_resolver.assert_awaited_once_with(context)


@pytest.mark.asyncio
async def test_human_deal_task_access_rejects_creator_without_relationship(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a deal alone must not expose every task when the user is not owner or participant."""
    import src.agents.tools.tasks.list_deal_tasks as module
    from src.domain.models.master_tables.user_models import UserAppRole
    from src.service.crm.access.record_access_policy import CRMAccessActor

    context.user_id = uuid4()
    member = CRMAccessActor(context.user_id, UserAppRole.MEMBER, context.user_email)

    class CreatorOnlyDealAccessPolicy:
        """Return view access without either relationship accepted by the task tool."""

        def __init__(self, session, organisation_id, actor):
            pass

        async def require_deal(self, deal_id, level):
            """Model creator-only visibility without an accepted task relationship."""
            return object(), SimpleNamespace(is_owner=False, is_participant=False)

    monkeypatch.setattr(module, "resolve_agent_access_actor", AsyncMock(return_value=member))
    monkeypatch.setattr(module, "DealAccessPolicy", CreatorOnlyDealAccessPolicy, raising=False)

    with pytest.raises(AgentCRMAccessError, match="Deal not found"):
        await module._require_human_deal_task_access(context, object(), context.deal_id)


@pytest.mark.asyncio
async def test_human_deal_task_access_allows_admin_without_deal_relationship(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An organisation admin may read any deal's tasks through GeneralAgent."""
    import src.agents.tools.tasks.list_deal_tasks as module
    from src.domain.models.master_tables.user_models import UserAppRole
    from src.service.crm.access.record_access_policy import CRMAccessActor

    context.user_id = uuid4()
    admin = CRMAccessActor(context.user_id, UserAppRole.ADMIN, "admin@example.com")

    class AdminDealAccessPolicy:
        """Return an authorized admin decision without a deal relationship."""

        def __init__(self, session, organisation_id, actor):
            self.actor = actor

        async def require_deal(self, deal_id, level):
            """Model the shared policy's organization-admin access."""
            return object(), SimpleNamespace(is_owner=False, is_participant=False)

    role_resolver = AsyncMock(return_value=admin)
    monkeypatch.setattr(module, "resolve_agent_access_actor", role_resolver, raising=False)
    monkeypatch.setattr(module, "DealAccessPolicy", AdminDealAccessPolicy)

    actor = await module._require_human_deal_task_access(context, object(), context.deal_id)

    assert actor is admin
    role_resolver.assert_awaited_once_with(context)


@pytest.mark.asyncio
async def test_human_chat_lists_every_deal_task_without_assignee_filtering(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deal-wide chat reads include every assignee through the dedicated deal tool."""
    context.user_id = uuid4()
    context.user_email = "admin@example.com"
    mine = _FakeTask(
        subject="My task",
        assigned_to_user_id=context.user_id,
        assigned_to_email=context.user_email,
    )
    another_users = _FakeTask(
        subject="Mayank's task",
        assigned_to_user_id=uuid4(),
        assigned_to_email="mayank@example.com",
    )
    _FakeService.listed_tasks = [mine, another_users]
    access_check = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks.DealTaskAutomationService",
        _FakeService,
    )
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks._require_human_deal_task_access",
        access_check,
    )

    tool = create_list_deal_tasks(context, lambda: None)
    result = await tool()

    assert [task["subject"] for task in result["tasks"]] == [
        "My task",
        "Mayank's task",
    ]
    assert _FakeService.instances[0].include_completed is True
    assert context.metadata["deal_task_list_checked"] is True
    access_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_human_chat_returns_all_deal_tasks_beyond_reconciliation_cap(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Human deal reads return the full authorized deal instead of automation's cap."""
    context.user_id = uuid4()
    _FakeService.listed_task_count = 201
    access_check = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks.DealTaskAutomationService",
        _FakeService,
    )
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks._require_human_deal_task_access",
        access_check,
    )

    result = await create_list_deal_tasks(context, lambda: None)()

    assert result["task_count"] == 201
    assert result["truncated"] is False
    assert len(result["tasks"]) == 201
    access_check.assert_awaited_once()


@pytest.mark.asyncio
async def test_listing_another_deal_does_not_unlock_context_deal_writes(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading deal B cannot satisfy deal A's read-before-write guard."""
    explicit_deal_id = uuid4()
    context.user_id = uuid4()
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks.DealTaskAutomationService",
        _FakeService,
    )
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks._require_human_deal_task_access",
        AsyncMock(),
    )

    await create_list_deal_tasks(context, lambda: None)(deal_id=str(explicit_deal_id))
    result = await create_create_deal_task(context, lambda: None)(title="Send proposal")

    assert result["status"] == "error"
    assert "ListDealTasks" in result["message"]


@pytest.mark.asyncio
async def test_list_deal_tasks_fails_writes_closed_when_result_is_truncated(
    context: AgentContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deals with over 200 open tasks are flagged so writes cannot add more noise."""
    _FakeService.listed_task_count = 201
    monkeypatch.setattr(
        "src.agents.tools.tasks.list_deal_tasks.DealTaskAutomationService",
        _FakeService,
    )

    result = await create_list_deal_tasks(context, lambda: None)()

    assert result["truncated"] is True
    assert len(result["tasks"]) == 200
    assert context.metadata["deal_task_list_truncated"] is True
