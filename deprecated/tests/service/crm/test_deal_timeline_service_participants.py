from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus
from src.domain.models.organisation_tables.sync.timeline_models import (
    DealTimelineEvent,
    TimelineEventSource,
    TimelineEventType,
)
from src.service.crm.timeline.deal_timeline_service import DealTimelineService


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class _TimelineSession:
    def __init__(self, value: object | None) -> None:
        self.values = list(value) if isinstance(value, list) else [value]
        self.statements = []

    async def execute(self, stmt):  # noqa: ANN001
        self.statements.append(stmt)
        return _ScalarResult(self.values.pop(0) if self.values else None)


@pytest.mark.asyncio
async def test_stakeholder_timeline_detail_fetches_participant_by_public_stakeholder_id() -> None:
    organisation_id = uuid4()
    deal_id = uuid4()
    participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_email="buyer@example.com",
        related_name="Buyer One",
        participant_source="CRM",
        approval_status=ApprovalStatus.APPROVED.value,
        stance="CHAMPION",
        decision_power="DECISION_MAKER",
        temperature="WARM",
    )
    event = DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.STAKEHOLDER_ADDED,
        event_source=TimelineEventSource.CRM_HUBSPOT,
        event_timestamp=datetime.now(UTC),
        source_record_id=str(participant.participant_id),
        title="Stakeholder Added: Buyer One",
        event_metadata={"stakeholder_id": str(participant.participant_id)},
    )
    session = _TimelineSession(participant)
    service = DealTimelineService(session, organisation_id)

    result = await service._fetch_stakeholder_event_data(event)  # noqa: SLF001

    assert result["stakeholder_id"] == str(participant.participant_id)
    assert result["email"] == "buyer@example.com"
    assert result["name"] == "Buyer One"
    assert result["job_title"] is None
    assert result["source"] == "CRM"
    assert result["stance"] == "CHAMPION"
    assert result["decision_power"] == "DECISION_MAKER"
    compiled = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "crm_deal_participants" in compiled
    assert "deal_stakeholders" not in compiled


@pytest.mark.asyncio
async def test_stakeholder_timeline_detail_prefers_linked_contact_profile() -> None:
    """Timeline details should render contact-owned profile fields when linked."""
    organisation_id = uuid4()
    deal_id = uuid4()
    contact_id = uuid4()
    participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=uuid4(),
        deal_id=deal_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_email="snapshot@example.com",
        related_name="Snapshot Buyer",
        job_title="Snapshot Title",
        participant_source="CRM",
        approval_status=ApprovalStatus.APPROVED.value,
        stance="CHAMPION",
        decision_power="DECISION_MAKER",
        temperature="WARM",
    )
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=participant.integration_id,
        contact_id=contact_id,
        email="contact@example.com",
        full_name="Contact Buyer",
        job_title="Contact Title",
    )
    event = DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.STAKEHOLDER_ADDED,
        event_source=TimelineEventSource.CRM_HUBSPOT,
        event_timestamp=datetime.now(UTC),
        source_record_id=str(participant.participant_id),
        title="Stakeholder Added: Snapshot Buyer",
        event_metadata={"stakeholder_id": str(participant.participant_id)},
    )
    session = _TimelineSession([participant, contact])
    service = DealTimelineService(session, organisation_id)

    result = await service._fetch_stakeholder_event_data(event)  # noqa: SLF001

    assert result["email"] == "contact@example.com"
    assert result["name"] == "Contact Buyer"
    assert result["job_title"] == "Contact Title"
