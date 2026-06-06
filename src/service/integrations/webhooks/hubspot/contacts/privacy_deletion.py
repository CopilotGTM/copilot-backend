"""HubSpot contact privacy deletion handler.

HubSpot privacy deletion events are provider-level compliance signals: when a
contact is deleted for privacy reasons, we scrub local contact PII and related
CRM/stakeholder references across approved tenant schemas that match the
webhook portal.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.domain.models.organisation_tables.sync.crm import (
    ApprovalStatus,
    CRMActivity,
    CRMContact,
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
    CRMSourceObjectType,
    DealStakeholder,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent

logger = get_logger(__name__)

_PRIVACY_DELETED_PAYLOAD = {"privacy_deleted": True}


class HubSpotContactPrivacyDeletionHandler:
    """Handle HubSpot contact privacy deletion events across tenant schemas."""

    @staticmethod
    def scrub_records_for_contact_privacy_deletion(
        *,
        contact: CRMContact,
        stakeholders: list[DealStakeholder],
        activities: list[CRMActivity],
        deal_participants: list[CRMDealParticipant] | None = None,
        deals: list[CRMDeal] | None = None,
    ) -> None:
        """Scrub PII from an already-loaded contact graph.

        The handler keeps historical rows as tombstones for audit and FK
        stability, but removes person-identifying fields from the contact and
        directly linked stakeholder/activity/deal participant records.
        """
        contact.is_deleted = True
        contact.first_name = None
        contact.last_name = None
        contact.full_name = None
        contact.job_title = None
        contact.company_name = None
        contact.email = None
        contact.additional_emails = []
        contact.phone = None
        contact.mobile_phone = None
        contact.fax = None
        contact.website = None
        contact.owner_email = None
        contact.owner_name = None
        contact.address = None
        contact.city = None
        contact.state = None
        contact.postal_code = None
        contact.country = None
        contact.timezone = None
        contact.properties = dict(_PRIVACY_DELETED_PAYLOAD)
        contact.metrics = dict(_PRIVACY_DELETED_PAYLOAD)

        for stakeholder in stakeholders:
            stakeholder.is_deleted = True
            stakeholder.email = (
                f"privacy-deleted-{stakeholder.stakeholder_id}@privacy-deleted.invalid"
            )
            stakeholder.name = None
            stakeholder.job_title = None
            stakeholder.department = None
            stakeholder.phone_number = None
            stakeholder.external_links = None
            stakeholder.motivation = None
            stakeholder.pain_points = None
            stakeholder.keywords = None
            stakeholder.approval_status = ApprovalStatus.REMOVED
            stakeholder.stakeholder_data = dict(_PRIVACY_DELETED_PAYLOAD)
            stakeholder.stakeholder_engagement_history = None
            stakeholder.stakeholder_research = None
            stakeholder.last_researched_at = None

        for activity in activities:
            activity.is_deleted = True
            activity.related_name = None
            activity.contact_id = None
            activity.subject = None
            activity.body = None
            activity.owner_name = None
            activity.owner_email = None
            activity.external_assignee_email = None
            activity.external_assignee_name = None
            activity.attendees = []
            activity.properties = dict(_PRIVACY_DELETED_PAYLOAD)
            activity.metrics = dict(_PRIVACY_DELETED_PAYLOAD)

        for participant in deal_participants or []:
            participant.is_deleted = True
            participant.related_local_id = None
            participant.related_name = None
            participant.related_email = None

        for deal in deals or []:
            deal.primary_contact_id = None
            deal.primary_contact_name = None
            deal.primary_contact_email = None

    @classmethod
    async def handle(cls, event: HubSpotWebhookEvent) -> None:
        """Process one HubSpot contact privacy deletion webhook event."""
        organisation_ids = await cls._list_active_organisation_ids()
        touched_contacts = 0

        for organisation_id in organisation_ids:
            touched_contacts += await cls._handle_for_organisation(
                organisation_id=organisation_id,
                portal_id=event.portal_id,
                object_id=event.object_id,
            )

        logger.info(
            "Processed HubSpot contact privacy deletion",
            extra={
                "event": "hubspot_contact_privacy_deletion_processed",
                "portal_id": event.portal_id,
                "object_id": event.object_id,
                "event_id": event.event_id,
                "contacts_scrubbed": touched_contacts,
            },
        )

    @staticmethod
    async def _list_active_organisation_ids() -> list[UUID]:
        """Return active approved organisations eligible for privacy scrubbing."""
        async for master_session in acquire_master_async_session():
            statement = select(col(Organisation.organisation_id)).where(
                col(Organisation.is_active).is_(True),
                col(Organisation.status) == OrganisationStatus.APPROVED,
            )
            result = await master_session.execute(statement)
            return list(result.scalars().all())
        return []

    @classmethod
    async def _handle_for_organisation(
        cls,
        *,
        organisation_id: UUID,
        portal_id: str,
        object_id: str,
    ) -> int:
        """Scrub matching contacts inside one organisation schema."""
        async for org_session in acquire_org_async_session(organisation_id):
            integrations = await cls._matching_hubspot_integrations(
                org_session=org_session,
                organisation_id=organisation_id,
                portal_id=portal_id,
            )
            touched_contacts = 0
            for integration in integrations:
                contacts = await cls._contacts_for_deleted_object(
                    org_session=org_session,
                    organisation_id=organisation_id,
                    integration_id=integration.integration_id,
                    object_id=object_id,
                )
                if not contacts:
                    continue

                contact_ids = [contact.contact_id for contact in contacts]
                stakeholders = await cls._stakeholders_for_contacts(
                    org_session=org_session,
                    organisation_id=organisation_id,
                    contact_ids=contact_ids,
                )
                activities = await cls._activities_for_contacts(
                    org_session=org_session,
                    organisation_id=organisation_id,
                    integration_id=integration.integration_id,
                    contact_ids=contact_ids,
                )
                deal_participants = await cls._deal_participants_for_contacts(
                    org_session=org_session,
                    organisation_id=organisation_id,
                    integration_id=integration.integration_id,
                    contact_ids=contact_ids,
                )
                deals = await cls._deals_for_contacts(
                    org_session=org_session,
                    organisation_id=organisation_id,
                    integration_id=integration.integration_id,
                    contact_ids=contact_ids,
                )

                for contact in contacts:
                    cls.scrub_records_for_contact_privacy_deletion(
                        contact=contact,
                        stakeholders=[
                            stakeholder
                            for stakeholder in stakeholders
                            if stakeholder.contact_id == contact.contact_id
                        ],
                        activities=[
                            activity
                            for activity in activities
                            if activity.contact_id == contact.contact_id
                        ],
                        deal_participants=[
                            participant
                            for participant in deal_participants
                            if participant.related_local_id == contact.contact_id
                        ],
                        deals=[
                            deal for deal in deals if deal.primary_contact_id == contact.contact_id
                        ],
                    )
                    org_session.add(contact)
                    touched_contacts += 1

                for stakeholder in stakeholders:
                    org_session.add(stakeholder)
                for activity in activities:
                    org_session.add(activity)
                for participant in deal_participants:
                    org_session.add(participant)
                for deal in deals:
                    org_session.add(deal)

            if touched_contacts:
                await org_session.commit()
            return touched_contacts
        return 0

    @staticmethod
    async def _matching_hubspot_integrations(
        *,
        org_session,
        organisation_id: UUID,
        portal_id: str,
    ) -> list[KnowledgeIntegration]:
        """Return active HubSpot integrations whose metadata matches the portal."""
        statement = select(KnowledgeIntegration).where(
            # Portal id comes from HubSpot, but org/schema and integration type
            # remain the tenant boundary before any privacy write is allowed.
            col(KnowledgeIntegration.organisation_id) == organisation_id,
            col(KnowledgeIntegration.integration_type) == IntegrationType.HUBSPOT,
            col(KnowledgeIntegration.is_active).is_(True),
        )
        result = await org_session.execute(statement)
        integrations = list(result.scalars().all())
        return [
            integration
            for integration in integrations
            if str((integration.connection_metadata or {}).get("hubspot_portal_id") or "")
            == portal_id
        ]

    @staticmethod
    async def _contacts_for_deleted_object(
        *,
        org_session,
        organisation_id: UUID,
        integration_id: UUID,
        object_id: str,
    ) -> list[CRMContact]:
        """Load contacts matching the deleted HubSpot contact object id."""
        local_ids = await CRMSourceMappingHelper(
            session=org_session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="hubspot",
        ).mark_external_ids_deleted(
            object_type=CRMSourceObjectType.CONTACT,
            external_ids=[object_id],
        )
        contact_id = local_ids.get(object_id.strip())
        if contact_id is None:
            return []

        statement = select(CRMContact).where(
            col(CRMContact.organisation_id) == organisation_id,
            col(CRMContact.integration_id) == integration_id,
            col(CRMContact.contact_id) == contact_id,
        )
        result = await org_session.execute(statement)
        return list(result.scalars().all())

    @staticmethod
    async def _stakeholders_for_contacts(
        *,
        org_session,
        organisation_id: UUID,
        contact_ids: list[UUID],
    ) -> list[DealStakeholder]:
        """Load stakeholders linked to the deleted contacts."""
        if not contact_ids:
            return []
        statement = select(DealStakeholder).where(
            col(DealStakeholder.organisation_id) == organisation_id,
            col(DealStakeholder.contact_id).in_(contact_ids),
        )
        result = await org_session.execute(statement)
        return list(result.scalars().all())

    @staticmethod
    async def _activities_for_contacts(
        *,
        org_session,
        organisation_id: UUID,
        integration_id: UUID,
        contact_ids: list[UUID],
    ) -> list[CRMActivity]:
        """Load activities that reference the deleted contact locally or externally."""
        statement = select(CRMActivity).where(
            col(CRMActivity.organisation_id) == organisation_id,
            col(CRMActivity.integration_id) == integration_id,
            col(CRMActivity.contact_id).in_(contact_ids),
        )
        result = await org_session.execute(statement)
        return list(result.scalars().all())

    @staticmethod
    async def _deal_participants_for_contacts(
        *,
        org_session,
        organisation_id: UUID,
        integration_id: UUID,
        contact_ids: list[UUID],
    ) -> list[CRMDealParticipant]:
        """Load deal participants that represent the deleted contact."""
        statement = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == organisation_id,
            col(CRMDealParticipant.integration_id) == integration_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.related_local_id).in_(contact_ids),
        )
        result = await org_session.execute(statement)
        return list(result.scalars().all())

    @staticmethod
    async def _deals_for_contacts(
        *,
        org_session,
        organisation_id: UUID,
        integration_id: UUID,
        contact_ids: list[UUID],
    ) -> list[CRMDeal]:
        """Load deals that use the deleted contact as their primary contact."""
        statement = select(CRMDeal).where(
            col(CRMDeal.organisation_id) == organisation_id,
            col(CRMDeal.integration_id) == integration_id,
            col(CRMDeal.primary_contact_id).in_(contact_ids),
        )
        result = await org_session.execute(statement)
        return list(result.scalars().all())
