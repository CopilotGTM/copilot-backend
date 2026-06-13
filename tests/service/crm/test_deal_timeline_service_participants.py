from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
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
        self.value = value
        self.statements = []

    async def execute(self, stmt):  # noqa: ANN001
        self.statements.append(stmt)
        return _ScalarResult(self.value)


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
        job_title="CFO",
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
    assert result["job_title"] == "CFO"
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
