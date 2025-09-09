from __future__ import annotations
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


def _compute_and_persist_checksum(
    loop: asyncio.AbstractEventLoop,
    storage: AzureOrganisationBlobStorage,
    source: OrganisationSource,
    session,
):
    """Compute checksum early (if missing) and commit immediately.

    Doing this before thumbnail generation ensures we still retain a checksum
    even if later steps fail (mirrors chat worker approach)."""
    try:
        if source.storage_path and not source.checksum:
            checksum = loop.run_until_complete(
                storage.compute_checksum(source.storage_path)
            )
            source.checksum = checksum
            session.merge(source)
            session.commit()
    except Exception:
        logger.exception("Failed to compute checksum for source %s", source.source_id)


def _publish_status_event(
    organisation_id: str,
    source: OrganisationSource,
    status: OrganisationSourceStatus,
    thumbnail_small_url: str | None,
    thumbnail_medium_url: str | None,
):
    """Publish knowledge source status update event (unified helper)."""
    publish_event = KnowledgeSourceEventUpdate(
        source_id=str(source.source_id),
        status=_get_status_str(status),
        updated_at=source.updated_at.isoformat() if source.updated_at else None,
        error_message=source.error_message,
        progress=None,
        thumbnail_medium_url=thumbnail_medium_url,
        thumbnail_small_url=thumbnail_small_url,
    )
    publish_source_status(organisation_id, publish_event)


@app.task(
    name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3
)
def post_upload_process(self, organisation_id: str, source_id: str):
    """Post-upload processing: checksum and thumbnail generation."""
    logger.info("post_upload_process started")
    logger.info("org=%s source=%s", organisation_id, source_id)

    # Getting session and storage (sync DB session; async storage via local loop)
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Load source record
        source: OrganisationSource | None = session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Source %s not found", source_id)
            return

        # Skip if not in expected state
        if source.status == OrganisationSourceStatus.PENDING_UPLOAD:
            logger.warning(
                "Source %s still pending upload; skipping processing run", source_id
            )
            return

        # Locate job for this step
        job: KnowledgeProcessingJob | None = (
            session.query(KnowledgeProcessingJob)
            .filter_by(source_id=source_id, job_type="post_upload_process")
            .first()
        )

        if not job:
            logger.warning(
                "No post_upload_process job found for source %s; skipping", source_id
            )
            return

        # Mark job as in progress and persist early
        job.status = JobStatus.IN_PROGRESS
        session.add(job)
        session.commit()

        # Compute checksum early (mirrors chat worker style)
        _compute_and_persist_checksum(loop, storage, source, session)

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
            thumbnail_small_url = None
            thumbnail_medium_url = None
            if source.thumbnail_small_path or source.thumbnail_medium_path:
                thumbnail_small_url, thumbnail_medium_url = loop.run_until_complete(
                    generate_thumbnail_sas_urls(
                        storage=storage,
                        small_path=source.thumbnail_small_path,
                        medium_path=source.thumbnail_medium_path,
                        minutes=120,
                    )
                )

            _publish_status_event(
                organisation_id,
                source,
                OrganisationSourceStatus.EXTRACTING,
                thumbnail_small_url,
                thumbnail_medium_url,
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
            job.status = JobStatus.FAILED
            session.add(job)
            session.commit()
        except Exception as e:
            logger.warning(
                "Thumbnail generation failed for source %s: %s", source_id, e
            )
            job.status = JobStatus.FAILED
            session.add(job)
            session.commit()
        logger.info("post_upload_process finished source=%s", source_id)
    except Exception as e:
        logger.exception(
            "knowledge post_upload_process failed org=%s source=%s error=%s",
            organisation_id,
            source_id,
            e,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass
        try:
            session_gen.close()
        except Exception:
            pass
        # Close async Azure clients (run in event loop)
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
