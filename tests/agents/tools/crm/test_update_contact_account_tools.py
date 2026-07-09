"""Regression tests for CRM contact/account update agent tools.

These tests lock the agent-specific safety contract: update tools reuse the
shared write services, but only after validating IDs, field allow-lists, tenant
context, and internal-CRM ownership.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.update_account import update_account_direct
from src.agents.tools.crm.update_contact import update_contact_direct
from src.agents.tools.registry import ToolName, ToolRegistry
from src.service.crm.accounts.contracts import AccountWriteServiceError
from src.service.crm.contacts.contracts import ContactWriteResult, ContactWriteServiceError


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _context(*, organisation_id: UUID = UUID("11111111-1111-1111-1111-111111111111")):
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    context = AgentContext(
        organisation_id=organisation_id,
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        user_email="owner@example.com",
        channel="web",
        metadata={"user_name": "Owner User"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    return context, session


def _contact(**overrides: object) -> SimpleNamespace:
    data = {
        "contact_id": uuid4(),
        "organisation_id": UUID("11111111-1111-1111-1111-111111111111"),
        "integration_id": UUID("33333333-3333-3333-3333-333333333333"),
        "is_deleted": False,
        "email": "jane@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "full_name": "Jane Doe",
        "job_title": "VP Sales",
        "phone": None,
        "mobile_phone": None,
        "linkedin_url": None,
        "company_id": None,
        "company_name": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _account(**overrides: object) -> SimpleNamespace:
    data = {
        "company_id": uuid4(),
        "organisation_id": UUID("11111111-1111-1111-1111-111111111111"),
        "integration_id": UUID("33333333-3333-3333-3333-333333333333"),
        "is_deleted": False,
        "name": "Acme",
        "domain": "acme.com",
        "website": "https://acme.com",
        "industry": "Software",
        "lifecycle_stage": None,
        "company_type": None,
        "description": None,
        "phone": None,
        "linkedin_url": None,
        "number_of_employees": None,
        "annual_revenue": None,
        "city": None,
        "state": None,
        "country": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_update_contact_direct_calls_write_service_and_commits() -> None:
    """Contact updates should pass normalized fields to the shared writer and commit once."""
    context, session = _context()
    contact = _contact(job_title="Chief Revenue Officer")
    session.get = AsyncMock(return_value=contact)

    with (
        patch("src.agents.tools.crm.update_contact.ContactWriteService") as service_cls,
        patch("src.agents.tools.crm.update_contact.InternalCRMSetupService") as setup_cls,
    ):
        service = MagicMock()
        service.update_contact = AsyncMock(
            return_value=ContactWriteResult(contact, False, "explicit_id")
        )
        service_cls.return_value = service
        setup = MagicMock()
        setup.get_internal_integration = AsyncMock(
            return_value=SimpleNamespace(integration_id=contact.integration_id)
        )
        setup_cls.return_value = setup

        result = await update_contact_direct(
            context=context,
            contact_id=str(contact.contact_id),
            fields={"job_title": " Chief Revenue Officer "},
        )

    service.update_contact.assert_awaited_once()
    args, kwargs = service.update_contact.call_args
    assert args[0] == contact.contact_id
    assert args[1] == {"job_title": "Chief Revenue Officer"}
    assert kwargs["context"].source == "agent_tool"
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    assert result["status"] == "updated"
    assert result["contact_id"] == str(contact.contact_id)
    assert result["updated_fields"] == {"job_title": "Chief Revenue Officer"}


@pytest.mark.asyncio
async def test_update_contact_direct_rejects_external_provider_contact() -> None:
    """Agent contact updates must not mutate provider-synced contact snapshots."""
    context, session = _context()
    contact = _contact(integration_id=UUID("44444444-4444-4444-4444-444444444444"))
    session.get = AsyncMock(return_value=contact)

    with (
        patch("src.agents.tools.crm.update_contact.ContactWriteService") as service_cls,
        patch("src.agents.tools.crm.update_contact.InternalCRMSetupService") as setup_cls,
    ):
        service = MagicMock()
        service.update_contact = AsyncMock()
        service_cls.return_value = service
        setup = MagicMock()
        setup.get_internal_integration = AsyncMock(
            return_value=SimpleNamespace(
                integration_id=UUID("33333333-3333-3333-3333-333333333333")
            )
        )
        setup_cls.return_value = setup

        result = await update_contact_direct(
            context=context,
            contact_id=str(contact.contact_id),
            fields={"job_title": "VP Sales"},
        )

    service.update_contact.assert_not_awaited()
    session.commit.assert_not_awaited()
    assert result == {
        "status": "error",
        "message": "Agent contact updates are limited to internal CRM contacts.",
    }


@pytest.mark.asyncio
async def test_update_contact_direct_rejects_missing_context_and_unknown_fields() -> None:
    """Contact updates should fail before opening a DB session when scope or fields are invalid."""
    missing_context = SimpleNamespace(organisation_id=None, acquire_session=MagicMock())
    result = await update_contact_direct(
        context=missing_context,
        contact_id=str(uuid4()),
        fields={"job_title": "VP Sales"},
    )
    assert result["status"] == "error"
    assert "organization" in result["message"]
    missing_context.acquire_session.assert_not_called()

    context, session = _context()
    result = await update_contact_direct(
        context=context,
        contact_id=str(uuid4()),
        fields={"company_name": "Acme"},
    )
    assert result == {
        "status": "error",
        "message": "Unsupported contact update fields: company_name",
    }
    context.acquire_session.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_contact_direct_rolls_back_on_service_error() -> None:
    """Expected contact writer failures should roll back and return tool-safe errors."""
    context, session = _context()
    contact_id = uuid4()
    contact = _contact(contact_id=contact_id)
    session.get = AsyncMock(return_value=contact)

    with (
        patch("src.agents.tools.crm.update_contact.ContactWriteService") as service_cls,
        patch("src.agents.tools.crm.update_contact.InternalCRMSetupService") as setup_cls,
    ):
        service = MagicMock()
        service.update_contact = AsyncMock(
            side_effect=ContactWriteServiceError("Contact not found", status_code=404)
        )
        service_cls.return_value = service
        setup = MagicMock()
        setup.get_internal_integration = AsyncMock(
            return_value=SimpleNamespace(integration_id=contact.integration_id)
        )
        setup_cls.return_value = setup

        result = await update_contact_direct(
            context=context,
            contact_id=str(contact_id),
            fields={"job_title": "VP Sales"},
        )

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert result == {"status": "error", "message": "Could not update contact: Contact not found"}


@pytest.mark.asyncio
async def test_update_account_direct_calls_write_service_and_commits() -> None:
    """Account updates should pass normalized fields to the shared writer and commit once."""
    context, session = _context()
    account = _account(industry="Healthcare")
    session.get = AsyncMock(return_value=account)

    with (
        patch("src.agents.tools.crm.update_account.AccountWriteService") as service_cls,
        patch("src.agents.tools.crm.update_account.InternalCRMSetupService") as setup_cls,
    ):
        service = MagicMock()
        service.update_account = AsyncMock(return_value=account)
        service_cls.return_value = service
        setup = MagicMock()
        setup.get_internal_integration = AsyncMock(
            return_value=SimpleNamespace(integration_id=account.integration_id)
        )
        setup_cls.return_value = setup

        result = await update_account_direct(
            context=context,
            account_id=str(account.company_id),
            fields={"industry": " Healthcare ", "domain": " https://www.acme.com/path "},
        )

    service.update_account.assert_awaited_once()
    args, kwargs = service.update_account.call_args
    assert args[0] == account.company_id
    assert args[1] == {"industry": "Healthcare", "domain": "https://www.acme.com/path"}
    assert kwargs["context"].source == "agent_tool"
    assert kwargs["context"].duplicate_scope == "strict_org"
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    assert result["status"] == "updated"
    assert result["account_id"] == str(account.company_id)
    assert result["updated_fields"] == {
        "industry": "Healthcare",
        "domain": "https://www.acme.com/path",
    }


@pytest.mark.asyncio
async def test_update_account_direct_rejects_external_provider_account() -> None:
    """Agent account updates must not mutate provider-synced account snapshots."""
    context, session = _context()
    account = _account(integration_id=UUID("44444444-4444-4444-4444-444444444444"))
    session.get = AsyncMock(return_value=account)

    with (
        patch("src.agents.tools.crm.update_account.AccountWriteService") as service_cls,
        patch("src.agents.tools.crm.update_account.InternalCRMSetupService") as setup_cls,
    ):
        service = MagicMock()
        service.update_account = AsyncMock()
        service_cls.return_value = service
        setup = MagicMock()
        setup.get_internal_integration = AsyncMock(
            return_value=SimpleNamespace(
                integration_id=UUID("33333333-3333-3333-3333-333333333333")
            )
        )
        setup_cls.return_value = setup

        result = await update_account_direct(
            context=context,
            account_id=str(account.company_id),
            fields={"industry": "Software"},
        )

    service.update_account.assert_not_awaited()
    session.commit.assert_not_awaited()
    assert result == {
        "status": "error",
        "message": "Agent account updates are limited to internal CRM accounts.",
    }


@pytest.mark.asyncio
async def test_update_account_direct_rejects_missing_context_and_unknown_fields() -> None:
    """Account updates should fail before opening a DB session when scope or fields are invalid."""
    missing_context = SimpleNamespace(organisation_id=None, acquire_session=MagicMock())
    result = await update_account_direct(
        context=missing_context,
        account_id=str(uuid4()),
        fields={"industry": "Software"},
    )
    assert result["status"] == "error"
    assert "organization" in result["message"]
    missing_context.acquire_session.assert_not_called()

    context, session = _context()
    result = await update_account_direct(
        context=context,
        account_id=str(uuid4()),
        fields={"owner_name": "Someone"},
    )
    assert result == {
        "status": "error",
        "message": "Unsupported account update fields: owner_name",
    }
    context.acquire_session.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_account_direct_rolls_back_on_service_error() -> None:
    """Expected account writer failures should roll back and return tool-safe errors."""
    context, session = _context()
    account_id = uuid4()
    account = _account(company_id=account_id)
    session.get = AsyncMock(return_value=account)

    with (
        patch("src.agents.tools.crm.update_account.AccountWriteService") as service_cls,
        patch("src.agents.tools.crm.update_account.InternalCRMSetupService") as setup_cls,
    ):
        service = MagicMock()
        service.update_account = AsyncMock(
            side_effect=AccountWriteServiceError("Account not found", status_code=404)
        )
        service_cls.return_value = service
        setup = MagicMock()
        setup.get_internal_integration = AsyncMock(
            return_value=SimpleNamespace(integration_id=account.integration_id)
        )
        setup_cls.return_value = setup

        result = await update_account_direct(
            context=context,
            account_id=str(account_id),
            fields={"industry": "Software"},
        )

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert result == {"status": "error", "message": "Could not update account: Account not found"}


def test_general_registry_exposes_contact_and_account_update_tools() -> None:
    """GeneralAgent should get update tools while VoiceRealtimeAgent stays write-conservative."""
    tools = ToolRegistry.get_available_tools_for_agent("GeneralAgent")
    voice_tools = ToolRegistry.get_available_tools_for_agent("VoiceRealtimeAgent")

    assert ToolName.UPDATE_CONTACT in tools
    assert ToolName.UPDATE_ACCOUNT in tools
    assert ToolName.UPDATE_CONTACT not in voice_tools
    assert ToolName.UPDATE_ACCOUNT not in voice_tools
