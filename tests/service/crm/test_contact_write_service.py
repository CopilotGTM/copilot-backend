from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.service.crm.accounts.contracts import AccountWriteResult
from src.service.crm.contacts.contracts import (
    ContactCreateCommand,
    ContactWriteContext,
    ContactWriteServiceError,
)
from src.service.crm.contacts.resolution_service import ContactIdentityResolutionResult
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.internal.internal_crm_setup_service import AUTOMATED_PIPELINE_USER_ID


def test_contact_create_command_supports_import_location_fields() -> None:
    assert {field.name for field in fields(ContactCreateCommand)} == {
        "email",
        "name",
        "job_title",
        "phone",
        "mobile_phone",
        "website",
        "address",
        "city",
        "state",
        "country",
        "postal_code",
        "linkedin_url",
        "company_id",
    }


class _FakeResult:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = scalars or []

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def first(self):
        return self._scalars[0] if self._scalars else None

    def all(self):
        return self._scalars


def _disable_account_resolution(service: ContactWriteService) -> None:
    service._account_writer.resolve_or_create_account_for_contact_email_with_result = AsyncMock(
        return_value=None
    )


def _disable_contact_created_projection(service: ContactWriteService) -> None:
    service._projection.upsert_contact_created = (  # type: ignore[method-assign]
        AsyncMock()
    )


@pytest.mark.asyncio
async def test_create_contact_normalizes_email_splits_name_and_does_not_commit() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    result = await service.create_contact(
        ContactCreateCommand(
            email=" Jane@Example.COM ",
            name="Jane Doe",
            mobile_phone=" +1 555 2000 ",
            website=" https://jane.example ",
            address=" 2 Main St ",
            city=" Brooklyn ",
            state=" NY ",
            country=" US ",
            postal_code=" 11201 ",
        ),
        context=ContactWriteContext(source="test"),
    )

    assert result.created is True
    assert result.contact.email == "jane@example.com"
    assert result.contact.first_name == "Jane"
    assert result.contact.last_name == "Doe"
    assert result.contact.mobile_phone == "+1 555 2000"
    assert result.contact.website == "https://jane.example"
    assert result.contact.address == "2 Main St"
    assert result.contact.city == "Brooklyn"
    assert result.contact.state == "NY"
    assert result.contact.country == "US"
    assert result.contact.postal_code == "11201"
    added_rows = [call.args[0] for call in session.add.call_args_list]
    assert any(isinstance(row, CRMContact) for row in added_rows)
    assert any(isinstance(row, CRMDealMutationEvent) for row in added_rows)
    projection = service._projection.upsert_contact_created
    projection.assert_awaited_once_with(result.contact)  # type: ignore[attr-defined]
    session.flush.assert_awaited()
    session.refresh.assert_awaited_once_with(result.contact)
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_create_contact_assigns_actor_owner_when_not_system_user() -> None:
    org_id = uuid4()
    actor_user_id = uuid4()
    crm_user_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            _FakeResult(),
            _FakeResult(scalars=[integration]),
        ]
    )
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)
    service._resolve_actor_crm_user = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(user_id=crm_user_id)
    )

    result = await service.create_contact(
        ContactCreateCommand(email="buyer@example.com"),
        context=ContactWriteContext(
            source="api",
            actor_user_id=actor_user_id,
            actor_name="Jane Owner",
            actor_email="jane@example.com",
        ),
    )

    assert result.contact.owner_id == crm_user_id
    assert result.contact.owner_name == "Jane Owner"
    assert result.contact.owner_email == "jane@example.com"
    service._resolve_actor_crm_user.assert_awaited_once_with(actor_user_id)  # type: ignore[attr-defined]
    added_rows = [call.args[0] for call in session.add.call_args_list]
    assert any(row is result.contact for row in added_rows)
    assert not any(row.__class__.__name__ == "CRMUser" for row in added_rows)


@pytest.mark.asyncio
async def test_create_contact_skips_system_actor_owner() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    result = await service.create_contact(
        ContactCreateCommand(email="buyer@example.com"),
        context=ContactWriteContext(source="api", actor_user_id=AUTOMATED_PIPELINE_USER_ID),
    )

    assert result.contact.owner_id is None


@pytest.mark.asyncio
async def test_create_contact_gets_internal_integration_from_setup_service(monkeypatch) -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    setup_calls = []

    class FakeSetupService:
        def __init__(self, session_arg, organisation_id_arg, user_id_arg):
            setup_calls.append((session_arg, organisation_id_arg, user_id_arg))

        async def get_or_create_internal_integration(self):
            return integration

    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult())
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    monkeypatch.setattr(
        "src.service.crm.contacts.write_service.InternalCRMSetupService",
        FakeSetupService,
    )

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    await service.create_contact(ContactCreateCommand(email="jane@example.com"))

    assert setup_calls == [(session, org_id, AUTOMATED_PIPELINE_USER_ID)]


@pytest.mark.asyncio
async def test_create_contact_promotes_linkedin_url_to_profile_column(monkeypatch) -> None:
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
        full_name="Jane Doe",
        linkedin_url="https://linkedin.com/in/jane",
    )
    session = MagicMock()
    service = ContactWriteService(session, org_id)

    class FakeContactIdentityResolutionService:
        def __init__(self, *_args, **_kwargs):
            pass

        async def resolve(self, _command):
            return ContactIdentityResolutionResult(
                status="resolved",
                contact=contact,
                created=True,
                matched_by="apollo",
            )

    monkeypatch.setattr(
        "src.service.crm.contacts.write_service.ContactIdentityResolutionService",
        FakeContactIdentityResolutionService,
    )

    result = await service.create_contact(
        ContactCreateCommand(
            email="jane@example.com",
            name="Jane Doe",
            linkedin_url="https://linkedin.com/in/jane",
        ),
    )

    assert result.contact.linkedin_url == "https://linkedin.com/in/jane"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_contact_linkedin_only_resolves_identity_before_writing(
    monkeypatch,
) -> None:
    """Direct contact create should use the same LinkedIn resolver as stakeholder create."""
    org_id = uuid4()
    company_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@acme.com",
        full_name="Jane Doe",
        linkedin_url="https://linkedin.com/in/jane",
    )
    session = MagicMock()
    service = ContactWriteService(session, org_id)
    calls = []

    class FakeContactIdentityResolutionService:
        def __init__(self, session_arg, organisation_id_arg, **kwargs):
            calls.append((session_arg, organisation_id_arg, kwargs))

        async def resolve(self, command):
            calls.append(command)
            return ContactIdentityResolutionResult(
                status="resolved",
                contact=contact,
                created=True,
                matched_by="apollo",
                created_account_id=company_id,
            )

    monkeypatch.setattr(
        "src.service.crm.contacts.write_service.ContactIdentityResolutionService",
        FakeContactIdentityResolutionService,
        raising=False,
    )

    result = await service.create_contact(
        ContactCreateCommand(
            email=None,
            name="Jane Doe",
            linkedin_url="https://linkedin.com/in/jane",
        ),
        context=ContactWriteContext(source="api"),
    )

    assert result.contact is contact
    assert result.created is True
    assert result.matched_by == "apollo"
    assert result.created_account_id == company_id
    assert calls[0][0] is session
    assert calls[0][1] == org_id
    assert calls[1].linkedin_url == "https://linkedin.com/in/jane"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_contact_rejects_company_linkedin_url_before_resolution(monkeypatch) -> None:
    """Service callers must reject company LinkedIn URLs before contact enrichment."""
    org_id = uuid4()
    session = MagicMock()
    service = ContactWriteService(session, org_id)

    class FakeContactIdentityResolutionService:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("Company LinkedIn URLs should not reach contact resolution")

    monkeypatch.setattr(
        "src.service.crm.contacts.write_service.ContactIdentityResolutionService",
        FakeContactIdentityResolutionService,
        raising=False,
    )

    with pytest.raises(ContactWriteServiceError) as exc_info:
        await service.create_contact(
            ContactCreateCommand(email=None, linkedin_url="https://linkedin.com/company/acme")
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Use a LinkedIn profile URL for contacts and stakeholders"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_contact_accepts_job_title_and_phone() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    result = await service.create_contact(
        ContactCreateCommand(
            email="jane@example.com",
            name="Jane Doe",
            job_title="CFO",
            phone="+15555550100",
        )
    )

    assert result.contact.job_title == "CFO"
    assert result.contact.phone == "+15555550100"


@pytest.mark.asyncio
async def test_create_contact_allows_missing_name_without_storing_email_as_name() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    result = await service.create_contact(ContactCreateCommand(email="buyer@example.com"))

    assert result.contact.full_name is None
    assert result.contact.first_name is None
    assert result.contact.last_name is None


@pytest.mark.asyncio
async def test_create_contact_allows_name_only_without_email_or_account_resolution() -> None:
    """Name-only contact create stores a placeholder contact without research/account identity."""
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[integration]))
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    service._account_writer.resolve_or_create_account_for_contact_email_with_result = AsyncMock()
    _disable_contact_created_projection(service)

    result = await service.create_contact(ContactCreateCommand(email=None, name="Jane Buyer"))

    assert result.contact.email is None
    assert result.contact.full_name == "Jane Buyer"
    assert result.contact.first_name == "Jane"
    assert result.contact.last_name == "Buyer"
    service._account_writer.resolve_or_create_account_for_contact_email_with_result.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_create_contact_links_account_returned_from_email_resolution() -> None:
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
        name="Acme",
        domain="acme.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    service._account_writer.resolve_or_create_account_for_contact_email_with_result = AsyncMock(
        return_value=AccountWriteResult(account=company, created=False, matched_by="domain")
    )
    _disable_contact_created_projection(service)

    result = await service.create_contact(ContactCreateCommand(email="buyer@example.com"))

    account_resolver = (
        service._account_writer.resolve_or_create_account_for_contact_email_with_result
    )
    account_resolver.assert_awaited_once()  # type: ignore[attr-defined]
    assert result.contact.company_id == company_id
    assert result.contact.company_name == "Acme"


@pytest.mark.asyncio
async def test_create_contact_reports_created_account_id() -> None:
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
        name="Acme",
        domain="acme.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    service._account_writer.resolve_or_create_account_for_contact_email_with_result = AsyncMock(
        return_value=AccountWriteResult(account=company, created=True, matched_by="created")
    )
    _disable_contact_created_projection(service)

    result = await service.create_contact(ContactCreateCommand(email="buyer@acme.com"))

    assert result.contact.company_id == company_id
    assert result.created_account_id == company_id


@pytest.mark.asyncio
async def test_create_contact_stays_unlinked_when_email_account_resolution_returns_none() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    service._account_writer.resolve_or_create_account_for_contact_email_with_result = AsyncMock(
        return_value=None
    )
    _disable_contact_created_projection(service)

    result = await service.create_contact(ContactCreateCommand(email="buyer@example.com"))

    account_resolver = (
        service._account_writer.resolve_or_create_account_for_contact_email_with_result
    )
    account_resolver.assert_awaited_once()  # type: ignore[attr-defined]
    assert result.contact.company_id is None
    assert result.contact.company_name is None


@pytest.mark.asyncio
async def test_create_contact_with_free_email_subdomain_stays_unlinked() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[_FakeResult(), _FakeResult(scalars=[integration])])
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_contact_created_projection(service)

    result = await service.create_contact(
        ContactCreateCommand(email="nickelsinglezerofive@enter.gmail.com")
    )

    assert result.contact.email == "nickelsinglezerofive@enter.gmail.com"
    assert result.contact.company_id is None
    assert result.contact.company_name is None


@pytest.mark.asyncio
async def test_create_contact_rejects_account_link_arguments() -> None:
    org_id = uuid4()
    company_id = uuid4()
    session = MagicMock()
    service = ContactWriteService(session, org_id)

    with pytest.raises(TypeError):
        await service.create_contact(
            ContactCreateCommand(email="buyer@example.com", name="Buyer"),
            company_id=company_id,
        )


@pytest.mark.asyncio
async def test_find_or_create_by_email_reuses_existing_contact_and_merges_profile() -> None:
    org_id = uuid4()
    contact_id = uuid4()
    existing = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="buyer@example.com",
        full_name="Old Name",
        properties={"linkedin_url": "https://linkedin.com/in/buyer"},
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[existing]))
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    result = await service.find_or_create_contact_by_email(
        email=" Buyer@Example.com ",
        name="New Name",
        context=ContactWriteContext(source="stakeholder"),
        profile_updates={
            "job_title": "CFO",
            "phone": "+15555550100",
            "properties": {"department": "Finance"},
        },
    )

    assert result.contact.contact_id == contact_id
    assert result.created is False
    assert result.matched_by == "email"
    assert existing.full_name == "New Name"
    assert existing.first_name == "New"
    assert existing.last_name == "Name"
    assert existing.job_title == "CFO"
    assert existing.phone == "+15555550100"
    assert existing.properties == {
        "linkedin_url": "https://linkedin.com/in/buyer",
        "department": "Finance",
    }
    assert session.flush.await_count == 2
    session.refresh.assert_awaited_once_with(existing)
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_by_email_creates_missing_contact() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_FakeResult(), _FakeResult(), _FakeResult(scalars=[integration])]
    )
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    result = await service.find_or_create_contact_by_email(
        email="buyer@example.com",
        name="Buyer Name",
        context=ContactWriteContext(source="stakeholder"),
        profile_updates={
            "job_title": "CFO",
            "phone": "+15555550100",
            "linkedin_url": "https://linkedin.com/in/buyer",
        },
    )

    assert result.created is True
    assert result.matched_by == "created"
    assert result.contact.email == "buyer@example.com"
    assert result.contact.full_name == "Buyer Name"
    assert result.contact.job_title == "CFO"
    assert result.contact.linkedin_url == "https://linkedin.com/in/buyer"
    assert result.contact.phone == "+15555550100"


@pytest.mark.asyncio
async def test_find_or_create_by_email_creates_missing_contact_without_synthetic_name() -> None:
    org_id = uuid4()
    integration = KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[_FakeResult(), _FakeResult(), _FakeResult(scalars=[integration])]
    )
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)
    _disable_account_resolution(service)
    _disable_contact_created_projection(service)

    result = await service.find_or_create_contact_by_email(
        email="buyer@example.com",
        name=None,
        context=ContactWriteContext(source="stakeholder"),
    )

    assert result.created is True
    assert result.contact.email == "buyer@example.com"
    assert result.contact.full_name is None
    assert result.contact.first_name is None
    assert result.contact.last_name is None


@pytest.mark.asyncio
async def test_create_contact_rejects_duplicate_email_in_org() -> None:
    org_id = uuid4()
    existing = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[existing]))

    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError) as exc:
        await service.create_contact(
            ContactCreateCommand(email="jane@example.com", name="Jane Doe")
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_contact_duplicate_email_records_no_activity_projection() -> None:
    org_id = uuid4()
    existing = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[existing]))

    service = ContactWriteService(session, org_id)
    _disable_contact_created_projection(service)

    with pytest.raises(ContactWriteServiceError):
        await service.create_contact(
            ContactCreateCommand(email="jane@example.com", name="Jane Doe")
        )

    service._projection.upsert_contact_created.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_update_contact_rejects_existing_email_change() -> None:
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))

    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError) as exc:
        await service.update_contact(contact.contact_id, {"email": "new@example.com"})

    assert exc.value.status_code == 400
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_contact_stores_linkedin_url_in_profile_column_without_commit() -> None:
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
        properties={},
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    service = ContactWriteService(session, org_id)

    result = await service.update_contact(
        contact.contact_id,
        {"linkedin_url": "https://linkedin.com/in/jane"},
    )

    assert result.contact.linkedin_url == "https://linkedin.com/in/jane"
    assert session.flush.await_count == 2
    session.refresh.assert_awaited_once_with(contact)
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_update_contact_rejects_company_linkedin_url() -> None:
    """Contact updates should reject account LinkedIn URLs before mutating the row."""
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))

    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError) as exc:
        await service.update_contact(
            contact.contact_id,
            {"linkedin_url": "https://linkedin.com/company/acme"},
        )

    assert exc.value.status_code == 400
    assert exc.value.message == "Use a LinkedIn profile URL for contacts and stakeholders"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_contact_rejects_existing_linkedin_url_change() -> None:
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
        linkedin_url="https://linkedin.com/in/jane",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))

    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError) as exc:
        await service.update_contact(
            contact.contact_id,
            {"linkedin_url": "https://linkedin.com/in/jane-new"},
        )

    assert exc.value.status_code == 400
    assert contact.linkedin_url == "https://linkedin.com/in/jane"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_contact_rejects_existing_linkedin_url_clear() -> None:
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
        linkedin_url="https://linkedin.com/in/jane",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))

    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError) as exc:
        await service.update_contact(contact.contact_id, {"linkedin_url": None})

    assert exc.value.status_code == 400
    assert contact.linkedin_url == "https://linkedin.com/in/jane"
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_contact_actor_context_does_not_overwrite_owner() -> None:
    org_id = uuid4()
    original_owner_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email="jane@example.com",
        owner_id=original_owner_id,
        owner_name="Original Owner",
        owner_email="original@example.com",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))
    session.flush = AsyncMock()
    session.refresh = AsyncMock()

    service = ContactWriteService(session, org_id)

    result = await service.update_contact(
        contact.contact_id,
        {"job_title": "CFO"},
        context=ContactWriteContext(
            source="api",
            actor_user_id=uuid4(),
            actor_name="Acting User",
            actor_email="acting@example.com",
        ),
    )

    assert result.contact.owner_id == original_owner_id
    assert result.contact.owner_name == "Original Owner"
    assert result.contact.owner_email == "original@example.com"
