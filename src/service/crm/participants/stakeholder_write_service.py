"""Participant-backed stakeholder semantic writes.

Called through the legacy stakeholder facade, agent tools, and deal UI actions.
This module owns writes to the canonical CRMDealParticipant rows while mirroring
durable person profile fields onto CRMContact whenever a contact can be linked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
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
from src.domain.models.organisation_tables.sync.crm.stakeholders import StakeholderSource
from src.infra.utils.logger import get_logger
from src.service.crm.activity_feed_projection_service import ActivityFeedProjectionService
from src.service.crm.contacts.contracts import ContactWriteContext
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.participants.contracts import StakeholderWriteContext
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
    ):
        """Capture immutable tenant context for the service instance."""
        self.org_session = org_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    @property
    def session(self) -> AsyncSession:
        """Return the org-scoped DB session for this service instance."""
        return self.org_session

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
        name: str | None,
        job_title: str | None = None,
        phone_number: str | None = None,
        linkedin_url: str | None = None,
        department: str | None = None,
        external_links: list[dict] | None = None,
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
            linkedin_url=linkedin_url,
            include_deleted=True,
        )
        if participant:
            # Reactivate the existing participant instead of inserting a duplicate.
            # Dedupe must include soft-deleted rows so remove/re-add keeps history on
            # the same participant ID for activity and research references.
            participant.related_local_id = participant.related_local_id or contact_id
            participant.related_email = email_normalized or participant.related_email
            participant.related_name = name or participant.related_name
            participant.job_title = job_title or participant.job_title
            participant.department = department or participant.department
            participant.phone_number = phone_number or participant.phone_number
            participant.linkedin_url = linkedin_url or participant.linkedin_url
            participant.external_links = external_links or participant.external_links
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
            related_name=name,
            related_email=email_normalized,
            job_title=job_title,
            department=department,
            phone_number=phone_number,
            linkedin_url=linkedin_url,
            external_links=external_links,
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
        linkedin_url: str | None = None,
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
            if linkedin_url:
                linkedin_normalized = clean_optional_string(linkedin_url)
                if linkedin_normalized:
                    identity_predicates.append(
                        func.lower(func.trim(col(CRMDealParticipant.linkedin_url)))
                        == linkedin_normalized.lower()
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
        participant.relationship_role = participant.relationship_role or "primary_contact"
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
        participant: CRMDealParticipant,
        updates: dict,
    ) -> None:
        """Write durable profile fields to contact first, then refresh snapshots."""
        if "name" in updates:
            if contact:
                contact.full_name = updates["name"]
            participant.related_name = updates["name"]

        if "job_title" in updates:
            if contact:
                contact.job_title = updates["job_title"]
            participant.job_title = updates["job_title"]

        if "phone_number" in updates:
            if contact:
                contact.phone = updates["phone_number"]
            participant.phone_number = updates["phone_number"]

        if "linkedin_url" in updates:
            if contact:
                contact.linkedin_url = updates["linkedin_url"]
            participant.linkedin_url = updates["linkedin_url"]

        contact_properties = dict(contact.properties or {}) if contact else {}
        if "department" in updates:
            if contact:
                if updates["department"] is None:
                    contact_properties.pop("department", None)
                else:
                    contact_properties["department"] = updates["department"]
            participant.department = updates["department"]

        if "external_links" in updates:
            serialized_links = serialize_external_links(updates["external_links"])
            if contact:
                if serialized_links is None:
                    contact_properties.pop("external_links", None)
                else:
                    contact_properties["external_links"] = serialized_links
            participant.external_links = serialized_links
            linkedin_url = extract_linkedin_url(serialized_links)
            if linkedin_url:
                # LinkedIn may arrive inside the generic external_links payload.
                # Mirror it to the first-class field so later identity matching can
                # find the same stakeholder without parsing the links blob.
                if contact:
                    contact.linkedin_url = linkedin_url
                participant.linkedin_url = linkedin_url

        if contact:
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

    @staticmethod
    def _contacts_conflict(*contacts: CRMContact | None) -> bool:
        """Return true when strong identity fields point at different contacts."""
        contact_ids = {contact.contact_id for contact in contacts if contact is not None}
        return len(contact_ids) > 1

    async def get_stakeholder(
        self, deal_id: UUID, participant_id: UUID
    ) -> CRMDealParticipant | None:
        """Get the canonical participant-backed stakeholder by participant ID."""
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
        """Manually create a new stakeholder for a deal.

        Args:
            deal_id: The deal to add the stakeholder to.
            request: The stakeholder creation request with user-provided details.

        Returns:
            The created stakeholder as StakeholderDetail DTO.

        Raises:
            StakeholdersServiceError: If email already exists for this deal.
        """
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

        # Guardrail: reject internal team members
        if email_normalized and await self._is_internal_email(email_normalized):
            raise StakeholdersServiceError(
                "This email belongs to an internal team member and cannot be added "
                "as a stakeholder. Stakeholders must be external contacts only.",
                status_code=403,
            )

        # Contact owns durable profile fields; participant keeps a deal-local
        # snapshot for list reads.
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
            linkedin_url=linkedin_url,
            include_deleted=True,
        )
        if existing_participant:
            # Existing stakeholder identity is an upsert target. Keep the row ID
            # stable so activity and research references do not split across rows.
            updates = request.model_dump(exclude_unset=True)
            if email_normalized:
                updates["email"] = email_normalized
            if name:
                updates["name"] = name
            self._apply_contact_profile_updates(
                contact=await self._get_participant_contact(existing_participant),
                participant=existing_participant,
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
            if contact and contact.contact_research:
                existing_participant.stakeholder_research = contact.contact_research
                existing_participant.last_researched_at = contact.last_researched_at
            existing_participant.approval_status = ACTIVE_STAKEHOLDER_STATUS
            existing_participant.is_deleted = False
            existing_participant.updated_at = datetime.now(UTC)
            await self._set_primary_contact_if_first_stakeholder(deal_id, existing_participant)
            await self._finish_write(existing_participant)
            contact = await self._get_participant_contact(existing_participant)
            return self._participant_detail(existing_participant, contact=contact)

        participant = await self._ensure_deal_participant(
            deal_id=deal_id,
            contact_id=contact_id,
            email=email_normalized,
            name=name,
            job_title=request.job_title,
            phone_number=request.phone_number,
            linkedin_url=linkedin_url,
            department=request.department,
            external_links=None,
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
        if contact and contact.contact_research:
            participant.stakeholder_research = contact.contact_research
            participant.last_researched_at = contact.last_researched_at

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
        return self._participant_detail(participant, contact=contact)

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
            participant=participant,
            updates=updates,
        )
        self._apply_participant_behavior_updates(participant, updates)
        participant.updated_at = datetime.now(UTC)

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
        added: list[StakeholderDetail] = []
        skipped: list[SkippedContact] = []

        # Fetch all requested contacts through the org boundary; contact IDs in the
        # client payload are not trusted across tenants.
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).in_(contact_ids),
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

            # Build a name from CRM contact fields so created rows always include a
            # human-friendly identifier when possible.
            name = (
                contact.full_name
                or " ".join(filter(None, [contact.first_name, contact.last_name]))
                or None
            )

            properties = dict(contact.properties or {})
            participant = await self._ensure_deal_participant(
                deal_id=deal_id,
                contact_id=contact.contact_id,
                email=contact.email,
                name=name,
                job_title=contact.job_title,
                phone_number=contact.phone or contact.mobile_phone,
                linkedin_url=contact.linkedin_url,
                department=properties.get("department"),
                external_links=properties.get("external_links")
                if isinstance(properties.get("external_links"), list)
                else None,
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
