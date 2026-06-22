from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.add_deal_note import add_deal_note_direct, create_add_deal_note
from src.service.crm.internal.deal_notes_service import DealNotesServiceError


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _context(*, channel: str = "teams") -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        user_email="seller@copilotgtm.com",
        channel=channel,
        metadata={"user_name": "Seller Person"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(MagicMock()))
    return context


def _note(*, note_id: UUID | None = None, deal_id: UUID | None = None):
    now = datetime(2026, 5, 5, 10, 30, tzinfo=UTC)
    return SimpleNamespace(
        activity_id=note_id or uuid4(),
        deal_id=deal_id or uuid4(),
        body="Budget approval is expected Friday.",
        subject="Budget approval",
        title="Budget approval",
        source_created_at=now,
        created_at=now,
        synced_at=now,
        integration_id=None,
        owner_id=None,
        owner_name="Seller Person",
        owner_email="seller@copilotgtm.com",
    )


@pytest.mark.asyncio
async def test_add_deal_note_direct_requires_body() -> None:
    result = await add_deal_note_direct(
        context=_context(),
        deal_id=str(uuid4()),
        deal_name="Acme Expansion",
        body=" ",
    )

    assert result["status"] == "error"
    assert "note body" in result["message"].lower()


@pytest.mark.asyncio
async def test_add_deal_note_direct_rejects_invalid_deal_id() -> None:
    result = await add_deal_note_direct(
        context=_context(),
        deal_id="not-a-uuid",
        deal_name="Acme Expansion",
        body="Budget approval is expected Friday.",
    )

    assert result["status"] == "error"
    assert "deal" in result["message"].lower()


@pytest.mark.asyncio
async def test_add_deal_note_direct_validates_body_and_title_lengths() -> None:
    long_body = "x" * 10001
    body_result = await add_deal_note_direct(
        context=_context(),
        deal_id=str(uuid4()),
        deal_name="Acme Expansion",
        body=long_body,
    )

    long_title = "x" * 501
    title_result = await add_deal_note_direct(
        context=_context(),
        deal_id=str(uuid4()),
        deal_name="Acme Expansion",
        body="Valid body",
        title=long_title,
    )

    assert body_result == {
        "status": "error",
        "message": "Note body must be 10,000 characters or less.",
    }
    assert title_result == {
        "status": "error",
        "message": "Note title must be 500 characters or less.",
    }


@pytest.mark.asyncio
async def test_add_deal_note_direct_creates_note_commits_and_triggers_pipeline() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal_id = uuid4()
    note_id = uuid4()
    note = _note(note_id=note_id, deal_id=deal_id)
    create_result = SimpleNamespace(note=note, created_new=True)
    insight_job_id = uuid4()

    with (
        patch("src.agents.tools.crm.add_deal_note.NotesWriteService") as write_cls,
        patch("src.agents.tools.crm.add_deal_note.NotesInsightDispatchService") as dispatch_cls,
    ):
        write_service = MagicMock()
        write_service.create_user_note = AsyncMock(return_value=create_result)
        write_cls.return_value = write_service
        dispatch_service = MagicMock()
        dispatch_service.trigger_deal_insight_pipeline = AsyncMock(return_value=insight_job_id)
        dispatch_cls.return_value = dispatch_service

        result = await add_deal_note_direct(
            context=context,
            deal_id=str(deal_id),
            deal_name=" Acme Expansion ",
            body=" Budget approval is expected Friday. ",
            title=" Budget approval ",
        )

    write_cls.assert_called_once_with(org_session, context.organisation_id)
    dispatch_cls.assert_called_once_with(org_session, context.organisation_id)
    write_service.create_user_note.assert_awaited_once_with(
        deal_id=deal_id,
        content="Budget approval is expected Friday.",
        subject="Budget approval",
        context=ANY,
    )
    note_context = write_service.create_user_note.await_args.kwargs["context"]
    assert note_context.source == "agent"
    assert note_context.actor_user_id == context.user_id
    assert note_context.actor_name == "Seller Person"
    assert note_context.actor_email == context.user_email
    assert note_context.created_via == "agent_add_deal_note"
    org_session.commit.assert_awaited_once()
    dispatch_service.trigger_deal_insight_pipeline.assert_awaited_once_with(
        deal_id=deal_id,
        user_id=context.user_id,
        trigger_source="note_creation",
        trigger_source_id=note_id,
    )
    assert result["status"] == "created"
    assert result["note_id"] == str(note_id)
    assert result["deal_id"] == str(deal_id)
    assert result["deal_name"] == "Acme Expansion"
    assert result["title"] == "Budget approval"
    assert result["body"] == "Budget approval is expected Friday."
    assert result["display_title"] == "Budget approval"
    assert result["insight_job_id"] == str(insight_job_id)
    assert result["_bot_channel_payload"]["type"] == "message"


@pytest.mark.asyncio
async def test_add_deal_note_direct_returns_success_when_insight_dispatch_fails() -> None:
    context = _context(channel="web")
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal_id = uuid4()
    note = _note(deal_id=deal_id)
    create_result = SimpleNamespace(note=note, created_new=True)

    with (
        patch("src.agents.tools.crm.add_deal_note.NotesWriteService") as write_cls,
        patch("src.agents.tools.crm.add_deal_note.NotesInsightDispatchService") as dispatch_cls,
    ):
        write_service = MagicMock()
        write_service.create_user_note = AsyncMock(return_value=create_result)
        write_cls.return_value = write_service
        dispatch_service = MagicMock()
        dispatch_service.trigger_deal_insight_pipeline = AsyncMock(
            side_effect=RuntimeError("broker down")
        )
        dispatch_cls.return_value = dispatch_service

        result = await add_deal_note_direct(
            context=context,
            deal_id=str(deal_id),
            deal_name="Acme Expansion",
            body=note.body,
            title=note.title,
        )

    org_session.commit.assert_awaited_once()
    org_session.rollback.assert_not_awaited()
    assert result["status"] == "created"
    assert result["note_id"] == str(note.activity_id)
    assert "insight_job_id" not in result


@pytest.mark.asyncio
async def test_add_deal_note_direct_existing_note_skips_insight_pipeline() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    note = _note()
    create_result = SimpleNamespace(note=note, created_new=False)

    with (
        patch("src.agents.tools.crm.add_deal_note.NotesWriteService") as write_cls,
        patch("src.agents.tools.crm.add_deal_note.NotesInsightDispatchService") as dispatch_cls,
    ):
        write_service = MagicMock()
        write_service.create_user_note = AsyncMock(return_value=create_result)
        write_cls.return_value = write_service
        dispatch_service = MagicMock()
        dispatch_service.trigger_deal_insight_pipeline = AsyncMock()
        dispatch_cls.return_value = dispatch_service

        result = await add_deal_note_direct(
            context=context,
            deal_id=str(note.deal_id),
            deal_name="Acme Expansion",
            body=note.body,
            title=note.title,
        )

    org_session.commit.assert_awaited_once()
    dispatch_service.trigger_deal_insight_pipeline.assert_not_awaited()
    assert result["status"] == "exists"
    assert "already exists" in result["message"]


@pytest.mark.asyncio
async def test_add_deal_note_direct_returns_structured_service_error() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))

    with patch("src.agents.tools.crm.add_deal_note.NotesWriteService") as write_cls:
        write_service = MagicMock()
        write_service.create_user_note = AsyncMock(
            side_effect=DealNotesServiceError("Deal not found or access denied", status_code=404)
        )
        write_cls.return_value = write_service

        result = await add_deal_note_direct(
            context=context,
            deal_id=str(uuid4()),
            deal_name="Acme Expansion",
            body="Budget approval is expected Friday.",
        )

    org_session.rollback.assert_awaited_once()
    assert result == {
        "status": "error",
        "message": "Could not add note: Deal not found or access denied",
    }


@pytest.mark.asyncio
async def test_add_deal_note_tool_hides_channel_payload_from_public_result() -> None:
    context = _context()
    tool = create_add_deal_note(context, lambda: None)

    with patch("src.agents.tools.crm.add_deal_note.add_deal_note_direct") as direct:
        direct.return_value = {
            "status": "created",
            "note_id": str(uuid4()),
            "_bot_channel_payload": {"type": "message", "content": {"text": "card"}},
        }

        result = await tool(
            deal_id=str(uuid4()),
            deal_name="Acme Expansion",
            body="Budget approval is expected Friday.",
        )

    assert result is not None
    assert result["status"] == "created"
    assert "_bot_channel_payload" not in result


@pytest.mark.asyncio
async def test_add_deal_note_direct_builds_slack_success_payload() -> None:
    context = _context(channel="slack")
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    note = _note()
    create_result = SimpleNamespace(note=note, created_new=True)

    with (
        patch("src.agents.tools.crm.add_deal_note.NotesWriteService") as write_cls,
        patch("src.agents.tools.crm.add_deal_note.NotesInsightDispatchService") as dispatch_cls,
    ):
        write_service = MagicMock()
        write_service.create_user_note = AsyncMock(return_value=create_result)
        write_cls.return_value = write_service
        dispatch_service = MagicMock()
        dispatch_service.trigger_deal_insight_pipeline = AsyncMock(return_value=None)
        dispatch_cls.return_value = dispatch_service

        result = await add_deal_note_direct(
            context=context,
            deal_id=str(note.deal_id),
            deal_name="Acme Expansion",
            body=note.body,
            title=note.title,
        )

    payload = result["_bot_channel_payload"]
    assert payload["type"] == "message"
    assert payload["content"]["blocks"][0]["type"] == "header"
    assert payload["content"]["blocks"][0]["text"]["text"] == "Note added"
    assert payload["text_fallback"] == "Added note to Acme Expansion."
