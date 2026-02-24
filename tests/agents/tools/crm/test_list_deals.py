"""Tests for list_deals tool, DealFilters, and helper functions."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.list_deals import (
    DealFilters,
    _apply_status_filter,
    _format_deal_output,
    list_deals,
)


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


def _make_deal(
    deal_id: str = "22222222-2222-2222-2222-222222222222",
    name: str = "Test Deal",
    company_name: str | None = "Test Corp",
    amount: float | None = 50000.0,
    currency: str | None = "USD",
    stage_name: str | None = "Negotiation",
    stage_label: str | None = "Negotiation",
    deal_status: str | None = "open",
    probability: float | None = 0.5,
    owner_name: str | None = "John Doe",
    owner_email: str | None = "john@test.com",
) -> MagicMock:
    deal = MagicMock()
    deal.deal_id = UUID(deal_id)
    deal.name = name
    deal.company_name = company_name
    deal.amount = amount
    deal.currency = currency
    deal.stage_name = stage_name
    deal.stage_label = stage_label
    deal.deal_status = deal_status
    deal.probability = probability
    deal.owner_name = owner_name
    deal.owner_email = owner_email
    deal.expected_close_date = None
    deal.source_created_at = datetime(2026, 1, 15, 10, 0, 0)
    deal.source_modified_at = datetime(2026, 2, 20, 14, 30, 0)
    deal.last_activity_at = None
    return deal


class TestDealFilters:
    """Tests for the DealFilters dataclass defaults."""

    def test_defaults(self) -> None:
        f = DealFilters()
        assert f.name is None
        assert f.status is None
        assert f.stage_name is None
        assert f.min_amount is None
        assert f.max_amount is None
        assert f.owner_email is None
        assert f.company_name is None
        assert f.sort_by == "source_modified_at"
        assert f.sort_order == "desc"
        assert f.limit == 20
        assert f.offset == 0

    def test_custom_values(self) -> None:
        f = DealFilters(
            name="Acme",
            status="open",
            min_amount=10000,
            max_amount=100000,
            limit=50,
            offset=10,
        )
        assert f.name == "Acme"
        assert f.status == "open"
        assert f.min_amount == 10000
        assert f.max_amount == 100000
        assert f.limit == 50
        assert f.offset == 10


class TestFormatDealOutput:
    """Tests for the _format_deal_output helper."""

    def test_full_deal(self) -> None:
        deal = _make_deal()
        output = _format_deal_output(deal)

        assert output["deal_id"] == "22222222-2222-2222-2222-222222222222"
        assert output["name"] == "Test Deal"
        assert output["company_name"] == "Test Corp"
        assert output["amount"] == 50000.0
        assert output["currency"] == "USD"
        assert output["stage_name"] == "Negotiation"
        assert output["deal_status"] == "open"
        assert output["owner_name"] == "John Doe"
        assert output["owner_email"] == "john@test.com"
        assert output["created_at"] == "2026-01-15T10:00:00"
        assert output["updated_at"] == "2026-02-20T14:30:00"

    def test_none_amount(self) -> None:
        deal = _make_deal(amount=None)
        output = _format_deal_output(deal)
        assert output["amount"] is None

    def test_none_dates(self) -> None:
        deal = _make_deal()
        deal.expected_close_date = None
        deal.last_activity_at = None
        deal.source_created_at = None
        deal.source_modified_at = None
        output = _format_deal_output(deal)
        assert output["expected_close_date"] is None
        assert output["last_activity_at"] is None
        assert output["created_at"] is None
        assert output["updated_at"] is None


class TestApplyStatusFilter:
    """Tests for _apply_status_filter helper."""

    def test_open_filter(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = MagicMock()
        result = _apply_status_filter(stmt, "open", c)
        stmt.where.assert_called_once()
        assert result is stmt

    def test_closed_filter(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = MagicMock()
        result = _apply_status_filter(stmt, "closed", c)
        stmt.where.assert_called_once()
        assert result is stmt

    def test_won_filter(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = MagicMock()
        result = _apply_status_filter(stmt, "won", c)
        stmt.where.assert_called_once()
        assert result is stmt

    def test_lost_filter(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = MagicMock()
        result = _apply_status_filter(stmt, "lost", c)
        stmt.where.assert_called_once()
        assert result is stmt

    def test_unknown_status_is_noop(self) -> None:
        """Unknown status values should not add a where clause."""
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = MagicMock()
        result = _apply_status_filter(stmt, "invalid_status", c)
        stmt.where.assert_not_called()
        assert result is stmt

    def test_case_insensitive(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = MagicMock()
        result = _apply_status_filter(stmt, "OPEN", c)
        stmt.where.assert_called_once()
        assert result is stmt


class TestListDeals:
    """Tests for the list_deals core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_empty(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await list_deals(context=mock_context, filters=DealFilters())

        assert result == []

    @pytest.mark.asyncio
    async def test_successful_listing(self, mock_context: MagicMock) -> None:
        deal = _make_deal()
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[deal])))
            )
        )

        result = await list_deals(context=mock_context, filters=DealFilters())

        assert len(result) == 1
        assert result[0]["name"] == "Test Deal"

    @pytest.mark.asyncio
    async def test_empty_results(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            )
        )

        result = await list_deals(context=mock_context, filters=DealFilters())

        assert result == []

    @pytest.mark.asyncio
    async def test_limit_clamped_to_100(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            )
        )

        # Should not error even with extreme limit
        result = await list_deals(context=mock_context, filters=DealFilters(limit=9999))

        assert result == []

    @pytest.mark.asyncio
    async def test_negative_offset_clamped_to_zero(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            )
        )

        result = await list_deals(context=mock_context, filters=DealFilters(offset=-5))

        assert result == []

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(side_effect=RuntimeError("DB error"))

        result = await list_deals(context=mock_context, filters=DealFilters())

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_deals_returned(self, mock_context: MagicMock) -> None:
        deals = [
            _make_deal(deal_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", name="Deal A"),
            _make_deal(deal_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", name="Deal B"),
        ]
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=deals)))
            )
        )

        result = await list_deals(context=mock_context, filters=DealFilters())

        assert len(result) == 2
        assert result[0]["name"] == "Deal A"
        assert result[1]["name"] == "Deal B"
