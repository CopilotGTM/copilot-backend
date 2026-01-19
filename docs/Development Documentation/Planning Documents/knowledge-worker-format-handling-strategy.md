# Knowledge Worker: Strategy for Handling Images and Markdown

## 1. Handling Images (`.png`, `.jpg`, `.tiff`)

### User's Question

*Do we need special "if/else" logic for images? Do we skip chunking and just save the text?*

### Strategy: Treat Images as "Scanned Documents"

We do **not** need special `if/else` logic in the parser. We should treat images exactly like **single-page PDFs**.

#### The Workflow

1. **Conversion (`docling`)**:
    * The `DoclingParser` receives the image file.
    * It runs **OCR** (Optical Character Recognition) on the image.
    * It creates a `DoclingDocument` object containing the extracted text and layout info.
2. **Chunking (`HybridChunker`)**:
    * We **DO** run chunking.
    * **Why?** An image might be a screenshot of a long contract, a photo of a receipt, or a slide with dense text.
    * If the text is long, the chunker splits it intelligently.
    * If the text is short (e.g., a meme or simple diagram), the chunker simply produces **one single chunk**.
3. **Result**:
    * We get a list of `ChunkData` objects.
    * This keeps our pipeline **uniform**. We don't need separate code paths for "files that need chunking" vs "files that don't".

#### Conclusion

We use the **exact same pipeline** as PDF.

* **Input**: `image.png`
* **Process**: OCR -> Text -> Chunking
* **Output**: Structured Chunks

---

## 2. Handling Markdown (`.md`)

### User's Question

*Is it the same as PDF? You will do chunking and identify images?*

### Strategy: Structured Parsing

**Yes, you are exactly right.**

#### The Workflow

1. **Conversion (`docling`)**:
    * `docling` reads the Markdown file.
    * Unlike `TextParser` (which sees flat text), `docling` understands the **structure**: Headers (`#`), Lists (`-`), Tables (`|...|`), and Images (`![alt](url)`).
2. **Chunking**:
    * The `HybridChunker` is "structure-aware".
    * It uses the Markdown headers to create logical chunks (e.g., keeping a section together).
3. **Images in Markdown**:
    * If the Markdown references local images, `docling` can identify them as `PictureItem`s.
    * These flow into our existing `pictures` list in `ChunkData`, allowing our `ImageSummaryAgent` to process them later.

#### Conclusion

Moving Markdown to `DoclingParser` is a huge upgrade over `TextParser`.

* **Old Way (`TextParser`)**: Flat text, tables broken, structure lost.
* **New Way (`DoclingParser`)**: Preserves hierarchy, tables, and image references.

## Summary of Recommendation

| Format | Parser | Strategy | Benefit |
| :--- | :--- | :--- | :--- |
| **Images** | `DoclingParser` | Treat as "Scanned PDF". Run OCR + Chunking. | Uniform pipeline. Handles both long-text screenshots and simple images automatically. |
| **Markdown** | `DoclingParser` | Parse structure (Headers/Tables). | Better chunking boundaries. Preserves tables and image links. |
