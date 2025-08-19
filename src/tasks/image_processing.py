from __future__ import annotations
"""Celery task for attachment image processing: checksum + optional thumbnails."""

from io import BytesIO
from typing import Tuple, Optional
import hashlib
from PIL import Image, UnidentifiedImageError

from src.celery_app import celery
from src.infra.utils.logger import get_logger
from src.infra.utils.database import get_org_session
import os
from src.domain.models.organisation_tables.chat_message_models import Attachment
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

logger = get_logger("image_processing_task")

THUMB_SIZES: list[Tuple[int, int]] = [(128, 128), (512, 512)]


async def _download_blob(storage: AzureOrganisationBlobStorage, path: str) -> bytes:
    blob_service = await storage._get_blob_service_client()  # accessing internal method intentionally
    blob_client = blob_service.get_blob_client("attachments", path)
    stream = await blob_client.download_blob()
    return await stream.readall()


async def _upload_blob(storage: AzureOrganisationBlobStorage, path: str, data: bytes, content_type: str):
    blob_service = await storage._get_blob_service_client()
    blob_client = blob_service.get_blob_client("attachments", path)
    await blob_client.upload_blob(data, overwrite=True, content_settings=None)


def _derive_thumb_path(original_path: str, suffix: str) -> str:
    if "." in original_path.rsplit("/", 1)[-1]:
        base, ext = original_path.rsplit(".", 1)
        return f"{base}_{suffix}.{ext.lower()}"
    return f"{original_path}_{suffix}"


def _stream_checksum_and_optionally_buffer_image(storage: AzureOrganisationBlobStorage, attachment) -> tuple[str, Optional[bytes]]:
    import asyncio

    async def _runner() -> tuple[str, Optional[bytes]]:
        blob_service = await storage._get_blob_service_client()  # internal access
        blob_client = blob_service.get_blob_client("attachments", attachment.storage_path)
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

    return asyncio.run(_runner())


def _generate_thumbnails_from_bytes(storage: AzureOrganisationBlobStorage, attachment, data: bytes):
    import asyncio
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
                thumb_path = _derive_thumb_path(attachment.storage_path, suffix)
                asyncio.run(_upload_blob(storage, thumb_path, out.read(), "image/jpeg"))
                if w <= 128:
                    attachment.thumbnail_small_path = thumb_path
                else:
                    attachment.thumbnail_medium_path = thumb_path
    except UnidentifiedImageError:
        logger.warning("Attachment %s is not a supported image for thumbnails", attachment.attachment_id)
    except Exception as e:
        logger.exception("Thumbnail generation failed for attachment %s", attachment.attachment_id)
        raise


@celery.task(name="attachments.process_image", bind=True, acks_late=True, max_retries=3)
def process_image(self, organisation_id: str, attachment_id: str):
    """Process attachment: compute checksum and (if image) generate thumbnails."""
    logger.info("Image processing task started org=%s attachment=%s", organisation_id, attachment_id)
    session_gen = get_org_session(organisation_id)
    session = next(session_gen)
    storage = AzureOrganisationBlobStorage(organisation_id=organisation_id)  # type: ignore[arg-type]
    try:
        try:
            attachment = session.get(Attachment, attachment_id)
            if not attachment:
                logger.warning("Attachment %s not found for processing", attachment_id)
                return
            if attachment.processing_status in {"thumbnails_generated", "processed"} and attachment.checksum:
                logger.info("Attachment %s already processed", attachment_id)
                return

            attachment.processing_status = "processing"
            session.add(attachment)
            session.commit()

            try:
                checksum, maybe_image_bytes = _stream_checksum_and_optionally_buffer_image(storage, attachment)
            except Exception as e:
                logger.exception("Download/checksum failed for attachment %s", attachment_id)
                attachment.processing_status = "failed"
                attachment.processing_error = f"download_failed: {e}"[:500]
                session.add(attachment)
                session.commit()
                raise

            # For images, generate and commit thumbnails first for a faster UI response
            if (attachment.mimetype or "").startswith("image/"):
                try:
                    if maybe_image_bytes is None: # Should not happen with current logic, but as a safeguard
                        import asyncio as _asyncio
                        maybe_image_bytes = _asyncio.run(_download_blob(storage, attachment.storage_path))
                    _generate_thumbnails_from_bytes(storage, attachment, maybe_image_bytes)
                    attachment.processing_status = "thumbnails_generated"
                    session.add(attachment)
                    session.commit()  # Early commit for faster UI update
                except Exception as e:
                    logger.exception("Thumbnail generation failed for attachment %s", attachment_id)
                    attachment.processing_status = "failed"
                    attachment.processing_error = f"thumbnail_failed: {e}"[:500]
                    # The checksum will still be saved in the next step

            # Save the checksum and set the final status
            attachment.checksum = checksum
            if attachment.processing_status == "thumbnails_generated":
                attachment.processing_status = "processed"  # All steps are now complete
            elif attachment.processing_status == "processing":  # Non-image case
                attachment.processing_status = "processed"
            
            session.add(attachment)
            session.commit()
            logger.info("Image processing task completed attachment=%s", attachment_id)
        except Exception:
            raise
    finally:
        try:
            session.close()
        except Exception:
            pass
