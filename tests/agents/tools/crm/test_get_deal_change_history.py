"""Tests for get_deal_change_history tool."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.get_deal_change_history import get_deal_change_history


@pytest.fixture
def mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.organisation_id = UUID("11111111-1111-1111-1111-111111111111")

    mock_session = AsyncMock()
    async_cm = AsyncMock()
    async_cm.__aenter__ = AsyncMock(return_value=mock_session)
    async_cm.__aexit__ = AsyncMock(return_value=False)
    ctx.acquire_session = MagicMock(return_value=async_cm)

    return ctx


DEAL_ID = "22222222-2222-2222-2222-222222222222"


def _make_deal_row() -> MagicMock:
    row = MagicMock()
    row.deal_id = UUID(DEAL_ID)
    row.name = "Acme Deal"
    row.company_name = "Acme Corp"
    row.stage_id = UUID("66666666-6666-6666-6666-666666666666")
    row.stage_name = "Negotiation"
    row.stage_label = "Negotiation"
    row.stage_entered_at = datetime(2026, 2, 10)
    row.pipeline_id = UUID("77777777-7777-7777-7777-777777777777")
    row.pipeline_name = "Main Pipeline"
    row.pipeline_label = "Main Pipeline"
    return row


def _make_history_row(
    stage_id: str = "44444444-4444-4444-4444-444444444444",
    entered_at: datetime = datetime(2026, 2, 10),
    exited_at: datetime | None = None,
    elapsed_seconds: int | None = 86400,
    amount_at_stage: float | None = 50000.0,
) -> MagicMock:
    row = MagicMock()
    row.history_id = UUID("55555555-5555-5555-5555-555555555555")
    row.stage_id = UUID(stage_id)
    row.entered_at = entered_at
    row.exited_at = exited_at
    row.elapsed_seconds = elapsed_seconds
    row.amount_at_stage = amount_at_stage
    row.probability_at_stage = 0.5
    row.source = "user"
    return row


def _make_stage_row(
    stage_id: str = "44444444-4444-4444-4444-444444444444",
    name: str = "Negotiation",
) -> MagicMock:
    row = MagicMock()
    row.stage_id = UUID(stage_id)
    row.name = name
    row.label = name
    row.stage_state = MagicMock(value="OPEN")
    row.is_closed = False
    return row


class TestGetDealChangeHistory:
    """Tests for the get_deal_change_history core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_none(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_deal_id_returns_none(self, mock_context: MagicMock) -> None:
        result = await get_deal_change_history("not-a-uuid", context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_deal_not_found_returns_none(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(one_or_none=MagicMock(return_value=None))
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_history_records(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[])),  # No history
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["deal_id"] == DEAL_ID
        assert result["deal_name"] == "Acme Deal"
        assert result["stage_changes"] == []
        assert result["summary"]["total_stage_changes"] == 0
        assert result["summary"]["first_stage_at"] is None
        assert result["summary"]["last_stage_at"] is None

    @pytest.mark.asyncio
    async def test_with_history_and_stage_enrichment(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()
        history_row = _make_history_row()
        stage_row = _make_stage_row()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[history_row])),
                MagicMock(all=MagicMock(return_value=[stage_row])),
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        assert len(result["stage_changes"]) == 1

        change = result["stage_changes"][0]
        assert change["stage_id"] == "44444444-4444-4444-4444-444444444444"
        assert change["stage_name"] == "Negotiation"
        assert change["stage_state"] == "OPEN"
        assert change["is_closed"] is False
        assert change["elapsed_seconds"] == 86400
        assert change["elapsed_days"] == 1.0
        assert change["amount_at_stage"] == 50000.0
        assert change["source"] == "user"

    @pytest.mark.asyncio
    async def test_history_without_stage_enrichment(self, mock_context: MagicMock) -> None:
        """When stage_id has no matching stage row, leaves stage names absent."""
        deal_row = _make_deal_row()
        history_row = _make_history_row()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[history_row])),
                MagicMock(all=MagicMock(return_value=[])),  # No matching stages
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        change = result["stage_changes"][0]
        assert "stage_name" not in change
        assert "stage_external_id" not in change

    @pytest.mark.asyncio
    async def test_elapsed_days_calculation(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()
        history_row = _make_history_row(elapsed_seconds=259200)  # 3 days

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[history_row])),
                MagicMock(all=MagicMock(return_value=[])),
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["stage_changes"][0]["elapsed_days"] == 3.0

    @pytest.mark.asyncio
    async def test_zero_amount_at_stage_is_preserved(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()
        history_row = _make_history_row(amount_at_stage=0)

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[history_row])),
                MagicMock(all=MagicMock(return_value=[])),
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["stage_changes"][0]["amount_at_stage"] == 0.0

    @pytest.mark.asyncio
    async def test_none_elapsed_seconds(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()
        history_row = _make_history_row(elapsed_seconds=None)

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[history_row])),
                MagicMock(all=MagicMock(return_value=[])),
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["stage_changes"][0]["elapsed_days"] is None

    @pytest.mark.asyncio
    async def test_summary_statistics(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()
        # Two history rows: most recent first (desc order)
        row1 = _make_history_row(entered_at=datetime(2026, 2, 10))
        row2 = _make_history_row(entered_at=datetime(2026, 1, 15))

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[row1, row2])),
                MagicMock(all=MagicMock(return_value=[])),
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        summary = result["summary"]
        assert summary["total_stage_changes"] == 2
        # first_stage_at = last item's entered_at, last_stage_at = first item's
        assert summary["first_stage_at"] == "2026-01-15T00:00:00"
        assert summary["last_stage_at"] == "2026-02-10T00:00:00"

    @pytest.mark.asyncio
    async def test_current_stage_in_output(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                MagicMock(all=MagicMock(return_value=[])),
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["current_stage"]["stage_id"] == "66666666-6666-6666-6666-666666666666"
        assert result["current_stage"]["name"] == "Negotiation"
        assert result["current_stage"]["entered_at"] == "2026-02-10T00:00:00"
        assert result["current_pipeline"]["pipeline_id"] == "77777777-7777-7777-7777-777777777777"
        assert result["current_pipeline"]["name"] == "Main Pipeline"

    @pytest.mark.asyncio
    async def test_db_error_on_deal_fetch_returns_none(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(side_effect=RuntimeError("DB error"))

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_db_error_on_history_fetch_returns_none(self, mock_context: MagicMock) -> None:
        deal_row = _make_deal_row()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(one_or_none=MagicMock(return_value=deal_row)),
                RuntimeError("history query failed"),
            ]
        )

        result = await get_deal_change_history(DEAL_ID, context=mock_context)

        assert result is None
