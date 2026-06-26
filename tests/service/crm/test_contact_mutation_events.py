"""Contact write mutation-event tests."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealMutationEvent
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
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

    def scalar_one_or_none(self):
        return self._scalars[0] if self._scalars else None


class _ContactSession:
    def __init__(
        self,
        *,
        existing_contact: CRMContact | None = None,
        integration: KnowledgeIntegration | None = None,
        company: CRMCompany | None = None,
    ) -> None:
        self.existing_contact = existing_contact
        self.integration = integration
        self.company = company
        self.added: list[object] = []
        self.flush_count = 0
        self.refreshed: list[object] = []
        self._execute_count = 0

    async def execute(self, _statement: object) -> _FakeResult:
        self._execute_count += 1
        if self._execute_count == 1:
            return _FakeResult(scalars=[self.existing_contact] if self.existing_contact else [])
        return _FakeResult(scalars=[self.integration] if self.integration else [])

    async def get(self, _model: object, _entity_id: object) -> CRMCompany | None:
        return self.company

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)


def _integration(org_id):
    return KnowledgeIntegration(
        organisation_id=org_id,
        integration_id=uuid4(),
        name="Copilot CRM",
    )


def _contact(org_id, **kwargs) -> CRMContact:
    values = {
        "organisation_id": org_id,
        "integration_id": uuid4(),
        "contact_id": uuid4(),
        "email": "buyer@example.com",
        "full_name": "Buyer Old",
        "is_deleted": False,
    }
    values.update(kwargs)
    return CRMContact(
        **values,
    )


def _company(org_id, company_id=None) -> CRMCompany:
    return CRMCompany(
        organisation_id=org_id,
        integration_id=uuid4(),
        company_id=company_id or uuid4(),
        name="Acme",
        is_deleted=False,
    )


def _mutation_events(session: _ContactSession) -> list[CRMDealMutationEvent]:
    return [obj for obj in session.added if isinstance(obj, CRMDealMutationEvent)]


@pytest.mark.asyncio
async def test_create_contact_records_contact_created_mutation_event() -> None:
    org_id = uuid4()
    company_id = uuid4()
    session = _ContactSession(
        integration=_integration(org_id),
        company=_company(org_id, company_id),
    )
    service = ContactWriteService(session, org_id)
    service._account_writer.resolve_or_create_account_for_contact_email = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )

    result = await service.create_contact(
        ContactCreateCommand(
            email=" Buyer@Example.com ",
            name="Buyer Person",
            job_title="CFO",
        ),
        context=ContactWriteContext(source="api", actor_user_id=uuid4()),
    )

    events = _mutation_events(session)
    assert result.created is True
    assert len(events) == 1
    assert events[0].entity_type == "contact"
    assert events[0].entity_id == result.contact.contact_id
    assert events[0].event_type == "crm.contact.created"
    assert events[0].semantic_event_types == ["crm.contact.created"]
    assert events[0].changed_fields["email"] == {"old": None, "new": "buyer@example.com"}
    assert "company_id" not in events[0].changed_fields
    assert events[0].changed_fields["job_title"] == {"old": None, "new": "CFO"}


@pytest.mark.asyncio
async def test_create_contact_duplicate_email_records_no_mutation_event() -> None:
    org_id = uuid4()
    session = _ContactSession(existing_contact=_contact(org_id))
    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError):
        await service.create_contact(
            ContactCreateCommand(email="buyer@example.com", name="Buyer Person"),
            context=ContactWriteContext(source="api", actor_user_id=uuid4()),
        )

    assert _mutation_events(session) == []


@pytest.mark.asyncio
async def test_update_contact_adds_email_mutation_event_for_email_less_contact() -> None:
    org_id = uuid4()
    contact = _contact(org_id, email=None)
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    result = await service.update_contact(
        contact.contact_id,
        {"email": " Buyer@Example.com "},
        context=ContactWriteContext(source="api", actor_user_id=uuid4()),
    )

    events = _mutation_events(session)
    assert result.contact.email == "buyer@example.com"
    assert len(events) == 1
    assert events[0].event_type == "crm.contact.updated"
    assert events[0].semantic_event_types == ["crm.contact.email_added"]
    assert events[0].changed_fields == {"email": {"old": None, "new": "buyer@example.com"}}


@pytest.mark.asyncio
async def test_update_contact_account_link_records_account_linked_event() -> None:
    org_id = uuid4()
    company_id = uuid4()
    contact = _contact(org_id, company_id=None, company_name=None)
    company = _company(org_id, company_id)
    session = _ContactSession(existing_contact=contact, company=company)
    service = ContactWriteService(session, org_id)

    await service.update_contact(
        contact.contact_id,
        {"company_id": company_id},
        context=ContactWriteContext(source="api", actor_user_id=uuid4()),
    )

    events = _mutation_events(session)
    assert len(events) == 1
    assert events[0].semantic_event_types == ["crm.contact.account_linked"]
    assert events[0].changed_fields["company_id"] == {"old": None, "new": str(company_id)}
    assert events[0].changed_fields["company_name"] == {"old": None, "new": "Acme"}


@pytest.mark.asyncio
async def test_update_contact_profile_edit_is_audit_only() -> None:
    org_id = uuid4()
    contact = _contact(org_id, job_title="CFO")
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    await service.update_contact(
        contact.contact_id,
        {"job_title": "COO"},
        context=ContactWriteContext(source="api", actor_user_id=uuid4()),
    )

    events = _mutation_events(session)
    assert len(events) == 1
    assert events[0].event_type == "crm.contact.updated"
    assert events[0].semantic_event_types == []
    assert events[0].changed_fields == {"job_title": {"old": "CFO", "new": "COO"}}


@pytest.mark.asyncio
async def test_update_contact_linkedin_url_is_profile_audit_only() -> None:
    org_id = uuid4()
    contact = _contact(org_id, linkedin_url=None)
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    await service.update_contact(
        contact.contact_id,
        {"linkedin_url": "https://linkedin.com/in/buyer"},
        context=ContactWriteContext(source="api", actor_user_id=uuid4()),
    )

    events = _mutation_events(session)
    assert contact.linkedin_url == "https://linkedin.com/in/buyer"
    assert len(events) == 1
    assert events[0].semantic_event_types == []
    assert events[0].changed_fields == {
        "linkedin_url": {"old": None, "new": "https://linkedin.com/in/buyer"}
    }


@pytest.mark.asyncio
async def test_update_contact_linkedin_url_clear_is_audited() -> None:
    org_id = uuid4()
    contact = _contact(org_id, linkedin_url="https://linkedin.com/in/buyer")
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    await service.update_contact(
        contact.contact_id,
        {"linkedin_url": None},
        context=ContactWriteContext(source="api", actor_user_id=uuid4()),
    )

    events = _mutation_events(session)
    assert contact.linkedin_url is None
    assert len(events) == 1
    assert events[0].changed_fields == {
        "linkedin_url": {"old": "https://linkedin.com/in/buyer", "new": None}
    }


@pytest.mark.asyncio
async def test_update_contact_profile_enrichment_is_audit_only() -> None:
    org_id = uuid4()
    contact = _contact(org_id, profile_enrichment={})
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    enrichment = {"apollo_id": "person_123", "seniority": "executive"}
    await service.update_contact(
        contact.contact_id,
        {
            "profile_enrichment": enrichment,
            "profile_enrichment_source": "apollo",
        },
        context=ContactWriteContext(source="agent_tool", actor_user_id=uuid4()),
    )

    events = _mutation_events(session)
    assert contact.profile_enrichment == enrichment
    assert len(events) == 1
    assert events[0].semantic_event_types == []
    assert events[0].changed_fields["profile_enrichment"] == {
        "old": {},
        "new": enrichment,
    }
    assert events[0].changed_fields["profile_enrichment_source"] == {
        "old": None,
        "new": "apollo",
    }


@pytest.mark.asyncio
async def test_update_contact_same_value_records_no_mutation_event() -> None:
    org_id = uuid4()
    contact = _contact(org_id, job_title="CFO")
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    await service.update_contact(
        contact.contact_id,
        {"job_title": "CFO"},
        context=ContactWriteContext(source="api", actor_user_id=uuid4()),
    )

    assert _mutation_events(session) == []


@pytest.mark.asyncio
async def test_update_contact_illegal_account_relink_records_no_mutation_event() -> None:
    org_id = uuid4()
    contact = _contact(org_id, company_id=uuid4())
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    with pytest.raises(ContactWriteServiceError):
        await service.update_contact(
            contact.contact_id,
            {"company_id": uuid4()},
            context=ContactWriteContext(source="api", actor_user_id=uuid4()),
        )

    assert _mutation_events(session) == []


@pytest.mark.asyncio
async def test_find_or_create_existing_contact_profile_update_records_audit_event() -> None:
    org_id = uuid4()
    contact = _contact(org_id, full_name="Old Name", job_title="CFO")
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    result = await service.find_or_create_contact_by_email(
        email="buyer@example.com",
        name="New Name",
        context=ContactWriteContext(source="agent_tool", actor_user_id=uuid4()),
        profile_updates={"job_title": "COO"},
    )

    events = _mutation_events(session)
    assert result.created is False
    assert len(events) == 1
    assert events[0].event_type == "crm.contact.updated"
    assert events[0].semantic_event_types == []
    assert events[0].changed_fields["full_name"] == {"old": "Old Name", "new": "New Name"}
    assert events[0].changed_fields["job_title"] == {"old": "CFO", "new": "COO"}


@pytest.mark.asyncio
async def test_find_or_create_existing_contact_no_profile_change_records_no_event() -> None:
    org_id = uuid4()
    contact = _contact(org_id, full_name="Buyer Old", first_name="Buyer", last_name="Old")
    session = _ContactSession(existing_contact=contact)
    service = ContactWriteService(session, org_id)

    result = await service.find_or_create_contact_by_email(
        email="buyer@example.com",
        name="Buyer Old",
        context=ContactWriteContext(source="agent_tool", actor_user_id=uuid4()),
    )

    assert result.created is False
    assert _mutation_events(session) == []
