"""Tests for get_deal_details tool."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from src.agents.tools.crm.get_deal_details import get_deal_details
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact


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


def _make_deal() -> SimpleNamespace:
    return SimpleNamespace(
        deal_id=UUID(DEAL_ID),
        name="Acme Deal",
        description="Big deal with Acme",
        company_id=UUID("44444444-4444-4444-4444-444444444444"),
        company_name="Acme Corp",
        company_domain="acme.com",
        amount=75000.0,
        predicted_amount=52500.0,
        currency="USD",
        deal_status="open",
        forecast_category="Commit",
        stage_id=UUID("66666666-6666-6666-6666-666666666666"),
        stage_name="Negotiation",
        stage_label="Negotiation",
        probability=0.6,
        pipeline_id=UUID("77777777-7777-7777-7777-777777777777"),
        pipeline_name="Main Pipeline",
        pipeline_label="Main Pipeline",
        owner_id=UUID("33333333-3333-3333-3333-333333333333"),
        owner_name="Jane Doe",
        owner_email="jane@test.com",
        primary_contact_id=UUID("55555555-5555-5555-5555-555555555555"),
        primary_contact_name="Bob Acme",
        primary_contact_email="bob@acme.com",
        source_created_at=datetime(2026, 1, 10),
        source_modified_at=datetime(2026, 2, 20),
        expected_close_date=datetime(2026, 6, 30),
        close_date=None,
        closed_won_at=None,
        closed_lost_at=None,
        last_activity_at=datetime(2026, 2, 18),
        stage_entered_at=datetime(2026, 2, 1),
        stage_updated_at=datetime(2026, 2, 15),
        closed_reason=None,
        deal_type="new_business",
        lead_source="inbound",
        priority="high",
        crm_url="https://crm.example.com/deals/123",
        associated_contact_count=3,
        associated_company_count=1,
        note_count=5,
        deal_summary_data={"executive_summary": "Looking good"},
    )


def _make_company() -> SimpleNamespace:
    return SimpleNamespace(
        industry="Manufacturing",
        website="https://acme.com",
        linkedin_url="https://linkedin.com/company/acme",
        country="United States",
    )


def _make_stakeholder(
    sid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    name: str = "Alice",
    email: str = "alice@acme.com",
) -> SimpleNamespace:
    return SimpleNamespace(
        participant_id=UUID(sid),
        related_local_id=None,
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
        company = _make_company()
        stakeholder = _make_stakeholder()
        risk = _make_risk()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                # Deal query
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                # Linked company profile query
                MagicMock(scalar_one_or_none=MagicMock(return_value=company)),
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
        assert result["predicted_amount"] == 52500.0
        assert result["forecast_category"] == "Commit"
        assert result["company_id"] == "44444444-4444-4444-4444-444444444444"
        assert result["company_industry"] == "Manufacturing"
        assert result["company_website"] == "https://acme.com"
        assert result["company_linkedin_url"] == "https://linkedin.com/company/acme"
        assert result["company_country"] == "United States"
        assert result["stage"]["stage_id"] == "66666666-6666-6666-6666-666666666666"
        assert result["stage"]["name"] == "Negotiation"
        assert result["stage"]["entered_at"] == "2026-02-01T00:00:00"
        assert result["stage"]["updated_at"] == "2026-02-15T00:00:00"
        assert result["pipeline"]["pipeline_id"] == "77777777-7777-7777-7777-777777777777"
        assert result["pipeline"]["name"] == "Main Pipeline"
        assert result["owner"]["owner_user_id"] == "33333333-3333-3333-3333-333333333333"
        assert result["owner"]["email"] == "jane@test.com"
        assert (
            result["primary_contact"]["primary_contact_id"]
            == "55555555-5555-5555-5555-555555555555"
        )
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
        company = _make_company()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=company)),
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
        company = _make_company()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=company)),
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
    async def test_stakeholders_prefer_linked_contact_profile(
        self, mock_context: MagicMock
    ) -> None:
        """Deal details should show the durable contact profile, not stale snapshots."""
        deal = _make_deal()
        company = _make_company()
        contact_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        stakeholder = _make_stakeholder(
            name="Old Snapshot",
            email="old@example.com",
        )
        stakeholder.related_local_id = contact_id
        contact = CRMContact(
            organisation_id=mock_context.organisation_id,
            integration_id=UUID("99999999-9999-9999-9999-999999999999"),
            contact_id=contact_id,
            full_name="Contact Profile",
            email="profile@example.com",
            job_title="Chief Revenue Officer",
        )

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=company)),
                MagicMock(
                    scalars=MagicMock(
                        return_value=MagicMock(all=MagicMock(return_value=[stakeholder]))
                    )
                ),
                MagicMock(
                    scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[contact])))
                ),
            ]
        )

        result = await get_deal_details(DEAL_ID, context=mock_context, include_risks=False)

        assert result is not None
        assert result["stakeholders"][0]["name"] == "Contact Profile"
        assert result["stakeholders"][0]["email"] == "profile@example.com"
        assert result["stakeholders"][0]["job_title"] == "Chief Revenue Officer"

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
    async def test_zero_amount_is_preserved(self, mock_context: MagicMock) -> None:
        deal = _make_deal()
        deal.amount = 0

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=deal))
        )

        result = await get_deal_details(
            DEAL_ID, context=mock_context, include_stakeholders=False, include_risks=False
        )

        assert result is not None
        assert result["amount"] == 0.0

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
        company = _make_company()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=company)),
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
        company = _make_company()

        session = await mock_context.acquire_session().__aenter__()
        session.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=deal)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=company)),
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
