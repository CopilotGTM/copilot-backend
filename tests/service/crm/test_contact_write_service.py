from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.service.crm.contacts.contracts import (
    ContactWriteContext,
    ContactWriteServiceError,
)
from src.service.crm.contacts.write_service import ContactWriteService


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

    result = await service.create_contact(
        email=" Jane@Example.COM ",
        name="Jane Doe",
        context=ContactWriteContext(source="test"),
    )

    assert result.created is True
    assert result.contact.email == "jane@example.com"
    assert result.contact.first_name == "Jane"
    assert result.contact.last_name == "Doe"
    session.add.assert_called_once()
    session.flush.assert_awaited()
    session.refresh.assert_awaited_once_with(result.contact)
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_create_contact_accepts_properties_payload() -> None:
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

    result = await service.create_contact(
        email="jane@example.com",
        name="Jane Doe",
        properties={"linkedin_url": "https://linkedin.com/in/jane"},
    )

    assert result.contact.properties == {"linkedin_url": "https://linkedin.com/in/jane"}


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
        await service.create_contact(email="jane@example.com", name="Jane Doe")

    assert exc.value.status_code == 409


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
async def test_update_contact_stores_linkedin_url_in_properties_without_commit() -> None:
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

    assert result.contact.properties == {"linkedin_url": "https://linkedin.com/in/jane"}
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(contact)
    session.commit.assert_not_called()
