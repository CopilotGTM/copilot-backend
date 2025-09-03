from __future__ import annotations

"""Deletion task for knowledge sources and related data.

Performs idempotent cleanup of:
- Derived documents in `knowledge_base` (by source_id)
- Processing jobs for the source
- Original and thumbnail blobs in storage (if present)
- OrganisationSource record itself
"""

from src.workers.knowledge_worker.celery_app import app
from src.infra.utils.logger import get_logger
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.domain.models.organisation_tables.knowledge_models import OrganisationSource
from src.domain.models.organisation_tables.job_models import KnowledgeProcessingJob
from src.workers.knowledge_worker.utils import DocumentStore

logger = get_logger("knowledge_delete_task")


@app.task(
    name="knowledge.delete_source",
    bind=True,
    acks_late=True,
    max_retries=3,
)
def delete_source_task(self, organisation_id: str, source_id: str):  # pragma: no cover
    """Delete a knowledge source and all related resources.

    Safety measures:
    - All delete operations are best-effort and idempotent.
    - Missing records or blobs are logged and ignored.
    """
    logger.info("Deleting knowledge source %s in org %s", source_id, organisation_id)

    # DB session & storage
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)

    try:
        # Fetch source (may be missing if already deleted)
        src = session.get(OrganisationSource, source_id)

        # Delete derived documents
        try:
            ds = DocumentStore(organisation_id)
            deleted_docs = ds.delete_documents_by_source(source_id)
            logger.info("Deleted %s derived documents for source %s", deleted_docs, source_id)
        except Exception as e:
            logger.warning("Failed deleting documents for %s: %s", source_id, e)

        # Delete jobs
        try:
            deleted_jobs = (
                session.query(KnowledgeProcessingJob)
                .filter(KnowledgeProcessingJob.source_id == source_id)
                .delete()
            )
            session.commit()
            logger.info("Deleted %s jobs for source %s", deleted_jobs, source_id)
        except Exception as e:
            session.rollback()
            logger.warning("Failed deleting jobs for %s: %s", source_id, e)

        # Delete blobs (original + thumbnails) if known
        try:
            if src and src.storage_path:
                # original
                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(storage.delete_blob(src.storage_path))
                    # thumbnails
                    if getattr(src, "thumbnail_small_path", None):
                        loop.run_until_complete(storage.delete_blob(src.thumbnail_small_path))
                    if getattr(src, "thumbnail_medium_path", None):
                        loop.run_until_complete(storage.delete_blob(src.thumbnail_medium_path))
                finally:
                    try:
                        loop.run_until_complete(storage.close())
                    except Exception:
                        pass
                    loop.close()
        except Exception as e:
            logger.warning("Failed deleting blobs for %s: %s", source_id, e)

        # Finally, delete the source record
        try:
            if src:
                session.delete(src)
                session.commit()
                logger.info("Deleted OrganisationSource %s", source_id)
        except Exception as e:
            session.rollback()
            logger.warning("Failed deleting OrganisationSource %s: %s", source_id, e)

    finally:
        try:
            session.close()
        except Exception:
            pass

