# Knowledge Worker Word Support Plan

## 1. Analysis

The user inquired if the existing `PDFParser` can be used for Word documents (`.docx`).

### Findings

1. **Underlying Technology**: `PDFParser` uses `docling.document_converter.DocumentConverter`.
2. **Configuration**: The `DocumentConverterService` in `src/workers/knowledge_worker/services/document_converter.py` is **already configured** to support `InputFormat.DOCX`.

    ```python
    self._converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        # ...
    )
    ```

3. **Conclusion**: Yes, the `PDFParser` is fully capable of parsing Word documents without any logic changes. It only requires configuration updates to register the MIME type.

## 2. Implementation Plan

### 2.1. Rename and Refactor

Since the parser handles more than just PDFs, we should rename it to reflect its broader capabilities.

* **Rename File**: `src/workers/knowledge_worker/parsers/pdf_parser.py` -> `src/workers/knowledge_worker/parsers/docling_parser.py`
* **Rename Class**: `PDFParser` -> `DoclingParser`

### 2.2. Update Supported MIME Types

Update the `supported_mimetypes` method in `DoclingParser` to include:

* `application/pdf`
* `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (DOCX)

### 2.3. Update Exports

Update `src/workers/knowledge_worker/parsers/__init__.py` to export `DoclingParser`.

### 2.4. Update Documentation

Update `docs/LLM Development Documentation/knowledge-worker-supported-formats.md` to reflect that Word is now supported.

## 3. Verification

* Parse a sample `.docx` file.
* Verify that text, tables, and images are extracted just like in PDFs.
