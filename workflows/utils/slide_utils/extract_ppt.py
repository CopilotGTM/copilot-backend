#!/usr/bin/env python3
"""
PPTX Text Extractor – keeps raw IDs and handles tables
"""
import re
from pathlib import Path
from pptx import Presentation


class PPTXExtractor:
    """Class to extract text and other content from PowerPoint presentations."""

    # text helpers
    def split_text_intelligently(self, text: str):
        """Same splitting logic you already had (slightly compacted)."""
        if not text.strip():
            return []

        text = text.strip()
        lines = [l for l in (ln.strip() for ln in text.split("\n")) if l]
        if len(lines) > 1:
            return lines

        bullet_pat = r"[•·▪▫◦‣⁃]\s*"
        num_pat = r"\d+\.\s+"
        if re.search(bullet_pat, text):
            return [p for p in re.split(bullet_pat, text) if p.strip()]
        if re.search(num_pat, text):
            return [p for p in re.split(num_pat, text) if p.strip()]

        for sep in (" - ", " | ", " / ", ": ", "; "):
            if sep in text:
                parts = [p.strip() for p in text.split(sep) if p.strip()]
                if parts:
                    return parts

        if len(text) > 100:
            sentences = re.split(r"[.!?]+\s+", text)
            cleaned = [
                s if s.endswith((".", "!", "?")) else s + "."
                for s in sentences if s.strip()
            ]
            if len(cleaned) > 1:
                return cleaned

        if len(text) > 200:
            words = text.split()
            size = max(10, len(words) // 3)
            return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]

        return [text]

    # id helper
    @staticmethod
    def _raw_shape_id(shape) -> str | None:
        """
        Return the raw numeric/string ID embedded in shape.name.
        Format: 'Anything;ID;pSomething'
        If not present, return None.
        """
        name = getattr(shape, "name", "")
        if ";" in name:
            parts = name.split(";")
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
        return None

    # table helper
    def extract_table_cells(self, table):
        """Flatten the table into a cleaned list of strings."""
        cells = []
        for row in table.rows:
            for cell in row.cells:
                txt = (cell.text_frame.text or "").strip() if cell.text_frame else ""
                if txt:
                    cells.extend(self.split_text_intelligently(txt))
        return cells

    # main api
    def extract_from_pptx(self, pptx_path: str):
        """
        Extract text and images from a PowerPoint presentation.

        Args:
            pptx_path: Path to the PowerPoint file.

        Returns:
            Dictionary containing metadata and extracted content from slides.

        Raises:
            RuntimeError: If there's an error processing the PPTX file.
        """
        try:
            prs = Presentation(pptx_path)
            slides_out = []

            anon_text_idx = 0  # fallback IDs when shape has no name
            anon_table_idx = 0
            anon_img_idx = 0

            for s_idx, slide in enumerate(prs.slides, start=1):
                slide_texts = []
                slide_imgs = []
                all_text = []

                for shape in slide.shapes:
                    sid = self._raw_shape_id(shape)

                    # Process text boxes and placeholders
                    if getattr(shape, "text_frame", None) and shape.text_frame.text.strip():
                        txt_full = shape.text_frame.text.strip()
                        txt_chunks = []

                        for para in shape.text_frame.paragraphs:
                            if para.runs:
                                txt_chunks.extend([
                                    run.text.strip()
                                    for run in para.runs if run.text.strip()
                                ])
                            elif para.text.strip():
                                txt_chunks.extend(
                                    self.split_text_intelligently(para.text.strip())
                                )

                        if not txt_chunks:
                            txt_chunks.extend(self.split_text_intelligently(txt_full))

                        if sid is None:
                            anon_text_idx += 1
                            sid = f"anon_text_{anon_text_idx}"

                        slide_texts.append({
                            "id": sid,
                            "type": "textbox",
                            "fullText": txt_full,
                            "textArray": txt_chunks
                        })
                        all_text.append(txt_full)

                    # Process tables
                    elif getattr(shape, "has_table", False):
                        chunks = self.extract_table_cells(shape.table)
                        if chunks:
                            if sid is None:
                                anon_table_idx += 1
                                sid = f"anon_table_{anon_table_idx}"

                            slide_texts.append({
                                "id": sid,
                                "type": "table",
                                "fullText": " ".join(chunks),
                                "textArray": chunks
                            })
                            all_text.extend(chunks)

                    # Process images
                    if getattr(shape, "image", None):
                        if sid is None:
                            anon_img_idx += 1
                            sid = f"anon_img_{anon_img_idx}"
                        slide_imgs.append({
                            "id": sid,
                            "name": f"slide_{s_idx}_image_{sid}.png"
                        })

                slides_out.append({
                    "slide_number": s_idx,
                    "texts": slide_texts,
                    "images": slide_imgs
                })

            return {
                "metadata": {
                    "file_name": Path(pptx_path).name,
                    "total_slides": len(slides_out),
                    "extraction_date": "2025-06-17T00:00:00Z"
                },
                "slides": slides_out
            }

        except Exception as exc:
            raise RuntimeError(f"Error processing PPTX file: {exc}")
