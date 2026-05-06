"""CRM service layer for org-scoped stakeholder lifecycle operations.

This service owns write-path business rules for creating, updating, and syncing
stakeholders on a per-organization deal scope. All DB queries include
`organisation_id` checks so tenant isolation is enforced at the data-access
layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
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
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealParticipant
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
    DealStakeholder,
    StakeholderSource,
)
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventType
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
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
            external_id=str(contact_id),
            email=email_normalized,
            full_name=name,
            job_title=job_title,
            phone=phone_number,
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(contact)
        await self.session.flush()
        return contact.contact_id

    async def _ensure_deal_participant(
        self,
        deal_id: UUID,
        contact_id: UUID,
        email: str,
        name: str | None,
    ) -> bool:
        """Create a CRMDealParticipant if the deal belongs to the internal CRM.

        Returns True if a participant was created, False if skipped.
        External CRM deals are never modified.
        """
        from src.domain.models.organisation_tables.sync.crm.deals import (
            CRMDeal,
            CRMDealParticipant,
            CRMParticipantType,
        )
        from src.service.crm.internal.internal_crm_setup_service import (
            InternalCRMSetupService,
        )

        if not self.user_id:
            return False

        setup_service = InternalCRMSetupService(self.session, self.organisation_id, self.user_id)
        integration = await setup_service.get_internal_integration()
        if not integration:
            return False

        # Validate the deal belongs to this org and internal CRM so participant writes
        # stay inside CRM-scoped data; external deals must not get internal
        # CRM participants.
        deal = await self.session.get(CRMDeal, deal_id)
        if (
            not deal
            or deal.organisation_id != self.organisation_id
            or deal.integration_id != integration.integration_id
        ):
            return False

        email_normalized = email.lower().strip()

        # Preserve idempotency: if this contact is already linked on the deal,
        # skip re-adding the participant record.
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.deal_id) == deal_id,
            col(CRMDealParticipant.related_email) == email_normalized,
            col(CRMDealParticipant.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        if result.scalars().first():
            return False

        from uuid import uuid4

        now = datetime.now(UTC)
        participant_id = uuid4()
        participant = CRMDealParticipant(
            participant_id=participant_id,
            organisation_id=self.organisation_id,
            integration_id=integration.integration_id,
            external_id=str(participant_id),
            deal_id=deal_id,
            participant_type=CRMParticipantType.CONTACT,
            related_external_id=str(contact_id),
            related_local_id=contact_id,
            related_name=name,
            related_email=email_normalized,
            synced_at=now,
            source_modified_at=now,
            last_seen_at=now,
            data_payload={"source": "manual_stakeholder"},
        )
        self.session.add(participant)
        return True

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

    async def _reactivate_removed_stakeholder(
        self,
        deal_id: UUID,
        stakeholder: DealStakeholder,
        request: CreateStakeholderRequest,
        email_normalized: str,
    ) -> StakeholderDetail:
        """Restore a previously removed stakeholder from the manual add flow."""
        contact_id = stakeholder.contact_id
        if not contact_id:
            contact_id = await self._ensure_contact_for_stakeholder(
                email=email_normalized,
                name=request.name,
                job_title=request.job_title,
                phone_number=request.phone_number,
            )
            stakeholder.contact_id = contact_id

        if contact_id:
            await self._ensure_deal_participant(
                deal_id=deal_id,
                contact_id=contact_id,
                email=email_normalized,
                name=request.name or stakeholder.name,
            )

        explicit_fields = request.model_fields_set
        if "name" in explicit_fields:
            stakeholder.name = request.name
        if "job_title" in explicit_fields:
            stakeholder.job_title = request.job_title
        if "phone_number" in explicit_fields:
            stakeholder.phone_number = request.phone_number
        if "department" in explicit_fields:
            stakeholder.department = request.department
        if "external_links" in explicit_fields:
            stakeholder.external_links = self._serialize_external_links(request.external_links)
        if "stance" in explicit_fields:
            stakeholder.stance = request.stance
        if "decision_power" in explicit_fields:
            stakeholder.decision_power = request.decision_power
        if "champion_potential" in explicit_fields and request.champion_potential is not None:
            stakeholder.champion_potential = request.champion_potential
        if "motivation" in explicit_fields:
            stakeholder.motivation = request.motivation
        if "pain_points" in explicit_fields:
            stakeholder.pain_points = request.pain_points

        stakeholder.email = email_normalized
        stakeholder.source = StakeholderSource.MANUAL
        stakeholder.approval_status = ApprovalStatus.APPROVED
        stakeholder.is_deleted = False
        stakeholder.updated_at = datetime.now(UTC)

        from sqlalchemy import delete
        from src.domain.models.organisation_tables.sync.timeline_models import (
            DealTimelineEvent,
        )

        for event_type in (
            TimelineEventType.STAKEHOLDER_ADDED,
            TimelineEventType.STAKEHOLDER_REMOVED,
        ):
            delete_stmt = delete(DealTimelineEvent).where(
                col(DealTimelineEvent.deal_id) == deal_id,
                col(DealTimelineEvent.event_type) == event_type,
                col(DealTimelineEvent.source_record_id) == str(stakeholder.stakeholder_id),
            )
            await self.session.execute(delete_stmt)

        await self.session.refresh(stakeholder)

        timeline_event = build_stakeholder_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            stakeholder=stakeholder,
            event_type=TimelineEventType.STAKEHOLDER_ADDED,
        )
        self.session.add(timeline_event)

        await self.session.commit()
        await self.session.refresh(stakeholder)

        return StakeholderDetail.model_validate(stakeholder)

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

        # Build list of approval statuses to include
        statuses = [ApprovalStatus.APPROVED]
        if include_pending:
            statuses.append(ApprovalStatus.PENDING)
        if include_rejected:
            statuses.append(ApprovalStatus.REJECTED)

        stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.approval_status).in_(statuses),
        )
        result = await self.session.execute(stmt)
        stakeholders = list(result.scalars().all())

        return StakeholderListResponse(
            company_domain=company_domain,
            company_name=company_name,
            stakeholders=[StakeholderDetail.model_validate(s) for s in stakeholders],
        )

    async def get_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder | None:
        """Get a specific stakeholder by ID."""
        stmt = select(DealStakeholder).where(
            col(DealStakeholder.stakeholder_id) == stakeholder_id,
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_stakeholder_detail(self, deal_id: UUID, stakeholder_id: UUID) -> dict | None:
        """Get full stakeholder detail including engagement history.

        Returns the stakeholder with all fields including engagement_history
        which shows how the stakeholder's metrics changed over time.

        Args:
            deal_id: The deal ID.
            stakeholder_id: The stakeholder ID.

        Returns:
            Dict with full stakeholder detail and engagement history, or None if not found.
        """
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            return None

        # Format engagement history from JSONB field
        engagement_history = []
        raw_history = stakeholder.stakeholder_engagement_history or []
        for snapshot in raw_history:
            if isinstance(snapshot, dict):
                engagement_history.append(
                    {
                        "recorded_at": snapshot.get("recorded_at"),
                        "stance": snapshot.get("stance"),
                        "temperature": snapshot.get("temperature"),
                        "engagement_score": snapshot.get("engagement_score"),
                        "champion_potential": snapshot.get("champion_potential"),
                        "decision_power": snapshot.get("decision_power"),
                    }
                )

        return {
            "stakeholder_id": stakeholder.stakeholder_id,
            "deal_id": stakeholder.deal_id,
            "contact_id": stakeholder.contact_id,
            "email": stakeholder.email,
            "name": stakeholder.name,
            "job_title": stakeholder.job_title,
            "phone_number": stakeholder.phone_number,
            "department": stakeholder.department,
            "external_links": stakeholder.external_links,
            "stance": stakeholder.stance,
            "decision_power": stakeholder.decision_power,
            "champion_potential": stakeholder.champion_potential,
            "motivation": stakeholder.motivation,
            "pain_points": stakeholder.pain_points,
            "keywords": stakeholder.keywords,
            "last_contact_at": stakeholder.last_contact_at,
            "engagement_score": stakeholder.engagement_score,
            "temperature": stakeholder.temperature,
            "source": stakeholder.source,
            "approval_status": stakeholder.approval_status,
            "engagement_history": engagement_history,
            "stakeholder_research": stakeholder.stakeholder_research,
            "last_researched_at": stakeholder.last_researched_at,
            "active_research_job": None,
            "created_at": stakeholder.created_at,
            "updated_at": stakeholder.updated_at,
        }

    async def approve_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder:
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

        if stakeholder.approval_status == ApprovalStatus.APPROVED:
            raise StakeholdersServiceError("Stakeholder is already approved", status_code=400)

        stakeholder.approval_status = ApprovalStatus.APPROVED
        await self.session.commit()
        await self.session.refresh(stakeholder)
        return stakeholder

    async def reject_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder:
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

        if stakeholder.approval_status == ApprovalStatus.REJECTED:
            raise StakeholdersServiceError("Stakeholder is already rejected", status_code=400)

        stakeholder.approval_status = ApprovalStatus.REJECTED
        await self.session.commit()
        await self.session.refresh(stakeholder)
        return stakeholder

    async def remove_stakeholder(self, deal_id: UUID, stakeholder_id: UUID) -> DealStakeholder:
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

        if stakeholder.approval_status == ApprovalStatus.REMOVED:
            raise StakeholdersServiceError("Stakeholder is already removed", status_code=400)

        if stakeholder.approval_status != ApprovalStatus.APPROVED:
            raise StakeholdersServiceError(
                "Only approved stakeholders can be removed", status_code=400
            )

        stakeholder.approval_status = ApprovalStatus.REMOVED

        # Soft-delete matching internal CRM participant
        if stakeholder.email:
            await self._soft_delete_internal_participant(deal_id, stakeholder.email)

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
            col(DealTimelineEvent.source_record_id) == str(stakeholder.stakeholder_id),
        )
        await self.session.execute(delete_stmt)
        # Refresh stakeholder after DELETE to prevent expired-attribute lazy-load errors
        await self.session.refresh(stakeholder)

        # Recreate the removal event in the same flush context so downstream
        # subscribers see a single ordered timeline sequence per update.
        timeline_event = build_stakeholder_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            stakeholder=stakeholder,
            event_type=TimelineEventType.STAKEHOLDER_REMOVED,
        )
        self.session.add(timeline_event)

        await self.session.commit()
        await self.session.refresh(stakeholder)
        return stakeholder

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

        # Check for duplicate email while preserving the rollback-safe re-add
        # flow for previously removed stakeholders.
        existing_stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.email) == email_normalized,
        )
        existing_result = await self.session.execute(existing_stmt)
        existing_stakeholder = existing_result.scalar_one_or_none()
        if existing_stakeholder:
            status = existing_stakeholder.approval_status
            conflict_data = {
                "stakeholder_id": str(existing_stakeholder.stakeholder_id),
                "approval_status": status.value if hasattr(status, "value") else str(status),
                "is_deleted": existing_stakeholder.is_deleted,
            }

            if status == ApprovalStatus.APPROVED and not existing_stakeholder.is_deleted:
                raise StakeholdersServiceError(
                    "A stakeholder with this email already exists for this deal",
                    status_code=409,
                    data=conflict_data,
                )

            if status == ApprovalStatus.REMOVED:
                return await self._reactivate_removed_stakeholder(
                    deal_id=deal_id,
                    stakeholder=existing_stakeholder,
                    request=request,
                    email_normalized=email_normalized,
                )

            # Stakeholder exists but is REJECTED or soft-deleted
            # Let the user decide — they can re-approve via the approve endpoint
            raise StakeholdersServiceError(
                f"Stakeholder exists but is currently {conflict_data['approval_status']}. "
                "Use the approve endpoint to reactivate it.",
                status_code=409,
                data=conflict_data,
            )

        external_links_data = self._serialize_external_links(request.external_links)

        # Manually added stakeholders are intentionally tagged by source so
        # downstream automation keeps approval and research behavior explicit.
        # Optional metrics use neutral defaults because they are updated later by
        # usage signals.
        champion_potential = (
            request.champion_potential if request.champion_potential is not None else 50.0
        )

        # Link to CRMContact when available so repeated stakeholders reuse profile
        # IDs and keep contact-level identity stable.
        contact_id = await self._ensure_contact_for_stakeholder(
            email=email_normalized,
            name=request.name,
            job_title=request.job_title,
            phone_number=request.phone_number,
        )

        # Build DealParticipant only for internal CRM deals to avoid leaking
        # ad-hoc participants into integrations that do not track them.
        if contact_id:
            await self._ensure_deal_participant(
                deal_id=deal_id,
                contact_id=contact_id,
                email=email_normalized,
                name=request.name,
            )

        new_stakeholder = DealStakeholder(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            contact_id=contact_id,
            created_by_user_id=self.user_id,
            email=email_normalized,
            name=request.name,
            job_title=request.job_title,
            phone_number=request.phone_number,
            department=request.department,
            external_links=external_links_data,
            stance=request.stance,
            decision_power=request.decision_power,
            champion_potential=champion_potential,
            motivation=request.motivation,
            pain_points=request.pain_points,
            source=StakeholderSource.MANUAL,
            approval_status=ApprovalStatus.APPROVED,
        )
        self.session.add(new_stakeholder)

        # Persist the add-event so downstream consumers can replay audit history
        # and rebuild timelines without inferring it from row timestamps alone.
        timeline_event = build_stakeholder_timeline_event(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            stakeholder=new_stakeholder,
            event_type=TimelineEventType.STAKEHOLDER_ADDED,
        )
        self.session.add(timeline_event)

        await self.session.commit()
        await self.session.refresh(new_stakeholder)

        return StakeholderDetail.model_validate(new_stakeholder)

    async def update_stakeholder(
        self,
        deal_id: UUID,
        stakeholder_id: UUID,
        request: UpdateStakeholderRequest,
    ) -> StakeholderDetail:
        """Partially update a stakeholder and mark fields as user-managed."""
        stakeholder = await self.get_stakeholder(deal_id, stakeholder_id)
        if not stakeholder:
            raise StakeholdersServiceError("Stakeholder not found", status_code=404)

        updates = request.model_dump(exclude_unset=True)
        if not updates:
            raise StakeholdersServiceError("No fields provided for update", status_code=400)

        non_nullable_fields = {"stance", "decision_power", "champion_potential"}
        null_non_nullable = [
            field for field in non_nullable_fields if field in updates and updates[field] is None
        ]
        if null_non_nullable:
            invalid = ", ".join(sorted(null_non_nullable))
            raise StakeholdersServiceError(
                f"Cannot set the following fields to null: {invalid}",
                status_code=400,
            )

        if "external_links" in updates:
            updates["external_links"] = self._serialize_external_links(updates["external_links"])

        for field_name, value in updates.items():
            setattr(stakeholder, field_name, value)

        stakeholder_data = dict(stakeholder.stakeholder_data or {})
        manual_overrides = set(stakeholder_data.get("manual_overrides") or [])
        manual_overrides.update(updates.keys())
        stakeholder_data["manual_overrides"] = sorted(manual_overrides)
        stakeholder.stakeholder_data = stakeholder_data

        await self.session.commit()
        await self.session.refresh(stakeholder)
        return StakeholderDetail.model_validate(stakeholder)

    async def list_available_contacts(
        self, deal_id: UUID, query: str | None = None, limit: int = 50
    ) -> list[AvailableContactItem]:
        """List contacts that are not yet stakeholders for the deal.

        Only returns contacts with valid email addresses, as email is required
        for stakeholder identification.
        """
        # Subquery for existing stakeholders
        subq = select(col(DealStakeholder.contact_id)).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )

        stmt = select(CRMContact).where(
            col(CRMContact.organisation_id) == self.organisation_id,
            col(CRMContact.contact_id).not_in(subq),
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

        # Convert to AvailableContactItem with relevant information
        return [
            AvailableContactItem(
                contact_id=c.contact_id,
                external_id=c.external_id,
                email=c.email,  # type: ignore - filtered above
                first_name=c.first_name,
                last_name=c.last_name,
                full_name=c.full_name,
                job_title=c.job_title,
                company_name=c.company_name,
                phone=c.phone,
                lifecycle_stage=c.lifecycle_stage,
                last_activity_at=c.last_activity_at,
            )
            for c in contacts
        ]

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

        # Build existing stakeholder/email membership once so the add loop can
        # enforce idempotency by email and contact id without N+1 checks.
        existing_stmt = select(DealStakeholder.email, DealStakeholder.contact_id).where(  # type: ignore
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
        )
        existing_result = await self.session.execute(existing_stmt)
        existing_rows = existing_result.all()
        existing_emails = {row.email.lower() for row in existing_rows if row.email}
        existing_contact_ids = {row.contact_id for row in existing_rows if row.contact_id}

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

            # Insert a CRM-derived stakeholder row with source=CRM for audit and
            # lifecycle clarity.
            new_stakeholder = DealStakeholder(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                contact_id=contact.contact_id,
                email=contact.email,
                name=name,
                job_title=contact.job_title,
                source=StakeholderSource.CRM,
            )
            self.session.add(new_stakeholder)

            # Emit timeline event so stakeholder additions are recoverable in activity
            # feeds and support incident debugging.
            timeline_event = build_stakeholder_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                stakeholder=new_stakeholder,
                event_type=TimelineEventType.STAKEHOLDER_ADDED,
            )
            self.session.add(timeline_event)

            # Track for response - add email to existing set
            existing_emails.add(contact.email.lower())
            existing_contact_ids.add(contact_id)

        # Commit all new stakeholders
        await self.session.commit()

        # Refresh added stakeholders for response
        added_stmt = select(DealStakeholder).where(
            col(DealStakeholder.deal_id) == deal_id,
            col(DealStakeholder.organisation_id) == self.organisation_id,
            col(DealStakeholder.contact_id).in_(
                [cid for cid in contact_ids if cid not in {s.contact_id for s in skipped}]
            ),
        )
        added_result = await self.session.execute(added_stmt)
        added_stakeholders = list(added_result.scalars().all())

        # Convert to StakeholderDetail
        added = [StakeholderDetail.model_validate(s) for s in added_stakeholders]

        return AddContactsAsStakeholdersResponse(
            added=added,
            skipped=skipped,
            total_requested=len(contact_ids),
            total_added=len(added),
            total_skipped=len(skipped),
        )

    async def has_deal_owner_or_participants(self, deal_id: UUID) -> bool:
        """Return True if a deal has an owner or any active participants."""
        deal_stmt = select(col(CRMDeal.owner_id), col(CRMDeal.owner_external_id)).where(
            col(CRMDeal.deal_id) == deal_id,
            col(CRMDeal.organisation_id) == self.organisation_id,
        )
        deal_result = await self.session.execute(deal_stmt)
        deal_row = deal_result.first()
        if not deal_row:
            return False

        owner_id, owner_external_id = deal_row
        if owner_id or owner_external_id:
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
