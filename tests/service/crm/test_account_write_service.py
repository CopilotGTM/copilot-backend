from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.integration_models import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.crm.accounts.contracts import (
    AccountWriteContext,
    AccountWriteServiceError,
)
from src.service.crm.accounts.write_service import AccountWriteService


def _result(first=None, all_rows: list[object] | None = None):
    scalars = MagicMock()
    scalars.first.return_value = first
    scalars.all.return_value = (
        all_rows if all_rows is not None else ([] if first is None else [first])
    )
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _integration(organisation_id):
    return KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        created_by_admin=False,
        integration_type=IntegrationType.INTERNAL_CRM,
        integration_scope=IntegrationScope.ORGANIZATION,
        name="Copilot CRM",
        credentials={},
        connection_metadata={},
        is_active=True,
    )


def _company(organisation_id, integration_id, *, name="Acme", domain="acme.com"):
    return CRMCompany(
        company_id=uuid4(),
        organisation_id=organisation_id,
        integration_id=integration_id,
        name=name,
        domain=domain,
        is_deleted=False,
    )


@pytest.mark.asyncio
async def test_create_account_rejects_duplicate_domain() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = _company(organisation_id, integration_id)
    session = SimpleNamespace(execute=AsyncMock(return_value=_result(all_rows=[existing])))
    service = AccountWriteService(session, organisation_id)

    with pytest.raises(AccountWriteServiceError) as exc_info:
        await service.create_account(name="Acme", domain="https://www.acme.com")

    assert exc_info.value.status_code == 409
    assert exc_info.value.data == {"company_id": str(existing.company_id)}


@pytest.mark.asyncio
async def test_create_account_rejects_duplicate_name_when_web_identity_missing() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = _company(organisation_id, integration_id, domain=None)
    session = SimpleNamespace(execute=AsyncMock(return_value=_result(all_rows=[existing])))
    service = AccountWriteService(session, organisation_id)

    with pytest.raises(AccountWriteServiceError) as exc_info:
        await service.create_account(name="Acme")

    assert exc_info.value.status_code == 409
    assert exc_info.value.data == {"company_id": str(existing.company_id)}


@pytest.mark.asyncio
async def test_create_account_allows_name_only_when_no_duplicate_exists() -> None:
    organisation_id = uuid4()
    integration = _integration(organisation_id)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(all_rows=[]), _result(first=integration)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    result = await service.create_account(name=" Acme ")

    assert result.account.name == "Acme"
    assert result.account.domain is None
    assert result.created is True
    session.add.assert_any_call(result.account)


@pytest.mark.asyncio
async def test_create_account_derives_name_from_domain_when_name_missing() -> None:
    organisation_id = uuid4()
    integration = _integration(organisation_id)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(all_rows=[]), _result(first=integration)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    result = await service.create_account(name=None, domain="https://www.acme.com/path")

    assert result.account.name == "acme.com"
    assert result.account.domain == "acme.com"


@pytest.mark.asyncio
async def test_resolve_or_create_account_for_contact_email_reuses_single_domain_account() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    existing = _company(organisation_id, integration_id)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_result(all_rows=[existing])),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    account = await service.resolve_or_create_account_for_contact_email(" Jane@Acme.com ")

    assert account is existing
    session.add.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_or_create_account_for_contact_email_creates_missing_domain_account() -> None:
    organisation_id = uuid4()
    integration = _integration(organisation_id)
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _result(all_rows=[]),
                _result(all_rows=[]),
                _result(first=integration),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    account = await service.resolve_or_create_account_for_contact_email(
        "jane@acme.com",
        context=AccountWriteContext(source="contact_create"),
    )

    assert account is not None
    assert account.name == "acme.com"
    assert account.domain == "acme.com"
    session.add.assert_any_call(account)
    session.flush.assert_awaited()
    session.refresh.assert_awaited_once_with(account)


@pytest.mark.asyncio
async def test_resolve_or_create_account_for_contact_email_returns_none_for_ambiguous_domain() -> (
    None
):
    organisation_id = uuid4()
    integration_id = uuid4()
    first = _company(organisation_id, integration_id, name="Acme US", domain="acme.com")
    second = _company(organisation_id, integration_id, name="Acme EU", domain="acme.com")
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_result(all_rows=[first, second])),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    account = await service.resolve_or_create_account_for_contact_email("jane@acme.com")

    assert account is None
    session.add.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_or_create_account_for_contact_email_returns_none_without_domain() -> None:
    service = AccountWriteService(SimpleNamespace(), uuid4())

    account = await service.resolve_or_create_account_for_contact_email("not-an-email")

    assert account is None


@pytest.mark.asyncio
async def test_resolve_or_create_account_for_contact_email_skips_free_email_subdomain() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, uuid4())

    account = await service.resolve_or_create_account_for_contact_email(
        "nickelsinglezerofive@enter.gmail.com"
    )

    assert account is None
    session.execute.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_account_normalizes_domain() -> None:
    organisation_id = uuid4()
    integration = _integration(organisation_id)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(all_rows=[]), _result(first=integration)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    result = await service.create_account(
        name="Acme",
        domain="https://www.ACME.com/about",
        context=AccountWriteContext(source="api"),
    )

    assert result.created is True
    assert result.matched_by == "created"
    assert result.account.domain == "acme.com"
    session.add.assert_any_call(result.account)
    session.flush.assert_awaited()
    session.refresh.assert_awaited_once_with(result.account)


@pytest.mark.asyncio
async def test_create_account_derives_domain_from_website() -> None:
    organisation_id = uuid4()
    integration = _integration(organisation_id)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(all_rows=[]), _result(first=integration)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    result = await service.create_account(
        name="Acme",
        website="https://www.acme.com/about",
        context=AccountWriteContext(source="api"),
    )

    assert result.account.domain == "acme.com"
    assert result.account.website == "https://www.acme.com/about"


@pytest.mark.asyncio
async def test_create_account_derives_name_from_website_when_name_missing() -> None:
    organisation_id = uuid4()
    integration = _integration(organisation_id)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(all_rows=[]), _result(first=integration)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    result = await service.create_account(name=None, website="https://www.acme.com/about")

    assert result.account.name == "acme.com"
    assert result.account.domain == "acme.com"
    assert result.account.website == "https://www.acme.com/about"


@pytest.mark.asyncio
async def test_create_account_rejects_missing_name_domain_and_website() -> None:
    organisation_id = uuid4()
    service = AccountWriteService(SimpleNamespace(), organisation_id)

    with pytest.raises(AccountWriteServiceError) as exc_info:
        await service.create_account(name=None)

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Account name, domain, or website is required"


@pytest.mark.asyncio
async def test_update_account_rejects_cross_org_or_deleted_account() -> None:
    organisation_id = uuid4()
    session = SimpleNamespace(execute=AsyncMock(return_value=_result(first=None)))
    service = AccountWriteService(session, organisation_id)

    with pytest.raises(AccountWriteServiceError) as exc_info:
        await service.update_account(uuid4(), {"name": "Renamed"})

    assert exc_info.value.status_code == 404
    assert exc_info.value.message == "Account not found"


@pytest.mark.asyncio
async def test_update_account_ignores_unknown_fields() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    company = _company(organisation_id, integration_id)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(first=company), _result(first=None)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    updated = await service.update_account(
        company.company_id,
        {"name": "Acme Corp", "domain": "https://www.acme.com/path", "unknown": "ignored"},
    )

    assert updated is company
    assert company.name == "Acme Corp"
    assert company.domain == "acme.com"
    assert not hasattr(company, "unknown")
    session.add.assert_any_call(company)
    session.flush.assert_awaited()
    session.refresh.assert_awaited_once_with(company)


@pytest.mark.asyncio
async def test_update_account_rejects_domain_owned_by_another_account() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    company = _company(organisation_id, integration_id, name="Acme", domain="acme.com")
    other = _company(organisation_id, integration_id, name="Beta", domain="beta.com")
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(first=company), _result(first=other)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    with pytest.raises(AccountWriteServiceError) as exc_info:
        await service.update_account(company.company_id, {"domain": "https://www.beta.com"})

    assert exc_info.value.status_code == 409
    assert exc_info.value.data == {"company_id": str(other.company_id)}
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_account_allows_keeping_own_domain() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    company = _company(organisation_id, integration_id, name="Acme", domain="acme.com")
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(first=company), _result(first=None)]),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = AccountWriteService(session, organisation_id)

    updated = await service.update_account(company.company_id, {"domain": "https://www.acme.com"})

    assert updated is company
    assert company.domain == "acme.com"
    session.add.assert_called_once_with(company)
