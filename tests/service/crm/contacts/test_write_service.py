from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.service.crm.accounts.contracts import AccountWriteServiceError
from src.service.crm.contacts.contracts import (
    ContactCreateCommand,
    ContactWriteContext,
    ContactWriteServiceError,
)
from src.service.crm.contacts.write_service import ContactWriteService


class _FakeResult:
    def __init__(self, *, scalars=None):
        self._scalars = scalars or []

    def scalars(self):
        return self

    def first(self):
        return self._scalars[0] if self._scalars else None


def _session_with_integration(integration: KnowledgeIntegration):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_create_contact_with_explicit_company_id_skips_email_domain_account_resolution():
    org_id = uuid4()
    company_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    company = CRMCompany(
        company_id=company_id,
        organisation_id=org_id,
        integration_id=integration.integration_id,
        name="Selected Account",
        domain="selected.example",
    )
    session = _session_with_integration(integration)
    service = ContactWriteService(session, org_id)
    service._account_writer.load_org_company = AsyncMock(return_value=company)
    service._account_writer.resolve_or_create_account_for_contact_email_with_result = AsyncMock()
    service._projection.upsert_contact_created = AsyncMock()

    result = await service.create_contact(
        ContactCreateCommand(
            email="buyer@different.example",
            name="Buyer Person",
            company_id=company_id,
        ),
        context=ContactWriteContext(source="crm_import"),
    )

    assert result.contact.company_id == company_id
    assert result.contact.company_name == "Selected Account"
    assert result.created_account_id is None
    service._account_writer.load_org_company.assert_awaited_once_with(company_id)
    service._account_writer.resolve_or_create_account_for_contact_email_with_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_contact_with_invalid_company_id_uses_account_loader_error():
    org_id = uuid4()
    company_id = uuid4()
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult())
    service = ContactWriteService(session, org_id)
    service._account_writer.load_org_company = AsyncMock(
        side_effect=AccountWriteServiceError("Account not found", status_code=404)
    )
    service._account_writer.resolve_or_create_account_for_contact_email_with_result = AsyncMock()

    with pytest.raises(ContactWriteServiceError) as exc_info:
        await service.create_contact(
            ContactCreateCommand(
                email="buyer@example.com",
                name="Buyer Person",
                company_id=company_id,
            ),
            context=ContactWriteContext(source="crm_import"),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.message == "Account not found"
    service._account_writer.load_org_company.assert_awaited_once_with(company_id)
    service._account_writer.resolve_or_create_account_for_contact_email_with_result.assert_not_awaited()
