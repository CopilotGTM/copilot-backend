"""Deal enrichment helper service.

Enriches CRMDeal records with denormalised data pulled from related
entities (users, contacts, companies). Intended to be reused by the
deal sync flow as well as other sync services that need to refresh
deal-derived details after owners/contacts/companies change.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMUser,
)


class DealEnrichmentService:
    """Populate denormalised deal fields from related CRM entities."""

    def __init__(self, *, db: AsyncSession, organisation_id: UUID) -> None:
        self.db = db
        self.organisation_id = organisation_id

    async def enrich_deals(self, deals: Iterable[CRMDeal]) -> None:
        """Enrich the provided deal records in-place.

        Args:
            deals: Iterable of deal ORM objects already attached to the session.
        """

        deal_list = [deal for deal in deals if isinstance(deal, CRMDeal)]
        if not deal_list:
            return

        owner_ids = {deal.owner_external_id for deal in deal_list if deal.owner_external_id}
        contact_ids = {
            deal.primary_contact_external_id
            for deal in deal_list
            if deal.primary_contact_external_id
        }
        company_ids = {deal.company_external_id for deal in deal_list if deal.company_external_id}

        user_map = await self._load_users(owner_ids) if owner_ids else {}
        contact_map = await self._load_contacts(contact_ids) if contact_ids else {}
        company_map = await self._load_companies(company_ids) if company_ids else {}

        for deal in deal_list:
            if deal.owner_external_id and deal.owner_external_id in user_map:
                self._apply_owner_details(deal, user_map[deal.owner_external_id])

            if deal.primary_contact_external_id and deal.primary_contact_external_id in contact_map:
                self._apply_contact_details(deal, contact_map[deal.primary_contact_external_id])

            if deal.company_external_id and deal.company_external_id in company_map:
                self._apply_company_details(deal, company_map[deal.company_external_id])

    async def enrich_deals_by_ids(
        self,
        deal_ids: Sequence[UUID],
    ) -> None:
        """Fetch deals by internal IDs and enrich them."""

        if not deal_ids:
            return

        result = await self.db.execute(
            select(CRMDeal)
            .where(CRMDeal.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDeal.deal_id.in_(deal_ids))  # type: ignore[attr-defined]
        )
        deals = list(result.scalars().all())
        if not deals:
            return
        await self.enrich_deals(deals)

    async def _load_users(self, owner_ids: set[str]) -> dict[str, CRMUser]:
        result = await self.db.execute(
            select(CRMUser)
            .where(CRMUser.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMUser.external_id.in_(owner_ids))  # type: ignore[attr-defined]
            .where(CRMUser.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        return {user.external_id: user for user in result.scalars().all()}

    async def _load_contacts(self, contact_ids: set[str]) -> dict[str, CRMContact]:
        result = await self.db.execute(
            select(CRMContact)
            .where(CRMContact.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMContact.external_id.in_(contact_ids))  # type: ignore[attr-defined]
            .where(CRMContact.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        return {contact.external_id: contact for contact in result.scalars().all()}

    async def _load_companies(self, company_ids: set[str]) -> dict[str, CRMCompany]:
        result = await self.db.execute(
            select(CRMCompany)
            .where(CRMCompany.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMCompany.external_id.in_(company_ids))  # type: ignore[attr-defined]
            .where(CRMCompany.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        return {company.external_id: company for company in result.scalars().all()}

    def _apply_owner_details(self, deal: CRMDeal, user: CRMUser) -> None:
        full_name = user.full_name or self._compose_name(user.first_name, user.last_name)
        if full_name and full_name != deal.owner_name:
            deal.owner_name = full_name

        if user.email and user.email != deal.owner_email:
            deal.owner_email = user.email

    def _apply_contact_details(self, deal: CRMDeal, contact: CRMContact) -> None:
        full_name = contact.full_name or self._compose_name(contact.first_name, contact.last_name)
        if full_name and full_name != deal.primary_contact_name:
            deal.primary_contact_name = full_name

        if contact.email and contact.email != deal.primary_contact_email:
            deal.primary_contact_email = contact.email

    def _apply_company_details(self, deal: CRMDeal, company: CRMCompany) -> None:
        if company.name and company.name != deal.company_name:
            deal.company_name = company.name

        if company.domain and company.domain != deal.company_domain:
            deal.company_domain = company.domain

    def _compose_name(self, first: Any, last: Any) -> str | None:
        parts = [str(part).strip() for part in (first, last) if part]
        return " ".join(parts) if parts else None

    async def enrich_participants(
        self, integration_id: UUID, *, deal_ids: Sequence[UUID] | None = None
    ) -> int:
        """Enrich deal participants with related entity details.

        Looks up CRMContact and CRMCompany records to populate related_name
        and related_email fields on CRMDealParticipant records.

        Args:
            integration_id: The integration to scope the enrichment to.
            deal_ids: Optional list of deal IDs to limit enrichment scope.
                     If None, enriches all participants for the integration.

        Returns:
            Number of participants enriched.
        """
        from src.domain.models.organisation_tables.sync.crm import (
            CRMDealParticipant,
            CRMParticipantType,
        )

        def _get_participant_type_str(ptype: Any) -> str:
            """Normalize participant type to string for comparison."""
            if hasattr(ptype, "value"):
                return ptype.value
            return str(ptype)

        # Build query for participants needing enrichment
        stmt = (
            select(CRMDealParticipant)
            .where(CRMDealParticipant.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDealParticipant.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDealParticipant.is_deleted.is_(False))  # type: ignore[attr-defined]
        )

        if deal_ids:
            stmt = stmt.where(CRMDealParticipant.deal_id.in_(deal_ids))  # type: ignore[attr-defined]

        result = await self.db.execute(stmt)
        participants = list(result.scalars().all())

        if not participants:
            return 0

        # Collect external IDs by type (handle both string and enum)
        contact_ext_ids: set[str] = set()
        company_ext_ids: set[str] = set()
        user_ext_ids: set[str] = set()

        for p in participants:
            ptype = _get_participant_type_str(p.participant_type)
            if ptype == CRMParticipantType.CONTACT.value and p.related_external_id:
                contact_ext_ids.add(p.related_external_id)
            elif ptype == CRMParticipantType.COMPANY.value and p.related_external_id:
                company_ext_ids.add(p.related_external_id)
            elif ptype == CRMParticipantType.USER.value and p.related_external_id:
                user_ext_ids.add(p.related_external_id)

        # Load related entities
        contact_map = await self._load_contacts(contact_ext_ids) if contact_ext_ids else {}
        company_map = await self._load_companies(company_ext_ids) if company_ext_ids else {}
        user_map = await self._load_users(user_ext_ids) if user_ext_ids else {}

        enriched_count = 0
        for p in participants:
            updated = False
            ptype = _get_participant_type_str(p.participant_type)

            if ptype == CRMParticipantType.CONTACT.value:
                contact = contact_map.get(p.related_external_id)
                if contact:
                    name = contact.full_name or self._compose_name(
                        contact.first_name, contact.last_name
                    )
                    if name and name != p.related_name:
                        p.related_name = name
                        updated = True
                    if contact.email and contact.email != p.related_email:
                        p.related_email = contact.email
                        updated = True
                    if contact.contact_id and p.related_local_id != contact.contact_id:
                        p.related_local_id = contact.contact_id
                        updated = True

            elif ptype == CRMParticipantType.COMPANY.value:
                company = company_map.get(p.related_external_id)
                if company:
                    if company.name and company.name != p.related_name:
                        p.related_name = company.name
                        updated = True
                    # Use company domain as related_email for companies
                    if company.domain and company.domain != p.related_email:
                        p.related_email = company.domain
                        updated = True
                    if company.company_id and p.related_local_id != company.company_id:
                        p.related_local_id = company.company_id
                        updated = True

            elif ptype == CRMParticipantType.USER.value:
                user = user_map.get(p.related_external_id)
                if user:
                    name = user.full_name or self._compose_name(user.first_name, user.last_name)
                    if name and name != p.related_name:
                        p.related_name = name
                        updated = True
                    if user.email and user.email != p.related_email:
                        p.related_email = user.email
                        updated = True
                    if user.user_id and p.related_local_id != user.user_id:
                        p.related_local_id = user.user_id
                        updated = True

            if updated:
                self.db.add(p)
                enriched_count += 1

        if enriched_count > 0:
            await self.db.commit()

        return enriched_count
