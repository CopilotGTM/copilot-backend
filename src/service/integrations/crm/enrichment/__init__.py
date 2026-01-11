"""CRM Enrichment Services.

Organization-level enrichment services that populate denormalized fields
on CRM entities from related records. These services are provider-agnostic
and work with normalized ORM models.

Services:
    - DealEnrichmentService: Enriches deals with owner, contact, company details
    - ContactEnrichmentService: Enriches contacts with owner, company details
    - CompanyEnrichmentService: Enriches companies with owner details
    - OwnerEnrichmentService: Resolves owner_id FKs across all entities
    - TaskEnrichmentService: Enriches activities with related entity FKs

Orchestrator:
    - CRMEnrichmentOrchestrator: Organization-level entry point for enrichment

Detectors (for on-demand reference data fetching):
    - MissingUsersDetector: Detects owner_external_ids without matching CRMUser
    - MissingPipelinesDetector: Detects pipeline_external_ids without matching CRMPipeline
"""

from .company_enrichment_service import CompanyEnrichmentService
from .contact_enrichment_service import ContactEnrichmentService
from .deal_enrichment_service import DealEnrichmentService
from .orchestrator import CRMEnrichmentOrchestrator
from .owner_enrichment_service import OwnerEnrichmentService
from .reference_data_detector import MissingPipelinesDetector, MissingUsersDetector
from .task_enrichment_service import TaskEnrichmentService

__all__ = [
    "CRMEnrichmentOrchestrator",
    "CompanyEnrichmentService",
    "ContactEnrichmentService",
    "DealEnrichmentService",
    "MissingPipelinesDetector",
    "MissingUsersDetector",
    "OwnerEnrichmentService",
    "TaskEnrichmentService",
]
