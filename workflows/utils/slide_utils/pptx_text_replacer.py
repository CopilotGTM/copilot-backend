#!/usr/bin/env python3
"""
PPTXTextReplacer – Text replacement utility for PowerPoint files.
Supports replacing text in text boxes, tables, and other shape types.
"""

import json
from pptx import Presentation


class PPTXTextReplacer:
    """
    Replace text in PowerPoint presentations using shape IDs.
    
    This class handles text replacement in text boxes and table cells,
    using shape IDs found in shape.name (e.g. '417') as identifiers.
    """

    def __init__(self, pptx_output: str):
        """
        Initialize the PowerPoint text replacer.

        Args:
            pptx_output: Path of the PPTX file to modify in-place.
        """
        self.pptx_output = pptx_output
        self.presentation = Presentation(self.pptx_output)

    @staticmethod
    def _build_replacement_map(replacements: list) -> dict[str, list[dict]]:
        """
        Convert flat list of replacements to a structured mapping.

        Converts:
            [{"id": "417", "previous_text": "old", "updated_text": "new"}]
        To:
            {"417": [{"old": "old", "new": "new"}, ...]}

        Args:
            replacements: List of replacement dictionaries.

        Returns:
            Dictionary mapping shape IDs to lists of replacement pairs.
        """
        mapping: dict[str, list[dict]] = {}
        for item in replacements:
            sid = item.get("id")
            if sid:
                mapping.setdefault(str(sid), []).append(
                    {"old": item["previous_text"], "new": item["updated_text"]}
                )
        return mapping

    @staticmethod
    def _raw_shape_id(shape) -> str | None:
        """
        Extract the raw ID from a shape's name.

        Args:
            shape: PowerPoint shape object.

        Returns:
            The ID string from the shape name (e.g., '417' from '...;417;p131'),
            or None if not found.
        """
        name = getattr(shape, "name", "")
        if ";" in name:
            parts = name.split(";")
            if len(parts) > 1:
                return parts[1]
        return None

    def _replace_in_table(self, table, rep_list: list[dict]) -> int:
        """
        Replace text in all cells of a table.

        Args:
            table: PowerPoint table object.
            rep_list: List of replacement dictionaries.

        Returns:
            Number of replacements made.
        """
        made = 0
        for row in table.rows:
            for cell in row.cells:
                # Replace text in each run to preserve formatting
                if cell.text_frame:
                    for para in cell.text_frame.paragraphs:
                        for run in para.runs:
                            for rep in rep_list:
                                if rep["old"] in run.text:
                                    run.text = run.text.replace(rep["old"], rep["new"])
                                    made += 1
                # Fallback to cell's text property if no replacements made
                if not made:
                    cell_text = cell.text
                    for rep in rep_list:
                        if rep["old"] in cell_text:
                            cell.text = cell_text.replace(rep["old"], rep["new"])
                            made += 1
        return made

    def _replace_in_slide(self, slide, replacements: list[dict]) -> int:
        """
        Process text replacements in a single slide.

        Args:
            slide: PowerPoint slide object.
            replacements: List of replacement specifications.

        Returns:
            Number of replacements made.
        """
        count = 0
        repl_map = self._build_replacement_map(replacements)

        for shape in slide.shapes:
            sid = self._raw_shape_id(shape)
            if sid is None or sid not in repl_map:
                continue

            rep_list = repl_map[sid]

            # Process text boxes and placeholders
            if getattr(shape, "text_frame", None):
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        for rep in rep_list:
                            if rep["old"] in run.text:
                                run.text = run.text.replace(rep["old"], rep["new"])
                                count += 1
                                print(f"   ✅ Replaced in shape {sid}: '{rep['old']}' → '{rep['new']}'")

            # Process tables
            elif getattr(shape, "has_table", False):
                made = self._replace_in_table(shape.table, rep_list)
                if made:
                    count += made
                    print(f"   ✅ Replaced {made} table item(s) in shape {sid}")

            # Process other shape types with text
            elif getattr(shape, "text", None):
                txt = shape.text
                for rep in rep_list:
                    if rep["old"] in txt:
                        shape.text = txt.replace(rep["old"], rep["new"])
                        count += 1
                        print(f"   ✅ Replaced in shape {sid}: '{rep['old']}' → '{rep['new']}'")

        return count

    def run(self, slide_data: dict) -> None:
        """
        Perform text replacements on a single slide.

        Args:
            slide_data: Dictionary containing:
                - slide_number: The 1-based slide number to modify
                - replacement_texts: List of replacement specifications with
                  id, previous_text, and updated_text fields

        Example:
            {
                "slide_number": 5,
                "replacement_texts": [
                    {
                        "id": "417",
                        "previous_text": "old",
                        "updated_text": "new"
                    }
                ]
            }
        """
        slide_number = slide_data.get("slide_number")
        repls = slide_data.get("replacement_texts", [])

        if slide_number is None or not repls:
            print("⚠️  Invalid slide data format")
            return

        try:
            idx = int(slide_number) - 1
        except ValueError:
            print(f"⚠️  Invalid slide number: {slide_number}")
            return

        if not (0 <= idx < len(self.presentation.slides)):
            print(f"⚠️  Slide {slide_number} out of range")
            return

        print(f"🔄 Processing slide {slide_number}")
        made = self._replace_in_slide(self.presentation.slides[idx], repls)

        if made:
            print(f"   📝 Made {made} replacement(s)")
            self.presentation.save(self.pptx_output)
        else:
            print("   ⚠️  No replacements made")

    def delete_slides(self, slide_numbers: list[int]) -> bool:
        """
        Delete multiple slides from the presentation.

        Args:
            slide_numbers: List of 1-based slide numbers to delete.

        Returns:
            True if all slides were successfully deleted, False otherwise.
        """
        if not slide_numbers:
            print("⚠️ No slide numbers provided")
            return False

        # Validate all slide numbers first
        total_slides = len(self.presentation.slides)
        validated_indices = []

        for slide_number in slide_numbers:
            try:
                idx = int(slide_number) - 1
                if idx < 0 or idx >= total_slides:
                    print(f"⚠️ Slide {slide_number} out of range")
                    return False
                validated_indices.append(idx)
            except Exception:
                print(f"⚠️ Invalid slide number: {slide_number}")
                return False

        # Sort indices in reverse order to prevent index shifting issues
        validated_indices.sort(reverse=True)

        # Get the XML slides
        xml_slides = self.presentation.slides._sldIdLst

        # Delete slides in reverse order
        for idx in validated_indices:
            xml_slides.remove(xml_slides[idx])
            print(f"🗑️ Deleted slide {idx + 1}")

        # Save the presentation after all deletions
        self.presentation.save(self.pptx_output)
        print(f"✅ Successfully deleted {len(validated_indices)} slides")
        return True
