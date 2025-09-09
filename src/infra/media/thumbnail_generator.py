from __future__ import annotations

"""Reusable thumbnail generation service for blobs (images and PDFs).

Designed for use from Celery workers (chat and knowledge). It performs:
 - Download of the original blob
 - Optional PDF first-page rendering to an image
 - Generation and upload of JPEG thumbnails in a deterministic thumbnails/ folder

This module is intentionally infra-level so both domains can depend on it.
"""

from io import BytesIO
from typing import Optional, Dict, Tuple, List

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


def _load_font_prefer_truetype(size: int):
    """Try to load a truetype font with the given size; fallback to default.

    Attempts common fonts often available with Pillow or on systems. Returns a PIL font object.
    """
    try:
        from PIL import ImageFont  # local import to avoid hard dep at module import

        candidates: List[str] = [
            "DejaVuSansMono.ttf",
            "DejaVuSans.ttf",
            "arial.ttf",
        ]
        for name in candidates:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        # Fallback
        return ImageFont.load_default()
    except Exception:  # pragma: no cover - defensive
        try:
            from PIL import ImageFont as _ImageFont

            return _ImageFont.load_default()
        except Exception:
            return None


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
    """Create a simple, readable placeholder PNG with a centered large label (e.g., 'TXT')."""
    try:
        from PIL import Image as _Image, ImageDraw

        img_size = 1024
        img = _Image.new("RGB", (img_size, img_size), (245, 246, 248))
        d = ImageDraw.Draw(img)
        text = label.upper()
        font = _load_font_prefer_truetype(size=int(img_size * 0.25))
        # Measure text and center
        try:
            tw = d.textlength(text, font=font)
        except Exception:
            # Fallback approximate
            tw = len(text) * (img_size * 0.12)
        th = int(img_size * 0.25)
        d.text(
            ((img_size - tw) / 2, (img_size - th) / 2),
            text,
            fill=(55, 65, 81),
            font=font,
        )
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.debug(
            "Failed to create placeholder image for label=%s", label, exc_info=True
        )
        return None


TEXT_MIME_LABELS = thumbnail_config.TEXT_LABEL_MAP


def _text_preview_bytes(text_bytes: bytes, label: str) -> Optional[bytes]:
    """Render a readable preview image from text bytes.

    - Decodes as UTF-8 with errors ignored.
    - Renders first lines with wrapping onto a canvas so small thumbnails remain legible.
    """
    try:
        from PIL import Image as _Image, ImageDraw
        import textwrap

        text = text_bytes.decode("utf-8", errors="ignore") if text_bytes else ""
        if not text.strip():
            return _placeholder_bytes("txt")

        # Normalize line endings and limit length
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        max_chars = 4000
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…"

        # Canvas and font
        W, H = 1024, 1024
        margin = 48
        bg = (255, 255, 255)
        fg = (20, 20, 20)
        subfg = (99, 102, 106)
        img = _Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)
        title_font = _load_font_prefer_truetype(size=40)
        body_font = _load_font_prefer_truetype(size=28)

        # Header chip
        chip_text = label.upper()[:10]
        chip_w = int(W * 0.18)
        chip_h = 56
        chip_x = margin
        chip_y = margin
        draw.rounded_rectangle(
            [chip_x, chip_y, chip_x + chip_w, chip_y + chip_h],
            radius=12,
            fill=(241, 245, 249),
        )
        try:
            tw = draw.textlength(chip_text, font=title_font)
        except Exception:
            tw = len(chip_text) * 20
        draw.text(
            (chip_x + (chip_w - tw) / 2, chip_y + 10),
            chip_text,
            fill=subfg,
            font=title_font,
        )

        # Text area
        text_x = margin
        text_y = chip_y + chip_h + 24
        text_w = W - 2 * margin
        text_h = H - text_y - margin

        # Wrap text by measuring average character width
        try:
            avg_char_w = max(12, int(draw.textlength("M" * 10, font=body_font) / 10))
        except Exception:
            avg_char_w = 14
        max_cols = max(20, int(text_w / avg_char_w))
        wrapped_lines: List[str] = []
        for line in text.split("\n"):
            wrapped_lines.extend(textwrap.wrap(line, width=max_cols) or [""])

        # Draw lines until we run out of space
        _, ascent = (0, 0)
        try:
            ascent, _ = body_font.getmetrics()  # type: ignore[attr-defined]
        except Exception:
            ascent = 28
        line_h = int(ascent * 1.4)
        y = text_y
        for line in wrapped_lines:
            if y + line_h > text_y + text_h:
                draw.text((text_x, y), "…", fill=fg, font=body_font)
                break
            draw.text((text_x, y), line, fill=fg, font=body_font)
            y += line_h

        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.debug("Failed to create text preview image", exc_info=True)
        return None


def _derive_image_bytes_for_thumbnail(
    *, mt: str, raw_bytes: bytes, original_blob_path: str
) -> Optional[bytes]:
    """Pure helper that returns representative image bytes or None based on mimetype."""
    if mt in thumbnail_config.IMAGE_MIMETYPES or (
        thumbnail_config.ENABLE_IMAGE_PREFIX_FALLBACK
        and any(mt.startswith(pref) for pref in thumbnail_config.IMAGE_PREFIXES)
    ):
        return raw_bytes
    if mt in thumbnail_config.PDF_MIMETYPES:
        preview = _pdf_page_preview_bytes(raw_bytes, original_blob_path)
        if not preview and thumbnail_config.ENABLE_PDF_PLACEHOLDER_FALLBACK:
            return _placeholder_bytes("pdf")
        return preview
    if mt in TEXT_MIME_LABELS and thumbnail_config.ENABLE_TEXT_PLACEHOLDERS:
        label = TEXT_MIME_LABELS.get(mt, "TXT")
        return _text_preview_bytes(raw_bytes, label) or _placeholder_bytes(
            label.lower()
        )
    if mt in thumbnail_config.DOCUMENT_LABEL_MAP:
        # We deliberately do NOT attempt to parse these binary/markup formats; a labeled placeholder is enough.
        label = thumbnail_config.DOCUMENT_LABEL_MAP.get(mt, "DOC")
        return _placeholder_bytes(label)
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
    except Exception as e:  # pragma: no cover - network path
        logger.exception("Failed to download blob for thumbnails: %s", e)
        return {}

    image_bytes = _derive_image_bytes_for_thumbnail(
        mt=mt, raw_bytes=raw_bytes, original_blob_path=original_blob_path
    )
    if image_bytes is None:
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
                await storage.upload_bytes(
                    thumb_path, out.read(), content_type="image/jpeg"
                )
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
        logger.debug(
            "Thumbnail SAS generation failed for %s | %s",
            small_path,
            medium_path,
            exc_info=True,
        )
    return small_url, medium_url
