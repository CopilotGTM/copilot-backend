"""Tests for get_deal_summary tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.get_deal_summary import get_deal_summary


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


def _make_deal(summary_data: dict | None = None) -> MagicMock:
    deal = MagicMock()
    deal.deal_id = UUID(DEAL_ID)
    deal.name = "Acme Deal"
    deal.company_id = UUID("33333333-3333-3333-3333-333333333333")
    deal.company_name = "Acme Corp"
    deal.amount = 50000.0
    deal.predicted_amount = 25000.0
    deal.currency = "USD"
    deal.deal_status = "open"
    deal.forecast_category = "Commit"
    deal.stage_id = UUID("44444444-4444-4444-4444-444444444444")
    deal.stage_name = "Negotiation"
    deal.pipeline_id = UUID("55555555-5555-5555-5555-555555555555")
    deal.pipeline_name = "Sales"
    deal.deal_summary_data = summary_data
    return deal


class TestGetDealSummary:
    """Tests for the get_deal_summary core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_none(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_deal_id_returns_none(self, mock_context: MagicMock) -> None:
        result = await get_deal_summary("not-a-uuid", context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_deal_not_found_returns_none(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_summary_completed(self, mock_context: MagicMock) -> None:
        summary = {
            "executive_summary": "Deal is on track",
            "deal_health": "healthy",
            "next_steps": ["Follow up with CTO"],
        }
        deal = _make_deal(summary_data=summary)

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["status"] == "COMPLETED"
        assert result["deal_summary"] == summary
        assert result["deal_id"] == DEAL_ID
        assert result["name"] == "Acme Deal"
        assert result["amount"] == 50000.0

    @pytest.mark.asyncio
    async def test_summary_not_generated(self, mock_context: MagicMock) -> None:
        deal = _make_deal(summary_data=None)

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["status"] == "NOT_GENERATED"
        assert result["deal_summary"] is None
        assert "not been generated" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_none_amount_in_output(self, mock_context: MagicMock) -> None:
        deal = _make_deal()
        deal.amount = None

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["amount"] is None

    @pytest.mark.asyncio
    async def test_zero_amount_is_preserved(self, mock_context: MagicMock) -> None:
        deal = _make_deal(summary_data={"executive_summary": "No value yet"})
        deal.amount = 0

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["amount"] == 0.0

    @pytest.mark.asyncio
    async def test_db_error_returns_none(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(side_effect=RuntimeError("DB error"))

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_output_includes_basic_fields(self, mock_context: MagicMock) -> None:
        deal = _make_deal(summary_data={"health": "ok"})

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_summary(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["company_id"] == "33333333-3333-3333-3333-333333333333"
        assert result["company_name"] == "Acme Corp"
        assert result["currency"] == "USD"
        assert result["predicted_amount"] == 25000.0
        assert result["deal_status"] == "open"
        assert result["forecast_category"] == "Commit"
        assert result["stage_id"] == "44444444-4444-4444-4444-444444444444"
        assert result["stage_name"] == "Negotiation"
        assert result["pipeline_id"] == "55555555-5555-5555-5555-555555555555"
        assert result["pipeline_name"] == "Sales"
