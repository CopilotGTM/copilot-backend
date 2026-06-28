"""Participant-backed stakeholder semantic writes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
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
from src.domain.models.master_tables.organisation_models import Organisation
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
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.contacts.contracts import ContactWriteContext
from src.service.crm.contacts.write_service import ContactWriteService
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.participants.errors import StakeholdersServiceError
from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)
from src.service.crm.timeline.deal_timeline_sync_service import (
    build_stakeholder_timeline_event,
)

logger = get_logger("stakeholder_write_service")

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
    """Participant-backed stakeholder semantic writes."""

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
        await self.session.flush()
        if obj is not None:
            await self.session.refresh(obj)

    async def _get_internal_domains(self) -> set[str]:
        """Load the organisation's internal email domains from Organisation.domain.

        Uses a master session because Organisation lives in the public schema,
        which org-scoped DB users cannot read.
        """
        stmt = select(col(Organisation.domain)).where(
            col(Organisation.organisation_id) == self.organisation_id,
        )
        async for master_session in acquire_master_async_session():
            result = await master_session.execute(stmt)
        domains_raw = result.scalar_one_or_none()
        domains: set[str] = set()
        if domains_raw and isinstance(domains_raw, list):
            for d in domains_raw:
                if not d or not d.strip():
                    continue
                d_lower = d.strip().lower()
                domains.add(d_lower)
                # Strip www. prefix so "www.acme.com" also blocks "@acme.com"
                if d_lower.startswith("www."):
                    domains.add(d_lower[4:])
        return domains

    async def _is_internal_email(self, email: str) -> bool:
        """Check whether an email belongs to an internal org domain."""
        if not email or "@" not in email:
            return False
        domain = email.strip().lower().split("@")[-1]
        internal_domains = await self._get_internal_domains()
        return domain in internal_domains

    async def _ensure_contact_for_stakeholder(
        self,
        email: str,
        name: str | None,
        job_title: str | None,
        phone_number: str | None,
        linkedin_url: str | None = None,
        department: str | None = None,
        external_links: list[dict] | None = None,
    ) -> UUID | None:
        """Find or create a CRMContact for a manually-added stakeholder.

        Returns the contact_id if found or created, or None when business rules
        intentionally skip durable contact materialization.
        """
        email_normalized = email.lower().strip()

        # Skip contact creation for internal team members and common personal email providers
        if await self._is_internal_email(email_normalized):
            return None
        domain = email_normalized.split("@")[-1] if "@" in email_normalized else ""
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
        return result.contact.contact_id

    async def _ensure_deal_participant(
        self,
        deal_id: UUID,
        contact_id: UUID | None,
        email: str,
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
        approval_status: str = ApprovalStatus.APPROVED.value,
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

        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        if not deal_result.scalar_one_or_none():
            return None

        email_normalized = email.lower().strip()

        participant = await self._find_contact_participant(
            deal_id=deal_id,
            email=email_normalized,
            contact_id=contact_id,
            include_deleted=True,
        )
        if participant:
            participant.related_local_id = participant.related_local_id or contact_id
            participant.related_email = email_normalized
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

        email_normalized = contact.email.lower().strip() if contact.email else None
        if email_normalized:
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
            predicates.append(col(CRMDealParticipant.participant_id) == participant_id)
        else:
            identity_predicates = []
            if contact_id:
                identity_predicates.append(col(CRMDealParticipant.related_local_id) == contact_id)
            if email:
                email_normalized = email.lower().strip()
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

    @staticmethod
    def _participant_timeline_stakeholder(
        participant: CRMDealParticipant,
    ) -> Any:
        """Build the stakeholder-shaped event payload until timeline is cut over."""
        return SimpleNamespace(
            participant_id=participant.participant_id,
            related_email=participant.related_email,
            related_name=participant.related_name,
            job_title=participant.job_title,
            participant_source=participant.participant_source,
            stance=participant.stance,
            decision_power=participant.decision_power,
            approval_status=participant.approval_status,
            created_at=participant.created_at,
            updated_at=participant.updated_at,
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
            serialized_links = self._serialize_external_links(updates["external_links"])
            if contact:
                if serialized_links is None:
                    contact_properties.pop("external_links", None)
                else:
                    contact_properties["external_links"] = serialized_links
            participant.external_links = serialized_links
            linkedin_url = self._extract_linkedin_url(serialized_links)
            if linkedin_url:
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

        email_normalized = email.lower().strip()
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

    @staticmethod
    def _serialize_external_links(external_links: list | None) -> list[dict] | None:
        """Serialize external links to JSON-friendly dicts.

        The service stores external links as JSON-like payloads; callers can pass
        either DTO instances or raw dictionaries from form payloads.
        """
        if external_links is None:
            return None

        serialized: list[dict] = []
        for link in external_links:
            if isinstance(link, dict):
                serialized.append(link)
                continue

            added_at = getattr(link, "added_at", None)
            serialized.append(
                {
                    "platform": link.platform,
                    "url": link.url,
                    "label": link.label,
                    "added_at": added_at.isoformat() if added_at else None,
                }
            )

        return serialized

    @staticmethod
    def _extract_linkedin_url(external_links: list[dict] | None) -> str | None:
        """Keep LinkedIn first-class without dropping the full external link list."""
        for link in external_links or []:
            platform = str(link.get("platform") or link.get("type") or "").lower()
            url = link.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            if platform == "linkedin" or "linkedin." in url.lower():
                return url.strip()
        return None

    async def get_stakeholder(
        self, deal_id: UUID, participant_id: UUID
    ) -> CRMDealParticipant | None:
        """Get the canonical participant-backed stakeholder by participant ID."""
        return await self._find_contact_participant(
            deal_id=deal_id,
            participant_id=participant_id,
        )

    async def approve_stakeholder(self, deal_id: UUID, participant_id: UUID) -> StakeholderDetail:
        """Approve a PENDING stakeholder for deal matching.

        Args:
            deal_id: The deal ID.
            participant_id: The participant to approve.

        Returns:
            The updated stakeholder.

        Raises:
            StakeholdersServiceError: If stakeholder not found or already approved.
        """
        stakeholder = await self.get_stakeholder(deal_id, participant_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.APPROVED.value:
            raise StakeholdersServiceError("Stakeholder is already approved", status_code=400)

        stakeholder.approval_status = ApprovalStatus.APPROVED.value
        stakeholder.updated_at = datetime.now(UTC)
        await self._finish_write(stakeholder)
        contact = await self._get_participant_contact(stakeholder)
        return self._participant_detail(stakeholder, contact=contact)

    async def reject_stakeholder(self, deal_id: UUID, participant_id: UUID) -> StakeholderDetail:
        """Reject a stakeholder to exclude from deal matching.

        Args:
            deal_id: The deal ID.
            participant_id: The participant to reject.

        Returns:
            The updated stakeholder.

        Raises:
            StakeholdersServiceError: If stakeholder not found or already rejected.
        """
        stakeholder = await self.get_stakeholder(deal_id, participant_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.REJECTED.value:
            raise StakeholdersServiceError("Stakeholder is already rejected", status_code=400)

        stakeholder.approval_status = ApprovalStatus.REJECTED.value
        stakeholder.updated_at = datetime.now(UTC)
        await self._finish_write(stakeholder)
        contact = await self._get_participant_contact(stakeholder)
        return self._participant_detail(stakeholder, contact=contact)

    async def remove_stakeholder(self, deal_id: UUID, participant_id: UUID) -> StakeholderDetail:
        """Remove an APPROVED stakeholder from the deal.

        This marks the stakeholder as REMOVED, excluding them from deal matching
        and stakeholder lists. Unlike REJECTED, REMOVED indicates the stakeholder
        was previously active but has been removed by the user.

        Args:
            deal_id: The deal ID.
            participant_id: The participant to remove.

        Returns:
            The updated stakeholder with REMOVED status.

        Raises:
            StakeholdersServiceError: If stakeholder not found or not in APPROVED status.
        """
        stakeholder = await self.get_stakeholder(deal_id, participant_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.REMOVED.value:
            raise StakeholdersServiceError("Stakeholder is already removed", status_code=400)

        if stakeholder.approval_status != ApprovalStatus.APPROVED.value:
            raise StakeholdersServiceError(
                "Only approved stakeholders can be removed", status_code=400
            )

        stakeholder.approval_status = ApprovalStatus.REMOVED.value
        stakeholder.is_deleted = True
        stakeholder.updated_at = datetime.now(UTC)

        # Remove any existing STAKEHOLDER_REMOVED timeline event for this stakeholder
        # (handles re-add then re-remove scenario without violating unique constraint)
        # Uses raw DELETE to avoid ORM flush/lazy-load issues (MissingGreenlet)
        from sqlalchemy import delete
        from src.domain.models.organisation_tables.sync.timeline_models import (
            DealTimelineEvent,
        )

        delete_stmt = delete(DealTimelineEvent).where(
            col(DealTimelineEvent.deal_id) == deal_id,
            col(DealTimelineEvent.event_type) == TimelineEventType.STAKEHOLDER_REMOVED,
            col(DealTimelineEvent.source_record_id) == str(stakeholder.participant_id),
        )
        await self.session.execute(delete_stmt)
        # Refresh stakeholder after DELETE to prevent expired-attribute lazy-load errors
        await self.session.refresh(stakeholder)

        # Recreate the removal event in the same flush context so downstream
        # subscribers see a single ordered timeline sequence per update.
        timeline_event = build_stakeholder_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            stakeholder=self._participant_timeline_stakeholder(stakeholder),
            event_type=TimelineEventType.STAKEHOLDER_REMOVED,
        )
        self.session.add(timeline_event)

        await self._finish_write(stakeholder)
        contact = await self._get_participant_contact(stakeholder)
        return self._participant_detail(stakeholder, contact=contact)

    async def create_stakeholder(
        self, deal_id: UUID, request: CreateStakeholderRequest
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
        # Guardrail: reject internal team members
        email_normalized = request.email.lower().strip()
        if await self._is_internal_email(email_normalized):
            raise StakeholdersServiceError(
                "This email belongs to an internal team member and cannot be added "
                "as a stakeholder. Stakeholders must be external contacts only.",
                status_code=403,
            )

        external_links_data = self._serialize_external_links(request.external_links)
        linkedin_url = request.linkedin_url or self._extract_linkedin_url(external_links_data)

        # Contact owns durable profile fields; participant keeps a deal-local
        # snapshot for list reads.
        contact_id = await self._ensure_contact_for_stakeholder(
            email=email_normalized,
            name=request.name,
            job_title=request.job_title,
            phone_number=request.phone_number,
            linkedin_url=linkedin_url,
            department=request.department,
            external_links=external_links_data,
        )

        existing_participant = await self._find_contact_participant(
            deal_id=deal_id,
            email=email_normalized,
            contact_id=contact_id,
            include_deleted=True,
        )
        if existing_participant:
            status = existing_participant.approval_status
            conflict_data = {
                "participant_id": str(existing_participant.participant_id),
                "approval_status": status.value if hasattr(status, "value") else str(status),
                "is_deleted": existing_participant.is_deleted,
            }

            if status == ApprovalStatus.APPROVED.value and not existing_participant.is_deleted:
                raise StakeholdersServiceError(
                    "A stakeholder with this email already exists for this deal",
                    status_code=409,
                    data=conflict_data,
                )

            if status == ApprovalStatus.REMOVED.value or existing_participant.is_deleted:
                updates = request.model_dump(exclude_unset=True)
                updates["email"] = email_normalized
                self._apply_contact_profile_updates(
                    contact=await self._get_participant_contact(existing_participant),
                    participant=existing_participant,
                    updates=updates,
                )
                self._apply_participant_behavior_updates(existing_participant, updates)
                existing_participant.related_local_id = (
                    existing_participant.related_local_id or contact_id
                )
                existing_participant.related_email = email_normalized
                existing_participant.participant_source = StakeholderSource.MANUAL.value
                existing_participant.approval_status = ApprovalStatus.APPROVED.value
                existing_participant.is_deleted = False
                existing_participant.updated_at = datetime.now(UTC)
                await self._finish_write(existing_participant)
                contact = await self._get_participant_contact(existing_participant)
                return self._participant_detail(existing_participant, contact=contact)

            # Stakeholder exists but is REJECTED or soft-deleted
            # Let the user decide — they can re-approve via the approve endpoint
            raise StakeholdersServiceError(
                f"Stakeholder exists but is currently {conflict_data['approval_status']}. "
                "Use the approve endpoint to reactivate it.",
                status_code=409,
                data=conflict_data,
            )

        participant = await self._ensure_deal_participant(
            deal_id=deal_id,
            contact_id=contact_id,
            email=email_normalized,
            name=request.name,
            job_title=request.job_title,
            phone_number=request.phone_number,
            linkedin_url=linkedin_url,
            department=request.department,
            external_links=external_links_data,
            stance=request.stance.value if hasattr(request.stance, "value") else request.stance,
            decision_power=(
                request.decision_power.value
                if hasattr(request.decision_power, "value")
                else request.decision_power
            ),
            motivation=request.motivation,
            pain_points=request.pain_points,
        )
        if not participant:
            raise StakeholdersServiceError(
                "Unable to create stakeholder participant for this deal",
                status_code=500,
            )

        # Persist the add-event so downstream consumers can replay audit history
        # and rebuild timelines without inferring it from row timestamps alone.
        timeline_event = build_stakeholder_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            stakeholder=self._participant_timeline_stakeholder(participant),
            event_type=TimelineEventType.STAKEHOLDER_ADDED,
        )
        self.session.add(timeline_event)

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

        # Fetch all requested contacts
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
            row.related_email.lower().strip() for row in existing_rows if row.related_email
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
            contact_email_normalized = contact.email.lower().strip()
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

            if internal_integration_id is None:
                setup_result = await InternalCRMSetupService(
                    self.session,
                    self.organisation_id,
                    self.user_id,
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
                approval_status=ApprovalStatus.APPROVED.value,
            )
            if not participant:
                skipped.append(
                    SkippedContact(
                        contact_id=contact_id,
                        reason="Unable to create stakeholder participant",
                    )
                )
                continue

            # Emit timeline event so stakeholder additions are recoverable in activity
            # feeds and support incident debugging.
            timeline_event = build_stakeholder_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                stakeholder=self._participant_timeline_stakeholder(participant),
                event_type=TimelineEventType.STAKEHOLDER_ADDED,
            )
            self.session.add(timeline_event)
            added.append(self._participant_detail(participant, contact=contact))

            # Track for response - add email to existing set
            existing_emails.add(contact.email.lower().strip())
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
