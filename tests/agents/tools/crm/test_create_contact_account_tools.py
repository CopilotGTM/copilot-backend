"""Regression tests for test create contact account tools; these cases lock the backend contract used by the changed feature."""

from __future__ import annotations

from inspect import signature
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.access import AgentCRMAccessError
from src.agents.tools.crm.create_account import create_account_direct, create_create_account
from src.agents.tools.crm.create_contact import create_contact_direct, create_create_contact
from src.agents.tools.crm.search_accounts import search_accounts
from src.service.crm.access.record_access_policy import CRMAccessActor
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
    """Build the contact hydration dispatch fixture used by the surrounding regression cases."""
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
    """Verify the contract that create account direct reuses existing domain account."""
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
    """Verify the contract that create account direct creates and commits."""
    context = _context(channel="web")
    caused_by_run_id = uuid4()
    context.metadata.update(
        {
            "automation_caused_by_run_id": str(caused_by_run_id),
            "automation_causation_depth": 2,
            "automation_correlation_id": "automation-chain-1",
        }
    )
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
            correlation_id="automation-chain-1",
            caused_by_run_id=caused_by_run_id,
            causation_depth=2,
        ),
    )
    org_session.commit.assert_awaited_once()
    assert result["status"] == "created"
    assert result["_bot_channel_payload"]["type"] == "ui_card"


@pytest.mark.asyncio
async def test_create_account_direct_allows_name_only_create() -> None:
    """Verify the contract that create account direct allows name only create."""
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
    """Verify the contract that create account direct allows domain only create."""
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
    """Verify the contract that create account direct allows linkedin only create."""
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
    """Verify the contract that create account direct rejects missing identity."""
    context = _context()

    result = await create_account_direct(context=context, name=None)

    assert result == {
        "status": "error",
        "message": "Account name, domain, website, or LinkedIn URL is required.",
    }
    context.acquire_session.assert_not_called()


@pytest.mark.asyncio
async def test_create_contact_direct_reuses_existing_email() -> None:
    """Verify the contract that create contact direct reuses existing email."""
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
    """Verify the contract that create contact direct does not resolve account."""
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
    """Verify the contract that create contact direct allows missing name."""
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
    """Verify the contract that create contact direct allows linkedin without email."""
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
    """Verify the contract that create contact direct allows name only contact."""
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
    """Verify the contract that create contact direct rejects missing identity."""
    context = _context()

    result = await create_contact_direct(context=context, email=None, name=None)

    assert result == {
        "status": "error",
        "message": "Contact name, email, or LinkedIn URL is required.",
    }
    context.acquire_session.assert_not_called()


def test_create_contact_direct_has_no_account_selection_inputs() -> None:
    """Verify the contract that create contact direct has no account selection inputs."""
    params = signature(create_contact_direct).parameters

    assert "account_id" not in params
    assert "account_name" not in params
    assert "account_domain" not in params
    assert "phone" not in params


def test_create_account_direct_uses_minimal_create_inputs() -> None:
    """Verify the contract that create account direct uses minimal create inputs."""
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
    """Verify the contract that create contact direct does not block on ambiguous account domain."""
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
    """Verify the contract that create contact direct can link contact to deal."""
    context = _context()
    org_session = MagicMock()
    org_session.commit = AsyncMock()
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(org_session))
    deal_id = uuid4()
    access_actor = CRMAccessActor(context.user_id)
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
        patch(
            "src.agents.tools.crm.create_contact.require_agent_deal_access",
            AsyncMock(return_value=access_actor),
            create=True,
        ) as require_access,
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
    require_access.assert_awaited_once_with(context, org_session, deal_id, "control")
    linker_cls.assert_called_once_with(org_session, context.organisation_id, access_actor)
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
async def test_create_contact_direct_rejects_deal_without_control_access() -> None:
    """Contact creation must roll back when the actor cannot control the selected deal."""
    context = _context()
    org_session = MagicMock()
    org_session.rollback = AsyncMock()
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
        patch("src.agents.tools.crm.create_contact.ContactReadService") as reader_cls,
        patch("src.agents.tools.crm.create_contact.ContactDealLinkService") as linker_cls,
        patch(
            "src.agents.tools.crm.create_contact.require_agent_deal_access",
            AsyncMock(side_effect=AgentCRMAccessError("forbidden")),
            create=True,
        ),
    ):
        reader = MagicMock()
        reader.find_contact_by_email = AsyncMock(return_value=contact)
        reader_cls.return_value = reader

        result = await create_contact_direct(
            context=context,
            email=contact.email,
            name=contact.full_name,
            deal_id=str(deal_id),
        )

    assert result == {"status": "error", "message": "Deal not found."}
    reader.find_contact_by_email.assert_not_awaited()
    linker_cls.assert_not_called()
    org_session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_accounts_returns_slim_results() -> None:
    """Verify the contract that search accounts returns slim results."""
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
    """Verify the contract that tool factories hide channel payloads."""
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
