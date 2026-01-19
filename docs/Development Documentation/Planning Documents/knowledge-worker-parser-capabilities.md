# Knowledge Worker Parser Capabilities

## Overview

This document outlines the current capabilities of our document parsers, their potential extensions, and the changes required to enable them.

| Parser Name | Currently Configured to Extract | Potential Additional Extraction | Required Changes for Extraction |
| :--- | :--- | :--- | :--- |
| **`DoclingParser`**<br>(formerly `PDFParser`) | • **PDF** (`.pdf`)<br>• **Word** (`.docx`) | • **PowerPoint** (`.pptx`)<br>• **Excel** (`.xlsx`)<br>• **HTML** (`.html`)<br>• **Images** (`.png`, `.jpg`, etc.)<br>• **Markdown** (`.md`)<br>• **AsciiDoc** (`.asciidoc`) | 1. **Update Service**: Modify `DocumentConverterService` in `src/workers/knowledge_worker/services/document_converter.py` to add `InputFormat.PPTX`, `InputFormat.XLSX`, etc. to `allowed_formats`.<br>2. **Update Parser**: Add corresponding MIME types (e.g., `application/vnd.openxmlformats-officedocument.presentationml.presentation`) to `DoclingParser.supported_mimetypes`. |
| **`TextParser`** | • **Plain Text** (`.txt`)<br>• **Markdown** (`.md`)<br>• **CSV** (`.csv`)<br>• **HTML** (`.html`)<br>• **JSON** (`.json`)<br>• **XML** (`.xml`) | • **None**<br>*(This parser is a simple text reader. It does not have hidden capabilities like Docling.)* | • **N/A**<br>*(Note: We plan to move JSON/XML to dedicated parsers and potentially move HTML/Markdown to `DoclingParser` for better quality.)* |

## Detailed Analysis

### 1. DoclingParser

This parser wraps the `docling` library. It is currently the most powerful tool we have.

- **Current Status**: We recently renamed it from `PDFParser` and enabled DOCX support.
- **Potential**: It can replace `TextParser` for HTML and Markdown to provide better structural understanding (tables, headers) instead of flat text.
- **Excel/PowerPoint**: These are "low hanging fruit" as `docling` supports them natively.

### 2. TextParser

This is a fallback parser that reads files as plain text strings.

- **Current Status**: It "supports" many formats but flattens them, losing structure (tables in CSV become lines of text, JSON becomes a string).
- **Recommendation**:
  - **JSON/XML**: Create dedicated `JsonParser` and `XmlParser` (as per previous plan) to pretty-print structure.
  - **HTML/Markdown**: Move responsibility to `DoclingParser`.
  - **CSV**: Keep here or move to a dedicated `CsvParser` (using `pandas` or `csv` module) for better row/column representation.
