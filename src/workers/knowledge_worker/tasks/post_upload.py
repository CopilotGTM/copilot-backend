from __future__ import annotations

"""Post-upload processing tasks: checksum and thumbnail generation (fast, fan-out).
"""

import asyncio

from src.workers.knowledge_worker.celery_app import app
from src.infra.utils.logger import get_logger
from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
)
from src.domain.models.organisation_tables.job_models import (
    KnowledgeProcessingJob,
    JobStatus,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate
from src.infra.media.thumbnail_generator import (
    generate_thumbnails,
    generate_thumbnail_sas_urls,
)

logger = get_logger("knowledge_post_upload_task")


def _get_status_str(status_enum):
    """Safely get the string value of a status enum."""
    if status_enum is None:
        return None
    return getattr(status_enum, "value", str(status_enum))


def _assign_generated_thumbnails(source: OrganisationSource, created: dict[str, str]):
    if not created:
        return
    if "thumb_small" in created:
        source.thumbnail_small_path = created["thumb_small"]
    if "thumb_medium" in created:
        source.thumbnail_medium_path = created["thumb_medium"]


@app.task(
    name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3
)
def post_upload_process(self, organisation_id: str, source_id: str):
    """Post-upload processing: checksum and thumbnail generation."""
    logger.info("post_upload_process started")
    logger.info("org=%s source=%s", organisation_id, source_id)

    # Getting session and storage
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        source: OrganisationSource | None = session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Source %s not found", source_id)
            return

        if source.status == OrganisationSourceStatus.PENDING_UPLOAD:
            logger.warning(
                "Source %s still pending upload; skipping processing run", source_id
            )
            return

        job: KnowledgeProcessingJob | None = (
            session.query(KnowledgeProcessingJob)
            .filter_by(source_id=source_id, job_type="post_upload_process")
            .first()
        )

        if not job:
            return

        # Mark job as in progress
        job.status = JobStatus.IN_PROGRESS
        session.add(job)

        # Compute and persist checksum (streamed)
        # try:
        #     if source.storage_path:
        #         checksum = loop.run_until_complete(
        #             storage.compute_checksum(source.storage_path)
        #         )
        #         source.checksum = checksum
        # except Exception:
        #     logger.exception("Failed to compute checksum for source %s", source_id)

        # Thumbnail generation using shared service
        try:
            created = loop.run_until_complete(
                generate_thumbnails(
                    storage=storage,
                    original_blob_path=source.storage_path or "",
                    mimetype=source.mimetype,
                )
            )
            _assign_generated_thumbnails(source, created)

            job.status = JobStatus.COMPLETED
            session.add(job)
            source.status = OrganisationSourceStatus.EXTRACTING
            session.merge(source)
            session.commit()

            # Generate SAS URLs for newly created thumbnails (if present)
            thumbnail_small_url, thumbnail_medium_url = loop.run_until_complete(
                generate_thumbnail_sas_urls(
                    storage=storage,
                    small_path=source.thumbnail_small_path,
                    medium_path=source.thumbnail_medium_path,
                    minutes=120,
                )
            )

            publish_event = KnowledgeSourceEventUpdate(
                source_id=str(source.source_id),
                status=_get_status_str(OrganisationSourceStatus.EXTRACTING),
                updated_at=source.updated_at.isoformat() if source.updated_at else None,
                error_message=source.error_message,
                progress=None,
                thumbnail_medium_url=thumbnail_medium_url,
                thumbnail_small_url=thumbnail_small_url,
            )

            publish_source_status(
                organisation_id,
                publish_event
            )

            app.send_task(
                "knowledge.document_extraction",
                args=[organisation_id, source_id],
                queue="knowledge_extract",
            )
        except self.MaxRetriesExceededError:
            logger.warning(
                "Max retries exceeded for thumbnail generation source=%s", source_id
            )
        except Exception as e:
            logger.warning(
                "Thumbnail generation failed for source %s: %s", source_id, e
            )
        logger.info("post_upload_process finished source=%s", source_id)
    finally:
        try:
            session.close()
        except Exception:
            pass
        # Close async Azure clients (run in event loop)
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
