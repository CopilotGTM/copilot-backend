"""Tests for get_deal_details tool."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.get_deal_details import get_deal_details


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


def _make_deal() -> MagicMock:
    deal = MagicMock()
    deal.deal_id = UUID(DEAL_ID)
    deal.name = "Acme Deal"
    deal.description = "Big deal with Acme"
    deal.company_name = "Acme Corp"
    deal.company_domain = "acme.com"
    deal.amount = 75000.0
    deal.currency = "USD"
    deal.deal_status = "open"
    deal.stage_name = "Negotiation"
    deal.stage_label = "Negotiation"
    deal.probability = 0.6
    deal.pipeline_name = "Main Pipeline"
    deal.pipeline_label = "Main Pipeline"
    deal.owner_name = "Jane Doe"
    deal.owner_email = "jane@test.com"
    deal.primary_contact_name = "Bob Acme"
    deal.primary_contact_email = "bob@acme.com"
    deal.source_created_at = datetime(2026, 1, 10)
    deal.source_modified_at = datetime(2026, 2, 20)
    deal.expected_close_date = datetime(2026, 6, 30)
    deal.close_date = None
    deal.closed_won_at = None
    deal.closed_lost_at = None
    deal.last_activity_at = datetime(2026, 2, 18)
    deal.closed_reason = None
    deal.deal_type = "new_business"
    deal.lead_source = "inbound"
    deal.priority = "high"
    deal.crm_url = "https://crm.example.com/deals/123"
    deal.associated_contact_count = 3
    deal.associated_company_count = 1
    deal.note_count = 5
    deal.deal_summary_data = {"executive_summary": "Looking good"}
    return deal


def _make_stakeholder(
    sid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    name: str = "Alice",
    email: str = "alice@acme.com",
) -> SimpleNamespace:
    return SimpleNamespace(
        participant_id=UUID(sid),
        related_name=name,
        related_email=email,
        job_title="CTO",
        decision_power="DECISION_MAKER",
        stance="CHAMPION",
    )


def _make_risk(
    rid: str = "cccccccc-cccc-cccc-cccc-cccccccccccc",
    title: str = "Budget freeze",
) -> MagicMock:
    r = MagicMock()
    r.risk_blocker_id = UUID(rid)
    r.item_type = MagicMock(value="RISK")
    r.title = title
    r.severity = MagicMock(value="HIGH")
    r.status = MagicMock(value="ACTIVE")
    return r


class TestGetDealDetails:
    """Tests for the get_deal_details core function."""

    @pytest.mark.asyncio
    async def test_missing_org_returns_none(self, mock_context: MagicMock) -> None:
        mock_context.organisation_id = None

        result = await get_deal_details(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_deal_id_returns_none(self, mock_context: MagicMock) -> None:
        result = await get_deal_details("not-a-uuid", context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_deal_not_found_returns_none(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        result = await get_deal_details(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_full_deal_details(self, mock_context: MagicMock) -> None:
        deal = _make_deal()
        stakeholder = _make_stakeholder()
        risk = _make_risk()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                # Deal query
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                # Stakeholders query
                MagicMock(
                    scalars=MagicMock(
                        return_value=MagicMock(all=MagicMock(return_value=[stakeholder]))
                    )
                ),
                # Risks query
                MagicMock(
                    scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[risk])))
                ),
            ]
        )

        result = await get_deal_details(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["deal_id"] == DEAL_ID
        assert result["name"] == "Acme Deal"
        assert result["amount"] == 75000.0
        assert result["stage"]["name"] == "Negotiation"
        assert result["pipeline"]["name"] == "Main Pipeline"
        assert result["owner"]["email"] == "jane@test.com"
        assert result["deal_summary"] == {"executive_summary": "Looking good"}

        # Stakeholders
        assert len(result["stakeholders"]) == 1
        assert result["stakeholders"][0]["name"] == "Alice"
        assert result["stakeholders"][0]["decision_power"] == "DECISION_MAKER"

        # Risks
        assert len(result["risks_blockers"]) == 1
        assert result["risks_blockers"][0]["title"] == "Budget freeze"
        assert result["risks_blockers"][0]["severity"] == "HIGH"

    @pytest.mark.asyncio
    async def test_exclude_stakeholders(self, mock_context: MagicMock) -> None:
        deal = _make_deal()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                # Only risks query (no stakeholder query)
                MagicMock(
                    scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
                ),
            ]
        )

        result = await get_deal_details(DEAL_ID, context=mock_context, include_stakeholders=False)

        assert result is not None
        assert "stakeholders" not in result
        assert "risks_blockers" in result

    @pytest.mark.asyncio
    async def test_exclude_risks(self, mock_context: MagicMock) -> None:
        deal = _make_deal()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                # Only stakeholders query (no risks query)
                MagicMock(
                    scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
                ),
            ]
        )

        result = await get_deal_details(DEAL_ID, context=mock_context, include_risks=False)

        assert result is not None
        assert "stakeholders" in result
        assert "risks_blockers" not in result

    @pytest.mark.asyncio
    async def test_exclude_both_stakeholders_and_risks(self, mock_context: MagicMock) -> None:
        deal = _make_deal()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_details(
            DEAL_ID, context=mock_context, include_stakeholders=False, include_risks=False
        )

        assert result is not None
        assert "stakeholders" not in result
        assert "risks_blockers" not in result

    @pytest.mark.asyncio
    async def test_none_amount_in_output(self, mock_context: MagicMock) -> None:
        deal = _make_deal()
        deal.amount = None

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_details(
            DEAL_ID, context=mock_context, include_stakeholders=False, include_risks=False
        )

        assert result is not None
        assert result["amount"] is None

    @pytest.mark.asyncio
    async def test_db_error_returns_none(self, mock_context: MagicMock) -> None:
        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(side_effect=RuntimeError("DB error"))

        result = await get_deal_details(DEAL_ID, context=mock_context)

        assert result is None

    @pytest.mark.asyncio
    async def test_stakeholder_fetch_error_returns_empty_list(
        self, mock_context: MagicMock
    ) -> None:
        """Stakeholder fetch failure should not break the whole response."""
        deal = _make_deal()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                RuntimeError("stakeholder query failed"),
                MagicMock(
                    scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
                ),
            ]
        )

        result = await get_deal_details(DEAL_ID, context=mock_context)

        assert result is not None
        assert result["stakeholders"] == []
        assert "risks_blockers" in result

    @pytest.mark.asyncio
    async def test_risk_fetch_error_returns_empty_list(self, mock_context: MagicMock) -> None:
        """Risk fetch failure should not break the whole response."""
        deal = _make_deal()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                MagicMock(
                    scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
                ),
                RuntimeError("risk query failed"),
            ]
        )

        result = await get_deal_details(DEAL_ID, context=mock_context)

        assert result is not None
        assert "stakeholders" in result
        assert result["risks_blockers"] == []

    @pytest.mark.asyncio
    async def test_dates_are_iso_formatted(self, mock_context: MagicMock) -> None:
        deal = _make_deal()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_details(
            DEAL_ID, context=mock_context, include_stakeholders=False, include_risks=False
        )

        assert result is not None
        assert result["dates"]["created_at"] == "2026-01-10T00:00:00"
        assert result["dates"]["updated_at"] == "2026-02-20T00:00:00"
        assert result["dates"]["expected_close_date"] == "2026-06-30T00:00:00"
        assert result["dates"]["last_activity_at"] == "2026-02-18T00:00:00"
