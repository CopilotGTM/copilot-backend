"""CRM Enrichment Orchestrator.

Organization-level CRM enrichment coordinator that provides a single entry point
to trigger enrichment for all or specific entity types within an organization
after sync completes.

Key Features:
- Provider-aware trigger logic (HubSpot vs Salesforce differences)
- Centralized enrichment coordination

For on-demand reference data fetching, use the separate detector classes:
- MissingUsersDetector: Detects owner_external_ids without matching CRMUser
- MissingPipelinesDetector: Detects pipeline_external_ids without matching CRMPipeline
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.utils.logger import get_logger

from .company_enrichment_service import CompanyEnrichmentService
from .contact_enrichment_service import ContactEnrichmentService
from .deal_enrichment_service import DealEnrichmentService
from .owner_enrichment_service import OwnerEnrichmentService
from .reference_data_detector import MissingPipelinesDetector, MissingUsersDetector
from .task_enrichment_service import TaskEnrichmentService

if TYPE_CHECKING:
    from src.domain.models.organisation_tables.sync import KnowledgeIntegration

logger = get_logger(__name__)


class CRMEnrichmentOrchestrator:
    """Organization-level CRM enrichment coordinator.

    Provides a single entry point to trigger enrichment for all or specific
    entity types within an organization after sync completes.

    The orchestrator handles provider-specific trigger logic:
    - HubSpot: Separate pipeline entity, Owners subset of users
    - Salesforce: Stages inline in Opportunity, Users table

    Usage:
        orchestrator = CRMEnrichmentOrchestrator(
            db=session,
            organisation_id=org_id,
            integration=integration,  # KnowledgeIntegration object
        )
        result = await orchestrator.enrich_after_deals_sync()
    """

    # Provider constants
    HUBSPOT_PROVIDERS = {IntegrationType.HUBSPOT}
    SALESFORCE_PROVIDERS = {IntegrationType.SALESFORCE}

    def __init__(
        self,
        *,
        db: AsyncSession,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
    ) -> None:
        self.db = db
        self.organisation_id = organisation_id
        self.integration = integration
        self.integration_id = integration.integration_id

        # Determine provider type
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)

        self.integration_type = integration_type
        self.is_hubspot = integration_type in self.HUBSPOT_PROVIDERS
        self.is_salesforce = integration_type in self.SALESFORCE_PROVIDERS

        # Lazily initialized services
        self._deal_enrichment: DealEnrichmentService | None = None
        self._contact_enrichment: ContactEnrichmentService | None = None
        self._company_enrichment: CompanyEnrichmentService | None = None
        self._owner_enrichment: OwnerEnrichmentService | None = None
        self._task_enrichment: TaskEnrichmentService | None = None

    @property
    def deal_enrichment(self) -> DealEnrichmentService:
        if self._deal_enrichment is None:
            self._deal_enrichment = DealEnrichmentService(
                db=self.db, organisation_id=self.organisation_id
            )
        return self._deal_enrichment

    @property
    def contact_enrichment(self) -> ContactEnrichmentService:
        if self._contact_enrichment is None:
            self._contact_enrichment = ContactEnrichmentService(
                db=self.db, organisation_id=self.organisation_id
            )
        return self._contact_enrichment

    @property
    def company_enrichment(self) -> CompanyEnrichmentService:
        if self._company_enrichment is None:
            self._company_enrichment = CompanyEnrichmentService(
                db=self.db, organisation_id=self.organisation_id
            )
        return self._company_enrichment

    @property
    def owner_enrichment(self) -> OwnerEnrichmentService:
        if self._owner_enrichment is None:
            self._owner_enrichment = OwnerEnrichmentService(
                db=self.db, organisation_id=self.organisation_id
            )
        return self._owner_enrichment

    @property
    def task_enrichment(self) -> TaskEnrichmentService:
        if self._task_enrichment is None:
            self._task_enrichment = TaskEnrichmentService(
                db=self.db, organisation_id=self.organisation_id
            )
        return self._task_enrichment

    async def enrich_after_deals_sync(self, detect_missing: bool = True) -> dict:
        """Called after deals sync - enriches deals + participants.

        HubSpot Flow:
            1. Enrich deals (owner, contact, company details)
            2. Enrich participants
            3. Detect missing reference data (pipelines, users)

        Salesforce Flow:
            1. Enrich deals (owner, contact, company details)
            2. Enrich participants
            3. Detect missing reference data (pipelines, users)

        Args:
            detect_missing: If True, detect missing pipelines/users after enrichment.
                           Returns list of missing IDs for caller to trigger re-sync.

        Returns:
            Dictionary with enrichment metrics and missing reference data IDs.
            If detect_missing=True, includes:
                - missing_pipelines: List of pipeline external IDs not found
                - missing_users: List of user external IDs not found
        """
        logger.info(
            "Running deal enrichment: org=%s integration=%s provider=%s",
            self.organisation_id,
            self.integration_id,
            self.integration_type.value
            if hasattr(self.integration_type, "value")
            else self.integration_type,
        )

        result: dict = {
            "entity": "deals",
            "deals_enriched": 0,
            "participants_enriched": 0,
            "missing_pipelines": [],
            "missing_users": [],
        }

        # Step 1: Enrich deals
        deals_count = await self.deal_enrichment.enrich_deals_for_integration(self.integration_id)
        result["deals_enriched"] = deals_count

        # Step 2: Enrich participants
        participants_count = await self.deal_enrichment.enrich_participants(self.integration_id)
        result["participants_enriched"] = participants_count

        # Step 3: Detect missing reference data (for on-demand sync trigger)
        if detect_missing:
            missing_pipelines = await self._detect_missing_pipelines()
            missing_users = await self._detect_missing_users()

            result["missing_pipelines"] = missing_pipelines
            result["missing_users"] = missing_users

            if missing_pipelines:
                logger.warning(
                    "Missing pipelines detected after deal enrichment: org=%s count=%d ids=%s",
                    self.organisation_id,
                    len(missing_pipelines),
                    missing_pipelines[:5],  # Log first 5 for brevity
                )

            if missing_users:
                logger.warning(
                    "Missing users detected after deal enrichment: org=%s count=%d ids=%s",
                    self.organisation_id,
                    len(missing_users),
                    missing_users[:5],  # Log first 5 for brevity
                )

        logger.info(
            "Deal enrichment complete: org=%s deals=%d participants=%d missing_pipelines=%d missing_users=%d",
            self.organisation_id,
            deals_count,
            participants_count,
            len(result.get("missing_pipelines", [])),
            len(result.get("missing_users", [])),
        )

        return result

    async def enrich_after_contacts_sync(self, detect_missing: bool = True) -> dict:
        """Called after contacts sync - enriches contacts + re-enriches related entities.

        Both HubSpot and Salesforce:
            1. Enrich contacts (owner details only - no company FK on contacts)
            2. Re-enrich deal participants (now that contacts exist)
            3. Re-enrich tasks (resolve contact_id FKs)
            4. Detect missing users (if detect_missing=True)

        Args:
            detect_missing: If True, detect missing users after enrichment.

        Returns:
            Dictionary with enrichment metrics and missing reference data.
        """
        logger.info(
            "Running contact enrichment: org=%s integration=%s",
            self.organisation_id,
            self.integration_id,
        )

        result: dict = {
            "entity": "contacts",
            "contacts_enriched": 0,
            "participants_enriched": 0,
            "tasks_enriched": 0,
            "missing_users": [],
        }

        # Step 1: Enrich contacts
        contacts_count = await self.contact_enrichment.enrich_contacts_for_integration(
            self.integration_id
        )
        result["contacts_enriched"] = contacts_count

        # Step 2: Re-enrich deal participants (now that contacts exist)
        participants_count = await self.deal_enrichment.enrich_participants(self.integration_id)
        result["participants_enriched"] = participants_count

        # Step 3: Re-enrich tasks (resolve contact_id FKs)
        tasks_count = await self.task_enrichment.enrich_tasks_for_integration(self.integration_id)
        result["tasks_enriched"] = tasks_count

        # Step 4: Detect missing users
        if detect_missing:
            missing_users = await self._detect_missing_users()
            result["missing_users"] = missing_users
            if missing_users:
                logger.warning(
                    "Missing users detected after contact enrichment: org=%s count=%d",
                    self.organisation_id,
                    len(missing_users),
                )

        logger.info(
            "Contact enrichment complete: org=%s contacts=%d participants=%d tasks=%d missing_users=%d",
            self.organisation_id,
            contacts_count,
            participants_count,
            tasks_count,
            len(result.get("missing_users", [])),
        )

        return result

    async def enrich_after_companies_sync(self) -> dict:
        """Called after companies sync - enriches companies + re-enriches related entities.

        HubSpot Flow:
            1. Enrich companies (owner details)
            2. Re-enrich deals (company_name)
            3. Re-enrich tasks (resolve company_id FKs)

        Salesforce Flow:
            1. Enrich accounts (owner details)
            2. Re-enrich opportunities (account_name)
            3. Re-enrich tasks

        Note: Contacts are NOT re-enriched here because CRMContact.company_name
        is a string field from the source system, not a FK to CRMCompany.

        Returns:
            Dictionary with enrichment metrics
        """
        logger.info(
            "Running company enrichment: org=%s integration=%s",
            self.organisation_id,
            self.integration_id,
        )

        result = {
            "entity": "companies",
            "companies_enriched": 0,
            "deals_enriched": 0,
            "tasks_enriched": 0,
        }

        # Step 1: Enrich companies
        companies_count = await self.company_enrichment.enrich_companies_for_integration(
            self.integration_id
        )
        result["companies_enriched"] = companies_count

        # Step 2: Re-enrich deals (company_name)
        deals_count = await self.deal_enrichment.enrich_deals_for_integration(self.integration_id)
        result["deals_enriched"] = deals_count

        # Step 3: Re-enrich tasks (resolve company_id FKs)
        tasks_count = await self.task_enrichment.enrich_tasks_for_integration(self.integration_id)
        result["tasks_enriched"] = tasks_count

        logger.info(
            "Company enrichment complete: org=%s companies=%d deals=%d tasks=%d",
            self.organisation_id,
            companies_count,
            deals_count,
            tasks_count,
        )

        return result

    async def enrich_after_users_sync(self) -> dict:
        """Called after users sync - resolves owner_id across all entities.

        This is a special enrichment that resolves owner_id FKs for all entity types
        that have owner_external_id but no owner_id set.

        Both HubSpot and Salesforce:
            1. Resolve owner_id for companies
            2. Resolve owner_id for contacts
            3. Resolve owner_id for deals

        Returns:
            Dictionary with enrichment metrics
        """
        logger.info(
            "Running owner enrichment after users sync: org=%s integration=%s",
            self.organisation_id,
            self.integration_id,
        )

        # Use OwnerEnrichmentService which handles all entity types
        owner_result = await self.owner_enrichment.enrich_owners_for_integration(
            self.integration_id
        )

        # Build result with entity identifier
        result: dict = {"entity": "users", **owner_result}

        logger.info(
            "Owner enrichment complete: org=%s results=%s",
            self.organisation_id,
            result,
        )

        return result

    async def enrich_after_activities_sync(self) -> dict:
        """Called after activities sync - enriches tasks with related entity FKs.

        Both HubSpot and Salesforce:
            1. Enrich tasks (owner, deal_id, contact_id, company_id)

        Returns:
            Dictionary with enrichment metrics
        """
        logger.info(
            "Running activity enrichment: org=%s integration=%s",
            self.organisation_id,
            self.integration_id,
        )

        result = {
            "entity": "activities",
            "tasks_enriched": 0,
        }

        # Enrich tasks
        tasks_count = await self.task_enrichment.enrich_tasks_for_integration(self.integration_id)
        result["tasks_enriched"] = tasks_count

        logger.info(
            "Activity enrichment complete: org=%s tasks=%d",
            self.organisation_id,
            tasks_count,
        )

        return result

    async def enrich_all(self) -> dict:
        """Full enrichment pass - useful for initial sync or repair.

        Runs all enrichments in the correct order:
            1. Companies (owner)
            2. Contacts (owner, company)
            3. Deals (owner, contact, company) + participants
            4. Activities (owner, deal, contact, company)

        Returns:
            Dictionary with all enrichment metrics
        """
        logger.info(
            "Running full enrichment: org=%s integration=%s",
            self.organisation_id,
            self.integration_id,
        )

        results = {
            "full_enrichment": True,
            "companies": {},
            "contacts": {},
            "deals": {},
            "activities": {},
        }

        # Step 1: Companies
        results["companies"] = {
            "enriched": await self.company_enrichment.enrich_companies_for_integration(
                self.integration_id
            )
        }

        # Step 2: Contacts
        results["contacts"] = {
            "enriched": await self.contact_enrichment.enrich_contacts_for_integration(
                self.integration_id
            )
        }

        # Step 3: Deals + participants
        deals_count = await self.deal_enrichment.enrich_deals_for_integration(self.integration_id)
        participants_count = await self.deal_enrichment.enrich_participants(self.integration_id)
        results["deals"] = {
            "enriched": deals_count,
            "participants": participants_count,
        }

        # Step 4: Activities
        results["activities"] = {
            "enriched": await self.task_enrichment.enrich_tasks_for_integration(self.integration_id)
        }

        logger.info(
            "Full enrichment complete: org=%s results=%s",
            self.organisation_id,
            results,
        )

        return results

    # =========================================================================
    # Helper methods for detecting missing reference data
    # =========================================================================

    async def _detect_missing_pipelines(self) -> list[str]:
        """Detect pipeline_external_ids that don't have matching CRMPipeline records.

        Used after deal enrichment to identify if pipeline sync is needed.

        Returns:
            List of pipeline external IDs not found in the database.
        """
        detector = MissingPipelinesDetector(
            db=self.db,
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
        )
        return await detector.detect()

    async def _detect_missing_users(self) -> list[str]:
        """Detect owner_external_ids that don't have matching CRMUser records.

        Used after any enrichment to identify if users sync is needed.

        Returns:
            List of user external IDs not found in the database.
        """
        detector = MissingUsersDetector(
            db=self.db,
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
        )
        return await detector.detect()

    async def detect_missing_reference_data(self) -> dict:
        """Detect all missing reference data for this integration.

        Convenience method that runs all detectors and returns a summary.
        Useful for diagnostics or triggering on-demand syncs.

        Returns:
            Dictionary with missing data summary:
            {
                "missing_pipelines": ["id1", "id2"],
                "missing_users": ["id3", "id4"],
                "needs_pipeline_sync": True,
                "needs_users_sync": True,
            }
        """
        missing_pipelines = await self._detect_missing_pipelines()
        missing_users = await self._detect_missing_users()

        return {
            "missing_pipelines": missing_pipelines,
            "missing_users": missing_users,
            "needs_pipeline_sync": len(missing_pipelines) > 0,
            "needs_users_sync": len(missing_users) > 0,
        }
