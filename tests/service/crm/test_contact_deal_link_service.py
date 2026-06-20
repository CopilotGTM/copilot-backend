from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.contacts.contracts import ContactDealLinkServiceError
from src.service.crm.contacts.deal_link_service import ContactDealLinkService


class _FakeResult:
    def __init__(self, *, scalars=None):
        self._scalars = scalars or []

    def scalars(self):
        return self

    def first(self):
        return self._scalars[0] if self._scalars else None


@pytest.mark.asyncio
async def test_add_contact_to_deal_rejects_missing_contact() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult())
    service = ContactDealLinkService(session, uuid4())

    with pytest.raises(ContactDealLinkServiceError) as exc:
        await service.add_contact_to_deal(contact_id=uuid4(), deal_id=uuid4())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_add_contact_to_deal_rejects_contact_without_email() -> None:
    org_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=uuid4(),
        email=None,
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))
    service = ContactDealLinkService(session, org_id)

    with pytest.raises(ContactDealLinkServiceError) as exc:
        await service.add_contact_to_deal(contact_id=contact.contact_id, deal_id=uuid4())

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_add_contact_to_deal_delegates_to_deal_participant_service() -> None:
    org_id = uuid4()
    contact_id = uuid4()
    deal_id = uuid4()
    actor_user_id = uuid4()
    contact = CRMContact(
        organisation_id=org_id,
        integration_id=uuid4(),
        contact_id=contact_id,
        email="jane@example.com",
    )
    skipped = SimpleNamespace(
        participant_type="contact",
        related_id=contact_id,
        relationship_role="buyer",
        reason="duplicate",
    )
    participant = CRMDealParticipant(
        participant_id=uuid4(),
        organisation_id=org_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        relationship_role="buyer",
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=_FakeResult(scalars=[contact]))

    with patch(
        "src.service.crm.contacts.deal_link_service.DealParticipantService"
    ) as participant_cls:
        participant_service = MagicMock()
        participant_service.add_participants = AsyncMock(
            return_value=([participant], [skipped], True, uuid4())
        )
        participant_cls.return_value = participant_service

        result = await ContactDealLinkService(session, org_id).add_contact_to_deal(
            contact_id=contact_id,
            deal_id=deal_id,
            relationship_role="buyer",
            actor_user_id=actor_user_id,
        )

    participant_cls.assert_called_once_with(
        session=session,
        organisation_id=org_id,
        actor_user_id=actor_user_id,
    )
    participant_service.add_participants.assert_awaited_once_with(
        deal_id,
        [
            {
                "participant_type": "contact",
                "related_id": contact_id,
                "relationship_role": "buyer",
            }
        ],
    )
    assert result.created == [
        {
            "participant_id": participant.participant_id,
            "participant_type": CRMParticipantType.CONTACT,
            "related_id": contact_id,
            "relationship_role": "buyer",
        }
    ]
    assert result.skipped[0]["reason"] == "duplicate"
    assert result.contact_participant_changed is True


def test_contact_deal_link_service_does_not_dispatch_stakeholder_sync() -> None:
    source = "src/service/crm/contacts/deal_link_service.py"

    with open(source) as handle:
        content = handle.read()

    assert "send_deal_task_async" not in content
