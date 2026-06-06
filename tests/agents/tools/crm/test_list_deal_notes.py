"""Tests for list_deal_notes tool."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.list_deal_notes import list_deal_notes


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")
    ctx.deal_id = UUID("22222222-2222-2222-2222-222222222222")

    mock_session = AsyncMock()
    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.acquire_session = MagicMock(return_value=async_cm)

    return ctx


DEAL_ID = "22222222-2222-2222-2222-222222222222"


def _make_note(
    activity_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    subject: str = "Follow-up",
    body: str = "Call Bob about pricing.",
) -> MagicMock:
    n = MagicMock()
    n.activity_id = UUID(activity_id)
    n.subject = subject
    n.body = body
    n.owner_name = "Jane Doe"
    n.owner_email = "jane@test.com"
    n.source_created_at = datetime(2026, 2, 15, 9, 0, 0)
    n.source_modified_at = datetime(2026, 2, 15, 10, 0, 0)
    n.synced_at = datetime(2026, 2, 15, 10, 30, 0)
    n.properties = {"key": "value"}
    return n


class TestListDealNotes:
    """Tests for the list_deal_notes core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_empty(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID)

        assert result == []

    @pytest.mark.asyncio
    async def test_no_deal_id_and_no_context_deal_returns_empty(
        self, mock_context: MagicMock
    ) -> None:
        mock_context.deal_id = None

        result = await list_deal_notes(context=mock_context, deal_id=None)

        assert result == []

    @pytest.mark.asyncio
    async def test_falls_back_to_context_deal_id(self, mock_context: MagicMock) -> None:
        """When deal_id is None, should use context.deal_id."""
        note = _make_note()
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=None)

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_successful_listing(self, mock_context: MagicMock) -> None:
        note = _make_note()
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID)

        assert len(result) == 1
        assert result[0]["activity_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        assert result[0]["subject"] == "Follow-up"
        assert result[0]["body"] == "Call Bob about pricing."
        assert result[0]["owner"]["name"] == "Jane Doe"
        assert result[0]["source_created_at"] == "2026-02-15T09:00:00"

    @pytest.mark.asyncio
    async def test_body_truncation(self, mock_context: MagicMock) -> None:
        note = _make_note(body="x" * 200)
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID, max_body_length=50)

        assert len(result) == 1
        assert len(result[0]["body"]) == 50

    @pytest.mark.asyncio
    async def test_no_body_truncation_when_none(self, mock_context: MagicMock) -> None:
        note = _make_note(body="Short body")
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID, max_body_length=None)

        assert result[0]["body"] == "Short body"

    @pytest.mark.asyncio
    async def test_exclude_body(self, mock_context: MagicMock) -> None:
        note = _make_note()
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID, include_body=False)

        assert result[0]["body"] is None

    @pytest.mark.asyncio
    async def test_include_properties(self, mock_context: MagicMock) -> None:
        note = _make_note()
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(
            context=mock_context, deal_id=DEAL_ID, include_properties=True
        )

        assert result[0]["properties"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_exclude_properties_by_default(self, mock_context: MagicMock) -> None:
        note = _make_note()
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID)

        assert "properties" not in result[0]

    @pytest.mark.asyncio
    async def test_limit_clamped_to_max_100(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            )
        )

        # Should not error with extreme limit
        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID, limit=999)

        assert result == []

    @pytest.mark.asyncio
    async def test_limit_clamped_to_min_1(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID, limit=0)

        assert result == []

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID)

        assert result == []

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(side_effect=RuntimeError("DB error"))

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID)

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_notes(self, mock_context: MagicMock) -> None:
        notes = [
            _make_note(activity_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", subject="Note 1"),
            _make_note(activity_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", subject="Note 2"),
        ]
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=notes)))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID)

        assert len(result) == 2
        assert result[0]["subject"] == "Note 1"
        assert result[1]["subject"] == "Note 2"

    @pytest.mark.asyncio
    async def test_none_dates_in_note(self, mock_context: MagicMock) -> None:
        note = _make_note()
        note.source_created_at = None
        note.source_modified_at = None
        note.synced_at = None

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[note])))
            )
        )

        result = await list_deal_notes(context=mock_context, deal_id=DEAL_ID)

        assert result[0]["source_created_at"] is None
        assert result[0]["source_modified_at"] is None
        assert result[0]["synced_at"] is None
