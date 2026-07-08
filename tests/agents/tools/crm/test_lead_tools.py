from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.create_lead import create_create_lead, create_lead_direct
from src.agents.tools.crm.list_leads import LeadFilters, create_list_leads, list_leads
from src.agents.tools.registry import ToolName, ToolRegistry
from src.domain.models.organisation_tables.sync.crm.leads import CRMLeadSourceType, CRMLeadStatus


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _context(*, channel: str = "web") -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        user_email="owner@example.com",
        channel=channel,
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(MagicMock()))
    return context


def _lead(**overrides: object) -> SimpleNamespace:
    data = {
        "lead_id": uuid4(),
        "status": CRMLeadStatus.NEW,
        "source_type": CRMLeadSourceType.MANUAL,
        "source_channel": "chat",
        "source_name": None,
        "source_ref": None,
        "priority": None,
        "score": None,
        "first_name": None,
        "last_name": None,
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "phone": None,
        "job_title": "VP Sales",
        "company_name": "Acme",
        "company_domain": "acme.com",
        "company_website": None,
        "industry": None,
        "country": None,
        "region": None,
        "city": None,
        "message": "Interested in CopilotGTM",
        "interest": None,
        "budget_amount": None,
        "budget_currency": None,
        "owner_id": None,
        "owner_name": None,
        "owner_email": None,
        "assigned_at": None,
        "contact_id": None,
        "company_id": None,
        "converted_deal_id": None,
        "converted_at": None,
        "disqualified_reason": None,
        "archive_reason": None,
        "form_id": None,
        "form_version_id": None,
        "form_submission_id": None,
        "utm_source": None,
        "utm_medium": None,
        "utm_campaign": None,
        "utm_term": None,
        "utm_content": None,
        "attribution": {},
        "consent": {},
        "properties": {},
        "metrics": {},
        "captured_at": None,
        "last_activity_at": None,
        "last_contacted_at": None,
        "qualified_at": None,
        "disqualified_at": None,
        "created_at": None,
        "updated_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_create_lead_direct_uses_lead_write_service() -> None:
    context = _context()
    lead = _lead()

    with patch("src.agents.tools.crm.create_lead.LeadWriteService") as service_cls:
        service = MagicMock()
        service.create_lead = AsyncMock(return_value=lead)
        service_cls.return_value = service

        result = await create_lead_direct(
            context=context,
            full_name=" Jane Doe ",
            email=" Jane@Example.com ",
            company_name=" Acme ",
            job_title=" VP Sales ",
            message=" Interested in CopilotGTM ",
        )

    service_cls.assert_called_once()
    payload = service.create_lead.call_args.args[0]
    assert payload["full_name"] == "Jane Doe"
    assert payload["email"] == "jane@example.com"
    assert payload["company_name"] == "Acme"
    assert payload["source_type"] == CRMLeadSourceType.MANUAL
    assert payload["source_channel"] == "chat"
    assert result["status"] == "created"
    assert result["lead_id"] == str(lead.lead_id)


@pytest.mark.asyncio
async def test_list_leads_uses_lead_read_service_filters() -> None:
    context = _context()
    lead = _lead()

    with patch("src.agents.tools.crm.list_leads.LeadReadService") as service_cls:
        service = MagicMock()
        service.list_leads = AsyncMock(return_value=([lead], 1))
        service_cls.return_value = service

        result = await list_leads(
            context=context,
            filters=LeadFilters(q="acme", status=CRMLeadStatus.NEW, limit=5),
        )

    service.list_leads.assert_awaited_once_with(
        page=1,
        limit=5,
        q="acme",
        status=CRMLeadStatus.NEW,
        owner_id=None,
        source_type=None,
    )
    assert result[0]["lead_id"] == str(lead.lead_id)
    assert result[0]["email"] == "jane@example.com"


@pytest.mark.asyncio
async def test_lead_tool_factories_hide_channel_payloads() -> None:
    context = _context()
    create_tool = create_create_lead(context, lambda: None)
    list_tool = create_list_leads(context, lambda: None)

    with (
        patch("src.agents.tools.crm.create_lead.create_lead_direct") as create_direct,
        patch("src.agents.tools.crm.list_leads.list_leads") as list_direct,
    ):
        create_direct.return_value = {
            "status": "created",
            "_bot_channel_payload": {"type": "message"},
        }
        list_direct.return_value = [{"lead_id": "lead-1"}]

        create_result = await create_tool(email="jane@example.com")
        list_result = await list_tool(q="jane")

    assert create_result == {"status": "created"}
    assert list_result == [{"lead_id": "lead-1"}]


def test_general_registry_exposes_lead_tools() -> None:
    assert ToolName.CREATE_LEAD in ToolRegistry.get_available_tools_for_agent("GeneralAgent")
    assert ToolName.LIST_LEADS in ToolRegistry.get_available_tools_for_agent("GeneralAgent")
