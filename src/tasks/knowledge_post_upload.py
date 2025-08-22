from __future__ import annotations
"""Post-upload processing tasks: checksum and thumbnail generation (fast, fan-out).
"""

from io import BytesIO
from typing import Optional, Tuple
import hashlib
from uuid import UUID
import asyncio

from PIL import Image, UnidentifiedImageError

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
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

logger = get_logger("knowledge_post_upload_task")


async def _download_blob(storage: AzureOrganisationBlobStorage, path: str) -> bytes:
    blob_service = await storage._get_blob_service_client()
    blob_client = blob_service.get_blob_client(storage.container, path)
    stream = await blob_client.download_blob()
    return await stream.readall()


# def _stream_checksum_and_maybe_buffer_image(storage: AzureOrganisationBlobStorage, source: OrganisationSource) -> tuple[str, Optional[bytes]]:
#     async def _runner() -> tuple[str, Optional[bytes]]:
#         blob_service = await storage._get_blob_service_client()
#         blob_client = blob_service.get_blob_client(storage.container, source.storage_path)  # type: ignore[arg-type]
#         downloader = await blob_client.download_blob()
#         hasher = hashlib.sha256()
#         buffer: Optional[BytesIO] = BytesIO() if (source.mimetype or "").startswith("image/") else None
#         async for chunk in downloader.chunks():  # type: ignore[attr-defined]
#             hasher.update(chunk)
#             if buffer is not None:
#                 buffer.write(chunk)
#         if buffer is not None:
#             buffer.seek(0)
#             return hasher.hexdigest(), buffer.read()
#         return hasher.hexdigest(), None

#     return asyncio.run(_runner())


def _generate_thumbnail_from_bytes(storage: AzureOrganisationBlobStorage, source: OrganisationSource, data: bytes):
    """Create small and medium JPEG thumbnails and upload them.

    Small: 128px max dimension
    Medium: 512px max dimension
    """
    try:
        with Image.open(BytesIO(data)) as img:
            img = img.convert("RGB")

            def make_and_upload(size: int, suffix: str) -> str:
                thumb = img.copy()
                thumb.thumbnail((size, size))
                out = BytesIO()
                thumb.save(out, format="JPEG", quality=80, optimize=True)
                out.seek(0)
                base = source.storage_path or ""
                # Use storage helper to create deterministic thumbnail path
                thumb_path = storage.thumbnail_blob_path(base, suffix)

                async def _upload():
                    from azure.storage.blob import ContentSettings

                    blob_service = await storage._get_blob_service_client()
                    blob_client = blob_service.get_blob_client(storage.container, thumb_path)
                    await blob_client.upload_blob(
                        out.read(),
                        overwrite=True,
                        content_settings=ContentSettings(content_type="image/jpeg"),
                    )

                asyncio.run(_upload())
                return thumb_path

            try:
                small_path = make_and_upload(128, "thumb_small")
                medium_path = make_and_upload(512, "thumb_medium")
                source.thumbnail_small_path = small_path
                source.thumbnail_medium_path = medium_path
            except Exception:
                logger.exception("Failed to upload thumbnail variants for source %s", source.source_id)
    except UnidentifiedImageError:
        logger.debug("Source %s is not an image; skipping thumbnail.", source.source_id)
    except Exception as e:
        logger.warning("Thumbnail generation failed for source %s: %s", source.source_id, e)


@celery.task(name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3)
def post_upload_process(self, organisation_id: str, source_id: str):
    logger.info("post_upload_process started org=%s source=%s", organisation_id, source_id)
    session_gen = get_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)

    try:
        source: OrganisationSource | None = session.get(OrganisationSource, source_id)
        if not source:
            logger.warning("Source %s not found", source_id)
            return

        if source.status in {OrganisationSourceStatus.COMPLETED, OrganisationSourceStatus.FAILED}:
            logger.info("Source %s already in terminal state %s", source_id, source.status)
            return

        if source.status == OrganisationSourceStatus.PENDING_UPLOAD:
            logger.warning("Source %s still pending upload; skipping processing run", source_id)
            return

        job: KnowledgeProcessingJob | None = session.query(KnowledgeProcessingJob).filter_by(
            source_id=source_id,
            job_type='post_upload_process'
        ).first()

        if not job:
            return

        job.status = JobStatus.IN_PROGRESS
        session.add(job)

        # Download + checksum (+ inline buffer if image)
        # try:
        #     checksum, maybe_image_bytes = _stream_checksum_and_maybe_buffer_image(storage, source)
        # except Exception as e:
        #     logger.exception("Download/checksum failed for source %s", source_id)
        #     source.status = OrganisationSourceStatus.FAILED
        #     source.error_message = f"download_failed: {e}"[:500]
        #     if job:
        #         job.status = JobStatus.FAILED
        #         job.error_message = source.error_message
        #         session.add(job)
        #     session.add(source)
        #     session.commit()
        #     return

        # Thumbnail if image
        maybe_image_bytes = None
        if (source.mimetype or "").startswith("image/"):
            try:
                maybe_image_bytes = asyncio.run(_download_blob(storage, source.storage_path or ""))
                if maybe_image_bytes:
                    _generate_thumbnail_from_bytes(storage, source, maybe_image_bytes)
            except Exception as e:
                logger.warning("Thumbnail generation failed for source %s: %s", source_id, e)

        # Persist computed checksum into explicit column
        # try:
        #     source.checksum = checksum
        # except Exception:
        #     logger.exception("Failed to persist checksum onto source %s", source_id)

        # Mark job completed
        if job:
            job.status = JobStatus.COMPLETED
            session.add(job)
        session.add(source)
        session.commit()
        # Enqueue extract phase
        try:
            celery.send_task(
                "knowledge.document_extraction",
                args=[organisation_id, source_id],
                queue="knowledge_extract",
            )
        except Exception as e:
            logger.error("Failed to enqueue extract for %s: %s", source_id, e)
        logger.info("post_upload_process finished source=%s", source_id)
    finally:
        try:
            session.close()
        except Exception:
            pass
        # Close async Azure clients (run in event loop)
        try:
            asyncio.run(storage.close())
        except Exception:
            pass
