from __future__ import annotations

"""Reusable thumbnail generation service for blobs (images and PDFs).

Designed for use from Celery workers (chat and knowledge). It performs:
 - Download of the original blob
 - Optional PDF first-page rendering to an image
 - Generation and upload of JPEG thumbnails in a deterministic thumbnails/ folder

This module is intentionally infra-level so both domains can depend on it.
"""

from io import BytesIO
from typing import Optional, Dict, Tuple

from PIL import Image, UnidentifiedImageError

try:  # Optional dependency
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None  # type: ignore

from src.infra.utils.logger import get_logger
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.config.configs import thumbnail_config

logger = get_logger("thumbnail_service")


THUMB_VARIANTS: list[Tuple[str, int]] = [
    ("thumb_small", 128),
    ("thumb_medium", 512),
]



def _pdf_page_preview_bytes(pdf_bytes: bytes, label: str) -> Optional[bytes]:
    """Render the first page of a PDF to PNG bytes, or None if not possible."""
    if not pdf_bytes:
        return None
    if fitz is None:
        logger.info("PyMuPDF not available; skipping PDF preview for %s", label)
        return None
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:  # type: ignore[attr-defined]
            if doc.page_count == 0:
                return None
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # type: ignore[attr-defined]
            return pix.tobytes("png")
    except Exception as e:
        logger.warning("PDF preview render failed (%s): %s", label, e)
        return None


def _placeholder_bytes(label: str) -> Optional[bytes]:
    """Create a simple placeholder PNG with a centered label (e.g., 'txt')."""
    try:
        from PIL import Image as _Image, ImageDraw, ImageFont
        img = _Image.new("RGB", (512, 512), (240, 240, 240))
        d = ImageDraw.Draw(img)
        text = label.upper()
        try:  # default font; platform-safe
            font = ImageFont.load_default()  # type: ignore
        except Exception:
            font = None
        # crude centering
        tw = d.textlength(text, font=font)
        th = 12
        d.text(((512 - tw) / 2, (512 - th) / 2), text, fill=(60, 60, 60), font=font)
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.debug("Failed to create placeholder image for label=%s", label, exc_info=True)
        return None


async def generate_thumbnails(
    *,
    storage: AzureOrganisationBlobStorage,
    original_blob_path: str,
    mimetype: Optional[str],
    variants: list[Tuple[str, int]] = THUMB_VARIANTS,
) -> Dict[str, str]:
    """Generate and upload thumbnails for the given blob.

    - For images (image/*): resizes directly.
    - For PDFs: renders the first page and resizes.

    Returns a mapping of variant name -> thumbnail blob path for successfully
    created thumbnails. Missing keys indicate generation wasn't applicable.
    """
    mt = (mimetype or "").lower()
    if not original_blob_path:
        return {}

    # Download original bytes
    try:
        raw_bytes = await storage.get_blob_content(original_blob_path)
    except Exception as e:
        logger.exception("Failed to download blob for thumbnails: %s", e)
        return {}

    image_bytes: Optional[bytes] = None
    match mt:
        case m if m.startswith("image/"):
            image_bytes = raw_bytes
        case "application/pdf":
            image_bytes = _pdf_page_preview_bytes(raw_bytes, original_blob_path)
            if not image_bytes and thumbnail_config.ENABLE_PDF_PLACEHOLDER_FALLBACK:
                image_bytes = _placeholder_bytes("pdf")
        case "text/plain":
            if thumbnail_config.ENABLE_TEXT_PLACEHOLDERS:
                image_bytes = _placeholder_bytes("txt")
        case _:
            return {}

    if not image_bytes:
        return {}

    # Open and generate variants
    created: Dict[str, str] = {}
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            for variant, size in variants:
                thumb = img.copy()
                thumb.thumbnail((size, size))
                out = BytesIO()
                thumb.save(out, format="JPEG", quality=85, optimize=True)
                out.seek(0)
                thumb_path = storage.thumbnail_blob_path(original_blob_path, variant)
                await storage.upload_bytes(thumb_path, out.read(), content_type="image/jpeg")
                created[variant] = thumb_path
    except UnidentifiedImageError:
        logger.debug("Not a supported image for thumbnailing: %s", original_blob_path)
    except Exception:
        logger.exception("Thumbnail generation failed for %s", original_blob_path)

    return created


async def generate_thumbnail_sas_urls(
    *,
    storage: AzureOrganisationBlobStorage,
    small_path: Optional[str],
    medium_path: Optional[str],
    minutes: int = 120,
) -> tuple[Optional[str], Optional[str]]:
    small_url = None
    medium_url = None
    try:
        if small_path:
            small_url = await storage.generate_read_sas(small_path, minutes=minutes)
        if medium_path:
            medium_url = await storage.generate_read_sas(medium_path, minutes=minutes)
    except Exception:
        logger.debug("Thumbnail SAS generation failed for %s | %s", small_path, medium_path, exc_info=True)
    return small_url, medium_url
