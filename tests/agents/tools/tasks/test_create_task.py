from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from src.agents.tools.tasks.create_task import create_task


@dataclass
class _FakeContext:
    organisation_id: UUID
    user_id: UUID | None
    channel: str
    metadata: dict = field(default_factory=dict)

    @asynccontextmanager
    async def acquire_session(self):
        yield None  # Not used for preview-only tests


# ---------------------------------------------------------------------------
# Validation tests (no external calls needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_requires_title() -> None:
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
    ctx = _FakeContext(organisation_id=uuid4(), user_id=None, channel="slack", metadata={})
    result = await create_task(context=ctx, title="Do something")
    assert result["status"] == "error"
    assert "user not identified" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_rejects_invalid_priority() -> None:
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
async def test_create_task_returns_error_without_slack_context() -> None:
    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="slack",
        metadata={},
    )
    result = await create_task(context=ctx, title="Do something")
    assert result["status"] == "error"
    assert "delivery context" in result["message"].lower()


@pytest.mark.asyncio
async def test_create_task_returns_error_without_teams_context() -> None:
    ctx = _FakeContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        channel="teams",
        metadata={},
    )
    result = await create_task(context=ctx, title="Do something")
    assert result["status"] == "error"
    assert "delivery context" in result["message"].lower()


# ---------------------------------------------------------------------------
# Preview card tests (mock the bot service at the source module)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_sends_slack_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict = {}

    class FakeSlackBotService:
        async def send_block_kit_message(
            self, team_id: str, channel_id: str, blocks: list, text_fallback: str
        ) -> bool:
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

    result = await create_task(
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
    sent: dict = {}

    class FakeTeamsBotService:
        async def send_proactive_payload(
            self, service_url: str, conversation_id: str, payload: dict, text_fallback: str
        ) -> bool:
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

    result = await create_task(
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
        async def send_block_kit_message(self, **kwargs) -> bool:
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

    result = await create_task(context=ctx, title="Simple task")
    assert result["status"] == "preview_sent"

    # MEDIUM is default — so it's omitted from payload to keep it small
    actions_block = [b for b in sent["blocks"] if b.get("type") == "actions"][0]
    payload = json.loads(actions_block["elements"][0]["value"])
    assert "priority" not in payload


@pytest.mark.asyncio
async def test_create_task_includes_description_in_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict = {}

    class FakeSlackBotService:
        async def send_block_kit_message(self, **kwargs) -> bool:
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

    result = await create_task(
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
