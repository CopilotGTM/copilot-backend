from __future__ import annotations

import asyncio
import contextlib

from src.api.dependencies.organisation_dependencies import acquire_org_session
from src.api.schemas.upload_models import AttachmentEventUpdate
from src.domain.models.organisation_tables.chat_message_models import (
    Attachment,
    AttachmentStatus,
)
from src.infra.config.configs import thumbnail_config
from src.infra.media.thumbnail_generator import generate_thumbnails
from src.infra.messaging.attachment_status_publish import publish_attachment_status
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.workers.chat_worker.celery_app import app

logger = get_logger("attachments_post_upload_task")


def _assign_generated_thumbnails(attachment: Attachment, created: dict[str, str]):
    if not created:
        return
    if "thumb_small" in created:
        attachment.thumbnail_small_path = created["thumb_small"]
    if "thumb_medium" in created:
        attachment.thumbnail_medium_path = created["thumb_medium"]


def _attachment_already_processed(attachment: Attachment | None) -> bool:
    """Return True if the attachment is already fully processed.

    Consider an attachment processed if status is COMPLETED/FAILED and a checksum exists.
    """
    if not attachment:
        return False
    return attachment.status in {
        AttachmentStatus.COMPLETED,
        AttachmentStatus.FAILED,
    } and bool(attachment.checksum)


def _compute_and_persist_checksum(
    loop: asyncio.AbstractEventLoop,
    storage: AzureOrganisationBlobStorage,
    attachment: Attachment,
    session,
) -> None:
    """Compute checksum if missing and persist it; swallow and log errors."""
    try:
        if attachment.storage_path and not attachment.checksum:
            checksum = loop.run_until_complete(storage.compute_checksum(attachment.storage_path))
            attachment.checksum = checksum
            session.merge(attachment)
            session.commit()
    except Exception:
        logger.exception("Checksum computation failed for attachment %s", attachment.attachment_id)


def _update_status_and_commit(
    session,
    attachment: Attachment,
    status: AttachmentStatus,
    error_message: str | None = None,
) -> None:
    """Update status and optional error message, then commit."""
    attachment.status = status
    if error_message:
        attachment.processing_error = error_message[:500]
    session.merge(attachment)
    session.commit()


def _generate_thumbnail_sas_url(
    loop: asyncio.AbstractEventLoop,
    storage: AzureOrganisationBlobStorage,
    path: str | None,
    minutes: int = 120,
) -> str | None:
    """Generate a read SAS URL for a thumbnail path; return None on failure or missing path."""
    if not path:
        return None
    try:
        return loop.run_until_complete(storage.generate_read_sas(path, minutes=minutes))
    except Exception:
        logger.debug("Thumbnail SAS generation failed for path=%s", path, exc_info=True)
        return None


def _publish_status_event(
    organisation_id: str,
    attachment: Attachment,
    status: AttachmentStatus,
    thumbnail_small_url: str | None,
) -> None:
    """Publish attachment status update event."""
    event_to_publish = AttachmentEventUpdate(
        attachment_id=str(attachment.attachment_id),
        status=status,
        updated_at=attachment.updated_at.isoformat() if attachment.updated_at else None,
        error_message=attachment.processing_error,
        thumbnail_small_url=thumbnail_small_url,
    )
    publish_attachment_status(organisation_id, event_to_publish)


@app.task(name="attachments.post_upload", bind=True, acks_late=True, max_retries=3)
def post_upload(self, organisation_id: str, attachment_id: str):
    """Post-upload processing: checksum and thumbnail generation.

    Uses a dedicated event loop (mirrors knowledge worker) to avoid repeated loop creation issues.
    """
    logger.info(
        "Attachment post-upload task started org=%s attachment=%s",
        organisation_id,
        attachment_id,
    )
    session_gen = acquire_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)  # type: ignore[arg-type]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    attachment: Attachment | None = None
    try:
        attachment = session.get(Attachment, attachment_id)
        if not attachment:
            logger.warning("Attachment %s not found for processing", attachment_id)
            return

        if _attachment_already_processed(attachment):
            logger.info("Attachment %s already processed", attachment_id)
            return

        # Compute checksum if missing
        _compute_and_persist_checksum(loop, storage, attachment, session)

        # Thumbnails via shared service (guard using config to avoid unnecessary work)
        try:
            mt = (attachment.mimetype or "").lower()
            if thumbnail_config.is_supported(mt):
                created = loop.run_until_complete(
                    generate_thumbnails(
                        storage=storage,
                        original_blob_path=attachment.storage_path,
                        mimetype=attachment.mimetype,
                    )
                )
                _assign_generated_thumbnails(attachment, created)
        except Exception as e:
            logger.exception(
                "Thumbnail generation failed for attachment %s",
                attachment.attachment_id,
            )
            attachment.status = AttachmentStatus.FAILED
            attachment.processing_error = f"thumbnail_failed: {e}"[:500]
        finally:
            session.merge(attachment)
            session.commit()

        # Finalize status and persist
        _update_status_and_commit(session, attachment, AttachmentStatus.COMPLETED)

        # Publish completion event with optional SAS
        thumbnail_small_url = _generate_thumbnail_sas_url(
            loop, storage, attachment.thumbnail_small_path, minutes=120
        )
        _publish_status_event(
            organisation_id, attachment, AttachmentStatus.COMPLETED, thumbnail_small_url
        )
        logger.info("Attachment post-upload task completed attachment=%s", attachment_id)
    except Exception as e:
        logger.error(
            "Attachment post-upload failed org=%s attachment=%s error=%s",
            organisation_id,
            attachment_id,
            e,
        )
        try:
            if attachment is not None:
                _update_status_and_commit(
                    session,
                    attachment,
                    AttachmentStatus.FAILED,
                    error_message=f"post_upload_failed: {e}",
                )
                _publish_status_event(organisation_id, attachment, AttachmentStatus.FAILED, None)
        except Exception:
            logger.debug(
                "Failed to persist/publish FAILED state for attachment %s",
                attachment_id,
                exc_info=True,
            )
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()
        try:
            loop.run_until_complete(storage.close())
        except Exception:
            pass
        finally:
            loop.close()
