"""Agent-only fuzzy fallback contracts for lead listing."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.agents.tools.crm.list_leads import LeadFilters, list_leads
from src.domain.models.master_tables.user_models import UserAppRole
from src.domain.models.organisation_tables.sync.crm.leads import CRMLeadStatus
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.leads.read_service import LeadReadService


class _Context:
    organisation_id = uuid4()
    user_id = uuid4()

    @asynccontextmanager
    async def acquire_session(self):  # noqa: ANN201
        """Yield one tenant-scoped session for the agent tool."""
        yield MagicMock()


def _lead() -> SimpleNamespace:
    return SimpleNamespace(
        lead_id=uuid4(),
        status="new",
        source_type="manual",
        source_channel=None,
        full_name="Jonathan Mehta",
        email="north.star@company.com",
        phone=None,
        job_title=None,
        company_name="North Star",
        company_domain="company.com",
        message=None,
        interest=None,
        owner_id=None,
        owner_name=None,
        owner_email=None,
        created_at=None,
        captured_at=None,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_list_leads_runs_fuzzy_search_only_after_literal_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent recovery preserves structured filters and exposes suggestion metadata."""
    lead = _lead()
    service = MagicMock()
    service.list_leads = AsyncMock(return_value=([], 0))
    service.search_leads_for_agent = AsyncMock(
        return_value=[
            {
                "lead": lead,
                "match": {"field": "email", "type": "fuzzy", "score": 0.91},
            }
        ]
    )
    monkeypatch.setattr(
        "src.agents.tools.crm.list_leads.resolve_agent_access_actor",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "src.agents.tools.crm.list_leads.LeadReadService",
        MagicMock(return_value=service),
    )
    filters = LeadFilters(q="northstar@company.com", status=CRMLeadStatus.NEW, limit=7)

    result = await list_leads(context=_Context(), filters=filters)

    service.search_leads_for_agent.assert_awaited_once_with(
        q="northstar@company.com",
        limit=7,
        status=CRMLeadStatus.NEW,
        owner_id=None,
        source_type=None,
    )
    assert result[0]["email"] == "north.star@company.com"
    assert result[0]["match"] == {"field": "email", "type": "fuzzy", "score": 0.91}


@pytest.mark.asyncio
async def test_list_leads_does_not_mix_fuzzy_candidates_into_literal_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct match remains authoritative and stops before fuzzy expansion."""
    lead = _lead()
    service = MagicMock()
    service.list_leads = AsyncMock(return_value=([lead], 1))
    service.search_leads_for_agent = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.crm.list_leads.resolve_agent_access_actor",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "src.agents.tools.crm.list_leads.LeadReadService",
        MagicMock(return_value=service),
    )

    result = await list_leads(context=_Context(), filters=LeadFilters(q="Jonathan"))

    service.search_leads_for_agent.assert_not_awaited()
    assert result[0]["name"] == "Jonathan Mehta"
    assert "match" not in result[0]


@pytest.mark.asyncio
async def test_lead_fuzzy_query_weights_email_local_part_and_keeps_status() -> None:
    """The SQL fallback cannot match solely because many leads share one domain."""
    lead = _lead()
    row = MagicMock(score=0.88, match_field="email")
    row.__getitem__.side_effect = lambda index: lead if index == 0 else None
    result = MagicMock()
    result.all.return_value = [row]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    actor_id = uuid4()
    service = LeadReadService(
        session,
        _Context.organisation_id,
        actor_user_id=actor_id,
        access_actor=CRMAccessActor(actor_id, UserAppRole.MEMBER),
    )

    rows = await service.search_leads_for_agent(
        q="northstar@company.com",
        status=CRMLeadStatus.NEW,
        limit=5,
    )

    statement = str(session.execute.await_args.args[0])
    assert "split_part" in statement
    assert "crm_leads.status" in statement
    assert rows[0]["match"] == {"field": "email", "type": "fuzzy", "score": 0.88}
