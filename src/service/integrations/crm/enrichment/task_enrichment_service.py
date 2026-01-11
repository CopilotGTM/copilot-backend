"""Task/Activity enrichment helper service.

Enriches CRMActivity (CRMDealTask) records with denormalised data pulled from
related entities (users, deals, contacts, companies). Intended to be used by
the activity sync flow to populate related entity details after syncing.

This service handles bidirectional linking:
- When activities are synced first: external IDs are stored, local IDs resolved later
- When related entities are synced first: activities get enriched with local IDs
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMDealTask,
    CRMUser,
)


class TaskEnrichmentService:
    """Populate denormalised activity fields from related CRM entities.

    Handles enrichment of:
    - Owner details (name, email) from CRMUser
    - Deal linkage (deal_id) from CRMDeal
    - Contact linkage (contact_id) from CRMContact
    - Company linkage (company_id) from CRMCompany
    """

    def __init__(self, *, db: AsyncSession, organisation_id: UUID) -> None:
        self.db = db
        self.organisation_id = organisation_id

    async def enrich_tasks(
        self, tasks: Iterable[CRMDealTask], integration_id: UUID | None = None
    ) -> None:
        """Enrich the provided activity records in-place.

        Args:
            tasks: Iterable of activity ORM objects already attached to the session.
            integration_id: Optional integration ID to scope entity lookups.
        """
        task_list = [task for task in tasks if isinstance(task, CRMDealTask)]
        if not task_list:
            return

        # Infer integration_id from first task if not provided
        if integration_id is None and task_list:
            integration_id = task_list[0].integration_id

        # Collect IDs that need enrichment
        owner_ids = {task.owner_external_id for task in task_list if task.owner_external_id}
        deal_ext_ids = {
            task.deal_external_id
            for task in task_list
            if task.deal_external_id and not task.deal_id
        }
        contact_ext_ids = {
            task.contact_external_id
            for task in task_list
            if task.contact_external_id and not task.contact_id
        }
        company_ext_ids = {
            task.company_external_id
            for task in task_list
            if task.company_external_id and not task.company_id
        }

        # Load related entities (scoped to integration)
        users = await self._load_users(owner_ids, integration_id) if owner_ids else {}
        deals = await self._load_deals(deal_ext_ids, integration_id) if deal_ext_ids else {}
        contacts = (
            await self._load_contacts(contact_ext_ids, integration_id) if contact_ext_ids else {}
        )
        companies = (
            await self._load_companies(company_ext_ids, integration_id) if company_ext_ids else {}
        )

        # Apply enrichment
        for task in task_list:
            if task.owner_external_id:
                user = users.get(task.owner_external_id)
                if user:
                    self._apply_owner_details(task, user)

            if task.deal_external_id and not task.deal_id:
                deal = deals.get(task.deal_external_id)
                if deal:
                    task.deal_id = deal.deal_id

            if task.contact_external_id and not task.contact_id:
                contact = contacts.get(task.contact_external_id)
                if contact:
                    task.contact_id = contact.contact_id

            if task.company_external_id and not task.company_id:
                company = companies.get(task.company_external_id)
                if company:
                    task.company_id = company.company_id

    async def enrich_tasks_for_integration(self, integration_id: UUID) -> int:
        """Fetch and enrich all tasks for a given integration.

        Args:
            integration_id: The integration to scope the enrichment to.

        Returns:
            Number of tasks processed.
        """
        # Include deleted tasks - they still need enrichment for historical data
        result = await self.db.execute(
            select(CRMDealTask)
            .where(CRMDealTask.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDealTask.integration_id == integration_id)  # type: ignore[arg-type]
        )
        tasks = list(result.scalars().all())
        if not tasks:
            return 0
        await self.enrich_tasks(tasks, integration_id)
        return len(tasks)

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
        query = (
            select(CRMUser)
            .where(CRMUser.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMUser.external_id.in_(owner_ids))  # type: ignore[attr-defined]
            .where(CRMUser.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        if integration_id is not None:
            query = query.where(CRMUser.integration_id == integration_id)  # type: ignore[arg-type]
        result = await self.db.execute(query)
        return {user.external_id: user for user in result.scalars().all()}

    def _apply_owner_details(self, task: CRMDealTask, user: CRMUser) -> None:
        """Apply owner details from user to task.

        Args:
            task: Task record to enrich
            user: User record with owner details
        """
        full_name = user.full_name or self._compose_name(user.first_name, user.last_name)
        if full_name and full_name != task.owner_name:
            task.owner_name = full_name

        if user.email and user.email != task.owner_email:
            task.owner_email = user.email

        # Also set owner_id FK if not set
        if not task.owner_id:
            task.owner_id = user.user_id

    async def _load_deals(
        self, deal_ext_ids: set[str], integration_id: UUID | None = None
    ) -> dict[str, CRMDeal]:
        """Load CRM deals by external IDs.

        Args:
            deal_ext_ids: Set of deal external IDs to load
            integration_id: Optional integration ID to scope lookup

        Returns:
            Dictionary mapping external_id to CRMDeal
        """
        query = (
            select(CRMDeal)
            .where(CRMDeal.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDeal.external_id.in_(deal_ext_ids))  # type: ignore[attr-defined]
            .where(CRMDeal.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        if integration_id is not None:
            query = query.where(CRMDeal.integration_id == integration_id)  # type: ignore[arg-type]
        result = await self.db.execute(query)
        return {deal.external_id: deal for deal in result.scalars().all()}

    async def _load_contacts(
        self, contact_ext_ids: set[str], integration_id: UUID | None = None
    ) -> dict[str, CRMContact]:
        """Load CRM contacts by external IDs.

        Args:
            contact_ext_ids: Set of contact external IDs to load
            integration_id: Optional integration ID to scope lookup

        Returns:
            Dictionary mapping external_id to CRMContact
        """
        query = (
            select(CRMContact)
            .where(CRMContact.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMContact.external_id.in_(contact_ext_ids))  # type: ignore[attr-defined]
            .where(CRMContact.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        if integration_id is not None:
            query = query.where(CRMContact.integration_id == integration_id)  # type: ignore[arg-type]
        result = await self.db.execute(query)
        return {contact.external_id: contact for contact in result.scalars().all()}

    async def _load_companies(
        self, company_ext_ids: set[str], integration_id: UUID | None = None
    ) -> dict[str, CRMCompany]:
        """Load CRM companies by external IDs.

        Args:
            company_ext_ids: Set of company external IDs to load
            integration_id: Optional integration ID to scope lookup

        Returns:
            Dictionary mapping external_id to CRMCompany
        """
        query = (
            select(CRMCompany)
            .where(CRMCompany.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMCompany.external_id.in_(company_ext_ids))  # type: ignore[attr-defined]
            .where(CRMCompany.is_deleted.is_(False))  # type: ignore[attr-defined]
        )
        if integration_id is not None:
            query = query.where(CRMCompany.integration_id == integration_id)  # type: ignore[arg-type]
        result = await self.db.execute(query)
        return {company.external_id: company for company in result.scalars().all()}

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
