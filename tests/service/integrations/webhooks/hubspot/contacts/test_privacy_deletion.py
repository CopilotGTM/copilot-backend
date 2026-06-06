"""Regression coverage for HubSpot contact privacy deletion scrubbing."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync.crm import (
    ApprovalStatus,
    CRMActivity,
    CRMContact,
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
    DealStakeholder,
)
from src.service.integrations.webhooks.hubspot.contacts.privacy_deletion import (
    HubSpotContactPrivacyDeletionHandler,
)


def test_scrub_records_for_contact_privacy_deletion_removes_contact_pii() -> None:
    """Privacy deletion scrubs PII while preserving tombstone rows for history."""
    organisation_id = uuid4()
    integration_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=integration_id,
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
        contact_id=contact.contact_id,
        subject="Call Ada",
        body="Ask about budget",
        related_name="Ada Lovelace",
        owner_email="owner@example.com",
        external_assignee_email="ada@example.com",
        attendees=[{"email": "ada@example.com", "name": "Ada"}],
        properties={"hs_body_preview": "Ask Ada"},
        metrics={"count": 1},
    )
    participant = CRMDealParticipant(
        organisation_id=organisation_id,
        integration_id=integration_id,
        deal_id=uuid4(),
        participant_type=CRMParticipantType.CONTACT,
        related_local_id=contact.contact_id,
        related_name="Ada Lovelace",
        related_email="ada@example.com",
    )
    deal = CRMDeal(
        organisation_id=organisation_id,
        integration_id=integration_id,
        primary_contact_id=contact.contact_id,
        primary_contact_name="Ada Lovelace",
        primary_contact_email="ada@example.com",
    )

    HubSpotContactPrivacyDeletionHandler.scrub_records_for_contact_privacy_deletion(
        contact=contact,
        stakeholders=[stakeholder],
        activities=[activity],
        deal_participants=[participant],
        deals=[deal],
    )

    assert contact.is_deleted is True
    assert contact.email is None
    assert contact.full_name is None
    assert contact.properties == {"privacy_deleted": True}
    assert contact.metrics == {"privacy_deleted": True}
    assert not hasattr(contact, "data_payload")

    assert stakeholder.is_deleted is True
    assert stakeholder.email.startswith("privacy-deleted-")
    assert stakeholder.email.endswith("@privacy-deleted.invalid")
    assert stakeholder.name is None
    assert stakeholder.job_title is None
    assert stakeholder.approval_status == ApprovalStatus.REMOVED
    assert stakeholder.stakeholder_data == {"privacy_deleted": True}
    assert stakeholder.stakeholder_research is None

    assert activity.is_deleted is True
    assert not hasattr(activity, "contact_external_id")
    assert activity.subject is None
    assert activity.body is None
    assert activity.attendees == []
    assert activity.properties == {"privacy_deleted": True}
    assert activity.metrics == {"privacy_deleted": True}
    assert not hasattr(activity, "data_payload")

    assert participant.is_deleted is True
    assert participant.related_local_id is None
    assert participant.related_name is None
    assert participant.related_email is None
    assert not hasattr(participant, "data_payload")

    assert deal.primary_contact_id is None
    assert deal.primary_contact_name is None
    assert deal.primary_contact_email is None
    assert not hasattr(deal, "primary_contact_external_id")


@pytest.mark.asyncio
async def test_contacts_for_deleted_object_resolves_contact_through_source_mapping(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    contact = CRMContact(
        organisation_id=organisation_id,
        integration_id=integration_id,
        contact_id=contact_id,
        external_id="456",
    )
    calls: list[dict[str, object]] = []

    class FakeMappingHelper:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def mark_external_ids_deleted(self, *, object_type, external_ids):  # noqa: ANN001
            calls.append({"object_type": object_type, "external_ids": list(external_ids)})
            return {"456": contact_id}

    class FakeSession:
        def __init__(self) -> None:
            self.statement = None

        async def execute(self, statement):  # noqa: ANN001
            self.statement = statement
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [contact]))

    session = FakeSession()
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.contacts.privacy_deletion.CRMSourceMappingHelper",
        FakeMappingHelper,
    )

    contacts = await HubSpotContactPrivacyDeletionHandler._contacts_for_deleted_object(
        org_session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        object_id="456",
    )

    where_clause = str(session.statement).partition("WHERE")[2]
    assert contacts == [contact]
    assert calls[0]["external_ids"] == ["456"]
    assert "crm_contacts.contact_id" in where_clause
    assert "crm_contacts.external_id" not in where_clause


@pytest.mark.asyncio
async def test_privacy_deletion_dependent_queries_use_local_contact_ids_only() -> None:
    organisation_id = uuid4()
    integration_id = uuid4()
    contact_id = uuid4()
    statements: list[object] = []

    class FakeSession:
        async def execute(self, statement):  # noqa: ANN001
            statements.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    session = FakeSession()

    await HubSpotContactPrivacyDeletionHandler._activities_for_contacts(
        org_session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        contact_ids=[contact_id],
    )
    await HubSpotContactPrivacyDeletionHandler._deal_participants_for_contacts(
        org_session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        contact_ids=[contact_id],
    )
    await HubSpotContactPrivacyDeletionHandler._deals_for_contacts(
        org_session=session,
        organisation_id=organisation_id,
        integration_id=integration_id,
        contact_ids=[contact_id],
    )

    where_clauses = [str(statement).partition("WHERE")[2] for statement in statements]
    assert "crm_activities.contact_id" in where_clauses[0]
    assert "crm_activities.contact_external_id" not in where_clauses[0]
    assert "crm_deal_participants.related_local_id" in where_clauses[1]
    assert "crm_deal_participants.related_external_id" not in where_clauses[1]
    assert "crm_deals.primary_contact_id" in where_clauses[2]
    assert "crm_deals.primary_contact_external_id" not in where_clauses[2]
