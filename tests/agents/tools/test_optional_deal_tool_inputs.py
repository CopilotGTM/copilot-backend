"""Public agent-tool contracts for optional deal selection."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.get_deal_change_history import create_get_deal_change_history
from src.agents.tools.crm.get_deal_details import create_get_deal_details
from src.agents.tools.crm.get_deal_memory import create_get_deal_memory
from src.agents.tools.crm.get_deal_summary import create_get_deal_summary
from src.agents.tools.intelligence.get_compressed_transcript import (
    create_get_compressed_transcript,
    get_compressed_transcript,
)
from src.agents.tools.intelligence.get_meeting_details import create_get_meeting_details
from src.agents.tools.intelligence.get_meeting_summary import (
    create_get_meeting_summary,
    get_meeting_summary,
)
from src.agents.tools.intelligence.get_meeting_transcript import (
    create_get_meeting_transcript,
    get_meeting_transcript,
)


def _context() -> AgentContext:
    """Build a human agent context without a page-scoped deal."""
    return AgentContext(
        organisation_id=uuid4(),
        user_id=uuid4(),
        deal_id=None,
        org_name="Example Org",
    )


class _MissingAssociationSession:
    """Return no deal-event association for transcript authorization checks."""

    def __init__(self) -> None:
        self.statements = []

    async def execute(self, _statement):
        """Model a visible deal whose requested event is unrelated."""
        self.statements.append(_statement)
        return SimpleNamespace(scalar_one_or_none=lambda: None)


class _SequenceSession:
    """Return configured scalar values while retaining generated SQL."""

    def __init__(self, values) -> None:
        self.values = list(values)
        self.statements = []

    async def execute(self, statement):
        """Return the next scalar response for a query."""
        self.statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.values.pop(0))


def _context_with_session() -> AgentContext:
    """Build a human context whose org session has no matching association."""
    context = _context()

    @asynccontextmanager
    async def acquire_session():
        """Yield the no-association session through the AgentContext contract."""
        yield _MissingAssociationSession()

    context.acquire_session = acquire_session  # type: ignore[method-assign]
    return context


@pytest.mark.asyncio
async def test_get_deal_memory_tool_forwards_optional_deal_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GeneralAgent can request memory for a deal returned by deal search."""
    deal_id = str(uuid4())
    implementation = AsyncMock(return_value={"status": "AVAILABLE"})
    monkeypatch.setattr(
        "src.agents.tools.crm.get_deal_memory.get_deal_memory",
        implementation,
    )

    result = await create_get_deal_memory(_context(), lambda: None)(deal_id=deal_id)

    assert result["status"] == "AVAILABLE"
    assert implementation.await_args.kwargs["deal_id"] == deal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_path", "factory", "result"),
    [
        (
            "src.agents.tools.crm.get_deal_details",
            create_get_deal_details,
            {"name": "Example Deal"},
        ),
        (
            "src.agents.tools.crm.get_deal_summary",
            create_get_deal_summary,
            {"status": "COMPLETED"},
        ),
        (
            "src.agents.tools.crm.get_deal_change_history",
            create_get_deal_change_history,
            {"deal_name": "Example Deal", "summary": {"total_stage_changes": 0}},
        ),
    ],
)
async def test_deal_read_tool_uses_context_deal_when_argument_is_omitted(
    module_path: str,
    factory,
    result: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deal-scoped agents can call each read tool without repeating their deal ID."""
    context = replace(_context(), deal_id=uuid4())
    implementation = AsyncMock(return_value=result)
    monkeypatch.setattr(f"{module_path}.{module_path.rsplit('.', 1)[-1]}", implementation)

    await factory(context, lambda: None)()

    assert implementation.await_args.kwargs["deal_id"] == str(context.deal_id)


@pytest.mark.asyncio
async def test_get_meeting_details_tool_forwards_optional_deal_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meeting details can select the requested deal association explicitly."""
    deal_id = str(uuid4())
    implementation = AsyncMock(return_value={"title": "Review"})
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_details.get_meeting_details",
        implementation,
    )

    await create_get_meeting_details(_context(), lambda: None)(
        event_id=str(uuid4()),
        deal_id=deal_id,
    )

    assert implementation.await_args.kwargs["deal_id"] == deal_id


@pytest.mark.asyncio
async def test_get_meeting_summary_tool_forwards_optional_deal_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meeting summaries can select the requested deal association explicitly."""
    deal_id = str(uuid4())
    implementation = AsyncMock(return_value={"status": "COMPLETED"})
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_summary.get_meeting_summary",
        implementation,
    )

    await create_get_meeting_summary(_context(), lambda: None)(
        event_id=str(uuid4()),
        deal_id=deal_id,
    )

    assert implementation.await_args.kwargs["deal_id"] == deal_id


@pytest.mark.asyncio
async def test_get_meeting_transcript_tool_forwards_optional_deal_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw transcript reads can carry their visible parent-deal boundary."""
    deal_id = str(uuid4())
    implementation = AsyncMock(return_value={"transcript_available": True})
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_transcript.get_meeting_transcript",
        implementation,
    )

    await create_get_meeting_transcript(_context(), lambda: None)(
        event_id=str(uuid4()),
        deal_id=deal_id,
    )

    assert implementation.await_args.kwargs["deal_id"] == deal_id


@pytest.mark.asyncio
async def test_get_compressed_transcript_tool_forwards_optional_deal_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compressed transcript reads carry the same deal boundary as raw reads."""
    deal_id = str(uuid4())
    implementation = AsyncMock(return_value={"transcript_available": True})
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_compressed_transcript.get_compressed_transcript",
        implementation,
    )

    await create_get_compressed_transcript(_context(), lambda: None)(
        event_id=str(uuid4()),
        deal_id=deal_id,
    )

    assert implementation.await_args.kwargs["deal_id"] == deal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_path", "reader"),
    [
        (
            "src.agents.tools.intelligence.get_meeting_transcript",
            get_meeting_transcript,
        ),
        (
            "src.agents.tools.intelligence.get_compressed_transcript",
            get_compressed_transcript,
        ),
    ],
)
async def test_transcript_reader_rejects_event_outside_explicit_deal_before_provider_fetch(
    module_path: str,
    reader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A visible deal ID cannot authorize an unrelated organisation transcript."""
    provider_fetch = AsyncMock()
    monkeypatch.setattr(
        f"{module_path}.require_agent_deal_access",
        AsyncMock(),
    )
    monkeypatch.setattr(
        f"{module_path}.TranscriptService",
        lambda _organisation_id: SimpleNamespace(ensure_transcript=provider_fetch),
    )

    result = await reader(
        str(uuid4()),
        context=_context_with_session(),
        deal_id=str(uuid4()),
    )

    assert result is None
    provider_fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_path", "reader"),
    [
        (
            "src.agents.tools.intelligence.get_meeting_transcript",
            get_meeting_transcript,
        ),
        (
            "src.agents.tools.intelligence.get_compressed_transcript",
            get_compressed_transcript,
        ),
    ],
)
async def test_transcript_reader_requires_current_user_event_before_provider_fetch(
    module_path: str,
    reader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without deal scope, an event UUID must belong to the caller's calendar."""
    provider_fetch = AsyncMock()
    monkeypatch.setattr(
        f"{module_path}.TranscriptService",
        lambda _organisation_id: SimpleNamespace(ensure_transcript=provider_fetch),
    )

    result = await reader(str(uuid4()), context=_context_with_session())

    assert result is None
    provider_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_meeting_summary_requires_active_org_deal_association(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale or cross-org associations must not expose a deal meeting summary."""
    event_id = uuid4()
    deal_id = uuid4()
    event = SimpleNamespace(event_id=event_id, title="Review", start_at=None)
    session = _SequenceSession([event, None])
    context = _context()

    @asynccontextmanager
    async def acquire_session():
        """Yield the query-capturing session through AgentContext."""
        yield session

    context.acquire_session = acquire_session  # type: ignore[method-assign]
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_summary.require_agent_deal_access",
        AsyncMock(),
    )

    result = await get_meeting_summary(
        str(event_id),
        context=context,
        deal_id=str(deal_id),
    )

    association_sql = str(session.statements[1])
    assert result["status"] == "NOT_AVAILABLE"
    assert "calendar_event_deal_associations.organisation_id =" in association_sql
    assert "calendar_event_deal_associations.status =" in association_sql
