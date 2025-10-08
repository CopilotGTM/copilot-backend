from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional, Union

from docling.document_converter import (
    DocumentConverter,
    PdfFormatOption,
    WordFormatOption,
)
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    PipelineOptions,
    TableStructureOptions,
    EasyOcrOptions,
    OcrOptions,
    TableFormerMode,
)


class DocumentConverterService:
    """Global, ready-to-use Docling converter for PDF and DOCX.

    - PDF uses StandardPdfPipeline with rich options (OCR, tables, pictures).
    - DOCX uses SimplePipeline with generic PipelineOptions.

    Exposes a callable interface, so you can do:
        `result = document_converter_service(path_or_str)`
    or call `convert_file()` explicitly.
    """

    def __init__(self) -> None:
        self._converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
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
    def converter(self) -> DocumentConverter:
        """Access the underlying Docling `DocumentConverter`."""
        return self._converter

    def convert_file(self, file_path: Union[str, Path]):
        """Convert a file from disk using the configured pipelines.

        Args:
            file_path: Path to a PDF or DOCX file.

        Returns:
            Docling conversion result.
        """
        return self._converter.convert(file_path)

    def __call__(self, file_path: Union[str, Path]):
        """Shorthand to call `convert_file` directly on the service instance."""
        return self.convert_file(file_path)


# Global, initialized instance ready for import-and-use
document_converter_service = DocumentConverterService()


# Backwards-compat accessors -------------------------------------------------
@lru_cache(maxsize=1)
def get_doc_converter() -> DocumentConverter:
    """Return the underlying global DocumentConverter (backwards-compatible)."""
    return document_converter_service.converter
