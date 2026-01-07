"""Chunking utilities with lazy loading.

This module provides lazy-loaded chunking and PDF conversion utilities.
Heavy imports (docling, transformers) are deferred until first use to
reduce memory footprint when workers start but haven't processed documents.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

# Default embedding/tokenizer model for chunking/token counts
DEFAULT_EMBED_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def get_tokenizer(model_id: str | None = None) -> Any:
    """Get tokenizer for chunking (lazy-loaded).

    The transformers library is only imported when this function is called.
    """
    from transformers import AutoTokenizer

    mid = model_id or DEFAULT_EMBED_MODEL_ID
    return AutoTokenizer.from_pretrained(mid)  # nosec B615


@lru_cache(maxsize=1)
def get_hybrid_chunker(model_id: str | None = None) -> Any:
    """Get hybrid chunker for document chunking (lazy-loaded).

    The docling library is only imported when this function is called.
    """
    from docling.chunking import HybridChunker

    tok = get_tokenizer(model_id)
    return HybridChunker(
        tokenizer=tok,
        max_tokens=tok.model_max_length - 2,
        merge_peers=True,
    )


def _default_pdf_pipeline_options() -> Any:
    """Create default PDF pipeline options (lazy-loaded)."""
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
    )

    return PdfPipelineOptions(
        do_ocr=True,
        do_table_structure=True,
        generate_page_images=False,
        generate_picture_images=True,
        generate_table_images=False,
        do_picture_classification=True,
        do_formula_enrichment=False,
        do_picture_description=False,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=True,
        ),
    )


@lru_cache(maxsize=1)
def get_pdf_converter() -> Any:
    """Get PDF converter (lazy-loaded)."""
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=_default_pdf_pipeline_options()),
        }
    )


@lru_cache(maxsize=1)
def get_light_pdf_converter() -> Any:
    """Get lightweight PDF converter for constrained environments (lazy-loaded)."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    light_opts = PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=False,
        generate_page_images=False,
        generate_picture_images=False,
        generate_table_images=False,
        do_picture_classification=False,
        do_formula_enrichment=False,
        do_picture_description=False,
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=light_opts),
        }
    )
