from __future__ import annotations

from uuid import uuid4

from src.domain.models.organisation_tables.sync.crm import (
    ApprovalStatus,
    CRMActivity,
    CRMContact,
    DealStakeholder,
)
from src.service.integrations.webhooks.hubspot.handlers.contact_privacy_deletion import (
    HubSpotContactPrivacyDeletionHandler,
)


def test_scrub_records_for_contact_privacy_deletion_removes_contact_pii() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="456",
        first_name="Ada",
        last_name="Lovelace",
        full_name="Ada Lovelace",
        job_title="CTO",
        company_name="Acme",
        email="ada@example.com",
        additional_emails=["ada@personal.example"],
        phone="+155501",
        mobile_phone="+155502",
        address="1 Main St",
        properties={"email": "ada@example.com"},
        metrics={"touches": 3},
        data_payload={"properties": {"email": "ada@example.com"}},
    )
    stakeholder = DealStakeholder(
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=uuid4(),
        contact_id=contact.contact_id,
        email="ada@example.com",
        name="Ada Lovelace",
        job_title="CTO",
        phone_number="+155501",
        external_links=[{"url": "https://linkedin.example/ada"}],
        stakeholder_data={"email": "ada@example.com"},
        stakeholder_research="profile text",
    )
    activity = CRMActivity(
        organisation_id=organisation_id,
        integration_id=integration_id,
        external_id="task-1",
        contact_id=contact.contact_id,
        contact_external_id="456",
        subject="Call Ada",
        body="Ask about budget",
        related_name="Ada Lovelace",
        owner_email="owner@example.com",
        external_assignee_email="ada@example.com",
        attendees=[{"email": "ada@example.com", "name": "Ada"}],
        properties={"hs_body_preview": "Ask Ada"},
        metrics={"count": 1},
        data_payload={"properties": {"hs_body_preview": "Ask Ada"}},
    )

    HubSpotContactPrivacyDeletionHandler.scrub_records_for_contact_privacy_deletion(
        contact=contact,
        stakeholders=[stakeholder],
        activities=[activity],
    )

    assert contact.is_deleted is True
    assert contact.email is None
    assert contact.full_name is None
    assert contact.properties == {"privacy_deleted": True}
    assert contact.metrics == {"privacy_deleted": True}
    assert contact.data_payload == {"privacy_deleted": True}

    assert stakeholder.is_deleted is True
    assert stakeholder.email.startswith("privacy-deleted-")
    assert stakeholder.email.endswith("@privacy-deleted.invalid")
    assert stakeholder.name is None
    assert stakeholder.job_title is None
    assert stakeholder.approval_status == ApprovalStatus.REMOVED
    assert stakeholder.stakeholder_data == {"privacy_deleted": True}
    assert stakeholder.stakeholder_research is None

    assert activity.is_deleted is True
    assert activity.contact_external_id is None
    assert activity.subject is None
    assert activity.body is None
    assert activity.attendees == []
    assert activity.properties == {"privacy_deleted": True}
    assert activity.metrics == {"privacy_deleted": True}
    assert activity.data_payload == {"privacy_deleted": True}
