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
from src.domain.models.organisation_tables.chat_message_models import Attachment
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

logger = get_logger("image_processing_task")

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


def _stream_checksum_and_optionally_buffer_image(storage: AzureOrganisationBlobStorage, attachment) -> tuple[str, Optional[bytes]]:
    async def _runner() -> tuple[str, Optional[bytes]]:
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

    return asyncio.run(_runner())


def _generate_thumbnails_from_bytes(storage: AzureOrganisationBlobStorage, attachment, data: bytes):
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
                # Use updated helper (will place inside thumbnails/ folder)
                thumb_path = storage.thumbnail_blob_path(attachment.storage_path, suffix)
                # Use explicit content settings for JPEG
                from azure.storage.blob import ContentSettings

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

            # For images or PDFs, generate thumbnails first for faster UI update
            mt = (attachment.mimetype or "").lower()
            if mt.startswith("image/") or mt == "application/pdf":
                try:
                    if maybe_image_bytes is None:
                        import asyncio as _asyncio
                        raw_bytes = _asyncio.run(_download_blob(storage, attachment.storage_path))
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
                        _generate_thumbnails_from_bytes(storage, attachment, maybe_image_bytes)
                        attachment.processing_status = "thumbnails_generated"
                        session.add(attachment)
                        session.commit()
                except Exception as e:
                    logger.exception("Thumbnail generation failed for attachment %s", attachment_id)
                    attachment.processing_status = "failed"
                    attachment.processing_error = f"thumbnail_failed: {e}"[:500]

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
