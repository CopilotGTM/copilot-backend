"""Reference data detection for CRM enrichment.

Detects missing reference data (users, pipelines) that prevents proper enrichment.
These detectors are used to identify when on-demand fetching is needed.

Usage:
    detector = MissingUsersDetector(db, organisation_id, integration_id)
    missing_ids = await detector.detect()
    if missing_ids:
        # Trigger user fetch for these IDs
        await fetch_users(missing_ids)
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync.crm import (
    CRMCompany,
    CRMContact,
    CRMDeal,
    CRMDealTask,
    CRMPipeline,
    CRMUser,
)
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class MissingUsersDetector:
    """Detects owner_external_ids that don't have matching CRMUser records.

    Scans across all entity types (companies, contacts, deals, tasks) to find
    owner_external_ids that reference users not yet synced to the database.

    Usage:
        detector = MissingUsersDetector(db, org_id, integration_id)
        missing = await detector.detect()
        # missing = ["user_123", "user_456"] or [] if all users exist
    """

    def __init__(
        self,
        db: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.db = db
        self.organisation_id = organisation_id
        self.integration_id = integration_id

    async def detect(self) -> list[str]:
        """Detect missing users across all CRM entities.

        Returns:
            List of owner_external_ids that don't have matching CRMUser records.
            Empty list if all referenced users exist.
        """
        # Collect all owner_external_ids from entities
        all_owner_ext_ids: set[str] = set()

        # From companies
        company_owners = await self._get_owner_ids_from_companies()
        all_owner_ext_ids.update(company_owners)

        # From contacts
        contact_owners = await self._get_owner_ids_from_contacts()
        all_owner_ext_ids.update(contact_owners)

        # From deals
        deal_owners = await self._get_owner_ids_from_deals()
        all_owner_ext_ids.update(deal_owners)

        # From tasks
        task_owners = await self._get_owner_ids_from_tasks()
        all_owner_ext_ids.update(task_owners)

        if not all_owner_ext_ids:
            return []

        # Get existing user external IDs
        existing_user_ids = await self._get_existing_user_ids()

        # Find missing
        missing = all_owner_ext_ids - existing_user_ids

        if missing:
            logger.info(
                "Detected %d missing users for org=%s integration=%s",
                len(missing),
                self.organisation_id,
                self.integration_id,
            )

        return list(missing)

    async def _get_owner_ids_from_companies(self) -> set[str]:
        """Get distinct owner_external_ids from companies."""
        result = await self.db.execute(
            select(distinct(CRMCompany.owner_external_id))  # type: ignore[arg-type]
            .where(CRMCompany.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMCompany.integration_id == self.integration_id)  # type: ignore[arg-type]
            .where(CRMCompany.owner_external_id.isnot(None))  # type: ignore[union-attr]
            .where(CRMCompany.is_deleted.is_(False))  # type: ignore[union-attr]
        )
        return {row[0] for row in result.all() if row[0]}

    async def _get_owner_ids_from_contacts(self) -> set[str]:
        """Get distinct owner_external_ids from contacts."""
        result = await self.db.execute(
            select(distinct(CRMContact.owner_external_id))  # type: ignore[arg-type]
            .where(CRMContact.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMContact.integration_id == self.integration_id)  # type: ignore[arg-type]
            .where(CRMContact.owner_external_id.isnot(None))  # type: ignore[union-attr]
            .where(CRMContact.is_deleted.is_(False))  # type: ignore[union-attr]
        )
        return {row[0] for row in result.all() if row[0]}

    async def _get_owner_ids_from_deals(self) -> set[str]:
        """Get distinct owner_external_ids from deals."""
        result = await self.db.execute(
            select(distinct(CRMDeal.owner_external_id))  # type: ignore[arg-type]
            .where(CRMDeal.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDeal.integration_id == self.integration_id)  # type: ignore[arg-type]
            .where(CRMDeal.owner_external_id.isnot(None))  # type: ignore[union-attr]
            .where(CRMDeal.is_deleted.is_(False))  # type: ignore[union-attr]
        )
        return {row[0] for row in result.all() if row[0]}

    async def _get_owner_ids_from_tasks(self) -> set[str]:
        """Get distinct owner_external_ids from tasks."""
        result = await self.db.execute(
            select(distinct(CRMDealTask.owner_external_id))  # type: ignore[arg-type]
            .where(CRMDealTask.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDealTask.integration_id == self.integration_id)  # type: ignore[arg-type]
            .where(CRMDealTask.owner_external_id.isnot(None))  # type: ignore[union-attr]
            .where(CRMDealTask.is_deleted.is_(False))  # type: ignore[union-attr]
        )
        return {row[0] for row in result.all() if row[0]}

    async def _get_existing_user_ids(self) -> set[str]:
        """Get all existing user external IDs for this integration."""
        result = await self.db.execute(
            select(distinct(CRMUser.external_id))  # type: ignore[arg-type]
            .where(CRMUser.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMUser.integration_id == self.integration_id)  # type: ignore[arg-type]
            .where(CRMUser.is_deleted.is_(False))  # type: ignore[union-attr]
        )
        return {row[0] for row in result.all() if row[0]}


class MissingPipelinesDetector:
    """Detects pipeline_external_ids that don't have matching CRMPipeline records.

    Scans deals to find pipeline_external_ids that reference pipelines not yet
    synced to the database.

    Usage:
        detector = MissingPipelinesDetector(db, org_id, integration_id)
        missing = await detector.detect()
        # missing = ["pipeline_123"] or [] if all pipelines exist
    """

    def __init__(
        self,
        db: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.db = db
        self.organisation_id = organisation_id
        self.integration_id = integration_id

    async def detect(self) -> list[str]:
        """Detect missing pipelines referenced by deals.

        Returns:
            List of pipeline_external_ids that don't have matching CRMPipeline records.
            Empty list if all referenced pipelines exist.
        """
        # Get all pipeline_external_ids from deals
        deal_pipeline_ids = await self._get_pipeline_ids_from_deals()

        if not deal_pipeline_ids:
            return []

        # Get existing pipeline external IDs
        existing_pipeline_ids = await self._get_existing_pipeline_ids()

        # Find missing
        missing = deal_pipeline_ids - existing_pipeline_ids

        if missing:
            logger.info(
                "Detected %d missing pipelines for org=%s integration=%s",
                len(missing),
                self.organisation_id,
                self.integration_id,
            )

        return list(missing)

    async def _get_pipeline_ids_from_deals(self) -> set[str]:
        """Get distinct pipeline_external_ids from deals."""
        result = await self.db.execute(
            select(distinct(CRMDeal.pipeline_external_id))  # type: ignore[arg-type]
            .where(CRMDeal.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMDeal.integration_id == self.integration_id)  # type: ignore[arg-type]
            .where(CRMDeal.pipeline_external_id.isnot(None))  # type: ignore[union-attr]
            .where(CRMDeal.is_deleted.is_(False))  # type: ignore[union-attr]
        )
        return {row[0] for row in result.all() if row[0]}

    async def _get_existing_pipeline_ids(self) -> set[str]:
        """Get all existing pipeline external IDs for this integration."""
        result = await self.db.execute(
            select(distinct(CRMPipeline.external_id))  # type: ignore[arg-type]
            .where(CRMPipeline.organisation_id == self.organisation_id)  # type: ignore[arg-type]
            .where(CRMPipeline.integration_id == self.integration_id)  # type: ignore[arg-type]
            .where(CRMPipeline.is_deleted.is_(False))  # type: ignore[union-attr]
        )
        return {row[0] for row in result.all() if row[0]}
