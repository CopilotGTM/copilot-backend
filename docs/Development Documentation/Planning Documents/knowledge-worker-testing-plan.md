# Knowledge Worker Parser Testing Plan

## 1. Overview
This document outlines the testing strategy for the enhanced Knowledge Worker parsers (`JsonParser`, `XmlParser`, `DoclingParser`, `TextParser`).

## 2. Test Structure
We will create a new directory structure for worker tests:
`tests/workers/knowledge_worker/parsers/`

## 3. Test Cases

### 3.1. JSON Parser (`test_json_parser.py`)
*   **`test_parse_small_json`**: Verify a small JSON object is returned as a single chunk.
*   **`test_parse_large_object`**: Verify a large JSON object is split by keys.
*   **`test_parse_large_array`**: Verify a large JSON array is split by items.
*   **`test_invalid_json`**: Verify invalid JSON falls back to plain text (single chunk) and logs a warning.
*   **`test_primitive_json`**: Verify a simple string/number JSON is handled correctly.

### 3.2. XML Parser (`test_xml_parser.py`)
*   **`test_parse_small_xml`**: Verify a small XML document is returned as a single chunk.
*   **`test_parse_large_xml`**: Verify a large XML document is split by root's children.
*   **`test_invalid_xml`**: Verify invalid XML falls back to plain text.
*   **`test_security_xxe`**: Verify that `defusedxml` prevents XXE attacks (attempt to read a local file).

### 3.3. Text Parser (`test_text_parser.py`)
*   **`test_parse_plain_text`**: Verify plain text is parsed and **newlines are preserved** (regression test for the fix).
*   **`test_parse_csv`**: Verify CSV content is preserved with line breaks.
*   **`test_unsupported_mime`**: Verify it rejects unsupported MIME types (if applicable, though registry handles this).

### 3.4. Docling Parser (`test_docling_parser.py`)
*   **`test_supported_mimetypes`**: Verify it claims support for PDF, DOCX, PPTX, XLSX, HTML, MD, Images.
*   **`test_parse_mock_docling`**: Mock the `DocumentConverter` and `HybridChunker` to verify the flow:
    *   Input file -> Converter -> Docling Document
    *   Docling Document -> Chunker -> Raw Chunks
    *   Raw Chunks -> `ChunkData` (verify text cleaning and metadata extraction).
*   **`test_empty_file`**: Verify handling of non-existent or empty files.

## 4. Implementation Steps
1.  Create `tests/workers/knowledge_worker/parsers/__init__.py`.
2.  Implement `test_json_parser.py`.
3.  Implement `test_xml_parser.py`.
4.  Implement `test_text_parser.py`.
5.  Implement `test_docling_parser.py` (using `unittest.mock`).

## 5. Execution
Run tests using `pytest tests/workers/knowledge_worker/parsers/`.
