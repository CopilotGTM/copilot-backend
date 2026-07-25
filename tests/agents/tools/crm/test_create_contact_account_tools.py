from __future__ import annotations

from inspect import signature
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.create_account import create_account_direct, create_create_account
from src.agents.tools.crm.create_contact import create_contact_direct, create_create_contact
from src.agents.tools.crm.search_accounts import search_accounts
from src.service.crm.accounts.contracts import (
    AccountCreateCommand,
    AccountWriteContext,
    AccountWriteResult,
)
from src.service.crm.contacts.contracts import (
    ContactCreateCommand,
    ContactDealLinkResult,
    ContactWriteResult,
)


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def contact_hydration_dispatch(monkeypatch) -> AsyncMock:
    dispatch = AsyncMock(return_value=["hydration-task"])
    monkeypatch.setattr(
        "src.agents.tools.crm.create_contact.dispatch_contact_communication_hydration_async",
        dispatch,
    )
    return dispatch


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

    with patch("src.agents.tools.crm.create_account.AccountReadService") as service_cls:
        reader = MagicMock()
        reader.find_accounts_by_domain = AsyncMock(return_value=[existing])
        service_cls.return_value = reader

        result = await create_account_direct(
            context=_context(),
            name="Acme",
            domain="https://www.acme.com/about",
        )

    reader.find_accounts_by_domain.assert_awaited_once_with("acme.com")
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

    with (
        patch("src.agents.tools.crm.create_account.AccountReadService") as reader_cls,
        patch("src.agents.tools.crm.create_account.AccountWriteService") as writer_cls,
    ):
        reader = MagicMock()
        reader.find_accounts_by_domain = AsyncMock(return_value=[])
        reader_cls.return_value = reader
        writer = MagicMock()
        writer.create_account = AsyncMock(
            return_value=AccountWriteResult(account=account, created=True, matched_by="created")
        )
        writer_cls.return_value = writer

        result = await create_account_direct(
            context=context,
            name=" Acme ",
            domain="https://www.acme.com",
            website=" https://acme.com ",
            industry=" Software ",
        )

    writer.create_account.assert_awaited_once_with(
        AccountCreateCommand(
            name="Acme",
            domain="acme.com",
            website="https://acme.com",
            industry="Software",
        ),
        context=AccountWriteContext(
            source="agent_tool",
            actor_user_id=context.user_id,
            created_via="chat_tool",
            duplicate_scope="internal_only",
        ),
    )
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"
    assert result["_bot_channel_payload"]["type"] == "ui_card"


@pytest.mark.asyncio
async def test_create_account_direct_allows_name_only_create() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    account = SimpleNamespace(
        company_id=uuid4(),
        name="Acme",
        domain=None,
        website=None,
        industry=None,
        linkedin_url=None,
    )

    with (
        patch("src.agents.tools.crm.create_account.AccountReadService") as reader_cls,
        patch("src.agents.tools.crm.create_account.AccountWriteService") as writer_cls,
    ):
        reader = MagicMock()
        reader.find_accounts_by_name = AsyncMock(return_value=[])
        reader_cls.return_value = reader
        writer = MagicMock()
        writer.create_account = AsyncMock(
            return_value=AccountWriteResult(account=account, created=True, matched_by="created")
        )
        writer_cls.return_value = writer

        result = await create_account_direct(context=context, name="Acme")

    reader.find_accounts_by_name.assert_awaited_once_with("Acme")
    writer.create_account.assert_awaited_once()
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_create_account_direct_allows_domain_only_create() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    account = SimpleNamespace(
        company_id=uuid4(),
        name="acme.com",
        domain="acme.com",
        website=None,
        industry=None,
        linkedin_url=None,
    )

    with (
        patch("src.agents.tools.crm.create_account.AccountReadService") as reader_cls,
        patch("src.agents.tools.crm.create_account.AccountWriteService") as writer_cls,
    ):
        reader = MagicMock()
        reader.find_accounts_by_domain = AsyncMock(return_value=[])
        reader_cls.return_value = reader
        writer = MagicMock()
        writer.create_account = AsyncMock(
            return_value=AccountWriteResult(account=account, created=True, matched_by="created")
        )
        writer_cls.return_value = writer

        result = await create_account_direct(context=context, name=None, domain="https://acme.com")

    reader.find_accounts_by_domain.assert_awaited_once_with("acme.com")
    writer.create_account.assert_awaited_once_with(
        AccountCreateCommand(name=None, domain="acme.com"),
        context=AccountWriteContext(
            source="agent_tool",
            actor_user_id=context.user_id,
            created_via="chat_tool",
            duplicate_scope="internal_only",
        ),
    )
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_create_account_direct_allows_linkedin_only_create() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    account = SimpleNamespace(
        company_id=uuid4(),
        name="Acme",
        domain="acme.com",
        website="https://acme.com",
        industry=None,
        linkedin_url="https://linkedin.com/company/acme",
    )

    with (
        patch("src.agents.tools.crm.create_account.AccountReadService") as reader_cls,
        patch("src.agents.tools.crm.create_account.AccountWriteService") as writer_cls,
    ):
        reader = MagicMock()
        reader.find_accounts_by_name = AsyncMock(return_value=[])
        reader_cls.return_value = reader
        writer = MagicMock()
        writer.create_account = AsyncMock(
            return_value=AccountWriteResult(account=account, created=True, matched_by="linkedin")
        )
        writer_cls.return_value = writer

        result = await create_account_direct(
            context=context,
            name=None,
            linkedin_url=" https://linkedin.com/company/acme ",
        )

    reader.find_accounts_by_name.assert_not_called()
    writer.create_account.assert_awaited_once_with(
        AccountCreateCommand(
            name=None,
            domain=None,
            website=None,
            linkedin_url="https://linkedin.com/company/acme",
        ),
        context=AccountWriteContext(
            source="agent_tool",
            actor_user_id=context.user_id,
            created_via="chat_tool",
            duplicate_scope="internal_only",
        ),
    )
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_create_account_direct_rejects_missing_identity() -> None:
    context = _context()

    result = await create_account_direct(context=context, name=None)

    assert result == {
        "status": "error",
        "message": "Account name, domain, website, or LinkedIn URL is required.",
    }
    context.acquire_session.assert_not_called()


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

    with (
        patch("src.agents.tools.crm.create_contact.ContactReadService") as reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactWriteService") as writer_cls,
    ):
        reader = MagicMock()
        reader.find_contact_by_email = AsyncMock(return_value=existing)
        reader_cls.return_value = reader
        writer = MagicMock()
        writer_cls.return_value = writer

        result = await create_contact_direct(
            context=_context(),
            email=" Jane@Acme.com ",
            name="Jane Doe",
        )

    writer.create_contact.assert_not_called()
    assert result["status"] == "exists"
    assert result["contact_id"] == str(existing.contact_id)


@pytest.mark.asyncio
async def test_create_contact_direct_does_not_resolve_account() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    company_id = uuid4()
    contact = SimpleNamespace(
        contact_id=uuid4(),
        email="jane@acme.com",
        full_name="Jane Doe",
        job_title=None,
        company_name="Acme",
        company_id=company_id,
    )

    with (
        patch("src.agents.tools.crm.create_contact.ContactReadService") as contact_reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactWriteService") as contact_writer_cls,
    ):
        contact_reader = MagicMock()
        contact_reader.find_contact_by_email = AsyncMock(return_value=None)
        contact_reader_cls.return_value = contact_reader
        contact_writer = MagicMock()
        contact_writer.create_contact = AsyncMock(
            return_value=ContactWriteResult(contact=contact, created=True, matched_by="created")
        )
        contact_writer_cls.return_value = contact_writer

        result = await create_contact_direct(
            context=context,
            email="jane@acme.com",
            name="Jane Doe",
        )

    contact_writer.create_contact.assert_awaited_once()
    command = contact_writer.create_contact.call_args.args[0]
    kwargs = contact_writer.create_contact.call_args.kwargs
    assert command == ContactCreateCommand(email="jane@acme.com", name="Jane Doe")
    assert "company_id" not in kwargs
    assert "company_name" not in kwargs
    org_session.commit.assert_awaited_once()
    assert result["company_id"] == str(company_id)


@pytest.mark.asyncio
async def test_create_contact_direct_allows_missing_name() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    contact = SimpleNamespace(
        contact_id=uuid4(),
        email="jane@acme.com",
        full_name="jane@acme.com",
        job_title=None,
        company_name=None,
        company_id=None,
    )

    with (
        patch("src.agents.tools.crm.create_contact.ContactReadService") as contact_reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactWriteService") as contact_writer_cls,
    ):
        contact_reader = MagicMock()
        contact_reader.find_contact_by_email = AsyncMock(return_value=None)
        contact_reader_cls.return_value = contact_reader
        contact_writer = MagicMock()
        contact_writer.create_contact = AsyncMock(
            return_value=ContactWriteResult(contact=contact, created=True, matched_by="created")
        )
        contact_writer_cls.return_value = contact_writer

        result = await create_contact_direct(
            context=context,
            email="jane@acme.com",
            name=None,
        )

    contact_writer.create_contact.assert_awaited_once()
    command = contact_writer.create_contact.call_args.args[0]
    assert command == ContactCreateCommand(email="jane@acme.com", name=None)
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_create_contact_direct_allows_linkedin_without_email() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    contact = SimpleNamespace(
        contact_id=uuid4(),
        email=None,
        full_name="Jane Buyer",
        job_title=None,
        company_name=None,
        company_id=None,
    )

    with (
        patch("src.agents.tools.crm.create_contact.ContactReadService") as contact_reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactWriteService") as contact_writer_cls,
    ):
        contact_reader = MagicMock()
        contact_reader.find_contact_by_email = AsyncMock(return_value=None)
        contact_reader_cls.return_value = contact_reader
        contact_writer = MagicMock()
        contact_writer.create_contact = AsyncMock(
            return_value=ContactWriteResult(contact=contact, created=True, matched_by="linkedin")
        )
        contact_writer_cls.return_value = contact_writer

        result = await create_contact_direct(
            context=context,
            email=None,
            name=None,
            linkedin_url="https://linkedin.com/in/jane-buyer",
        )

    contact_reader.find_contact_by_email.assert_not_called()
    contact_writer.create_contact.assert_awaited_once()
    command = contact_writer.create_contact.call_args.args[0]
    assert command == ContactCreateCommand(
        email=None,
        name=None,
        linkedin_url="https://linkedin.com/in/jane-buyer",
    )
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_create_contact_direct_allows_name_only_contact() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    contact = SimpleNamespace(
        contact_id=uuid4(),
        email=None,
        full_name="Jane Buyer",
        job_title=None,
        company_name=None,
        company_id=None,
    )

    with (
        patch("src.agents.tools.crm.create_contact.ContactReadService") as contact_reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactWriteService") as contact_writer_cls,
    ):
        contact_reader = MagicMock()
        contact_reader.find_contact_by_email = AsyncMock(return_value=None)
        contact_reader_cls.return_value = contact_reader
        contact_writer = MagicMock()
        contact_writer.create_contact = AsyncMock(
            return_value=ContactWriteResult(contact=contact, created=True, matched_by="created")
        )
        contact_writer_cls.return_value = contact_writer

        result = await create_contact_direct(context=context, email=None, name="Jane Buyer")

    contact_reader.find_contact_by_email.assert_not_called()
    command = contact_writer.create_contact.call_args.args[0]
    assert command == ContactCreateCommand(email=None, name="Jane Buyer")
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"


@pytest.mark.asyncio
async def test_create_contact_direct_rejects_missing_identity() -> None:
    context = _context()

    result = await create_contact_direct(context=context, email=None, name=None)

    assert result == {
        "status": "error",
        "message": "Contact name, email, or LinkedIn URL is required.",
    }
    context.acquire_session.assert_not_called()


def test_create_contact_direct_has_no_account_selection_inputs() -> None:
    params = signature(create_contact_direct).parameters

    assert "account_id" not in params
    assert "account_name" not in params
    assert "account_domain" not in params
    assert "phone" not in params


def test_create_account_direct_uses_minimal_create_inputs() -> None:
    params = signature(create_account_direct).parameters

    assert "name" in params
    assert "domain" in params
    assert "website" in params
    assert "industry" in params
    assert "linkedin_url" in params
    assert "phone" not in params
    assert "country" not in params
    assert "annual_revenue" not in params
    assert "number_of_employees" not in params


@pytest.mark.asyncio
async def test_create_contact_direct_does_not_block_on_ambiguous_account_domain() -> None:
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    org_session.rollback = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    contact = SimpleNamespace(
        contact_id=uuid4(),
        email="jane@acme.com",
        full_name="Jane Doe",
        job_title=None,
        company_name=None,
        company_id=None,
    )

    with (
        patch("src.agents.tools.crm.create_contact.ContactReadService") as contact_reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactWriteService") as contact_writer_cls,
    ):
        contact_reader = MagicMock()
        contact_reader.find_contact_by_email = AsyncMock(return_value=None)
        contact_reader_cls.return_value = contact_reader
        contact_writer = MagicMock()
        contact_writer.create_contact = AsyncMock(
            return_value=ContactWriteResult(contact=contact, created=True, matched_by="created")
        )
        contact_writer_cls.return_value = contact_writer

        result = await create_contact_direct(
            context=context,
            email="jane@acme.com",
            name="Jane Doe",
        )

    contact_writer.create_contact.assert_awaited_once()
    kwargs = contact_writer.create_contact.call_args.kwargs
    assert "company_id" not in kwargs
    assert "company_name" not in kwargs
    assert result["status"] == "created"


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
        patch("src.agents.tools.crm.create_contact.ContactReadService") as contact_reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactWriteService") as contact_writer_cls,
        patch("src.agents.tools.crm.create_contact.ContactDealLinkService") as linker_cls,
        patch("src.agents.tools.crm.create_contact.send_deal_task_async", AsyncMock()) as dispatch,
    ):
        contact_reader = MagicMock()
        contact_reader.find_contact_by_email = AsyncMock(return_value=None)
        contact_reader_cls.return_value = contact_reader
        contact_writer = MagicMock()
        contact_writer.create_contact = AsyncMock(
            return_value=ContactWriteResult(contact=contact, created=True, matched_by="created")
        )
        contact_writer_cls.return_value = contact_writer
        linker = MagicMock()
        linker.add_contact_to_deal = AsyncMock(
            return_value=ContactDealLinkResult(
                created=[{"id": "p1"}],
                skipped=[],
                contact_participant_changed=True,
            )
        )
        linker_cls.return_value = linker

        result = await create_contact_direct(
            context=context,
            email="jane@acme.com",
            name="Jane Doe",
            deal_id=str(deal_id),
            deal_name="Acme Renewal",
        )

    # Contact creation and deal linking are one transaction; hydration starts only after it commits.
    assert org_session.commit.await_count == 1
    dispatch.assert_awaited_once_with(
        "stakeholder.sync_deal_stakeholders",
        kwargs={
            "organisation_id": str(context.organisation_id),
            "deal_id": str(deal_id),
            "change_type": "PARTICIPANT_ADDED",
            "integration_id": None,
            "integration_type": None,
            "trigger_rematching": True,
        },
    )
    assert result["deal_link_status"] == "linked"
    assert result["deal_id"] == str(deal_id)


@pytest.mark.asyncio
async def test_search_accounts_returns_slim_results() -> None:
    account_id = uuid4()
    with patch("src.agents.tools.crm.search_accounts.AccountReadService") as service_cls:
        service = MagicMock()
        service.search_accounts_for_agent = AsyncMock(
            return_value=[
                {
                    "company": SimpleNamespace(
                        company_id=account_id,
                        name="Acme",
                        domain="acme.com",
                        website="https://acme.com",
                        industry="Software",
                    ),
                    "contact_count": 3,
                    "deal_count": 2,
                    "match": {
                        "field": "name",
                        "value": "Acme",
                        "type": "exact",
                        "score": 2.0,
                    },
                }
            ]
        )
        service_cls.return_value = service

        result = await search_accounts(context=_context(), query="acme", limit=5)

    service.search_accounts_for_agent.assert_awaited_once_with(q="acme", limit=5)
    assert result[0]["account_id"] == str(account_id)
    assert result[0]["contact_count"] == 3
    assert result[0]["match"]["field"] == "name"


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
