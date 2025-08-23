from __future__ import annotations
"""Celery task for attachment image processing: checksum + optional thumbnails."""

from io import BytesIO
from typing import Tuple, Optional
import hashlib
from PIL import Image, UnidentifiedImageError
try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore
import asyncio

from src.celery_app import celery
from src.infra.utils.logger import get_logger
from src.infra.utils.database import get_org_session
import os
from src.domain.models.organisation_tables.chat_message_models import Attachment, AttachmentStatus
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.messaging.redis_pubsub import publish_attachment_status_async

logger = get_logger("attachments_post_upload_task")

THUMB_SIZES: list[Tuple[int, int]] = [(128, 128), (512, 512)]


async def _download_blob(storage: AzureOrganisationBlobStorage, path: str) -> bytes:
    blob_service = await storage._get_blob_service_client()  # accessing internal method intentionally
    blob_client = blob_service.get_blob_client(storage.container, path)
    stream = await blob_client.download_blob()
    return await stream.readall()


async def _upload_blob(storage: AzureOrganisationBlobStorage, path: str, data: bytes, content_type: str):
    from azure.storage.blob import ContentSettings

    blob_service = await storage._get_blob_service_client()
    blob_client = blob_service.get_blob_client(storage.container, path)
    await blob_client.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type))


def _derive_thumb_path(original_path: str, suffix: str) -> str:
    # New folder-based variant: <dir>/thumbnails/<suffix>.jpg
    if "/" in original_path:
        dir_part, _ = original_path.rsplit("/", 1)
    else:
        dir_part = ""
    base_dir = f"{dir_part}/thumbnails" if dir_part else "thumbnails"
    return f"{base_dir}/{suffix}.jpg"


async def _stream_checksum_and_optionally_buffer_image_async(storage: AzureOrganisationBlobStorage, attachment) -> tuple[str, Optional[bytes]]:
    blob_service = await storage._get_blob_service_client()  # internal access
    blob_client = blob_service.get_blob_client(storage.container, attachment.storage_path)
    downloader = await blob_client.download_blob()
    hasher = hashlib.sha256()
    image_bytes: Optional[BytesIO] = BytesIO() if (attachment.mimetype or "").startswith("image/") else None
    async for chunk in downloader.chunks():  # type: ignore[attr-defined]
        hasher.update(chunk)
        if image_bytes is not None:
            image_bytes.write(chunk)
    if image_bytes is not None:
        image_bytes.seek(0)
        return hasher.hexdigest(), image_bytes.read()
    return hasher.hexdigest(), None


async def _generate_thumbnails_from_bytes_async(storage: AzureOrganisationBlobStorage, attachment, data: bytes):
    try:
        with Image.open(BytesIO(data)) as img:
            img = img.convert("RGB")
            for w, h in THUMB_SIZES:
                thumb = img.copy()
                thumb.thumbnail((w, h))
                out = BytesIO()
                thumb.save(out, format="JPEG", quality=85, optimize=True)
                out.seek(0)
                suffix = f"thumb_{w}x{h}"
                thumb_path = storage.thumbnail_blob_path(attachment.storage_path, suffix)
                await _upload_blob(storage, thumb_path, out.read(), "image/jpeg")
                if w <= 128:
                    attachment.thumbnail_small_path = thumb_path
                else:
                    attachment.thumbnail_medium_path = thumb_path
    except UnidentifiedImageError:
        logger.warning("Attachment %s is not a supported image for thumbnails", attachment.attachment_id)
    except Exception:
        logger.exception("Thumbnail generation failed for attachment %s", attachment.attachment_id)
        raise


@celery.task(name="attachments.post_upload", bind=True, acks_late=True, max_retries=3)
def post_upload(self, organisation_id: str, attachment_id: str):
    """Post-upload processing: checksum + optional thumbnails.

    Uses a single dedicated event loop (mirrors knowledge_post_upload) to avoid
    'Event loop is closed' errors from repeated asyncio.run() calls.
    """
    logger.info("Attachment post-upload task started org=%s attachment=%s", organisation_id, attachment_id)
    session_gen = get_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)  # type: ignore[arg-type]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _async_main():
        attachment = session.get(Attachment, attachment_id)
        if not attachment:
            logger.warning("Attachment %s not found for processing", attachment_id)
            return
        if attachment.status in {AttachmentStatus.COMPLETED, AttachmentStatus.FAILED} and attachment.checksum:
            logger.info("Attachment %s already processed", attachment_id)
            return

        # Transition to processing
        attachment.status = AttachmentStatus.PROCESSING
        session.add(attachment)
        session.commit()
        try:
            await publish_attachment_status_async(organisation_id, attachment_id, attachment.status.value, attachment.updated_at)
        except Exception:
            pass
        # Stream checksum (and maybe buffer image) -- DISABLED temporarily (checksum deferred)
        # checksum: str = ""
        maybe_image_bytes: Optional[bytes] | None = None
        # try:
        #     checksum, maybe_image_bytes = await _stream_checksum_and_optionally_buffer_image_async(storage, attachment)
        # except Exception as e:
        #     logger.exception("Download/checksum failed for attachment %s", attachment_id)
        #     attachment.status = AttachmentStatus.FAILED
        #     attachment.processing_error = f"download_failed: {e}"[:500]
        #     session.add(attachment)
        #     session.commit()
        #     try:
        #         await publish_attachment_status_async(organisation_id, attachment_id, attachment.status.value, attachment.updated_at, attachment.processing_error)
        #     except Exception:
        #         pass
        #     return

        # Thumbnails for images / PDFs
        mt = (attachment.mimetype or "").lower()
        if mt.startswith("image/") or mt == "application/pdf":
            try:
                if maybe_image_bytes is None:
                    raw_bytes = await _download_blob(storage, attachment.storage_path)
                    if mt == "application/pdf" and fitz is not None:
                        try:
                            with fitz.open(stream=raw_bytes, filetype="pdf") as doc:  # type: ignore[attr-defined]
                                if doc.page_count > 0:
                                    page = doc.load_page(0)
                                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # type: ignore[attr-defined]
                                    maybe_image_bytes = pix.tobytes("png")
                        except Exception as pe:  # pragma: no cover
                            logger.warning("PDF render failed for attachment %s: %s", attachment_id, pe)
                    else:
                        maybe_image_bytes = raw_bytes
                if maybe_image_bytes:
                    await _generate_thumbnails_from_bytes_async(storage, attachment, maybe_image_bytes)
                    attachment.status = AttachmentStatus.UPLOADED
                    session.add(attachment)
                    session.commit()
                    try:
                        await publish_attachment_status_async(organisation_id, attachment_id, attachment.status.value, attachment.updated_at)
                    except Exception:
                        pass
            except Exception as e:
                logger.exception("Thumbnail generation failed for attachment %s", attachment_id)
                attachment.status = AttachmentStatus.FAILED
                attachment.processing_error = f"thumbnail_failed: {e}"[:500]
                session.add(attachment)
                session.commit()
                try:
                    await publish_attachment_status_async(organisation_id, attachment_id, attachment.status.value, attachment.updated_at, attachment.processing_error)
                except Exception:
                    pass

        # Finalize status (checksum skipped)
        # attachment.checksum = checksum
        if attachment.status in {AttachmentStatus.UPLOADED, AttachmentStatus.PROCESSING}:
            attachment.status = AttachmentStatus.COMPLETED
        session.add(attachment)
        session.commit()
        try:
            await publish_attachment_status_async(organisation_id, attachment_id, attachment.status.value, attachment.updated_at)
        except Exception:
            pass
        logger.info("Attachment post-upload task completed attachment=%s", attachment_id)

    try:
        loop.run_until_complete(_async_main())
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
