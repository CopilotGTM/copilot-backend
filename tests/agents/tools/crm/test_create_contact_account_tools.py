from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.create_account import create_account_direct, create_create_account
from src.agents.tools.crm.create_contact import create_contact_direct, create_create_contact
from src.agents.tools.crm.search_accounts import search_accounts


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _context(*, channel: str = "teams") -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel=channel,
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(MagicMock()))
    return context


@pytest.mark.asyncio
async def test_create_account_direct_reuses_existing_domain_account() -> None:
    existing = SimpleNamespace(
        company_id=uuid4(),
        name="Acme",
        domain="acme.com",
        website=None,
        industry=None,
        linkedin_url=None,
    )

    with patch("src.agents.tools.crm.create_account.AccountsAccessService") as service_cls:
        service = MagicMock()
        service.find_accounts_by_domain = AsyncMock(return_value=[existing])
        service_cls.return_value = service

        result = await create_account_direct(
            context=_context(),
            name="Acme",
            domain="https://www.acme.com/about",
        )

    service.find_accounts_by_domain.assert_awaited_once_with("acme.com")
    service.create_account.assert_not_called()
    assert result["status"] == "exists"
    assert result["account_id"] == str(existing.company_id)


@pytest.mark.asyncio
async def test_create_account_direct_creates_and_commits() -> None:
    context = _context(channel="web")
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    account = SimpleNamespace(
        company_id=uuid4(),
        name="Acme",
        domain="acme.com",
        website="https://acme.com",
        industry="Software",
        linkedin_url=None,
    )

    with patch("src.agents.tools.crm.create_account.AccountsAccessService") as service_cls:
        service = MagicMock()
        service.find_accounts_by_domain = AsyncMock(return_value=[])
        service.create_account = AsyncMock(return_value=account)
        service_cls.return_value = service

        result = await create_account_direct(
            context=context,
            name=" Acme ",
            domain="https://www.acme.com",
            website=" https://acme.com ",
            industry=" Software ",
        )

    service.create_account.assert_awaited_once_with(
        name="Acme",
        domain="acme.com",
        website="https://acme.com",
        industry="Software",
    )
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"
    assert result["_bot_channel_payload"]["type"] == "ui_card"


@pytest.mark.asyncio
async def test_create_contact_direct_reuses_existing_email() -> None:
    existing = SimpleNamespace(
        contact_id=uuid4(),
        email="jane@acme.com",
        full_name="Jane Doe",
        job_title=None,
        company_name="Acme",
        company_id=uuid4(),
    )

    with patch("src.agents.tools.crm.create_contact.ContactsAccessService") as service_cls:
        service = MagicMock()
        service.find_contact_by_email = AsyncMock(return_value=existing)
        service_cls.return_value = service

        result = await create_contact_direct(
            context=_context(),
            email=" Jane@Acme.com ",
            name="Jane Doe",
        )

    service.create_contact.assert_not_called()
    assert result["status"] == "exists"
    assert result["contact_id"] == str(existing.contact_id)


@pytest.mark.asyncio
async def test_create_contact_direct_links_single_domain_account() -> None:
    company_id = uuid4()
    account = SimpleNamespace(company_id=company_id, name="Acme", domain="acme.com")
    contact = SimpleNamespace(
        contact_id=uuid4(),
        email="jane@acme.com",
        full_name="Jane Doe",
        job_title=None,
        company_name="Acme",
        company_id=company_id,
    )

    with patch("src.agents.tools.crm.create_contact.ContactsAccessService") as service_cls:
        service = MagicMock()
        service.find_contact_by_email = AsyncMock(return_value=None)
        service.find_accounts_by_domain = AsyncMock(return_value=[account])
        service.create_contact = AsyncMock(return_value=contact)
        service_cls.return_value = service

        result = await create_contact_direct(
            context=_context(),
            email="jane@acme.com",
            name="Jane Doe",
        )

    service.create_contact.assert_awaited_once_with(
        email="jane@acme.com",
        name="Jane Doe",
        company_id=company_id,
        company_name="Acme",
    )
    assert result["company_id"] == str(company_id)


@pytest.mark.asyncio
async def test_create_contact_direct_returns_account_selection_for_ambiguous_domain() -> None:
    account_one = SimpleNamespace(company_id=uuid4(), name="Acme US", domain="acme.com")
    account_two = SimpleNamespace(company_id=uuid4(), name="Acme EU", domain="acme.com")

    with patch("src.agents.tools.crm.create_contact.ContactsAccessService") as service_cls:
        service = MagicMock()
        service.find_contact_by_email = AsyncMock(return_value=None)
        service.find_accounts_by_domain = AsyncMock(return_value=[account_one, account_two])
        service_cls.return_value = service

        result = await create_contact_direct(
            context=_context(),
            email="jane@acme.com",
            name="Jane Doe",
        )

    service.create_contact.assert_not_called()
    assert result["status"] == "needs_account_selection"
    assert len(result["candidates"]) == 2


@pytest.mark.asyncio
async def test_create_contact_direct_can_link_contact_to_deal() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal_id = uuid4()
    contact = SimpleNamespace(
        contact_id=uuid4(),
        email="jane@acme.com",
        full_name="Jane Doe",
        job_title=None,
        company_name=None,
        company_id=None,
    )

    with (
        patch("src.agents.tools.crm.create_contact.ContactsAccessService") as service_cls,
        patch("src.agents.tools.crm.create_contact.send_deal_task_async", AsyncMock()) as dispatch,
    ):
        service = MagicMock()
        service.find_contact_by_email = AsyncMock(return_value=None)
        service.find_accounts_by_domain = AsyncMock(return_value=[])
        service.create_contact = AsyncMock(return_value=contact)
        service.add_contact_to_deal = AsyncMock(return_value={"created": [{"id": "p1"}]})
        service_cls.return_value = service

        result = await create_contact_direct(
            context=context,
            email="jane@acme.com",
            name="Jane Doe",
            deal_id=str(deal_id),
            deal_name="Acme Renewal",
        )

    org_session.commit.assert_awaited_once()
    dispatch.assert_awaited_once()
    assert result["deal_link_status"] == "linked"
    assert result["deal_id"] == str(deal_id)


@pytest.mark.asyncio
async def test_search_accounts_returns_slim_results() -> None:
    account_id = uuid4()
    with patch("src.agents.tools.crm.search_accounts.AccountsAccessService") as service_cls:
        service = MagicMock()
        service.list_accounts = AsyncMock(
            return_value=(
                [
                    {
                        "company_id": account_id,
                        "name": "Acme",
                        "domain": "acme.com",
                        "website": "https://acme.com",
                        "industry": "Software",
                        "contact_count": 3,
                        "deal_count": 2,
                    }
                ],
                1,
            )
        )
        service_cls.return_value = service

        result = await search_accounts(context=_context(), query="acme", limit=5)

    service.list_accounts.assert_awaited_once_with(page=1, limit=5, q="acme")
    assert result[0]["account_id"] == str(account_id)
    assert result[0]["contact_count"] == 3


@pytest.mark.asyncio
async def test_tool_factories_hide_channel_payloads() -> None:
    context = _context()
    contact_tool = create_create_contact(context, lambda: None)
    account_tool = create_create_account(context, lambda: None)

    with (
        patch("src.agents.tools.crm.create_contact.create_contact_direct") as contact_direct,
        patch("src.agents.tools.crm.create_account.create_account_direct") as account_direct,
    ):
        contact_direct.return_value = {
            "status": "created",
            "_bot_channel_payload": {"type": "message"},
        }
        account_direct.return_value = {
            "status": "created",
            "_bot_channel_payload": {"type": "message"},
        }

        contact_result = await contact_tool(email="jane@acme.com", name="Jane Doe")
        account_result = await account_tool(name="Acme")

    assert contact_result == {"status": "created"}
    assert account_result == {"status": "created"}
