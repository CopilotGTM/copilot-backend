"""Canonical raw participant membership writer for internal CRM deals.

Called by participant routes, contact-to-deal linking, and the legacy
DealParticipantService facade. The service mutates only internal CRM
participants and leaves commits, job creation, and worker dispatch to callers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.companies import CRMCompany
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMParticipantType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.participants.contracts import (
    ParticipantServiceError,
    ParticipantSkipped,
    ParticipantWriteResult,
)
from src.service.crm.utils.account import clean_optional_string
from src.service.crm.utils.crm_user_identity import (
    CRMUserIdentityResolutionError,
    ensure_app_user_crm_identity,
)
from src.service.crm.utils.identity import normalize_email

logger = get_logger(__name__)


class ParticipantWriteService:
    """Tenant-scoped writer for raw internal CRM deal participants."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        actor_user_id: UUID,
        master_session: AsyncSession | None = None,
    ):
        self.session = session
        self.organisation_id = organisation_id
        self.actor_user_id = actor_user_id
        self.master_session = master_session

    async def close(self) -> None:
        """Close the org-scoped session when the dependency owns its lifecycle."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("ParticipantWriteService close exception", exc_info=True)

    async def add_participants(
        self, deal_id: UUID, payloads: list[dict[str, Any]]
    ) -> ParticipantWriteResult:
        """Add or restore participant rows for one internal CRM deal.

        Contact participants dedupe by contact/email instead of role because the
        same buyer can be both primary contact and decision-maker. The caller
        commits after this returns so post-commit jobs cannot outlive a failed DB
        transaction.
        """
        deal, integration_id = await self._get_internal_deal(deal_id)
        now = datetime.now(UTC)

        created: list[CRMDealParticipant] = []
        skipped: list[ParticipantSkipped] = []
        contact_participant_changed = False
        seen_keys: set[tuple[str, str, str]] = set()
        seen_contact_participants: dict[str, CRMDealParticipant] = {}

        for payload in payloads:
            participant_type = self._parse_participant_type(payload.get("participant_type"))
            related_id = payload.get("related_id")
            relationship_role = clean_optional_string(payload.get("relationship_role"))

            if not isinstance(related_id, UUID):
                raise ParticipantServiceError("Invalid related_id", status_code=400)

            related_local_id, related_name, related_email = await self._resolve_participant_entity(
                participant_type, related_id, integration_id
            )

            role_key = relationship_role or ""
            identity_key = self._participant_identity_key(
                participant_type=participant_type,
                related_local_id=related_local_id,
                related_email=related_email,
            )
            request_key = (participant_type.value, identity_key, role_key)
            if participant_type == CRMParticipantType.CONTACT:
                # Contact role is state on one person row, not a uniqueness axis.
                # Company/user rows keep role-specific behavior for existing callers.
                request_key = (participant_type.value, identity_key, "")
            if request_key in seen_keys:
                if participant_type == CRMParticipantType.CONTACT and self._is_primary_contact_role(
                    relationship_role
                ):
                    seen_participant = seen_contact_participants.get(identity_key)
                    if seen_participant is not None:
                        seen_participant.is_primary_contact = True
                skipped.append(
                    ParticipantSkipped(
                        participant_type=participant_type.value,
                        related_id=related_id,
                        relationship_role=relationship_role,
                        reason="Duplicate participant in request",
                    )
                )
                continue
            seen_keys.add(request_key)
            existing = await self._find_existing_participant(
                deal_id,
                integration_id,
                participant_type,
                related_local_id,
                role_key,
            )

            if existing and not existing.is_deleted:
                skipped.append(
                    ParticipantSkipped(
                        participant_type=participant_type.value,
                        related_id=related_id,
                        relationship_role=relationship_role,
                        reason="Participant already exists",
                    )
                )
                continue

            if existing and existing.is_deleted:
                participant = existing
                participant.is_deleted = False
            else:
                participant = CRMDealParticipant(
                    participant_id=uuid4(),
                    organisation_id=self.organisation_id,
                    integration_id=integration_id,
                    deal_id=deal_id,
                    participant_type=participant_type,
                    relationship_role=relationship_role,
                )

            participant.related_local_id = related_local_id
            participant.related_name = (
                None if participant_type == CRMParticipantType.CONTACT else related_name
            )
            participant.related_email = related_email
            if participant_type == CRMParticipantType.CONTACT and self._is_primary_contact_role(
                relationship_role
            ):
                participant.is_primary_contact = True
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now
            participant.participant_source = "MANUAL"

            self.session.add(participant)
            created.append(participant)

            if participant_type == CRMParticipantType.CONTACT:
                # Contact participants render person names from CRMContact. Keeping
                # related_name empty avoids creating a second profile snapshot.
                contact_participant_changed = True
                seen_contact_participants[identity_key] = participant

            if participant_type == CRMParticipantType.COMPANY:
                # Company participants drive the deal company snapshot. Keep the
                # snapshot update in the same transaction as the participant row.
                await self._maybe_apply_company_to_deal(deal, integration_id, related_local_id, now)

        return ParticipantWriteResult(
            created=created,
            skipped=skipped,
            contact_participant_changed=contact_participant_changed,
            integration_id=integration_id,
        )

    async def remove_participant(
        self, deal_id: UUID, participant_id: UUID
    ) -> tuple[CRMDealParticipant, bool, UUID]:
        """Soft-delete an internal participant and report whether stakeholders changed.

        External CRM participant rows are treated as provider-owned and read-only;
        callers must not remove provider-synced memberships through this path.
        """
        deal, integration_id = await self._get_internal_deal(deal_id)
        participant = await self.session.get(CRMDealParticipant, participant_id)

        if not participant or participant.organisation_id != self.organisation_id:
            raise ParticipantServiceError("Participant not found", status_code=404)
        if participant.deal_id != deal_id:
            raise ParticipantServiceError("Participant not found", status_code=404)
        if participant.integration_id != integration_id:
            raise ParticipantServiceError("External CRM participants are read-only", 409)
        if participant.is_deleted:
            raise ParticipantServiceError("Participant not found", status_code=404)

        now = datetime.now(UTC)
        participant.is_deleted = True
        if participant.participant_type == CRMParticipantType.CONTACT:
            # Clearing email prevents soft-deleted contacts from blocking future
            # email-based stakeholder dedupe during manual re-add flows.
            participant.related_email = None
        participant.source_modified_at = now
        participant.synced_at = now
        participant.last_seen_at = now
        self.session.add(participant)

        if (
            participant.participant_type == CRMParticipantType.COMPANY
            and participant.related_local_id == deal.company_id
        ):
            await self._refresh_deal_company_after_removal(
                deal=deal,
                integration_id=integration_id,
                removed_participant_id=participant.participant_id,
                updated_at=now,
            )

        contact_participant_changed = participant.participant_type == CRMParticipantType.CONTACT
        return participant, contact_participant_changed, integration_id

    async def update_participant_role(
        self,
        deal_id: UUID,
        participant_id: UUID,
        relationship_role: str | None,
    ) -> CRMDealParticipant:
        """Update the role metadata for one internal participant.

        The method preserves the old role uniqueness rule for non-contact
        participant types while keeping contact primary-contact state on one row.
        """
        _, integration_id = await self._get_internal_deal(deal_id)
        participant = await self.session.get(CRMDealParticipant, participant_id)

        if not participant or participant.organisation_id != self.organisation_id:
            raise ParticipantServiceError("Participant not found", status_code=404)
        if participant.deal_id != deal_id:
            raise ParticipantServiceError("Participant not found", status_code=404)
        if participant.integration_id != integration_id:
            raise ParticipantServiceError("External CRM participants are read-only", 409)
        if participant.is_deleted:
            raise ParticipantServiceError("Participant not found", status_code=404)

        new_role = clean_optional_string(relationship_role)
        role_key = new_role or ""

        participant_type = self._parse_participant_type(participant.participant_type)
        existing = await self._find_existing_participant(
            deal_id,
            integration_id,
            participant_type,
            participant.related_local_id,
            role_key,
        )
        if (
            existing
            and existing.participant_id != participant.participant_id
            and not existing.is_deleted
        ):
            raise ParticipantServiceError("Participant with role already exists", status_code=409)

        participant.participant_type = participant_type
        if participant_type == CRMParticipantType.CONTACT and self._is_primary_contact_role(
            new_role
        ):
            participant.is_primary_contact = True
        participant.relationship_role = new_role
        now = datetime.now(UTC)
        participant.source_modified_at = now
        participant.synced_at = now
        participant.last_seen_at = now
        participant.participant_source = "MANUAL"
        self.session.add(participant)
        return participant

    async def sync_owner_participant(self, *, deal: CRMDeal, owner_user_id: UUID) -> None:
        """Create the owner participant projection and remove stale owner rows."""
        await self.add_participants(
            deal.deal_id,
            [
                {
                    "participant_type": CRMParticipantType.USER.value,
                    "related_id": owner_user_id,
                    "relationship_role": "owner",
                }
            ],
        )
        await self._soft_delete_owner_participants(deal=deal, keep_user_id=owner_user_id)

    async def clear_owner_participants(self, *, deal: CRMDeal) -> None:
        """Soft-delete owner projection rows while preserving normal collaborators."""
        await self._soft_delete_owner_participants(deal=deal, keep_user_id=None)

    async def _get_internal_deal(self, deal_id: UUID) -> tuple[CRMDeal, UUID]:
        """Return the internal CRM deal and integration boundary for writes."""
        setup_service = InternalCRMSetupService(
            self.session, self.organisation_id, self.actor_user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration:
            raise ParticipantServiceError("Internal CRM is not configured", status_code=409)
        integration_id = integration.integration_id

        deal = await self.session.get(CRMDeal, deal_id)
        # The deal ID can come from API clients and agents; organisation and
        # integration checks are the trust boundary before any mutation happens.
        if not deal or deal.organisation_id != self.organisation_id:
            raise ParticipantServiceError("Deal not found", status_code=404)
        if deal.integration_id != integration_id:
            raise ParticipantServiceError("External CRM deals are read-only", 409)
        if deal.is_deleted:
            raise ParticipantServiceError("Deal is deleted", 404)

        return deal, integration_id

    async def _resolve_participant_entity(
        self,
        participant_type: CRMParticipantType,
        related_id: UUID,
        integration_id: UUID,
    ) -> tuple[UUID, str | None, str | None]:
        """Resolve the participant target and reject provider-owned CRM records."""
        if participant_type == CRMParticipantType.CONTACT:
            contact = await self.session.get(CRMContact, related_id)
            if not contact or contact.organisation_id != self.organisation_id:
                raise ParticipantServiceError("Contact not found", status_code=404)
            if contact.integration_id != integration_id:
                raise ParticipantServiceError("External CRM contacts are read-only", 409)
            if contact.is_deleted:
                raise ParticipantServiceError("Contact is deleted", 404)

            name = (
                contact.full_name
                or " ".join(p for p in [contact.first_name, contact.last_name] if p)
                or None
            )
            return contact.contact_id, name, contact.email

        if participant_type == CRMParticipantType.COMPANY:
            company = await self.session.get(CRMCompany, related_id)
            if not company or company.organisation_id != self.organisation_id:
                raise ParticipantServiceError("Company not found", status_code=404)
            if company.integration_id != integration_id:
                raise ParticipantServiceError("External CRM companies are read-only", 409)
            if company.is_deleted:
                raise ParticipantServiceError("Company is deleted", 404)

            return company.company_id, company.name, None

        if participant_type == CRMParticipantType.USER:
            if not self.master_session:
                raise ParticipantServiceError(
                    "Master session required for user participants", status_code=500
                )
            try:
                crm_user = await ensure_app_user_crm_identity(
                    org_session=self.session,
                    master_session=self.master_session,
                    organisation_id=self.organisation_id,
                    app_user_id=related_id,
                    actor_user_id=self.actor_user_id,
                )
            except CRMUserIdentityResolutionError as exc:
                raise ParticipantServiceError(exc.message, status_code=exc.status_code) from exc

            return crm_user.user_id, crm_user.full_name, crm_user.email

        raise ParticipantServiceError("Unsupported participant type", status_code=400)

    async def _find_existing_participant(
        self,
        deal_id: UUID,
        integration_id: UUID,
        participant_type: CRMParticipantType,
        related_local_id: UUID | None,
        role_key: str,
    ) -> CRMDealParticipant | None:
        """Find an existing participant row under the internal integration."""
        c = cast(Any, CRMDealParticipant).__table__.c
        if related_local_id is None:
            raise ParticipantServiceError("Participant identity missing", status_code=400)

        filters = [
            c.organisation_id == self.organisation_id,
            c.integration_id == integration_id,
            c.deal_id == deal_id,
            c.participant_type == participant_type.value,
            c.related_local_id == related_local_id,
        ]
        if participant_type != CRMParticipantType.CONTACT:
            # Historical non-contact participants can be role-specific. Contacts
            # intentionally ignore role here to avoid duplicate buyer rows.
            filters.append(func.coalesce(c.relationship_role, "") == role_key)

        stmt = select(CRMDealParticipant).where(*filters)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    def _participant_identity_key(
        self,
        *,
        participant_type: CRMParticipantType,
        related_local_id: UUID | None,
        related_email: str | None,
    ) -> str:
        if related_local_id is not None:
            return str(related_local_id)
        if participant_type == CRMParticipantType.CONTACT:
            email = normalize_email(related_email)
            if email:
                return email
        raise ParticipantServiceError("Participant identity missing", status_code=400)

    @staticmethod
    def _parse_participant_type(value: Any) -> CRMParticipantType:
        if isinstance(value, CRMParticipantType):
            return value
        if isinstance(value, str):
            try:
                return CRMParticipantType(value)
            except ValueError as exc:
                raise ParticipantServiceError(
                    f"Unsupported participant type: {value}", status_code=400
                ) from exc
        raise ParticipantServiceError("Unsupported participant type", status_code=400)

    async def _maybe_apply_company_to_deal(
        self,
        deal: CRMDeal,
        integration_id: UUID,
        related_local_id: UUID,
        updated_at: datetime,
    ) -> None:
        """Refresh deal company fields when the company participant is authoritative."""
        if deal.company_id and related_local_id and deal.company_id != related_local_id:
            return

        company = None
        if related_local_id:
            company = await self.session.get(CRMCompany, related_local_id)
        if (
            company
            and company.organisation_id == self.organisation_id
            and company.integration_id == integration_id
            and not company.is_deleted
        ):
            deal.company_id = company.company_id
            deal.company_name = company.name
            deal.company_domain = company.domain
            deal.source_modified_at = updated_at
            deal.synced_at = updated_at
            deal.last_seen_at = updated_at
        self.session.add(deal)

    async def _soft_delete_owner_participants(
        self,
        *,
        deal: CRMDeal,
        keep_user_id: UUID | None,
    ) -> None:
        """Remove owner-role USER projections under the internal deal boundary."""
        now = datetime.now(UTC)
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == deal.integration_id,
            col(CRMDealParticipant.deal_id) == deal.deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.USER.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            self.owner_role_sql_expression() == "owner",
        )
        result = await self.session.execute(stmt)
        for participant in result.scalars().all():
            if not self.is_owner_role(participant.relationship_role):
                continue
            if keep_user_id is not None and participant.related_local_id == keep_user_id:
                continue
            participant.is_deleted = True
            participant.source_modified_at = now
            participant.synced_at = now
            participant.last_seen_at = now
            self.session.add(participant)

    async def _refresh_deal_company_after_removal(
        self,
        *,
        deal: CRMDeal,
        integration_id: UUID,
        removed_participant_id: UUID,
        updated_at: datetime,
    ) -> None:
        """Promote another active company participant or clear the deal snapshot."""
        stmt = select(CRMDealParticipant).where(
            col(CRMDealParticipant.organisation_id) == self.organisation_id,
            col(CRMDealParticipant.integration_id) == integration_id,
            col(CRMDealParticipant.deal_id) == deal.deal_id,
            col(CRMDealParticipant.participant_type) == CRMParticipantType.COMPANY.value,
            col(CRMDealParticipant.is_deleted).is_(False),
            col(CRMDealParticipant.participant_id) != removed_participant_id,
        )
        result = await self.session.execute(stmt)
        replacement = result.scalars().first()

        if replacement and replacement.related_local_id:
            company = await self.session.get(CRMCompany, replacement.related_local_id)
            if company and company.integration_id == integration_id and not company.is_deleted:
                deal.company_id = company.company_id
                deal.company_name = company.name
                deal.company_domain = company.domain
            else:
                deal.company_id = None
                deal.company_name = None
                deal.company_domain = None
        else:
            deal.company_id = None
            deal.company_name = None
            deal.company_domain = None

        deal.source_modified_at = updated_at
        deal.synced_at = updated_at
        deal.last_seen_at = updated_at
        self.session.add(deal)

    @staticmethod
    def _is_primary_contact_role(value: str | None) -> bool:
        cleaned = clean_optional_string(value)
        if not cleaned:
            return False
        return cleaned.lower().replace(" ", "_") == "primary_contact"

    @staticmethod
    def is_owner_role(value: str | None) -> bool:
        """Normalize legacy role strings before deciding whether a row is owner-only."""
        cleaned = clean_optional_string(value)
        if not cleaned:
            return False
        return cleaned.lower().replace(" ", "_") == "owner"

    @staticmethod
    def owner_role_sql_expression() -> Any:
        """Return the SQL expression used to identify owner projection rows."""
        c = cast(Any, CRMDealParticipant).__table__.c
        return func.lower(func.replace(func.trim(func.coalesce(c.relationship_role, "")), " ", "_"))
