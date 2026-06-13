"""CRM service layer for org-scoped stakeholder lifecycle operations.

This service owns write-path business rules for creating, updating, and syncing
stakeholders on a per-organization deal scope. All DB queries include
`organisation_id` checks so tenant isolation is enforced at the data-access
layer.
"""

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
    AvailableContactItem,
    CreateStakeholderRequest,
    SkippedContact,
    StakeholderDetail,
    StakeholderListResponse,
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
from src.service.crm.insights.participant_stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)
from src.service.crm.timeline.deal_timeline_sync_service import (
    build_stakeholder_timeline_event,
)

logger = get_logger("stakeholders_service")

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


class StakeholdersServiceError(Exception):
    """Custom exception for stakeholders service errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        data: dict | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


class StakeholdersService:
    """Service for managing CRM deal stakeholders.

    Boundaries:
    - Called with one org-scoped session and explicit organization context.
    - Responsible for idempotent writes and timeline visibility.
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
        department: str | None = None,
        external_links: list[dict] | None = None,
    ) -> UUID | None:
        """Find or create a CRMContact for a manually-added stakeholder.

        Returns the contact_id if found or created, None if no internal
        CRM integration exists (contact creation skipped).
        """
        from src.service.crm.internal.internal_crm_setup_service import (
            InternalCRMSetupService,
        )

        email_normalized = email.lower().strip()

        # Skip contact creation for internal team members and common personal email providers
        if await self._is_internal_email(email_normalized):
            return None
        domain = email_normalized.split("@")[-1] if "@" in email_normalized else ""
        if domain in PERSONAL_EMAIL_DOMAINS:
            return None

        # If the same email already exists in this org, reuse it to avoid creating
        # duplicate local CRM contacts for multiple stakeholders.
        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.email) == email_normalized,
        )
        result = await self.session.execute(stmt)
        existing_contact = result.scalars().first()
        if existing_contact:
            if name:
                existing_contact.full_name = name
            if job_title:
                existing_contact.job_title = job_title
            if phone_number:
                existing_contact.phone = phone_number
            if department or external_links:
                properties = dict(existing_contact.properties or {})
                if department:
                    properties["department"] = department
                if external_links:
                    properties["external_links"] = external_links
                existing_contact.properties = properties
            return existing_contact.contact_id

        # No existing contact — create one under the internal CRM integration
        if not self.user_id:
            return None
        setup_service = InternalCRMSetupService(self.session, self.organisation_id, self.user_id)
        integration = await setup_service.get_internal_integration()
        if not integration:
            return None

        from uuid import uuid4

        now = datetime.now(UTC)
        contact_id = uuid4()
        contact = CRMContact(
            contact_id=contact_id,
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            email=email_normalized,
            full_name=name,
            job_title=job_title,
            phone=phone_number,
            properties={
                key: value
                for key, value in {
                    "department": department,
                    "external_links": external_links,
                }.items()
                if value
            },
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(contact)
        await self.session.flush()
        return contact.contact_id

    async def _ensure_deal_participant(
        self,
        deal_id: UUID,
        contact_id: UUID | None,
        email: str,
        name: str | None,
        job_title: str | None = None,
        phone_number: str | None = None,
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

    async def list_stakeholders_by_deal(
        self,
        deal_id: UUID,
        include_pending: bool = True,
        include_rejected: bool = False,
    ) -> StakeholderListResponse:
        """Get stakeholders associated with a specific deal.

        Args:
            deal_id: The deal to get stakeholders for.
            include_pending: Include PENDING stakeholders (default True).
            include_rejected: Include REJECTED stakeholders (default False).

        Returns:
            StakeholderListResponse with company_domain for frontend validation
            and list of stakeholders filtered by approval_status.
        """
        # Fetch deal fields needed by the UI contract before filtering stakeholders.
        # Keeping query-scoped org checks here prevents cross-org data leakage in list
        # endpoints.
        deal_stmt = select(CRMDeal).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        deal = deal_result.scalar_one_or_none()

        company_domain = deal.company_domain if deal else None
        company_name = deal.company_name if deal else None

        statuses = [ApprovalStatus.APPROVED.value]
        if include_pending:
            statuses.append(ApprovalStatus.PENDING.value)
        if include_rejected:
            statuses.append(ApprovalStatus.REJECTED.value)

        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.approval_status).in_(statuses),
        )
        result = await self.session.execute(stmt)
        participants = list(result.scalars().all())

        return StakeholderListResponse(
            company_domain=company_domain,
            company_name=company_name,
            stakeholders=[
                StakeholderDetail.model_validate(participant_to_stakeholder_detail_dict(p))
                for p in participants
            ],
        )

    async def get_stakeholder(
        self, deal_id: UUID, stakeholder_id: UUID
    ) -> CRMDealParticipant | None:
        """Get the canonical participant-backed stakeholder by public ID."""
        return await self._find_contact_participant(
            deal_id=deal_id,
            participant_id=stakeholder_id,
        )

    async def get_stakeholder_detail(self, deal_id: UUID, stakeholder_id: UUID) -> dict | None:
        """Get full participant-backed stakeholder detail.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder ID.

        Returns:
            Dict with full stakeholder detail and engagement history, or None if not found.
        """
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.participant_id) == stakeholder_id,
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        participant = result.scalar_one_or_none()
        if not participant:
            return None

        contact = None
        if participant.related_local_id:
            contact_stmt = select(CRMContact).where(
                col(CRMContact.contact_id) == participant.related_local_id,
                col(CRMContact.organisation_id) == self.organisation_id,
                col(CRMContact.is_deleted).is_(False),
            )
            contact_result = await self.session.execute(contact_stmt)
            contact = contact_result.scalar_one_or_none()

        payload = participant_to_stakeholder_detail_dict(participant, contact=contact)
        payload["engagement_history"] = []
        payload["active_research_job"] = None
        return payload

    async def approve_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> StakeholderDetail:
        """Approve a PENDING stakeholder for deal matching.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder to approve.

        Returns:
            The updated stakeholder.

        Raises:
            StakeholdersServiceError: If stakeholder not found or already approved.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.APPROVED.value:
            raise StakeholdersServiceError("Stakeholder is already approved", status_code=400)

        stakeholder.approval_status = ApprovalStatus.APPROVED.value
        stakeholder.updated_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(stakeholder)
        contact = await self._get_participant_contact(stakeholder)
        return self._participant_detail(stakeholder, contact=contact)

    async def reject_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> StakeholderDetail:
        """Reject a stakeholder to exclude from deal matching.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder to reject.

        Returns:
            The updated stakeholder.

        Raises:
            StakeholdersServiceError: If stakeholder not found or already rejected.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        if stakeholder.approval_status == ApprovalStatus.REJECTED.value:
            raise StakeholdersServiceError("Stakeholder is already rejected", status_code=400)

        stakeholder.approval_status = ApprovalStatus.REJECTED.value
        stakeholder.updated_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(stakeholder)
        contact = await self._get_participant_contact(stakeholder)
        return self._participant_detail(stakeholder, contact=contact)

    async def remove_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> StakeholderDetail:
        """Remove an APPROVED stakeholder from the deal.

        This marks the stakeholder as REMOVED, excluding them from deal matching
        and stakeholder lists. Unlike REJECTED, REMOVED indicates the stakeholder
        was previously active but has been removed by the user.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder to remove.

        Returns:
            The updated stakeholder with REMOVED status.

        Raises:
            StakeholdersServiceError: If stakeholder not found or not in APPROVED status.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
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

        await self.session.commit()
        await self.session.refresh(stakeholder)
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

        # Contact owns durable profile fields; participant keeps a deal-local
        # snapshot for list reads.
        contact_id = await self._ensure_contact_for_stakeholder(
            email=email_normalized,
            name=request.name,
            job_title=request.job_title,
            phone_number=request.phone_number,
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
                "stakeholder_id": str(existing_participant.participant_id),
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
                await self.session.commit()
                await self.session.refresh(existing_participant)
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

        await self.session.commit()
        await self.session.refresh(participant)

        contact = await self._get_participant_contact(participant)
        return self._participant_detail(participant, contact=contact)

    async def update_stakeholder(
        self,
        deal_id: UUID,
        stakeholder_id: UUID,
        request: UpdateStakeholderRequest,
    ) -> StakeholderDetail:
        """Partially update a stakeholder and mark fields as user-managed."""
        participant = await self.get_stakeholder(deal_id, stakeholder_id)
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

        await self.session.commit()
        await self.session.refresh(participant)
        contact = await self._get_participant_contact(participant)
        return self._participant_detail(participant, contact=contact)

    async def list_available_contacts(
        self, deal_id: UUID, query: str | None = None, limit: int = 50
    ) -> list[AvailableContactItem]:
        """List contacts that are not yet stakeholders for the deal.

        Only returns contacts with valid email addresses, as email is required
        for stakeholder identification.
        """
        contact_id_subq = select(col(CRMDealParticipant.related_local_id)).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.related_local_id).isnot(None),
        )
        email_subq = select(func.lower(func.trim(col(CRMDealParticipant.related_email)))).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.CONTACT.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.related_email).isnot(None),
            col(CRMDealParticipant.related_email) != "",
        )

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).not_in(contact_id_subq),
            func.lower(func.trim(col(CRMContact.email))).not_in(email_subq),
            col(CRMContact.email).isnot(None),  # Only contacts with email
            col(CRMContact.email) != "",  # Exclude empty email strings
        )

        if query:
            search = f"%{query}%"
            stmt = stmt.where(
                (col(CRMContact.first_name).ilike(search))
                | (col(CRMContact.last_name).ilike(search))
                | (col(CRMContact.full_name).ilike(search))
                | (col(CRMContact.email).ilike(search))
                | (col(CRMContact.company_name).ilike(search))
            )

        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        contacts = list(result.scalars().all())

        available_contacts: list[AvailableContactItem] = []
        for c in contacts:
            if not c.email:
                continue
            available_contacts.append(
                AvailableContactItem(
                    contact_id=c.contact_id,
                    external_id=None,
                    email=c.email,
                    first_name=c.first_name,
                    last_name=c.last_name,
                    full_name=c.full_name,
                    job_title=c.job_title,
                    company_name=c.company_name,
                    phone=c.phone,
                    lifecycle_stage=c.lifecycle_stage,
                    last_activity_at=c.last_activity_at,
                )
            )
        return available_contacts

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
            if contact.email.lower() in existing_emails:
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
            existing_emails.add(contact.email.lower())
            existing_contact_ids.add(contact_id)

        # Commit all new participants
        await self.session.commit()

        return AddContactsAsStakeholdersResponse(
            added=added,
            skipped=skipped,
            total_requested=len(contact_ids),
            total_added=len(added),
            total_skipped=len(skipped),
        )

    async def has_deal_owner_or_participants(self, deal_id: UUID) -> bool:
        """Return True if a deal has an owner or any active participants."""
        deal_stmt = select(col(CRMDeal.owner_id)).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        deal_row = deal_result.first()
        if not deal_row:
            return False

        if deal_row[0]:
            return True

        participants_stmt = (
            select(func.count())
            .select_from(CRMDealParticipant)
            .where(
                col(CRMDealParticipant.deal_id) == deal_id,
                col(CRMDealParticipant.organisation_id) == self.organisation_id,
                col(CRMDealParticipant.is_deleted).is_(False),
            )
        )
        participants_result = await self.session.execute(participants_stmt)
        participant_count = participants_result.scalar() or 0
        return participant_count > 0

    async def trigger_deal_mapping_if_ready(self, deal_id: UUID) -> None:
        """Trigger calendar/email deal matching after stakeholder changes, if allowed."""
        try:
            should_trigger = await self.has_deal_owner_or_participants(deal_id)
            if not should_trigger:
                return

            from src.workers.deal_worker.client import send_deal_task_async
            from src.workers.deal_worker.services.deal_change_detector import (
                StakeholderChangeType,
            )

            org_id = str(self.organisation_id)
            deal_id_str = str(deal_id)

            # Dispatch calendar + email matching, then stakeholder sync
            # Using send_deal_task_async to avoid importing task modules in the API process
            await send_deal_task_async(
                "deal.match_calendar_to_deals",
                kwargs={"organisation_id": org_id, "deal_id": deal_id_str},
            )
            await send_deal_task_async(
                "deal.match_email_to_deals",
                kwargs={"organisation_id": org_id, "deal_id": deal_id_str},
            )
            await send_deal_task_async(
                "stakeholder.sync_deal_stakeholders",
                kwargs={
                    "organisation_id": org_id,
                    "deal_id": deal_id_str,
                    "change_type": StakeholderChangeType.MANUAL_REFRESH.value,
                    "integration_id": None,
                    "integration_type": None,
                    "trigger_rematching": False,
                },
            )
        except Exception:
            logger.warning(
                "Failed to trigger deal mapping after stakeholder update for deal %s",
                deal_id,
                exc_info=True,
            )

    async def close(self):
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("StakeholdersService close exception", exc_info=True)
