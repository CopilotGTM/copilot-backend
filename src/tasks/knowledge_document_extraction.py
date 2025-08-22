from __future__ import annotations
"""Heavy document extraction task (PDF/DOC extraction).
"""
from typing import Optional
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
from src.domain.models.organisation_tables.job_models import JobStatus
# from src.tasks.pdf_extraction import extract_pdf
# from src.tasks.doc_extraction import extract_doc
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from sqlalchemy import text

logger = get_logger("knowledge_document_extraction")


@celery.task(name="knowledge.document_extraction", bind=True, acks_late=True, max_retries=3)
def document_extraction(self, organisation_id: str, source_id: str):  # pragma: no cover
    """Heavy extraction task (PDF/DOC etc.)."""
    session_gen = get_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)  # type: ignore[arg-type]
    try:
        source: OrganisationSource | None = session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Source %s not found for extract", source_id)
            return
        if source.status in {OrganisationSourceStatus.COMPLETED, OrganisationSourceStatus.FAILED}:
            return
        # Fetch job row
        job = session.query(KnowledgeProcessingJob).filter_by(source_id=source_id, job_type='document_extraction').first()
        if job:
            job.status = JobStatus.IN_PROGRESS
            session.add(job)
        
        # Transition status
        source.status = OrganisationSourceStatus.EXTRACTING
        session.add(source)
        session.commit()

        # Download blob (could stream if large)
        try:
            async def _fetch():
                blob_service = await storage._get_blob_service_client()
                blob_client = blob_service.get_blob_client(storage.container, source.storage_path)  # type: ignore[arg-type]
                downloader = await blob_client.download_blob()
                return await downloader.readall()
            data = asyncio.run(_fetch())
        except Exception as e:
            logger.exception("Extract download failed %s", source_id)
            source.status = OrganisationSourceStatus.FAILED
            source.error_message = f"extract_download_failed: {e}"[:500]
            if job:
                job.status = JobStatus.FAILED
                job.error_message = source.error_message
                session.add(job)
            session.add(source)
            session.commit()
            return

        # Simple mime routing
        mimetype = (source.mimetype or "").lower()
        result: dict | None = None
        # if mimetype in {"application/pdf"}:
        #     result = extract_pdf(source, data)
        # elif mimetype in {"application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}:
        #     result = extract_doc(source, data)
        # else:
        #     result = {"text": "", "meta": {"note": "unsupported / noop"}}

        # Persist simplistic result_data into job (placeholder)
        if job and result is not None:
            job.result_data = {"extraction": result}
            job.status = JobStatus.COMPLETED
            session.add(job)
        # Mark source completed for now (embedding step not yet implemented)
        source.status = OrganisationSourceStatus.COMPLETED
        session.add(source)
        session.commit()
    finally:
        try:
            session.close()
        except Exception:
            pass
        try:
            asyncio.run(storage.close())
        except Exception:
            pass
