"""Document converter service with lazy loading.

This module provides lazy-loaded document conversion using Docling.
Heavy imports (docling) are deferred until first use to reduce memory
footprint when workers start but haven't processed any documents yet.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any


class DocumentConverterService:
    """Lazy-loaded Docling converter for PDF and DOCX.

    - PDF uses StandardPdfPipeline with rich options (OCR, tables, pictures).
    - DOCX uses SimplePipeline with generic PipelineOptions.

    The converter is lazily initialized on first use to avoid loading
    heavy ML libraries (docling, easyocr, etc.) until actually needed.

    Exposes a callable interface, so you can do:
        `result = document_converter_service(path_or_str)`
    or call `convert_file()` explicitly.
    """

    def __init__(self) -> None:
        self._converter: Any = None

    def _create_converter(self) -> Any:
        """Create the DocumentConverter with all pipeline options.

        This is called lazily on first use to defer heavy imports.
        """
        # Lazy imports - only load heavy libraries when actually needed
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            EasyOcrOptions,
            PdfPipelineOptions,
            PipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.document_converter import (
            DocumentConverter,
            PdfFormatOption,
            WordFormatOption,
        )

        return DocumentConverter(
            allowed_formats=[
                InputFormat.PDF,
                InputFormat.DOCX,
                InputFormat.PPTX,
                InputFormat.XLSX,
                InputFormat.HTML,
                InputFormat.IMAGE,
                InputFormat.MD,
            ],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=PdfPipelineOptions(
                        do_ocr=True,
                        do_table_structure=True,
                        generate_page_images=True,
                        generate_picture_images=True,
                        generate_table_images=True,
                        do_picture_classification=True,
                        do_formula_enrichment=False,
                        do_picture_description=False,
                        table_structure_options=TableStructureOptions(
                            mode=TableFormerMode.ACCURATE,
                            do_cell_matching=False,
                        ),
                        ocr_options=EasyOcrOptions(
                            force_full_page_ocr=True,
                        ),
                    )
                ),
                InputFormat.DOCX: WordFormatOption(
                    pipeline_options=PipelineOptions(create_legacy_output=False)
                ),
            },
        )

    @property
    def converter(self) -> Any:
        """Access the underlying Docling DocumentConverter (lazy-loaded)."""
        if self._converter is None:
            self._converter = self._create_converter()
        return self._converter

    def convert_file(self, file_path: str | Path) -> Any:
        """Convert a file from disk using the configured pipelines.

        Args:
            file_path: Path to a PDF or DOCX file.

        Returns:
            Docling conversion result.
        """
        return self.converter.convert(file_path)

    def __call__(self, file_path: str | Path) -> Any:
        """Shorthand to call `convert_file` directly on the service instance."""
        return self.convert_file(file_path)


# Global, initialized instance ready for import-and-use
document_converter_service = DocumentConverterService()


# Backwards-compat accessors -------------------------------------------------
@lru_cache(maxsize=1)
def get_doc_converter() -> Any:
    """Return the underlying global DocumentConverter (backwards-compatible).

    Note: Returns Any to avoid importing DocumentConverter at module level.
    The actual return type is docling.document_converter.DocumentConverter.
    """
    return document_converter_service.converter
