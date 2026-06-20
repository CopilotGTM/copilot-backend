from uuid import uuid4

from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealParticipant,
    CRMParticipantType,
)
from src.service.crm.insights.participant_stakeholder_adapter import (
    participant_to_stakeholder_detail_dict as legacy_participant_to_stakeholder_detail_dict,
)
from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)


def test_participant_adapter_uses_snapshots_without_contact_join() -> None:
    participant_id = uuid4()
    participant = CRMDealParticipant(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        deal_id=uuid4(),
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_name="Alex Buyer",
        related_email="alex@example.com",
        job_title="CFO",
        phone_number="+1 555 0100",
        approval_status="APPROVED",
        participant_source="CRM",
        stakeholder_research="Participant research",
    )

    payload = participant_to_stakeholder_detail_dict(participant)

    assert payload["participant_id"] == participant.participant_id
    assert "stakeholder_id" not in payload
    assert payload["email"] == "alex@example.com"
    assert payload["job_title"] == "CFO"
    assert payload["phone_number"] == "+1 555 0100"
    assert payload["stakeholder_research"] == "Participant research"
    assert "champion_potential" not in payload
    assert "engagement_score" not in payload


def test_legacy_participant_adapter_reexports_new_adapter() -> None:
    assert legacy_participant_to_stakeholder_detail_dict is participant_to_stakeholder_detail_dict


def test_participant_adapter_prefers_contact_profile_when_loaded() -> None:
    participant_id = uuid4()
    contact_id = uuid4()
    participant = CRMDealParticipant(
        organisation_id=uuid4(),
        integration_id=uuid4(),
        deal_id=uuid4(),
        participant_id=participant_id,
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact_id,
        related_name="Old Snapshot",
        related_email="old@example.com",
        job_title="Old Title",
        phone_number="+1 555 0000",
        approval_status="APPROVED",
        participant_source="MANUAL",
        stakeholder_research="Old participant research",
    )
    contact = CRMContact(
        organisation_id=participant.organisation_id,
        integration_id=participant.integration_id,
        contact_id=contact_id,
        full_name="New Person",
        email="new@example.com",
        job_title="CFO",
        phone="+1 555 0100",
        contact_research="Fresh contact research",
    )

    payload = participant_to_stakeholder_detail_dict(participant, contact=contact)

    assert payload["participant_id"] == participant.participant_id
    assert "stakeholder_id" not in payload
    assert payload["name"] == "New Person"
    assert payload["email"] == "new@example.com"
    assert payload["job_title"] == "CFO"
    assert payload["phone_number"] == "+1 555 0100"
    assert payload["stakeholder_research"] == "Fresh contact research"
