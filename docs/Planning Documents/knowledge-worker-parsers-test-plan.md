# Knowledge Worker Parsers Test Plan

## 1. Overview

This plan outlines the testing strategy for the enhanced Knowledge Worker parsers (`JsonParser`, `XmlParser`, `DoclingParser`, `TextParser`). The goal is to verify that each parser correctly handles its supported formats, preserves structure where applicable, and chunks content appropriately.

## 2. Test Cases

### 2.1. JsonParser Tests

* **File**: `tests/workers/knowledge_worker/parsers/test_json_parser.py`
* **Scenarios**:
    1. **Valid JSON Object**: Verify that a standard JSON object is parsed, pretty-printed, and structure is preserved.
    2. **Valid JSON Array**: Verify that a JSON array is parsed and chunked by items.
    3. **Small JSON**: Verify that a small JSON file results in a single chunk.
    4. **Large JSON**: Verify that a large JSON file is split into multiple chunks respecting structure (keys/items).
    5. **Invalid JSON**: Verify fallback to plain text parsing when JSON is invalid.
    6. **MIME Types**: Verify supported MIME types (`application/json`).

### 2.2. XmlParser Tests

* **File**: `tests/workers/knowledge_worker/parsers/test_xml_parser.py`
* **Scenarios**:
    1. **Valid XML**: Verify that a standard XML file is parsed, pretty-printed, and structure is preserved.
    2. **Small XML**: Verify that a small XML file results in a single chunk.
    3. **Large XML**: Verify that a large XML file is split by root children.
    4. **Invalid XML**: Verify fallback to plain text parsing when XML is invalid.
    5. **Security**: Verify that the parser uses `defusedxml` (implicit verification by successful parsing of valid XML, explicit XXE tests are complex but we can ensure the library is used).
    6. **MIME Types**: Verify supported MIME types (`application/xml`, `text/xml`).

### 2.3. DoclingParser Tests

* **File**: `tests/workers/knowledge_worker/parsers/test_docling_parser.py`
* **Scenarios**:
    1. **Supported Formats**: Verify that the parser accepts PDF, DOCX, PPTX, XLSX, HTML, Markdown, and Images.
    2. **Mocked Conversion**: Since `docling` is a heavy library, we will mock the `DocumentConverter` and `HybridChunker` to verify the flow:
        * `convert()` is called with correct path.
        * `chunk()` is called with the document.
        * `ParsedContent` is returned with correct metadata and chunks.
    3. **Error Handling**: Verify behavior when file doesn't exist or conversion fails.

### 2.4. TextParser Tests

* **File**: `tests/workers/knowledge_worker/parsers/test_text_parser.py`
* **Scenarios**:
    1. **Plain Text**: Verify parsing of `.txt` files.
    2. **CSV**: Verify parsing of `.csv` files.
    3. **Newline Preservation**: Verify that newlines are preserved (not flattened) in the output chunks.
    4. **MIME Types**: Verify supported MIME types (`text/plain`, `text/csv`).

## 3. Implementation Details

* Use `pytest` as the testing framework.
* Use `unittest.mock` for mocking external dependencies (`docling`, `pathlib.Path.read_text`).
* Create temporary files or use `tmp_path` fixture for file operations.
