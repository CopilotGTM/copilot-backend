"""Owner enrichment helper service.

Enriches owner_id FK references on CRM entities (companies, contacts, deals)
after users sync. In Salesforce, Users ARE Owners - there's no separate concept.
The OwnerId field on any Salesforce object points to a User record.

This service resolves owner_external_id → owner_id (FK to crm_users) for:
- CRMCompany records
- CRMContact records
- CRMDeal records

Should be triggered after users sync completes to resolve owner_id for entities
that were synced before users were available.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMUser,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class OwnerEnrichmentService:
    """Resolve owner_id FK from owner_external_id for CRM entities.

    After users are synced, this service updates companies, contacts, and deals
    to set their owner_id FK by joining on owner_external_id → crm_users.external_id.
    """

    def __init__(self, *, db: AsyncSession, organisation_id: UUID) -> None:
        self.db = db
        self.organisation_id = organisation_id

    async def enrich_owners_for_integration(self, integration_id: UUID) -> dict[str, int]:
        """Enrich owner_id for all entities in an integration.

        Args:
            integration_id: The CRM integration UUID

        Returns:
            Dictionary with counts of enriched records per entity type
        """
        results = {
            "companies": await self._enrich_company_owners(integration_id),
            "contacts": await self._enrich_contact_owners(integration_id),
            "deals": await self._enrich_deal_owners(integration_id),
        }

        total = sum(results.values())
        if total > 0:
            logger.info(
                "Enriched owner_id for entities: org=%s integration=%s results=%s",
                self.organisation_id,
                integration_id,
                results,
            )

        return results

    async def _enrich_company_owners(self, integration_id: UUID) -> int:
        """Resolve owner_id for companies where owner_external_id is set but owner_id is null.

        Args:
            integration_id: The integration to scope the enrichment to

        Returns:
            Number of companies enriched
        """
        # Find companies that need enrichment
        companies_stmt = (
            select(CRMCompany)
            .where(CRMCompany.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMCompany.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMCompany.owner_external_id.isnot(None))  # type: ignore[attr-defined]
            .where(CRMCompany.owner_id.is_(None))  # type: ignore[attr-defined]
            .where(CRMCompany.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(companies_stmt)
        companies = list(result.scalars().all())

        if not companies:
            return 0

        # Load users for these owner_external_ids
        owner_ext_ids = {c.owner_external_id for c in companies if c.owner_external_id}
        users = await self._load_users(owner_ext_ids, integration_id)

        # Update companies
        enriched = 0
        for company in companies:
            if company.owner_external_id and company.owner_external_id in users:
                user = users[company.owner_external_id]
                company.owner_id = user.user_id
                # Also update owner_name/email if not set
                if not company.owner_name:
                    company.owner_name = user.full_name or self._compose_name(
                        user.first_name, user.last_name
                    )
                if not company.owner_email:
                    company.owner_email = user.email
                enriched += 1

        return enriched

    async def _enrich_contact_owners(self, integration_id: UUID) -> int:
        """Resolve owner_id for contacts where owner_external_id is set but owner_id is null.

        Args:
            integration_id: The integration to scope the enrichment to

        Returns:
            Number of contacts enriched
        """
        # Find contacts that need enrichment
        contacts_stmt = (
            select(CRMContact)
            .where(CRMContact.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMContact.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMContact.owner_external_id.isnot(None))  # type: ignore[attr-defined]
            .where(CRMContact.owner_id.is_(None))  # type: ignore[attr-defined]
            .where(CRMContact.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(contacts_stmt)
        contacts = list(result.scalars().all())

        if not contacts:
            return 0

        # Load users for these owner_external_ids
        owner_ext_ids = {c.owner_external_id for c in contacts if c.owner_external_id}
        users = await self._load_users(owner_ext_ids, integration_id)

        # Update contacts
        enriched = 0
        for contact in contacts:
            if contact.owner_external_id and contact.owner_external_id in users:
                user = users[contact.owner_external_id]
                contact.owner_id = user.user_id
                # Also update owner_name/email if not set
                if not contact.owner_name:
                    contact.owner_name = user.full_name or self._compose_name(
                        user.first_name, user.last_name
                    )
                if not contact.owner_email:
                    contact.owner_email = user.email
                enriched += 1

        return enriched

    async def _enrich_deal_owners(self, integration_id: UUID) -> int:
        """Resolve owner_id for deals where owner_external_id is set but owner_id is null.

        Args:
            integration_id: The integration to scope the enrichment to

        Returns:
            Number of deals enriched
        """
        # Find deals that need enrichment
        deals_stmt = (
            select(CRMDeal)
            .where(CRMDeal.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDeal.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMDeal.owner_external_id.isnot(None))  # type: ignore[attr-defined]
            .where(CRMDeal.owner_id.is_(None))  # type: ignore[attr-defined]
            .where(CRMDeal.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        result = await self.db.execute(deals_stmt)
        deals = list(result.scalars().all())

        if not deals:
            return 0

        # Load users for these owner_external_ids
        owner_ext_ids = {d.owner_external_id for d in deals if d.owner_external_id}
        users = await self._load_users(owner_ext_ids, integration_id)

        # Update deals
        enriched = 0
        for deal in deals:
            if deal.owner_external_id and deal.owner_external_id in users:
                user = users[deal.owner_external_id]
                deal.owner_id = user.user_id
                # Also update owner_name/email if not set
                if not deal.owner_name:
                    deal.owner_name = user.full_name or self._compose_name(
                        user.first_name, user.last_name
                    )
                if not deal.owner_email:
                    deal.owner_email = user.email
                enriched += 1

        return enriched

    async def _load_users(
        self, owner_ext_ids: set[str], integration_id: UUID
    ) -> dict[str, CRMUser]:
        """Load CRM users by external IDs for a specific integration.

        Args:
            owner_ext_ids: Set of owner external IDs to load
            integration_id: Integration to scope user lookup

        Returns:
            Dictionary mapping external_id to CRMUser
        """
        if not owner_ext_ids:
            return {}

        result = await self.db.execute(
            select(CRMUser)
            .where(CRMUser.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMUser.integration_id == integration_id)  # type: ignore[arg-type]
            .where(CRMUser.external_id.in_(owner_ext_ids))  # type: ignore[attr-defined]
            .where(CRMUser.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        return {user.external_id: user for user in result.scalars().all()}

    def _compose_name(self, first: str | None, last: str | None) -> str | None:
        """Compose full name from first and last name."""
        parts = [str(part).strip() for part in (first, last) if part]
        return " ".join(parts) if parts else None
