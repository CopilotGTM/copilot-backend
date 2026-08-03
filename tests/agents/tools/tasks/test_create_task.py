"""Contracts for direct task creation, assignment, reminders, and channel receipts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.tools.tasks.create_task import create_create_task, create_task, create_task_form
from src.domain.models.master_tables.user_models import UserAppRole
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.orgnisation.organisation_policy_service import OrganisationPolicyDeniedError


class _FakeTasksWriteServiceError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass
class _FakeContext:
    organisation_id: UUID
    user_id: UUID | None
    channel: str
    metadata: dict = field(default_factory=dict)

    @asynccontextmanager
    async def acquire_session(self):
        """Yield the tenant session used by task services."""
        yield None  # Not used for preview-only tests


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _direct_context(*, channel: str = "teams", user_id: UUID | None = None) -> _FakeContext:
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=user_id or UUID("22222222-2222-2222-2222-222222222222"),
        channel=channel,
        metadata={},
    )
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    return context


def _created_task(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "activity_id": UUID("33333333-3333-3333-3333-333333333333"),
        "subject": "Follow up with Acme",
        "priority": "MEDIUM",
        "status": "NOT_STARTED",
        "due_date": None,
        "related_name": None,
        "owner_name": "Current User",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _stub_task_confirmation_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.service.bots.form_handlers.nonce.create_nonce",
        lambda _prefix: "nonce-for-test",
    )
    monkeypatch.setattr(
        "src.agents.tools.tasks.create_task.ensure_crm_write_scope",
        AsyncMock(),
        raising=False,
    )


# ---------------------------------------------------------------------------
# Validation tests (no external calls needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_requires_title() -> None:
    """A write cannot proceed without meaningful task content."""
    ctx = _FakeContext(organisation_id=uuid4(), user_id=uuid4(), channel="slack", metadata={})
    result = await create_task(context=ctx, title="")
    assert result["status"] == "error"
    assert "title" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_rejects_generic_titles() -> None:
    """Titles like "Task", "todo", "new task" are too vague — tool should reject them."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T1", "channel_id": "C1"},
    )
    for bad_title in ["Task", "todo", "New Task", "a task", "TO-DO"]:
        result = await create_task(context=ctx, title=bad_title)
        assert result["status"] == "error", f"Should reject generic title: {bad_title}"
        assert "generic" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_rejects_assignment_only_placeholder_titles() -> None:
    """Assignment wording alone is not persisted as a task title."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T1", "channel_id": "C1"},
    )

    for bad_title in [
        "Create a task",
        "Create task",
        "Create a task and assign it to me",
        "Assign it to me",
        "Test task",
    ]:
        result = await create_task(context=ctx, title=bad_title)
        assert result["status"] == "error", f"Should reject placeholder title: {bad_title}"
        assert "what should the task be about" in result["message"].lower()


def test_fix_date_typos_corrects_common_misspellings() -> None:
    """Month typos like 'mrach' should be corrected before parsing."""
    from src.agents.tools.tasks.create_task import _fix_date_typos

    assert _fix_date_typos("15th of mrach") == "15th of march"
    assert _fix_date_typos("Mrach 15") == "March 15"
    assert _fix_date_typos("febuary 20") == "february 20"
    assert _fix_date_typos("March 15") == "March 15"  # correct stays correct
    assert _fix_date_typos("tomorrow") == "tomorrow"  # no month, untouched


@pytest.mark.asyncio
async def test_create_task_requires_user_id() -> None:
    """Missing actor identity fails closed before task creation."""
    ctx = _FakeContext(organisation_id=uuid4(), user_id=None, channel="slack", metadata={})
    result = await create_task(context=ctx, title="Do something")
    assert result["status"] == "error"
    assert "user not identified" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_honours_disabled_organisation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _direct_context()
    denied = AsyncMock(
        side_effect=OrganisationPolicyDeniedError(
            "Internal CRM write scope 'tasks:create' is disabled for this organisation",
            code="crm_write_scope_disabled",
        )
    )
    monkeypatch.setattr(
        "src.agents.tools.tasks.create_task.ensure_crm_write_scope",
        denied,
        raising=False,
    )

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        result = await create_task(context=context, title="Follow up with Acme")

    denied.assert_awaited_once_with(context.organisation_id, "tasks:create")
    loader.assert_not_called()
    assert result == {
        "status": "error",
        "message": "Internal CRM write scope 'tasks:create' is disabled for this organisation",
    }


@pytest.mark.asyncio
async def test_create_task_rejects_invalid_priority() -> None:
    """Unknown priorities cannot cross the task service boundary."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T1", "channel_id": "C1"},
    )
    result = await create_task(context=ctx, title="Do something", priority="CRITICAL")
    assert result["status"] == "error"
    assert "priority" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_rejects_invalid_reminder_option() -> None:
    """Unsupported reminder shortcuts are rejected before date calculation."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T1", "channel_id": "C1"},
    )
    result = await create_task(context=ctx, title="Do something", reminder="2h_before")
    assert result["status"] == "error"
    assert "reminder" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_rejects_unparseable_due_date() -> None:
    """An unreadable due date cannot silently become a different date."""
    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T1", "channel_id": "C1"},
    )
    result = await create_task(context=ctx, title="Do something", due_date="gibberish xyz 999")
    assert result["status"] == "error"
    assert "could not understand" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_title_only_creates_with_context_owner_and_defaults() -> None:
    """The smallest valid request uses the authenticated owner and safe defaults."""
    context = _direct_context()
    task = _created_task()
    org_session = context.acquire_session().session

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title=" Follow up with Acme ")

    service_cls.assert_called_once_with(
        org_session,
        context.organisation_id,
        context.user_id,
    )
    service.create_task.assert_awaited_once_with(
        {
            "subject": "Follow up with Acme",
            "owner_user_id": context.user_id,
            "priority": "MEDIUM",
            "status": "NOT_STARTED",
            "created_via": "agent_create_task",
        },
        deal_id=None,
    )
    org_session.commit.assert_awaited_once()
    org_session.rollback.assert_not_awaited()
    assert result["status"] == "created"
    assert result["task_id"] == str(task.activity_id)
    assert result["title"] == "Follow up with Acme"
    assert result["priority"] == "MEDIUM"
    assert "due_date" not in result


@pytest.mark.asyncio
async def test_create_task_persists_explicit_due_date() -> None:
    """An explicit due date reaches the write service unchanged after parsing."""
    context = _direct_context()
    task = _created_task(due_date=datetime(2030, 6, 15, 17, tzinfo=UTC))

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(
            context=context,
            title="Send proposal",
            due_date="2030-06-15T17:00:00+00:00",
        )

    payload = service.create_task.await_args.args[0]
    assert payload["due_date"] == datetime(2030, 6, 15, 17, tzinfo=UTC)
    assert result["due_date"] == "2030-06-15T17:00:00+00:00"


@pytest.mark.asyncio
async def test_create_task_omitted_optional_fields_do_not_block_creation() -> None:
    """Optional CRM fields remain optional in the direct path."""
    context = _direct_context(channel="web")
    task = _created_task(subject="Update CRM notes", owner_name="Current User")

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title="Update CRM notes")

    payload = service.create_task.await_args.args[0]
    assert set(payload) == {
        "subject",
        "owner_user_id",
        "priority",
        "status",
        "created_via",
    }
    assert result["ui_card"]["kind"] == "task_created_confirmation"


@pytest.mark.asyncio
async def test_create_task_deal_name_association_remains_org_scoped() -> None:
    """A resolved deal ID stays bound to the authenticated organisation."""
    context = _direct_context()
    deal_id = UUID("44444444-4444-4444-4444-444444444444")
    task = _created_task(deal_id=deal_id, related_name="Acme")

    with (
        patch(
            "src.agents.tools.tasks.create_task._resolve_deal_by_name",
            AsyncMock(return_value=(deal_id, "Acme")),
        ) as resolve_deal,
        patch(
            "src.agents.tools.tasks.create_task.resolve_agent_access_actor",
            AsyncMock(return_value=CRMAccessActor(context.user_id, UserAppRole.MEMBER)),
        ),
        patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader,
    ):
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title="Follow up", deal_name="Acme")

    resolve_deal.assert_awaited_once_with(context, "Acme")
    assert isinstance(service_cls.call_args.kwargs["access_actor"], CRMAccessActor)
    assert service.create_task.await_args.kwargs["deal_id"] == deal_id
    assert result["deal_id"] == str(deal_id)
    assert result["deal_name"] == "Acme"
    assert str(task.activity_id) in result["message"]
    assert str(deal_id) in result["message"]


@pytest.mark.asyncio
async def test_create_task_assignee_resolution_uses_org_scoped_lookup() -> None:
    """Assignee names resolve through the context-bound canonical directory."""
    context = _direct_context()
    assignee_id = UUID("55555555-5555-5555-5555-555555555555")
    task = _created_task(owner_name="Mrinmoy")

    with (
        patch(
            "src.agents.tools.tasks.create_task._resolve_user_by_name",
            AsyncMock(return_value=(assignee_id, "Mrinmoy")),
        ) as resolve_user,
        patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader,
    ):
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title="Review deck", assign_to="Mrinmoy")

    resolve_user.assert_awaited_once_with(context, "Mrinmoy")
    payload = service.create_task.await_args.args[0]
    assert payload["owner_user_id"] == assignee_id
    assert result["assignee_name"] == "Mrinmoy"


@pytest.mark.asyncio
async def test_create_task_creates_reminder_only_when_requested_with_due_date() -> None:
    """Reminder configuration is written only with explicit reminder intent."""
    context = _direct_context(channel="slack")
    context.metadata = {"user_timezone": "UTC"}
    due_date = datetime(2030, 6, 15, 17, tzinfo=UTC)
    task = _created_task(due_date=due_date)

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        await create_task(
            context=context,
            title="Send proposal",
            due_date="2030-06-15T17:00:00+00:00",
            reminder="1h_before",
        )

    payload = service.create_task.await_args.args[0]
    assert payload["reminder"]["enabled"] is True
    assert payload["reminder"]["trigger_time"] == datetime(2030, 6, 15, 16, tzinfo=UTC)
    assert payload["reminder"]["delivery_channel"] == "slack"


@pytest.mark.asyncio
async def test_create_task_due_date_uses_anchored_user_local_weekday_for_reminder() -> None:
    """Relative weekdays use the captured local clock rather than worker time."""
    context = _direct_context(channel="slack")
    context.metadata = {
        "user_timezone": "Asia/Kolkata",
        "current_datetime": "2030-06-07T20:30:00+00:00",
    }
    task = _created_task(due_date=datetime(2030, 6, 8, 16, 30, tzinfo=UTC))

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        await create_task(
            context=context,
            title="Send proposal",
            due_date="Saturday 10 PM",
            reminder="1h_before",
        )

    payload = service.create_task.await_args.args[0]
    assert payload["due_date"] == datetime(2030, 6, 8, 16, 30, tzinfo=UTC)
    assert payload["reminder"]["trigger_time"] == datetime(2030, 6, 8, 15, 30, tzinfo=UTC)
    assert payload["reminder"]["timezone"] == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_create_task_preserves_whatsapp_channel_for_reminder_delivery() -> None:
    """WhatsApp-originated reminders retain their delivery channel."""
    context = _direct_context(channel="whatsapp")
    context.metadata = {"user_timezone": "UTC"}
    due_date = datetime(2030, 6, 15, 17, tzinfo=UTC)
    task = _created_task(due_date=due_date)

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        await create_task(
            context=context,
            title="Send proposal",
            due_date="2030-06-15T17:00:00+00:00",
            reminder="1h_before",
        )

    payload = service.create_task.await_args.args[0]
    assert payload["reminder"]["delivery_channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_create_task_rejects_reminder_without_due_date() -> None:
    """A reminder needs a concrete due instant to schedule safely."""
    context = _direct_context()
    result = await create_task(context=context, title="Follow up", reminder="at_due")

    assert result["status"] == "error"
    assert "due date" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_returns_structured_failure_for_write_service_error() -> None:
    """Service validation failures remain explicit agent errors."""
    context = _direct_context()
    org_session = context.acquire_session().session

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(
            side_effect=_FakeTasksWriteServiceError("owner does not belong to organisation", 400)
        )
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title="Follow up")

    org_session.commit.assert_not_awaited()
    org_session.rollback.assert_awaited_once()
    assert result == {
        "status": "error",
        "message": "Could not create task: owner does not belong to organisation",
    }


@pytest.mark.asyncio
async def test_create_task_builds_slack_success_payload() -> None:
    """Slack receives a channel-native task confirmation payload."""
    context = _direct_context(channel="slack")
    task = _created_task(priority="HIGH", related_name="Acme")

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title="Follow up with Acme", priority="HIGH")

    payload = result["_bot_channel_payload"]
    assert payload["type"] == "message"
    assert payload["content"]["blocks"][0]["type"] == "header"
    assert payload["content"]["blocks"][0]["text"]["text"] == "Task created"


@pytest.mark.asyncio
async def test_create_task_builds_teams_success_payload() -> None:
    """Teams receives a channel-native task confirmation payload."""
    context = _direct_context(channel="teams")
    task = _created_task()

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title="Follow up with Acme")

    payload = result["_bot_channel_payload"]
    card = payload["content"]["attachments"][0]["content"]
    assert payload["type"] == "message"
    assert card["type"] == "AdaptiveCard"
    assert card["body"][0]["text"] == "Task created"


@pytest.mark.asyncio
async def test_create_task_builds_whatsapp_text_payload() -> None:
    """WhatsApp receives concise text confirmation instead of a web card."""
    context = _direct_context(channel="whatsapp")
    deal_id = UUID("44444444-4444-4444-4444-444444444444")
    task = _created_task(related_name="Acme", deal_id=deal_id)

    with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
        service = MagicMock()
        service.create_task = AsyncMock(return_value=task)
        service.send_pending_assignment_notifications = AsyncMock()
        service_cls = MagicMock(return_value=service)
        loader.return_value = (service_cls, _FakeTasksWriteServiceError)

        result = await create_task(context=context, title="Follow up with Acme")

    assert result["_bot_channel_payload"]["content"]["text"] == (
        "Task created\n"
        "Title: Follow up with Acme\n"
        "Task ID: 33333333-3333-3333-3333-333333333333\n"
        "Priority: Medium\n"
        "Status: Not started\n"
        "Deal: Acme\n"
        f"Deal ID: {deal_id}"
    )
    assert str(deal_id) in result["_bot_channel_payload"]["text_fallback"]


@pytest.mark.asyncio
async def test_create_task_web_and_voice_include_ui_card_payload() -> None:
    """Web and voice clients receive the structured confirmation card."""
    for channel in ("web", "voice"):
        context = _direct_context(channel=channel)
        task = _created_task()

        with patch("src.agents.tools.tasks.create_task._load_tasks_write_service") as loader:
            service = MagicMock()
            service.create_task = AsyncMock(return_value=task)
            service.send_pending_assignment_notifications = AsyncMock()
            service_cls = MagicMock(return_value=service)
            loader.return_value = (service_cls, _FakeTasksWriteServiceError)

            result = await create_task(context=context, title="Follow up with Acme")

        assert result["ui_card"]["kind"] == "task_created_confirmation"
        assert result["_bot_channel_payload"] == {
            "type": "ui_card",
            "content": {"card": result["ui_card"]},
            "text_fallback": (
                "Created task Follow up with Acme. Task ID: 33333333-3333-3333-3333-333333333333."
            ),
            "attachments": None,
            "source_text": None,
        }


@pytest.mark.asyncio
async def test_create_task_tool_uses_direct_create_path_and_hides_channel_payload() -> None:
    """Internal channel payloads do not leak through the public tool result."""
    context = _direct_context()
    tool = create_create_task(context, lambda: None)

    with patch("src.agents.tools.tasks.create_task.create_task") as direct_create:
        direct_create.return_value = {
            "status": "created",
            "title": "Follow up",
            "_bot_channel_payload": {"type": "message", "content": {"text": "card"}},
        }

        result = await tool(title="Follow up")

    assert result == {"status": "created", "title": "Follow up"}
    direct_create.assert_awaited_once()


# ---------------------------------------------------------------------------
# Preview card tests (mock the bot service at the source module)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_sends_slack_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy Slack preview delivery retains its payload contract."""
    sent: dict = {}

    class FakeSlackBotService:
        """Capture a Slack preview without external delivery."""

        async def send_block_kit_message(
            self, team_id: str, channel_id: str, blocks: list, text_fallback: str
        ) -> bool:
            """Record the Block Kit request for assertions."""
            sent["team_id"] = team_id
            sent["channel_id"] = channel_id
            sent["blocks"] = blocks
            sent["text_fallback"] = text_fallback
            return True

    monkeypatch.setattr(
        "src.service.bots.slack_bot_service.SlackBotService",
        FakeSlackBotService,
    )

    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T123", "channel_id": "C456"},
    )

    result = await create_task_form(
        context=ctx,
        title="Follow up with Acme",
        priority="HIGH",
        due_date="2030-06-15T17:00:00",
    )

    assert result["status"] == "preview_sent"
    assert sent["team_id"] == "T123"
    assert sent["channel_id"] == "C456"
    assert sent["text_fallback"] == "Task preview: Follow up with Acme"

    blocks = sent["blocks"]

    # Header block "New Task"
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "New Task"

    # Task title in a section block
    assert blocks[1]["type"] == "section"
    assert "Follow up with Acme" in blocks[1]["text"]["text"]

    # Priority and due date in section fields (2-column)
    field_blocks = [b for b in blocks if b.get("type") == "section" and "fields" in b]
    assert len(field_blocks) >= 1
    all_field_text = " ".join(f["text"] for b in field_blocks for f in b["fields"])
    assert "High" in all_field_text
    assert "Due Date" in all_field_text
    assert "Assigned To" in all_field_text

    # Confirm/cancel buttons
    actions_block = [b for b in blocks if b.get("type") == "actions"][0]
    buttons = actions_block["elements"]
    assert len(buttons) == 2
    assert buttons[0]["action_id"] == "confirm_task_submit"
    assert buttons[1]["action_id"] == "confirm_task_cancel"
    assert buttons[0]["text"]["text"] == "Create Task"

    # Button value contains valid JSON payload
    payload = json.loads(buttons[0]["value"])
    assert payload["title"] == "Follow up with Acme"
    assert payload["priority"] == "HIGH"
    assert "due_date" in payload
    assert payload["organisation_id"] == str(ctx.organisation_id)


@pytest.mark.asyncio
async def test_create_task_sends_teams_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy Teams preview delivery retains its payload contract."""
    sent: dict = {}

    class FakeTeamsBotService:
        """Capture a Teams preview without external delivery."""

        async def send_proactive_payload(
            self, service_url: str, conversation_id: str, payload: dict, text_fallback: str
        ) -> bool:
            """Record the proactive card request for assertions."""
            sent["service_url"] = service_url
            sent["conversation_id"] = conversation_id
            sent["payload"] = payload
            sent["text_fallback"] = text_fallback
            return True

    monkeypatch.setattr(
        "src.service.bots.teams_bot_service.TeamsBotService",
        FakeTeamsBotService,
    )

    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="teams",
        metadata={"service_url": "https://svc", "conversation_id": "conv123"},
    )

    result = await create_task_form(
        context=ctx,
        title="Send proposal",
        priority="MEDIUM",
    )

    assert result["status"] == "preview_sent"
    assert sent["service_url"] == "https://svc"
    assert sent["conversation_id"] == "conv123"

    # Verify the Adaptive Card structure
    card = sent["payload"]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert any(a.get("verb") == "confirm_task_submit" for a in card["actions"])


@pytest.mark.asyncio
async def test_create_task_default_priority_is_medium(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no priority is specified, the card payload should not include it
    (MEDIUM is the default and TasksWriteService handles the default)."""
    sent: dict = {}

    class FakeSlackBotService:
        """Capture the default-priority Slack preview."""

        async def send_block_kit_message(self, **kwargs) -> bool:
            """Record the Block Kit request for assertions."""
            sent.update(kwargs)
            return True

    monkeypatch.setattr(
        "src.service.bots.slack_bot_service.SlackBotService",
        FakeSlackBotService,
    )

    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T1", "channel_id": "C1"},
    )

    result = await create_task_form(context=ctx, title="Simple task")
    assert result["status"] == "preview_sent"

    # MEDIUM is default — so it's omitted from payload to keep it small
    actions_block = [b for b in sent["blocks"] if b.get("type") == "actions"][0]
    payload = json.loads(actions_block["elements"][0]["value"])
    assert "priority" not in payload


@pytest.mark.asyncio
async def test_create_task_includes_description_in_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-provided descriptions remain visible in the review payload."""
    sent: dict = {}

    class FakeSlackBotService:
        """Capture the description-bearing Slack preview."""

        async def send_block_kit_message(self, **kwargs) -> bool:
            """Record the Block Kit request for assertions."""
            sent.update(kwargs)
            return True

    monkeypatch.setattr(
        "src.service.bots.slack_bot_service.SlackBotService",
        FakeSlackBotService,
    )

    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={"team_id": "T1", "channel_id": "C1"},
    )

    result = await create_task_form(
        context=ctx,
        title="Write docs",
        description="Document the new API endpoints",
    )
    assert result["status"] == "preview_sent"

    # Description appears in a section fields block
    blocks = sent["blocks"]
    all_field_text = " ".join(
        f["text"]
        for b in blocks
        if b.get("type") == "section" and "fields" in b
        for f in b["fields"]
    )
    assert "Document the new API endpoints" in all_field_text
