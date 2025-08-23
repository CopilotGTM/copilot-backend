from __future__ import annotations

"""Post-upload processing tasks: checksum and thumbnail generation (fast, fan-out).
"""

from io import BytesIO
from typing import Optional, Tuple
import hashlib
from uuid import UUID
import asyncio

from PIL import Image, UnidentifiedImageError
import fitz


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
from src.infra.messaging.knowledge_source_status_publish import publish_source_status
from src.api.schemas.knowledge_upload_models import KnowledgeSourceEventUpdate

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


def _generate_thumbnail_from_bytes(
    storage: AzureOrganisationBlobStorage,
    source: OrganisationSource,
    data: bytes,
    loop: asyncio.AbstractEventLoop,
):
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
                    blob_client = blob_service.get_blob_client(
                        storage.container, thumb_path
                    )
                    await blob_client.upload_blob(
                        out.read(),
                        overwrite=True,
                        content_settings=ContentSettings(content_type="image/jpeg"),
                    )

                loop.run_until_complete(_upload())
                return thumb_path

            try:
                small_path = make_and_upload(128, "thumb_small")
                medium_path = make_and_upload(512, "thumb_medium")
                source.thumbnail_small_path = small_path
                source.thumbnail_medium_path = medium_path
            except Exception:
                logger.exception(
                    "Failed to upload thumbnail variants for source %s",
                    source.source_id,
                )
    except UnidentifiedImageError:
        logger.debug("Source %s is not an image; skipping thumbnail.", source.source_id)
    except Exception as e:
        logger.warning(
            "Thumbnail generation failed for source %s: %s", source.source_id, e
        )


def _placeholder_bytes(label: str) -> bytes:
    from PIL import Image as _Image, ImageDraw, ImageFont

    img = _Image.new("RGB", (512, 512), (240, 240, 240))
    d = ImageDraw.Draw(img)
    text = label.upper()
    try:
        font = ImageFont.load_default()  # type: ignore
    except Exception:  # pragma: no cover
        font = None
    tw, th = d.textlength(text, font=font), 12
    d.text(((512 - tw) / 2, (512 - th) / 2), text, fill=(60, 60, 60), font=font)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _pdf_preview_bytes(self, pdf_bytes: bytes, source_id: str) -> bytes:
    """Return representative PNG bytes for first page of a PDF.

    Strategy:
      1. If file very small (< 800 bytes), log a warning.
      2. Try rendering first page at 2x scale via PyMuPDF.
      3. On any render failures, return a placeholder image.
    """
    size = len(pdf_bytes or b"")
    logger.debug("PDF diagnostics source=%s size_bytes=%s", source_id, size)
    # Guard for incomplete upload (sometimes blob not fully committed yet)
    if size < 800:
        logger.warning("PDF size %s bytes likely incomplete.", size)
    if fitz is None:  # type: ignore
        logger.info("PyMuPDF not available; using placeholder for PDF %s", source_id)
        return _placeholder_bytes("pdf")
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:  # type: ignore[attr-defined]
            if doc.page_count == 0:
                logger.info("PDF has 0 pages; using placeholder for %s", source_id)
                return _placeholder_bytes("pdf")
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # type: ignore[attr-defined]
            png_bytes = pix.tobytes("png")
            if len(png_bytes) < 200:
                logger.warning(
                    "PDF render produced very small image source=%s", source_id
                )
            return png_bytes
    except Exception as e:
        logger.warning(
            "PDF render failed source=%s error=%s; using placeholder", source_id, e
        )
        return _placeholder_bytes("pdf")


@celery.task(
    name="knowledge.post_upload_process", bind=True, acks_late=True, max_retries=3
)
def post_upload_process(self, organisation_id: str, source_id: str):
    """Post-upload processing: checksum and thumbnail generation."""
    logger.info("post_upload_process started")
    logger.info("org=%s source=%s", organisation_id, source_id)

    # Getting session and storage
    session_gen = get_org_session(organisation_id)
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

        # Persist computed checksum into explicit column
        # try:
        #     source.checksum = checksum
        # except Exception:
        #     logger.exception("Failed to persist checksum onto source %s", source_id)

        # Thumbnail if image (folder-based thumbnails/<variant>.jpg)
        mimetype = (source.mimetype or "").lower()
        maybe_image_bytes: Optional[bytes] = None

        try:
            match mimetype:
                case m if m.startswith("image/"):
                    maybe_image_bytes = loop.run_until_complete(
                        _download_blob(storage, source.storage_path or "")
                    )
                case "application/pdf":
                    pdf_bytes = loop.run_until_complete(
                        _download_blob(storage, source.storage_path or "")
                    )
                    maybe_image_bytes = _pdf_preview_bytes(
                        self, pdf_bytes, str(source.source_id)
                    )
                case _:
                    maybe_image_bytes = None

            if maybe_image_bytes:
                _generate_thumbnail_from_bytes(storage, source, maybe_image_bytes, loop)

            job.status = JobStatus.COMPLETED
            session.add(job)
            source.status = OrganisationSourceStatus.EXTRACTING
            session.add(source)

            # Generate SAS URLs for newly created thumbnails (if present)
            thumbnail_small_url = None
            thumbnail_medium_url = None
            try:
                if source.thumbnail_small_path:
                    thumbnail_small_url = loop.run_until_complete(
                        storage.generate_read_sas(source.thumbnail_small_path, minutes=120)
                    )
                if source.thumbnail_medium_path:
                    thumbnail_medium_url = loop.run_until_complete(
                        storage.generate_read_sas(source.thumbnail_medium_path, minutes=120)
                    )
            except Exception:
                logger.debug(
                    "Thumbnail SAS generation failed for source %s", source.source_id, exc_info=True
                )

            publish_event = KnowledgeSourceEventUpdate(
                source_id=str(source.source_id),
                status=source.status.value,
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

            celery.send_task(
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
