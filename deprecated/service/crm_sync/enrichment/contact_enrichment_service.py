"""Contact enrichment helper service.

Enriches CRMContact records with denormalised data pulled from related
entities (users for owner). Intended to be used after contacts sync
to populate owner details.

Note: CRMContact does not have a FK to companies - company_name is just
a string field from the source system, not a relationship.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMContact,
    CRMUser,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class ContactEnrichmentService:
    """Populate denormalised contact fields from related CRM entities.

    Handles enrichment of:
    - Owner details (name, email, owner_id FK) from CRMUser
    """

    def __init__(self, *, db: AsyncSession, organisation_id: UUID) -> None:
        self.db = db
        self.organisation_id = organisation_id

    async def enrich_contacts(
        self, contacts: Iterable[CRMContact], integration_id: UUID | None = None
    ) -> None:
        """Enrich the provided contact records in-place.

        Args:
            contacts: Iterable of contact ORM objects already attached to the session.
            integration_id: Optional integration ID to scope user lookup.
        """
        contact_list = [c for c in contacts if isinstance(c, CRMContact)]
        if not contact_list:
            return

        # Collect owner IDs that need enrichment
        owner_ids = {c.owner_external_id for c in contact_list if c.owner_external_id}

        # Infer integration_id from first contact if not provided
        if integration_id is None and contact_list:
            integration_id = contact_list[0].integration_id

        # Log enrichment stats
        contacts_with_owner = len([c for c in contact_list if c.owner_external_id])
        contacts_needing_owner_id = len(
            [c for c in contact_list if c.owner_external_id and not c.owner_id]
        )
        logger.info(
            "Contact enrichment: total=%d, with_owner_external_id=%d, needing_owner_id=%d, unique_owner_ids=%d",
            len(contact_list),
            contacts_with_owner,
            contacts_needing_owner_id,
            len(owner_ids),
        )

        # Load related entities
        users = await self._load_users(owner_ids, integration_id) if owner_ids else {}

        logger.info(
            "Loaded %d CRM users for %d owner_external_ids",
            len(users),
            len(owner_ids),
        )

        # Apply enrichment
        enriched_count = 0
        for contact in contact_list:
            if contact.owner_external_id:
                user = users.get(contact.owner_external_id)
                if user:
                    self._apply_owner_details(contact, user)
                    enriched_count += 1
                else:
                    logger.debug(
                        "No CRM user found for contact owner_external_id=%s",
                        contact.owner_external_id,
                    )

        logger.info("Enriched %d contacts with owner details", enriched_count)

    async def enrich_contacts_for_integration(self, integration_id: UUID) -> int:
        """Fetch and enrich all contacts for a given integration.

        Args:
            integration_id: The integration to scope the enrichment to.

        Returns:
            Number of contacts processed.
        """
        # Include deleted contacts - they still need enrichment for historical data
        result = await self.db.execute(
            select(CRMContact)
            .where(CRMContact.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMContact.integration_id == integration_id)  # type: ignore[arg-type]
        )
        contacts = list(result.scalars().all())
        if not contacts:
            return 0

        await self.enrich_contacts(contacts, integration_id)

        logger.debug(
            "Enriched %d contacts for integration %s",
            len(contacts),
            integration_id,
        )
        return len(contacts)

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

    def _apply_owner_details(self, contact: CRMContact, user: CRMUser) -> None:
        """Apply owner details from user to contact.

        Args:
            contact: Contact record to enrich
            user: User record with owner details
        """
        full_name = user.full_name or self._compose_name(user.first_name, user.last_name)

        # Set owner_id FK if not set
        if not contact.owner_id:
            contact.owner_id = user.user_id

        # Update owner_name if user has a name and it differs
        if full_name and full_name != contact.owner_name:
            contact.owner_name = full_name

        # Update owner_email if user has an email and it differs
        if user.email and user.email != contact.owner_email:
            contact.owner_email = user.email

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
