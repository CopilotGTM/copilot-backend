"""Tests for list_deals tool, DealFilters, and helper functions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.list_deals import (
    DealFilters,
    _apply_filters,
    _apply_sorting,
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
    company_id: str | None = "44444444-4444-4444-4444-444444444444",
    company_name: str | None = "Test Corp",
    amount: float | None = 50000.0,
    predicted_amount: float | None = 25000.0,
    currency: str | None = "USD",
    stage_id: str | None = "55555555-5555-5555-5555-555555555555",
    stage_name: str | None = "Negotiation",
    stage_label: str | None = "Negotiation",
    pipeline_id: str | None = "66666666-6666-6666-6666-666666666666",
    deal_status: str | None = "open",
    probability: float | None = 0.5,
    owner_name: str | None = "John Doe",
    owner_email: str | None = "john@test.com",
    owner_id: str | None = "33333333-3333-3333-3333-333333333333",
) -> MagicMock:
    deal = MagicMock()
    deal.deal_id = UUID(deal_id)
    deal.name = name
    deal.company_id = UUID(company_id) if company_id else None
    deal.company_name = company_name
    deal.amount = amount
    deal.predicted_amount = predicted_amount
    deal.currency = currency
    deal.stage_id = UUID(stage_id) if stage_id else None
    deal.stage_name = stage_name
    deal.stage_label = stage_label
    deal.pipeline_id = UUID(pipeline_id) if pipeline_id else None
    deal.deal_status = deal_status
    deal.probability = probability
    deal.owner_id = UUID(owner_id) if owner_id else None
    deal.owner_name = owner_name
    deal.owner_email = owner_email
    deal.expected_close_date = None
    deal.close_date = None
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

    def test_date_filter_annotations_accept_voice_schema_strings(self) -> None:
        hints = get_type_hints(DealFilters)

        for field_name in (
            "min_close_date",
            "max_close_date",
            "min_expected_close_date",
            "max_expected_close_date",
        ):
            assert str in get_args(hints[field_name])


class TestFormatDealOutput:
    """Tests for the _format_deal_output helper."""

    def test_full_deal(self) -> None:
        deal = _make_deal()
        output = _format_deal_output(deal)

        assert output["deal_id"] == "22222222-2222-2222-2222-222222222222"
        assert output["name"] == "Test Deal"
        assert output["company_id"] == "44444444-4444-4444-4444-444444444444"
        assert output["company_name"] == "Test Corp"
        assert output["amount"] == 50000.0
        assert output["predicted_amount"] == 25000.0
        assert output["currency"] == "USD"
        assert output["stage_id"] == "55555555-5555-5555-5555-555555555555"
        assert output["stage_name"] == "Negotiation"
        assert output["pipeline_id"] == "66666666-6666-6666-6666-666666666666"
        assert output["deal_status"] == "open"
        assert output["owner_user_id"] == "33333333-3333-3333-3333-333333333333"
        assert output["owner_name"] == "John Doe"
        assert output["owner_email"] == "john@test.com"
        assert output["created_at"] == "2026-01-15T10:00:00"
        assert output["updated_at"] == "2026-02-20T14:30:00"

    def test_outputs_expected_and_actual_close_dates(self) -> None:
        deal = _make_deal()
        deal.expected_close_date = datetime(2026, 6, 30, 0, 0, 0)
        deal.close_date = datetime(2026, 7, 2, 12, 30, 0)

        output = _format_deal_output(deal)

        assert output["expected_close_date"] == "2026-06-30T00:00:00"
        assert output["close_date"] == "2026-07-02T12:30:00"

    def test_none_amount(self) -> None:
        deal = _make_deal(amount=None)
        output = _format_deal_output(deal)
        assert output["amount"] is None

    def test_zero_amount_is_preserved(self) -> None:
        deal = _make_deal(amount=0)
        output = _format_deal_output(deal)
        assert output["amount"] == 0.0

    def test_none_dates(self) -> None:
        deal = _make_deal()
        deal.expected_close_date = None
        deal.close_date = None
        deal.last_activity_at = None
        deal.source_created_at = None
        deal.source_modified_at = None
        output = _format_deal_output(deal)
        assert output["expected_close_date"] is None
        assert output["close_date"] is None
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


class TestApplyFilters:
    """Tests for scalar/range deal filters."""

    class _FilterColumn:
        def __init__(self) -> None:
            self.ge_calls = 0
            self.le_calls = 0

        def __ge__(self, other):  # noqa: ANN001
            self.ge_calls += 1
            return ("ge", other)

        def __le__(self, other):  # noqa: ANN001
            self.le_calls += 1
            return ("le", other)

    class _Columns:
        def __init__(self) -> None:
            self.name = MagicMock()
            self.deal_status = MagicMock()
            self.stage_name = MagicMock()
            self.amount = MagicMock()
            self.owner_email = MagicMock()
            self.company_name = MagicMock()
            self.close_date = TestApplyFilters._FilterColumn()
            self.expected_close_date = TestApplyFilters._FilterColumn()

    def test_expected_close_date_filters_use_forecast_date(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = self._Columns()

        result = _apply_filters(
            stmt,
            DealFilters(
                min_expected_close_date=datetime(2026, 7, 1),
                max_expected_close_date=datetime(2026, 7, 31),
            ),
            c,
        )

        assert result is stmt
        assert stmt.where.call_count == 2
        assert c.expected_close_date.ge_calls == 1
        assert c.expected_close_date.le_calls == 1
        assert c.close_date.ge_calls == 0
        assert c.close_date.le_calls == 0

    def test_string_expected_close_date_filters_are_parsed(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = self._Columns()

        _apply_filters(
            stmt,
            DealFilters(min_expected_close_date="2026-07-01T00:00:00Z"),  # type: ignore[arg-type]
            c,
        )

        comparison_value = stmt.where.call_args.args[0][1]
        assert comparison_value == datetime(2026, 7, 1, 0, 0, 0)

    def test_aware_datetime_close_date_filter_is_normalized(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = self._Columns()

        _apply_filters(
            stmt,
            DealFilters(min_close_date=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)),
            c,
        )

        comparison_value = stmt.where.call_args.args[0][1]
        assert comparison_value == datetime(2026, 7, 1, 0, 0, 0)

    def test_invalid_string_close_date_filter_is_ignored(self) -> None:
        stmt = MagicMock()
        stmt.where = MagicMock(return_value=stmt)
        c = self._Columns()

        result = _apply_filters(
            stmt,
            DealFilters(min_close_date="not-a-date"),  # type: ignore[arg-type]
            c,
        )

        assert result is stmt
        stmt.where.assert_not_called()


class TestApplySorting:
    """Tests for _apply_sorting helper."""

    class _SortColumn:
        def __init__(self) -> None:
            self.desc_calls = 0
            self.asc_calls = 0

        def desc(self) -> TestApplySorting._SortColumn:
            self.desc_calls += 1
            return self

        def asc(self) -> TestApplySorting._SortColumn:
            self.asc_calls += 1
            return self

        def nulls_last(self) -> TestApplySorting._SortColumn:
            return self

    class _Columns:
        def __init__(self) -> None:
            self.amount = TestApplySorting._SortColumn()
            self.source_created_at = TestApplySorting._SortColumn()
            self.close_date = TestApplySorting._SortColumn()
            self.expected_close_date = TestApplySorting._SortColumn()
            self.source_modified_at = TestApplySorting._SortColumn()
            self.name = TestApplySorting._SortColumn()

    def test_close_date_sort_uses_actual_close_date(self) -> None:
        stmt = MagicMock()
        stmt.order_by = MagicMock(return_value=stmt)
        c = self._Columns()

        result = _apply_sorting(stmt, DealFilters(sort_by="close_date"), c)

        stmt.order_by.assert_called_once()
        assert result is stmt
        assert c.close_date.desc_calls == 1
        assert c.expected_close_date.desc_calls == 0

    def test_expected_close_date_sort_uses_forecast_date(self) -> None:
        stmt = MagicMock()
        stmt.order_by = MagicMock(return_value=stmt)
        c = self._Columns()

        result = _apply_sorting(stmt, DealFilters(sort_by="expected_close_date"), c)

        stmt.order_by.assert_called_once()
        assert result is stmt
        assert c.expected_close_date.desc_calls == 1
        assert c.close_date.desc_calls == 0


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
