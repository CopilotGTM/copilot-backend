"""Participant-backed stakeholder semantic writes.

Called through the legacy stakeholder facade, agent tools, and deal UI actions.
This module owns writes to the canonical CRMDealParticipant rows while mirroring
durable person profile fields onto CRMContact whenever a contact can be linked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.contracts.crm.stakeholders import (
    AddContactsAsStakeholdersResponse,
    CreateStakeholderRequest,
    SkippedContact,
    StakeholderDetail,
    UpdateStakeholderRequest,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    StakeholderSource,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMRecordAccessError,
    DealAccessPolicy,
)
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.contacts.contracts import ContactWriteContext
from src.service.crm.contacts.resolution_service import (
    ContactIdentityResolutionService,
    ContactIdentityResolveCommand,
)
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.participants.contracts import (
    CommunicationStakeholderWriteResult,
    StakeholderWriteContext,
    StakeholderWriteResult,
)
from src.service.crm.participants.errors import StakeholdersServiceError
from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)
from src.service.crm.utils.account import clean_optional_string
from src.service.crm.utils.external_links import extract_linkedin_url, serialize_external_links
from src.service.crm.utils.identity import extract_email_domain, normalize_email
from src.service.crm.utils.internal_domains import (
    get_internal_domains,
)
from src.service.crm.utils.internal_domains import (
    is_internal_email as email_matches_internal_domain,
)

logger = get_logger("stakeholder_write_service")

ACTIVE_STAKEHOLDER_STATUS = "APPROVED"

# Personal consumer providers are skipped to reduce false-positive internal
# participant linking for manually-added external stakeholders.
PERSONAL_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "mail.com",
        "protonmail.com",
        "live.com",
        "msn.com",
        "ymail.com",
        "googlemail.com",
    }
)


class StakeholderWriteService:
    """Write stakeholder actions into org-scoped contact participant rows.

    The service treats stakeholder IDs as participant IDs; callers must pass the
    org-scoped session and deal context so every lookup can enforce tenant and
    deal boundaries before mutating rows.
    """

    def __init__(
        self,
        org_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID | None = None,
        access_actor: CRMAccessActor | None = None,
    ):
        """Capture immutable tenant context for the service instance."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._access = DealAccessPolicy(org_session, organisation_id, access_actor)

    @property
    def session(self) -> AsyncSession:
        """Return the org-scoped DB session for this service instance."""
        return self.org_session

    async def add_communication_discovered_contacts(
        self,
        *,
        deal_id: UUID,
        integration_id: UUID,
        customer_domains: frozenset[str],
        contact_emails_by_id: dict[UUID, str],
        source: StakeholderSource,
    ) -> CommunicationStakeholderWriteResult:
        """Add same-customer communication participants, capped to one approved hop."""
        if not contact_emails_by_id:
            return CommunicationStakeholderWriteResult()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"communication-participants:{self.organisation_id}:{deal_id}"},
        )
        rows = await self.session.execute(
            select(CRMDealParticipant).where(
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.deal_id) == deal_id,
                col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT,
            )
        )
        existing = list(rows.scalars().all())
        existing_contact_ids = {
            participant.related_local_id
            for participant in existing
            if participant.related_local_id is not None
        }
        active_by_email = {
            email: participant
            for participant in existing
            if not participant.is_deleted and (email := normalize_email(participant.related_email))
        }
        candidate_emails = {
            email
            for candidate_email in contact_emails_by_id.values()
            if (email := normalize_email(candidate_email))
        }
        discovery_sources = {
            StakeholderSource.EMAIL_DISCOVERY.value,
            StakeholderSource.CALENDAR_DISCOVERY.value,
        }
        discovered_count = sum(
            participant.participant_source in discovery_sources and not participant.is_deleted
            for participant in existing
        )
        approved = {
            participant.related_local_id
            for participant in existing
            if participant.related_local_id is not None
            and (
                participant.related_local_id in contact_emails_by_id
                or normalize_email(participant.related_email) in candidate_emails
            )
            and participant.participant_source in discovery_sources
            and participant.approval_status == ApprovalStatus.APPROVED.value
            and not participant.is_deleted
        }
        projection_participants = [
            participant
            for participant in existing
            if (
                participant.related_local_id in contact_emails_by_id
                or normalize_email(participant.related_email) in candidate_emails
            )
            and participant.participant_source in discovery_sources
            and participant.approval_status == ApprovalStatus.APPROVED.value
            and not participant.is_deleted
        ]
        now = datetime.now(UTC)
        created = False
        approved_added = 0
        pending_added = 0
        skipped_domain = 0
        for contact_id in sorted(contact_emails_by_id.keys() - existing_contact_ids, key=str):
            email = normalize_email(contact_emails_by_id[contact_id])
            # Contact resolution can temporarily produce two contact IDs for one
            # address. The participant uniqueness contract is email-based, so the
            # active row (or the first row in this batch) remains canonical.
            if not email or email in active_by_email:
                continue
            if extract_email_domain(email) not in customer_domains:
                skipped_domain += 1
                continue
            approval_status = (
                ApprovalStatus.APPROVED.value
                if discovered_count < 5
                else ApprovalStatus.PENDING.value
            )
            participant = CRMDealParticipant(
                organisation_id=self.organisation_id,
                integration_id=integration_id,
                deal_id=deal_id,
                participant_type=CRMParticipantType.CONTACT,
                related_local_id=contact_id,
                related_email=email,
                participant_source=source.value,
                approval_status=approval_status,
                last_seen_at=now,
                source_modified_at=now,
                synced_at=now,
            )
            self.session.add(participant)
            active_by_email[email] = participant
            created = True
            discovered_count += 1
            if approval_status == ApprovalStatus.APPROVED.value:
                approved.add(contact_id)
                approved_added += 1
                projection_participants.append(participant)
            else:
                pending_added += 1
        if created:
            await self.session.flush()
        if projection_participants:
            # Project new rows and replayed discovery rows. The activity upsert is idempotent,
            # so a later hydration pass repairs historical missing cards without duplicates.
            projection = ActivityFeedProjectionService(self.session, self.organisation_id)
            for participant in projection_participants:
                await projection.upsert_stakeholder_added(
                    participant,
                    contact=await self._get_participant_contact(participant),
                    source=participant.participant_source,
                    source_label="Communication discovery",
                    created_via="communication_hydration",
                )
        return CommunicationStakeholderWriteResult(
            approved_contact_ids=frozenset(approved),
            approved_added=approved_added,
            pending_added=pending_added,
            skipped_domain=skipped_domain,
        )

    async def _finish_write(self, obj: object | None = None) -> None:
        """Flush pending stakeholder/contact/activity changes before returning DTOs."""
        await self.session.flush()
        if obj is not None:
            await self.session.refresh(obj)

    async def _get_internal_domains(self) -> set[str]:
        """Load internal email domains through the shared CRM utility."""
        return await get_internal_domains(self.organisation_id)

    async def _is_internal_email(self, email: str) -> bool:
        """Check whether an email belongs to an internal org domain."""
        if not email or "@" not in email:
            return False
        internal_domains = await self._get_internal_domains()
        return email_matches_internal_domain(email, internal_domains)

    async def _ensure_contact_for_stakeholder(
        self,
        email: str,
        name: str | None,
        job_title: str | None,
        phone_number: str | None,
        linkedin_url: str | None = None,
        department: str | None = None,
        external_links: list[dict] | None = None,
    ) -> CRMContact | None:
        """Find or create a CRMContact for a manually-added stakeholder.

        Returns the contact if found or created, or None when business rules
        intentionally keep the stakeholder deal-local.
        """
        email_normalized = normalize_email(email)
        if not email_normalized:
            return None

        # Keep manual stakeholders lightweight when the address is likely an employee
        # or a consumer inbox; the participant row still records the deal-local person.
        if await self._is_internal_email(email_normalized):
            return None
        domain = extract_email_domain(email_normalized) or ""
        if domain in PERSONAL_EMAIL_DOMAINS:
            return None

        contact_writer = ContactWriteService(self.session, self.organisation_id)
        result = await contact_writer.find_or_create_contact_by_email(
            email=email_normalized,
            name=name,
            context=ContactWriteContext(
                source="stakeholder",
                actor_user_id=self.user_id,
                source_label="Stakeholder",
                created_via="stakeholder_write_service",
            ),
            profile_updates={
                key: value
                for key, value in {
                    "job_title": job_title,
                    "phone": phone_number,
                    "linkedin_url": linkedin_url,
                    "properties": {
                        profile_key: profile_value
                        for profile_key, profile_value in {
                            "department": department,
                            "external_links": external_links,
                        }.items()
                        if profile_value
                    },
                }.items()
                if value
            },
        )
        return result.contact

    async def _ensure_deal_participant(
        self,
        deal_id: UUID,
        contact_id: UUID | None,
        email: str | None,
        stance: str | None = None,
        decision_power: str | None = None,
        motivation: str | None = None,
        pain_points: list[str] | None = None,
        participant_source: str = StakeholderSource.MANUAL.value,
        approval_status: str = ACTIVE_STAKEHOLDER_STATUS,
    ) -> CRMDealParticipant | None:
        """Create or reuse the canonical contact participant for a deal."""
        from src.service.crm.internal.internal_crm_setup_service import (
            InternalCRMSetupService,
        )

        if not self.user_id:
            return None

        setup_service = InternalCRMSetupService(self.session, self.organisation_id, self.user_id)
        setup_result = await setup_service.setup_internal_crm(commit=False)
        integration = setup_result.integration

        # The org predicate is the tenant boundary; a deal_id supplied by an API
        # caller is never trusted without matching this service's organisation.
        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        if not deal_result.scalar_one_or_none():
            return None

        email_normalized = normalize_email(email)

        participant = await self._find_contact_participant(
            deal_id=deal_id,
            email=email_normalized,
            contact_id=contact_id,
            include_deleted=True,
        )
        if participant:
            # Reactivate the existing participant instead of inserting a duplicate.
            # Dedupe must include soft-deleted rows so remove/re-add keeps history on
            # the same participant ID for activity and research references.
            participant.related_local_id = participant.related_local_id or contact_id
            participant.related_email = email_normalized or participant.related_email
            participant.participant_source = participant_source
            participant.approval_status = approval_status
            participant.is_deleted = False
            participant.last_seen_at = datetime.now(UTC)
            return participant

        from uuid import uuid4

        now = datetime.now(UTC)
        participant_id = uuid4()
        participant = CRMDealParticipant(
            participant_id=participant_id,
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            deal_id=deal_id,
            participant_type=CRMParticipantType.CONTACT,
            related_local_id=contact_id,
            related_email=email_normalized,
            stance=stance or "UNKNOWN",
            decision_power=decision_power or "UNKNOWN",
            motivation=motivation,
            pain_points=pain_points,
            participant_source=participant_source,
            approval_status=approval_status,
            synced_at=now,
            source_modified_at=now,
            last_seen_at=now,
        )
        self.session.add(participant)
        return participant

    async def _ensure_internal_contact_from_selected_contact(
        self,
        contact: CRMContact,
        *,
        internal_integration_id: UUID,
    ) -> CRMContact:
        """Return an internal CRM contact for a selected stakeholder contact."""
        if contact.integration_id == internal_integration_id:
            return contact

        email_normalized = normalize_email(contact.email)
        if email_normalized:
            # Copy external CRM contacts into the internal CRM integration before
            # linking them to stakeholders; participant writes should not mutate the
            # provider-owned contact row that sync jobs may later refresh.
            stmt = select(CRMContact).where(
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.integration_id) == internal_integration_id,
                col(CRMContact.is_deleted).is_(False),
                func.lower(CRMContact.email) == email_normalized,
            )
            result = await self.session.execute(stmt)
            existing = result.scalars().first()
            if existing:
                return existing

        now = datetime.now(UTC)
        copied_contact = CRMContact(
            organisation_id=self.organisation_id,
            integration_id=internal_integration_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            full_name=(
                contact.full_name
                or " ".join(filter(None, [contact.first_name, contact.last_name]))
                or None
            ),
            job_title=contact.job_title,
            company_name=contact.company_name,
            company_id=None,
            email=email_normalized,
            additional_emails=list(contact.additional_emails or []),
            phone=contact.phone,
            mobile_phone=contact.mobile_phone,
            website=contact.website,
            linkedin_url=contact.linkedin_url,
            city=contact.city,
            state=contact.state,
            postal_code=contact.postal_code,
            country=contact.country,
            timezone=contact.timezone,
            source_created_at=now,
            source_modified_at=now,
            synced_at=now,
            last_seen_at=now,
            properties={
                **(contact.properties or {}),
                "source": "internal_crm",
                "created_via": "stakeholder_add_contacts",
                "copied_from_contact_id": str(contact.contact_id),
                "copied_from_integration_id": str(contact.integration_id),
                **(
                    {"copied_from_company_id": str(contact.company_id)}
                    if contact.company_id
                    else {}
                ),
            },
        )
        self.session.add(copied_contact)
        await self.session.flush()
        return copied_contact

    async def _find_contact_participant(
        self,
        *,
        deal_id: UUID,
        email: str | None = None,
        contact_id: UUID | None = None,
        participant_id: UUID | None = None,
        include_deleted: bool = False,
    ) -> CRMDealParticipant | None:
        """Find the deal contact participant that owns stakeholder state."""
        predicates = [
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
        ]
        if not include_deleted:
            predicates.append(col(CRMDealParticipant.is_deleted).is_(False))

        if participant_id:
            # participant_id is caller-supplied, so keep deal and org predicates in
            # place even for direct ID lookups.
            predicates.append(col(CRMDealParticipant.participant_id) == participant_id)
        else:
            identity_predicates = []
            if contact_id:
                identity_predicates.append(col(CRMDealParticipant.related_local_id) == contact_id)
            if email:
                email_normalized = normalize_email(email) or ""
                identity_predicates.append(
                    func.lower(func.trim(col(CRMDealParticipant.related_email))) == email_normalized
                )
            if not identity_predicates:
                return None
            predicates.append(or_(*identity_predicates))

        stmt = select(CRMDealParticipant).where(*predicates)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _get_participant_contact(self, participant: CRMDealParticipant) -> CRMContact | None:
        """Load the canonical contact profile for a participant, when linked."""
        if not participant.related_local_id:
            return None

        contact_stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == participant.related_local_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        contact_result = await self.session.execute(contact_stmt)
        return contact_result.scalar_one_or_none()

    async def _set_primary_contact_if_first_stakeholder(
        self,
        deal_id: UUID,
        participant: CRMDealParticipant,
    ) -> None:
        """Promote the first stakeholder to the deal's default contact."""
        if participant.is_primary_contact:
            return

        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        deal = deal_result.scalar_one_or_none()
        if (
            not deal
            or deal.primary_contact_id
            or deal.primary_contact_name
            or deal.primary_contact_email
        ):
            return

        existing_stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.participant_id) != participant.participant_id,
        )
        existing_result = await self.session.execute(existing_stmt)
        if existing_result.scalars().first():
            return

        now = datetime.now(UTC)
        participant.is_primary_contact = True
        participant.source_modified_at = now
        participant.synced_at = now
        participant.last_seen_at = now
        deal.primary_contact_id = participant.related_local_id
        deal.primary_contact_name = participant.related_name
        deal.primary_contact_email = participant.related_email
        deal.source_modified_at = now
        deal.synced_at = now
        deal.last_seen_at = now
        self.session.add(participant)
        self.session.add(deal)

    def _participant_detail(
        self,
        participant: CRMDealParticipant,
        *,
        contact: CRMContact | None = None,
    ) -> StakeholderDetail:
        """Convert the canonical participant row to the public stakeholder DTO."""
        return StakeholderDetail.model_validate(
            participant_to_stakeholder_detail_dict(participant, contact=contact)
        )

    def _apply_contact_profile_updates(
        self,
        *,
        contact: CRMContact | None,
        updates: dict,
    ) -> None:
        """Write durable profile fields only when a contact owns them."""
        if not contact:
            return

        if "name" in updates:
            contact.full_name = updates["name"]

        if "job_title" in updates:
            contact.job_title = updates["job_title"]

        if "phone_number" in updates:
            contact.phone = updates["phone_number"]

        if "linkedin_url" in updates:
            contact.linkedin_url = updates["linkedin_url"]

        contact_properties = dict(contact.properties or {})
        if "department" in updates:
            if updates["department"] is None:
                contact_properties.pop("department", None)
            else:
                contact_properties["department"] = updates["department"]

        if "external_links" in updates:
            serialized_links = serialize_external_links(updates["external_links"])
            if serialized_links is None:
                contact_properties.pop("external_links", None)
            else:
                contact_properties["external_links"] = serialized_links
            linkedin_url = extract_linkedin_url(serialized_links)
            if linkedin_url:
                # LinkedIn may arrive inside the generic external_links payload.
                # Mirror it to the first-class field so later identity matching can
                # find the same stakeholder without parsing the links blob.
                contact.linkedin_url = linkedin_url

        contact.properties = contact_properties

    @staticmethod
    def _apply_participant_behavior_updates(
        participant: CRMDealParticipant,
        updates: dict,
    ) -> None:
        """Write only deal-specific fields to the participant row."""
        for field_name in (
            "stance",
            "decision_power",
            "motivation",
            "pain_points",
            "keywords",
            "last_contact_at",
            "temperature",
        ):
            if field_name in updates:
                value = updates[field_name]
                if hasattr(value, "value"):
                    value = value.value
                setattr(participant, field_name, value)

    async def _soft_delete_internal_participant(
        self,
        deal_id: UUID,
        email: str,
    ) -> None:
        """Soft-delete the CRMDealParticipant for a removed stakeholder.

        Only deletes participants belonging to the internal CRM integration.
        External CRM participants are never touched.
        """
        from src.domain.models.organisation_tables.sync.crm.deals import (
            CRMDealParticipant,
        )
        from src.service.crm.internal.internal_crm_setup_service import (
            InternalCRMSetupService,
        )

        actor_id = self.user_id
        if not actor_id:
            return

        setup_service = InternalCRMSetupService(self.session, self.organisation_id, actor_id)
        integration = await setup_service.get_internal_integration()
        if not integration:
            return

        email_normalized = normalize_email(email) or ""
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.integration_id) == integration.integration_id,
            col(CRMDealParticipant.related_email) == email_normalized,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        participant = result.scalars().first()
        if participant:
            participant.is_deleted = True
            now = datetime.now(UTC)
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now

    async def _find_contact_by_linkedin_url(self, linkedin_url: str | None) -> CRMContact | None:
        """Find an org-owned contact by normalized LinkedIn URL."""
        linkedin_normalized = clean_optional_string(linkedin_url)
        if not linkedin_normalized:
            return None
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(func.trim(col(CRMContact.linkedin_url))) == linkedin_normalized.lower(),
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _find_contact_by_email(self, email: str | None) -> CRMContact | None:
        """Find an active org-owned contact by normalized email."""
        email_normalized = normalize_email(email)
        if not email_normalized:
            return None
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
            func.lower(func.trim(col(CRMContact.email))) == email_normalized,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _find_contact_by_id(self, contact_id: UUID | None) -> CRMContact | None:
        """Find an active org-owned contact by ID from a caller payload."""
        if not contact_id:
            return None
        stmt = select(CRMContact).where(
            col(CRMContact.contact_id) == contact_id,
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _resolve_contact_identity(
        self,
        *,
        email: str | None,
        linkedin_url: str | None,
        name: str | None,
    ):
        """Resolve a contact through the contact-owned Apollo enrichment boundary."""
        return await ContactIdentityResolutionService(
            self.session,
            self.organisation_id,
        ).resolve(
            ContactIdentityResolveCommand(
                email=email,
                linkedin_url=linkedin_url,
                name=name,
                context=ContactWriteContext(
                    source="stakeholder",
                    actor_user_id=self.user_id,
                    source_label="Stakeholder",
                    created_via="stakeholder_write_service",
                ),
            )
        )

    @staticmethod
    def _contacts_conflict(*contacts: CRMContact | None) -> bool:
        """Return true when strong identity fields point at different contacts."""
        contact_ids = {contact.contact_id for contact in contacts if contact is not None}
        return len(contact_ids) > 1

    async def get_stakeholder(
        self, deal_id: UUID, participant_id: UUID
    ) -> CRMDealParticipant | None:
        """Get the canonical participant-backed stakeholder by participant ID."""
        await self._require_deal_control(deal_id)
        return await self._find_contact_participant(
            deal_id=deal_id,
            participant_id=participant_id,
        )

    async def remove_stakeholder(self, deal_id: UUID, participant_id: UUID) -> StakeholderDetail:
        """Soft-delete a stakeholder participant from the deal."""
        stakeholder = await self.get_stakeholder(deal_id, participant_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        stakeholder.is_deleted = True
        stakeholder.updated_at = datetime.now(UTC)

        contact = await self._get_participant_contact(stakeholder)
        await ActivityFeedProjectionService(
            self.session,
            self.organisation_id,
        ).upsert_stakeholder_removed(
            stakeholder,
            contact=contact,
            actor_user_id=self.user_id,
            source="api",
            source_label="API",
            created_via="remove_stakeholder",
        )

        await self._finish_write(stakeholder)
        return self._participant_detail(stakeholder, contact=contact)

    async def create_stakeholder(
        self,
        deal_id: UUID,
        request: CreateStakeholderRequest,
        *,
        context: StakeholderWriteContext | None = None,
    ) -> StakeholderDetail:
        """Create or reuse a stakeholder and return the compatibility DTO."""
        return (
            await self.create_stakeholder_with_outcome(
                deal_id,
                request,
                context=context,
            )
        ).stakeholder

    async def create_stakeholder_with_outcome(
        self,
        deal_id: UUID,
        request: CreateStakeholderRequest,
        *,
        context: StakeholderWriteContext | None = None,
    ) -> StakeholderWriteResult:
        """Manually create a new stakeholder for a deal.

        Args:
            deal_id: The deal to add the stakeholder to.
            request: The stakeholder creation request with user-provided details.

        Returns:
            The created stakeholder as StakeholderDetail DTO.

        Raises:
            StakeholdersServiceError: If email already exists for this deal.
        """
        await self._require_deal_control(deal_id)
        email_normalized = normalize_email(request.email)
        name = clean_optional_string(request.name)
        linkedin_url = clean_optional_string(request.linkedin_url)

        def hydrate_from_contact(contact: CRMContact) -> None:
            """Use a resolved contact as the canonical profile source for create."""
            nonlocal email_normalized, name, linkedin_url

            contact_properties = dict(contact.properties or {})
            contact_name = (
                contact.full_name
                or " ".join(filter(None, [contact.first_name, contact.last_name]))
                or None
            )
            email_normalized = normalize_email(contact.email) or email_normalized
            name = contact_name or name
            linkedin_url = linkedin_url or contact.linkedin_url
            request.job_title = request.job_title or contact.job_title
            request.phone_number = request.phone_number or contact.phone or contact.mobile_phone
            request.department = request.department or contact_properties.get("department")

        # Resolve strong claims independently. Agent tools may overfill contact_id,
        # email, and LinkedIn; rejecting contradictions here prevents silent CRM
        # identity corruption before any participant/contact write happens.
        contact_by_id = await self._find_contact_by_id(request.contact_id)
        if request.contact_id and not contact_by_id:
            raise StakeholdersServiceError("Contact not found", status_code=404)
        contact_by_email = (
            await self._find_contact_by_email(email_normalized) if email_normalized else None
        )
        contact_by_linkedin = (
            await self._find_contact_by_linkedin_url(linkedin_url) if linkedin_url else None
        )
        if self._contacts_conflict(contact_by_id, contact_by_email, contact_by_linkedin):
            raise StakeholdersServiceError(
                "Conflicting stakeholder identity fields resolve to different contacts",
                status_code=409,
                data={
                    key: str(contact.contact_id)
                    for key, contact in {
                        "contact_id": contact_by_id,
                        "email": contact_by_email,
                        "linkedin_url": contact_by_linkedin,
                    }.items()
                    if contact is not None
                },
            )

        contact = contact_by_id or contact_by_email or contact_by_linkedin
        if contact:
            # A resolved CRM contact wins over manually typed profile fragments.
            # It gives the participant a stable contact link for future
            # research/profile refreshes.
            hydrate_from_contact(contact)
        elif linkedin_url and not email_normalized:
            resolution = await self._resolve_contact_identity(
                email=email_normalized,
                linkedin_url=linkedin_url,
                name=name,
            )
            if resolution.status == "resolved" and resolution.contact:
                contact = resolution.contact
                hydrate_from_contact(contact)
            else:
                raise StakeholdersServiceError(
                    resolution.error_detail or "Unable to resolve LinkedIn URL to a contact",
                    status_code=404,
                )

        # Guardrail: reject internal team members
        if email_normalized and await self._is_internal_email(email_normalized):
            raise StakeholdersServiceError(
                "This email belongs to an internal team member and cannot be added "
                "as a stakeholder. Stakeholders must be external contacts only.",
                status_code=403,
            )

        # Contact owns durable profile fields. The participant keeps only deal
        # membership and identity fields so create/update flows do not fork person data.
        contact_id = contact.contact_id if contact else None
        if email_normalized and not contact:
            contact = await self._ensure_contact_for_stakeholder(
                email=email_normalized,
                name=name,
                job_title=request.job_title,
                phone_number=request.phone_number,
                linkedin_url=linkedin_url,
                department=request.department,
                external_links=None,
            )
            if contact:
                hydrate_from_contact(contact)
                contact_id = contact.contact_id

        existing_participant = await self._find_contact_participant(
            deal_id=deal_id,
            email=email_normalized,
            contact_id=contact_id,
            include_deleted=True,
        )
        if existing_participant:
            was_deleted = existing_participant.is_deleted
            # Existing stakeholder identity is an upsert target. Keep the row ID
            # stable so activity and research references do not split across rows.
            updates = request.model_dump(exclude_unset=True)
            if email_normalized:
                updates["email"] = email_normalized
            if name:
                updates["name"] = name
            self._apply_contact_profile_updates(
                contact=await self._get_participant_contact(existing_participant),
                updates=updates,
            )
            self._apply_participant_behavior_updates(existing_participant, updates)
            existing_participant.related_local_id = (
                existing_participant.related_local_id or contact_id
            )
            existing_participant.related_email = (
                email_normalized or existing_participant.related_email
            )
            existing_participant.participant_source = (
                StakeholderSource.CRM.value if contact else StakeholderSource.MANUAL.value
            )
            existing_participant.approval_status = ACTIVE_STAKEHOLDER_STATUS
            existing_participant.is_deleted = False
            existing_participant.updated_at = datetime.now(UTC)
            await self._set_primary_contact_if_first_stakeholder(deal_id, existing_participant)
            await self._finish_write(existing_participant)
            contact = await self._get_participant_contact(existing_participant)
            return StakeholderWriteResult(
                stakeholder=self._participant_detail(existing_participant, contact=contact),
                created=False,
                reactivated=was_deleted,
                reused=not was_deleted,
            )

        participant = await self._ensure_deal_participant(
            deal_id=deal_id,
            contact_id=contact_id,
            email=email_normalized,
            stance=request.stance.value if hasattr(request.stance, "value") else request.stance,
            decision_power=(
                request.decision_power.value
                if hasattr(request.decision_power, "value")
                else request.decision_power
            ),
            motivation=request.motivation,
            pain_points=request.pain_points,
            participant_source=(
                StakeholderSource.CRM.value if contact else StakeholderSource.MANUAL.value
            ),
        )
        if not participant:
            raise StakeholdersServiceError(
                "Unable to create stakeholder participant for this deal",
                status_code=500,
            )

        await self._set_primary_contact_if_first_stakeholder(deal_id, participant)

        await ActivityFeedProjectionService(
            self.session,
            self.organisation_id,
        ).upsert_stakeholder_added(
            participant,
            contact=contact,
            actor_user_id=context.actor_user_id if context else self.user_id,
            source=context.source if context else None,
            source_label=context.source_label if context else None,
            created_via=context.created_via if context else None,
        )

        await self._finish_write(participant)

        contact = await self._get_participant_contact(participant)
        return StakeholderWriteResult(
            stakeholder=self._participant_detail(participant, contact=contact),
            created=True,
            reactivated=False,
            reused=False,
        )

    async def update_stakeholder(
        self,
        deal_id: UUID,
        participant_id: UUID,
        request: UpdateStakeholderRequest,
    ) -> StakeholderDetail:
        """Partially update a stakeholder and mark fields as user-managed."""
        participant = await self.get_stakeholder(deal_id, participant_id)
        if not participant:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        updates = request.model_dump(exclude_unset=True)
        if not updates:
            raise StakeholdersServiceError("No fields provided for update", status_code=400)

        non_nullable_fields = {"stance", "decision_power"}
        null_non_nullable = [
            field for field in non_nullable_fields if field in updates and updates[field] is None
        ]
        if null_non_nullable:
            invalid = ", ".join(sorted(null_non_nullable))
            raise StakeholdersServiceError(
                f"Cannot set the following fields to null: {invalid}",
                status_code=400,
            )

        contact = await self._get_participant_contact(participant)
        self._apply_contact_profile_updates(
            contact=contact,
            updates=updates,
        )
        self._apply_participant_behavior_updates(participant, updates)
        participant.updated_at = datetime.now(UTC)

        await ActivityFeedProjectionService(
            self.session,
            self.organisation_id,
        ).upsert_stakeholder_updated(
            participant,
            contact=contact,
            actor_user_id=self.user_id,
        )
        await self._finish_write(participant)
        contact = await self._get_participant_contact(participant)
        return self._participant_detail(participant, contact=contact)

    async def add_contacts_as_stakeholders(
        self, deal_id: UUID, contact_ids: list[UUID]
    ) -> AddContactsAsStakeholdersResponse:
        """Add selected contacts as stakeholders to a deal.

        Only contacts with valid email addresses are added. Contacts without
        emails or those that already exist as stakeholders are skipped.
        """
        await self._require_deal_control(deal_id)
        added: list[StakeholderDetail] = []
        skipped: list[SkippedContact] = []

        # Fetch all requested contacts through the org boundary; contact IDs in the
        # client payload are not trusted across tenants.
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).in_(contact_ids),
            col(CRMContact.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        contacts_map = {c.contact_id: c for c in result.scalars().all()}

        # Build existing participant membership once so the add loop enforces
        # one active contact participant per deal/contact/email.
        existing_stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        existing_result = await self.session.execute(existing_stmt)
        existing_rows = list(existing_result.scalars().all())
        existing_emails = {
            email for row in existing_rows if (email := normalize_email(row.related_email))
        }
        existing_contact_ids = {
            row.related_local_id for row in existing_rows if row.related_local_id
        }
        internal_integration_id: UUID | None = None

        for contact_id in contact_ids:
            contact = contacts_map.get(contact_id)

            # Ignore stale IDs from the client payload so partial syncs remain resilient.
            if not contact:
                skipped.append(SkippedContact(contact_id=contact_id, reason="Contact not found"))
                continue

            # Skip entries that cannot resolve to contact points; email is required
            # for identity and downstream matching.
            if not contact.email or not contact.email.strip():
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Contact has no email address",
                    )
                )
                continue

            # De-duplicate by email first because it is the matching key in all
            # discovery and dedupe paths.
            contact_email_normalized = normalize_email(contact.email)
            if contact_email_normalized in existing_emails:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Stakeholder with this email already exists",
                    )
                )
                continue

            # De-duplicate by contact_id to prevent the same CRM record from becoming
            # two stakeholders on the same deal.
            if contact_id in existing_contact_ids:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Contact is already a stakeholder",
                    )
                )
                continue

            user_id = self.user_id
            if user_id is None:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Unable to create stakeholder participant",
                    )
                )
                continue

            if internal_integration_id is None:
                # Resolve the internal integration lazily so an all-skipped batch does
                # not create CRM setup side effects.
                setup_result = await InternalCRMSetupService(
                    self.session,
                    self.organisation_id,
                    user_id,
                ).setup_internal_crm(commit=False)
                internal_integration_id = setup_result.integration.integration_id
            contact = await self._ensure_internal_contact_from_selected_contact(
                contact,
                internal_integration_id=internal_integration_id,
            )

            participant = await self._ensure_deal_participant(
                deal_id=deal_id,
                contact_id=contact.contact_id,
                email=contact.email,
                participant_source=StakeholderSource.CRM.value,
                approval_status=ACTIVE_STAKEHOLDER_STATUS,
            )
            if not participant:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Unable to create stakeholder participant",
                    )
                )
                continue

            await ActivityFeedProjectionService(
                self.session,
                self.organisation_id,
            ).upsert_stakeholder_added(
                participant,
                contact=contact,
                actor_user_id=self.user_id,
                source="api",
                source_label="API",
                created_via="stakeholder_add_contacts",
            )
            await self._set_primary_contact_if_first_stakeholder(deal_id, participant)
            added.append(self._participant_detail(participant, contact=contact))

            # These membership sets are batch-local uniqueness state; without them,
            # duplicate IDs later in the same request could create a second row.
            if contact_email_normalized:
                existing_emails.add(contact_email_normalized)
            existing_contact_ids.add(contact_id)

        await self._finish_write()

        return AddContactsAsStakeholdersResponse(
            added=added,
            skipped=skipped,
            total_requested=len(contact_ids),
            total_added=len(added),
            total_skipped=len(skipped),
        )

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("StakeholderWriteService close exception", exc_info=True)

    async def _require_deal_control(self, deal_id: UUID) -> None:
        if self._access.actor is None:
            return
        try:
            await self._access.require_deal(deal_id, "control")
        except CRMRecordAccessError as exc:
            raise StakeholdersServiceError(exc.message, exc.status_code) from exc
