"""Company enrichment helper service.

Enriches CRMCompany records with denormalised data pulled from related
entities (users for owner). Intended to be used after companies sync
to populate owner details.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMCompany,
    CRMUser,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class CompanyEnrichmentService:
    """Populate denormalised company fields from related CRM entities.

    Handles enrichment of:
    - Owner details (name, email, owner_id FK) from CRMUser
    """

    def __init__(self, *, db: AsyncSession, organisation_id: UUID) -> None:
        self.db = db
        self.organisation_id = organisation_id

    async def enrich_companies(
        self, companies: Iterable[CRMCompany], integration_id: UUID | None = None
    ) -> None:
        """Enrich the provided company records in-place.

        Args:
            companies: Iterable of company ORM objects already attached to the session.
            integration_id: Optional integration ID to scope user lookup.
        """
        company_list = [c for c in companies if isinstance(c, CRMCompany)]
        if not company_list:
            return

        # Collect owner IDs that need enrichment
        owner_ids = {c.owner_external_id for c in company_list if c.owner_external_id}

        # Infer integration_id from first company if not provided
        if integration_id is None and company_list:
            integration_id = company_list[0].integration_id

        # Load related entities
        users = await self._load_users(owner_ids, integration_id) if owner_ids else {}

        # Apply enrichment
        for company in company_list:
            if company.owner_external_id:
                user = users.get(company.owner_external_id)
                if user:
                    self._apply_owner_details(company, user)

    async def enrich_companies_for_integration(self, integration_id: UUID) -> int:
        """Fetch and enrich all companies for a given integration.

        Args:
            integration_id: The integration to scope the enrichment to.

        Returns:
            Number of companies processed.
        """
        # Include deleted companies - they still need enrichment for historical data
        result = await self.db.execute(
            select(CRMCompany)
            .where(CRMCompany.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMCompany.integration_id == integration_id)  # type: ignore[arg-type]
        )
        companies = list(result.scalars().all())
        if not companies:
            return 0

        await self.enrich_companies(companies, integration_id)

        logger.debug(
            "Enriched %d companies for integration %s",
            len(companies),
            integration_id,
        )
        return len(companies)

    async def _load_users(
        self, owner_ids: set[str], integration_id: UUID | None = None
    ) -> dict[str, CRMUser]:
        """Load CRM users by external IDs.

        Args:
            owner_ids: Set of owner external IDs to load
            integration_id: Optional integration ID to scope user lookup

        Returns:
            Dictionary mapping external_id to CRMUser
        """
        if not owner_ids:
            return {}

        query = (
            select(CRMUser)
            .where(CRMUser.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMUser.external_id.in_(owner_ids))  # type: ignore[attr-defined]
            .where(CRMUser.is_deleted.is_(False))  # type: ignore[attr-defined]
        )

        # Filter by integration_id if provided
        if integration_id is not None:
            query = query.where(CRMUser.integration_id == integration_id)  # type: ignore[arg-type]

        result = await self.db.execute(query)
        return {user.external_id: user for user in result.scalars().all()}

    def _apply_owner_details(self, company: CRMCompany, user: CRMUser) -> None:
        """Apply owner details from user to company.

        Args:
            company: Company record to enrich
            user: User record with owner details
        """
        full_name = user.full_name or self._compose_name(user.first_name, user.last_name)

        # Set owner_id FK if not set
        if not company.owner_id:
            company.owner_id = user.user_id

        # Set owner_name if not set
        if not company.owner_name and full_name:
            company.owner_name = full_name

        # Set owner_email if not set
        if not company.owner_email and user.email:
            company.owner_email = user.email

    def _compose_name(self, first: Any, last: Any) -> str | None:
        """Compose full name from first and last name.

        Args:
            first: First name
            last: Last name

        Returns:
            Composed full name or None
        """
        parts = [str(part).strip() for part in (first, last) if part]
        return " ".join(parts) if parts else None
