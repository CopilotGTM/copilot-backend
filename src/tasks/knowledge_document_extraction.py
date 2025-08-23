from __future__ import annotations
"""Heavy document extraction task (PDF/DOC extraction).
"""
import asyncio

from src.celery_app import celery
from src.infra.utils.logger import get_logger
from src.infra.utils.database import get_org_session
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.domain.models.organisation_tables.job_models import (
    KnowledgeProcessingJob,
    JobStatus,
)
from src.infra.messaging.knowledge_publish import publish_source_status
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate

logger = get_logger("knowledge_document_extraction")


@celery.task(name="knowledge.document_extraction", bind=True, acks_late=True, max_retries=3)
def document_extraction(self, organisation_id: str, source_id: str):  # pragma: no cover
    """Heavy extraction task (PDF/DOC etc.)."""
    session_gen = get_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    try:
        source: OrganisationSource | None = session.get(OrganisationSource, source_id)

        # Check if source exists and is not in a terminal state
        if not source:
            logger.warning("Source %s not found for extract", source_id)
            return

        # Normalise status to string for comparisons (can be Enum or raw str)
        current_status_val = source.status.value if hasattr(source.status, "value") else str(source.status)
        if current_status_val in {OrganisationSourceStatus.COMPLETED.value, OrganisationSourceStatus.FAILED.value}:
            return

        # Fetch job row
        job = session.query(KnowledgeProcessingJob).filter_by(source_id=source_id, job_type='document_extraction').first()
        if not job:
            return

        # Transition status
        source.status = OrganisationSourceStatus.COMPLETED
        session.add(source)
        session.commit()

        # Prepare safe status value (Enum or str)
        final_status_val = source.status.value if hasattr(source.status, "value") else str(source.status)

        publish_event = KnowledgeSourceEventUpdate(
            source_id=str(source.source_id),
            status=final_status_val,
            updated_at=source.updated_at.isoformat() if source.updated_at else None,
            error_message=source.error_message,
            progress=None,
            thumbnail_medium_url=None,
            thumbnail_small_url=None,
        )

        publish_source_status(organisation_id, publish_event)
        logger.info("Published source status update", extra={"event": publish_event})
    finally:
        try:
            session.close()
        except Exception:
            pass
        try:
            asyncio.run(storage.close())
        except Exception:
            pass
