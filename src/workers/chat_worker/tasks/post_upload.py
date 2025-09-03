from __future__ import annotations
"""Celery task for attachment image processing: checksum + optional thumbnails."""

from typing import Optional
import asyncio

from src.workers.chat_worker.celery_app import app
from src.infra.utils.logger import get_logger
from src.api.dependencies.organisation_dependencies import acquire_org_session
import os
from src.domain.models.organisation_tables.chat_message_models import Attachment, AttachmentStatus
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.messaging.attachment_status_publish import publish_attachment_status
from src.api.schemas.upload_models import AttachmentEventUpdate
from src.infra.media.thumbnail_generator import generate_thumbnails

logger = get_logger("attachments_post_upload_task")


@app.task(name="attachments.post_upload", bind=True, acks_late=True, max_retries=3)
def post_upload(self, organisation_id: str, attachment_id: str):
    """Post-upload processing: checksum + optional thumbnails.

    Uses a single dedicated event loop (mirrors knowledge_post_upload) to avoid
    'Event loop is closed' errors from repeated asyncio.run() calls.
    """
    logger.info("Attachment post-upload task started org=%s attachment=%s", organisation_id, attachment_id)
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)  # type: ignore[arg-type]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        attachment = session.get(Attachment, attachment_id)
        if not attachment:
            logger.warning("Attachment %s not found for processing", attachment_id)
            return
        if attachment.status in {AttachmentStatus.COMPLETED, AttachmentStatus.FAILED} and attachment.checksum:
            logger.info("Attachment %s already processed", attachment_id)
            return

        # Compute checksum (streamed)
        maybe_image_bytes: Optional[bytes] | None = None
        # try:
        #     if attachment.storage_path:
        #         checksum = loop.run_until_complete(
        #             storage.compute_checksum(attachment.storage_path)
        #         )
        #         attachment.checksum = checksum
        #         session.add(attachment)
        #         session.commit()
        # except Exception as e:
        #     logger.exception("Checksum computation failed for attachment %s", attachment_id)


        # Thumbnails for images / PDFs via shared service
        mt = (attachment.mimetype or "").lower()
        if mt.startswith("image/") or mt == "application/pdf":
            try:
                created = loop.run_until_complete(
                    generate_thumbnails(
                        storage=storage,
                        original_blob_path=attachment.storage_path,
                        mimetype=attachment.mimetype,
                    )
                )
                if "thumb_small" in created:
                    attachment.thumbnail_small_path = created["thumb_small"]
                if "thumb_medium" in created:
                    attachment.thumbnail_medium_path = created["thumb_medium"]
                attachment.status = AttachmentStatus.UPLOADED
                session.merge(attachment)
                session.commit()

            except Exception as e:
                logger.exception("Thumbnail generation failed for attachment %s", attachment_id)
                attachment.status = AttachmentStatus.FAILED
                attachment.processing_error = f"thumbnail_failed: {e}"[:500]
                session.merge(attachment)
                session.commit()

        # Finalize status
        attachment.status = AttachmentStatus.COMPLETED
        session.merge(attachment)
        session.commit()

        thumbnail_small_url = None
        try:
            if attachment.thumbnail_small_path:
                thumbnail_small_url = loop.run_until_complete(
                    storage.generate_read_sas(attachment.thumbnail_small_path, minutes=120)
                )
        except Exception:
            logger.debug(
                "Thumbnail SAS generation failed for attachment %s", attachment.attachment_id, exc_info=True
            )
        event_to_publish = AttachmentEventUpdate(
            attachment_id=str(attachment.attachment_id),
            status=AttachmentStatus.COMPLETED,
            updated_at=attachment.updated_at.isoformat() if getattr(attachment, "updated_at", None) else None,
            error_message=attachment.processing_error,
            thumbnail_small_url=thumbnail_small_url,
        )
        publish_attachment_status(organisation_id, event_to_publish)
        logger.info("Attachment post-upload task completed attachment=%s", attachment_id)
    finally:
        try:
            session.close()
        except Exception:
            pass
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
