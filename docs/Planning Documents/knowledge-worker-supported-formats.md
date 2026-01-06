# Knowledge Worker File Format Support Analysis

## 1. Currently Supported Formats

These formats are currently implemented in the codebase.

| Format | Extension | Parser | Status |
|--------|-----------|--------|--------|
| **PDF** | `.pdf` | `PDFParser` | **Full Support** (via Docling). Supports text, tables, and images. |
| **Plain Text** | `.txt` | `TextParser` | **Basic Support**. Flattens text (needs improvement). |
| **Markdown** | `.md` | `TextParser` | **Basic Support**. Flattens structure (needs improvement). |
| **CSV** | `.csv` | `TextParser` | **Basic Support**. Flattens rows (needs improvement). |
| **HTML** | `.html` | `TextParser` | **Basic Support**. Flattens tags (needs improvement). |
| **JSON** | `.json` | `TextParser` | **Basic Support**. Flattens structure (Planned for `JsonParser`). |
| **XML** | `.xml` | `TextParser` | **Basic Support**. Flattens structure (Planned for `XmlParser`). |

## 2. Missing & Easily Doable Formats

These formats are not currently registered but can be easily added because the underlying library (`docling`) already supports them.

| Format | Extension | Implementation Strategy | Difficulty |
|--------|-----------|-------------------------|------------|
| **Microsoft Word** | `.docx` | Create `DocxParser` using `docling` (similar to `PDFParser`). | **Low** (Library support exists) |
| **Microsoft PowerPoint** | `.pptx` | Create `PptxParser` using `docling` (similar to `PDFParser`). | **Low** (Library support exists) |

## 3. Missing & Requires Additional Work

These formats require additional libraries or complex logic to handle effectively.

| Format | Extension | Implementation Strategy | Difficulty |
|--------|-----------|-------------------------|------------|
| **Microsoft Excel** | `.xlsx` | Requires `pandas` or `openpyxl`. Needs strategy for large sheets (row-by-row vs whole sheet). | **Medium** |
| **Images** | `.png`, `.jpg` | Requires OCR (e.g., `tesseract` or `docling` with OCR enabled). | **Medium** |
| **E-books** | `.epub` | Requires `ebooklib` or similar. | **Medium** |

## 4. Recommendation

1. **Immediate Priority**: Implement `JsonParser` and `XmlParser` (as per current plan).
2. **High Priority**: Implement `DocxParser` and `PptxParser` leveraging the existing `docling` infrastructure. This will cover the vast majority of business documents.
3. **Future**: Evaluate the need for Excel and Image support based on user feedback.
