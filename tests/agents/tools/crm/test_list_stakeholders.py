"""Speech-friendly identity matching for deal stakeholder reads."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.agents.tools.crm.list_stakeholders import list_deal_stakeholders


class _Context:
    organisation_id = uuid4()
    user_id = uuid4()
    deal_id = uuid4()

    @asynccontextmanager
    async def acquire_session(self):  # noqa: ANN201
        """Yield the org session after the immutable deal boundary is set."""
        yield MagicMock()


def _stakeholder() -> dict:
    return {
        "participant_id": uuid4(),
        "deal_id": _Context.deal_id,
        "contact_id": uuid4(),
        "email": "north.star@company.com",
        "name": "Jonathan Mehta",
        "job_title": "VP Sales",
        "relationship_role": "decision_maker",
        "phone_number": None,
        "department": "Sales",
        "linkedin_url": None,
        "external_links": [],
        "stance": None,
        "decision_power": None,
        "motivation": None,
        "pain_points": [],
        "keywords": [],
        "temperature": None,
        "last_contact_at": None,
        "source": None,
        "stakeholder_research": None,
        "last_researched_at": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "field"),
    [("Jonatan Mehta", "name"), ("northstar@company.com", "email")],
)
async def test_stakeholder_query_falls_back_on_name_and_email_identity(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    field: str,
) -> None:
    """A literal miss searches only bounded person identity fields."""
    service = MagicMock()
    service.list_stakeholders_by_deal = AsyncMock(
        return_value=MagicMock(stakeholders=[_stakeholder()])
    )
    monkeypatch.setattr(
        "src.agents.tools.crm.list_stakeholders.require_agent_deal_access",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "src.agents.tools.crm.list_stakeholders.StakeholderReadService",
        MagicMock(return_value=service),
    )

    result = await list_deal_stakeholders(context=_Context(), query=query)

    assert result[0]["name"] == "Jonathan Mehta"
    assert result[0]["match"]["field"] == field
    assert result[0]["match"]["type"] == "fuzzy"


@pytest.mark.asyncio
async def test_stakeholder_query_keeps_title_filter_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-identity fields do not gain fuzzy semantics from the fallback."""
    service = MagicMock()
    service.list_stakeholders_by_deal = AsyncMock(
        return_value=MagicMock(stakeholders=[_stakeholder()])
    )
    monkeypatch.setattr(
        "src.agents.tools.crm.list_stakeholders.require_agent_deal_access",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "src.agents.tools.crm.list_stakeholders.StakeholderReadService",
        MagicMock(return_value=service),
    )

    assert await list_deal_stakeholders(context=_Context(), query="VP Slaes") == []
