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
