#!/usr/bin/env python3
"""
PPTXTextReplacer: Class-based text replacement in PPTX based on JSON.
"""

import json
import shutil
from pptx import Presentation


class PPTXTextReplacer:
    """Encapsulates loading slide definitions and replacing text in a PPTX."""

    def __init__(self, pptx_output: str):
        """
        Args:
            pptx_output: Path where modified PPTX will be saved.
        """
        self.pptx_output = pptx_output
        self.presentation = Presentation(self.pptx_output)

    @staticmethod
    def _build_replacement_map(replacements: list) -> dict:
        """Transform list of replacements into a dict keyed by shape ID."""
        mapping = {}
        for item in replacements:
            sid = item.get("id")
            if not sid:
                continue
            mapping.setdefault(sid, []).append({"old": item["previous_text"], "new": item["updated_text"]})
        return mapping

    def _replace_in_slide(self, slide, replacements: list) -> int:
        """
        Replace text for a single slide.

        Returns:
            Number of replacements made.
        """
        count = 0
        repl_map = self._build_replacement_map(replacements)

        for shape in slide.shapes:
            sid = None
            if getattr(shape, "name", None) and ";" in shape.name:
                parts = shape.name.split(";")
                if len(parts) > 1:
                    sid = parts[1]

            if sid not in repl_map:
                continue

            for rep in repl_map[sid]:
                # text_frame run-based
                if getattr(shape, "text_frame", None):
                    for para in shape.text_frame.paragraphs:
                        for run in para.runs:
                            if rep["old"] in run.text:
                                run.text = run.text.replace(rep["old"], rep["new"])
                                count += 1
                                print(f"   ✅ Replaced in shape {sid}: '{rep['old']}' → '{rep['new']}'")

                # fallback: direct text property
                elif getattr(shape, "text", None):
                    if rep["old"] in shape.text:
                        shape.text = shape.text.replace(rep["old"], rep["new"])
                        count += 1
                        print(f"   ✅ Replaced in shape {sid}: '{rep['old']}' → '{rep['new']}'")
        return count

    def run(self, slide_data: dict) -> None:
        """Execute the replacement workflow for a single slide.

        Args:
            slide_data: Dictionary containing slide_number and replacement_texts
                Example: {
                    'slide_number': 1,
                    'replacement_texts': [
                        {
                            'id': '417',
                            'previous_text': 'old text',
                            'updated_text': 'new text'
                        }
                    ]
                }
        """
        slide_number = slide_data.get("slide_number")
        replacements = slide_data.get("replacement_texts", [])

        if slide_number is None or not replacements:
            print("⚠️ Invalid slide data format")
            return

        try:
            idx = int(slide_number) - 1
        except Exception:
            print(f"⚠️ Invalid slide number: {slide_number}")
            return

        if idx < 0 or idx >= len(self.presentation.slides):
            print(f"⚠️ Slide {slide_number} out of range")
            return

        print(f"🔄 Processing slide {slide_number}")
        made = self._replace_in_slide(self.presentation.slides[idx], replacements)

        if made:
            print(f"   📝 Made {made} replacements")
            self.presentation.save(self.pptx_output)
        else:
            print(f"   ⚠️ No replacements made")

    def delete_slides(self, slide_numbers: list[int]) -> bool:
        """Delete multiple slides from the presentation by their numbers.

        Args:
            slide_numbers: List of 1-based indices of the slides to delete.

        Returns:
            bool: True if all specified slides were successfully deleted, False if any deletion failed.
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
