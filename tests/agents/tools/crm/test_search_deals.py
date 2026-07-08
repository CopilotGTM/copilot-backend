"""Tests for search_deals tool."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.search_deals import search_deals


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


@dataclass
class _FakeSearchRow:
    deal: MagicMock
    match: dict[str, object] | None = None


class _FakeSearchService:
    rows: list[_FakeSearchRow] = []
    error: Exception | None = None
    calls: list[dict[str, object]] = []

    def __init__(self, session: object, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    async def search_deals_for_agent(
        self,
        *,
        q: str,
        limit: int,
    ) -> list[_FakeSearchRow]:
        self.calls.append({"query": q, "limit": limit})
        if self.error:
            raise self.error
        return [{"deal": row.deal, "match": row.match} for row in self.rows]


@pytest.fixture(autouse=True)
def fake_agentic_search(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSearchService.rows = []
    _FakeSearchService.error = None
    _FakeSearchService.calls = []
    monkeypatch.setattr(
        "src.agents.tools.crm.search_deals.DealReadService",
        _FakeSearchService,
    )


def _make_deal(
    deal_id: str,
    name: str,
    company: str | None = None,
    company_id: str | None = "33333333-3333-3333-3333-333333333333",
    amount: float | None = 50000.0,
    predicted_amount: float | None = 25000.0,
    currency: str | None = "USD",
    stage_id: str | None = "44444444-4444-4444-4444-444444444444",
    stage_name: str | None = "Negotiation",
    pipeline_id: str | None = "55555555-5555-5555-5555-555555555555",
    pipeline_name: str | None = "Sales",
) -> MagicMock:
    deal = MagicMock()
    deal.deal_id = UUID(deal_id)
    deal.name = name
    deal.company_id = UUID(company_id) if company_id else None
    deal.company_name = company
    deal.amount = amount
    deal.predicted_amount = predicted_amount
    deal.currency = currency
    deal.deal_status = "open"
    deal.stage_id = UUID(stage_id) if stage_id else None
    deal.stage_name = stage_name
    deal.pipeline_id = UUID(pipeline_id) if pipeline_id else None
    deal.pipeline_name = pipeline_name
    deal.primary_contact_name = "Jane Buyer"
    deal.primary_contact_email = "jane@example.com"
    return deal


class TestSearchDeals:
    """Tests for the search_deals core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_empty(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await search_deals(context=mock_context, query="Acme")

        assert result == []

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, mock_context: MagicMock) -> None:
        result = await search_deals(context=mock_context, query="")

        assert result == []

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty(self, mock_context: MagicMock) -> None:
        result = await search_deals(context=mock_context, query="   ")

        assert result == []

    @pytest.mark.asyncio
    async def test_none_query_returns_empty(self, mock_context: MagicMock) -> None:
        result = await search_deals(context=mock_context, query=None)  # type: ignore[arg-type]

        assert result == []

    @pytest.mark.asyncio
    async def test_successful_search_returns_deals(self, mock_context: MagicMock) -> None:
        deal = _make_deal("22222222-2222-2222-2222-222222222222", "Acme Deal", "Acme Corp")
        _FakeSearchService.rows = [
            _FakeSearchRow(
                deal,
                {
                    "field": "primary_contact_name",
                    "value": "Jane Buyer",
                    "type": "fuzzy",
                    "score": 1.2,
                },
            )
        ]

        result = await search_deals(context=mock_context, query="Acme")

        assert len(result) == 1
        assert result[0]["deal_id"] == "22222222-2222-2222-2222-222222222222"
        assert result[0]["name"] == "Acme Deal"
        assert result[0]["company_id"] == "33333333-3333-3333-3333-333333333333"
        assert result[0]["company_name"] == "Acme Corp"
        assert result[0]["amount"] == 50000.0
        assert result[0]["predicted_amount"] == 25000.0
        assert result[0]["currency"] == "USD"
        assert result[0]["deal_status"] == "open"
        assert result[0]["stage_id"] == "44444444-4444-4444-4444-444444444444"
        assert result[0]["stage_name"] == "Negotiation"
        assert result[0]["pipeline_id"] == "55555555-5555-5555-5555-555555555555"
        assert result[0]["pipeline_name"] == "Sales"
        assert result[0]["primary_contact_name"] == "Jane Buyer"
        assert result[0]["primary_contact_email"] == "jane@example.com"
        assert result[0]["match"] == {
            "field": "primary_contact_name",
            "value": "Jane Buyer",
            "type": "fuzzy",
            "score": 1.2,
        }

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty_list(self, mock_context: MagicMock) -> None:
        result = await search_deals(context=mock_context, query="Nonexistent")

        assert result == []

    @pytest.mark.asyncio
    async def test_limit_clamped_to_max_20(self, mock_context: MagicMock) -> None:
        """Limits above 20 should be clamped."""
        result = await search_deals(context=mock_context, query="test", limit=999)

        assert result == []
        assert _FakeSearchService.calls[0]["limit"] == 20

    @pytest.mark.asyncio
    async def test_limit_clamped_to_min_1(self, mock_context: MagicMock) -> None:
        """Limits below 1 should be clamped to 1."""
        result = await search_deals(context=mock_context, query="test", limit=0)

        assert result == []
        assert _FakeSearchService.calls[0]["limit"] == 1

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self, mock_context: MagicMock) -> None:
        _FakeSearchService.error = RuntimeError("DB connection lost")

        result = await search_deals(context=mock_context, query="Acme")

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_results(self, mock_context: MagicMock) -> None:
        deals = [
            _make_deal("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "Acme Deal 1", "Acme Corp"),
            _make_deal("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "Acme Deal 2", "Acme Inc"),
        ]
        _FakeSearchService.rows = [_FakeSearchRow(deal) for deal in deals]

        result = await search_deals(context=mock_context, query="Acme")

        assert len(result) == 2
        assert result[0]["name"] == "Acme Deal 1"
        assert result[1]["name"] == "Acme Deal 2"
