#!/usr/bin/env python3
"""
PPTX Text Extractor - Extract text content and structure from PowerPoint presentations
"""
import os
from pptx import Presentation
from pathlib import Path
from datetime import datetime


class PPTXExtractor:
    """Extract text content from PPTX files with intelligent text splitting."""

    def __init__(self):
        """Initialize the extractor."""
        pass

    def split_text_intelligently(self, text):
        """Split text into meaningful portions for better AI processing."""
        if not text.strip():
            return []

        # Clean up text
        text = text.strip()
        portions = []

        # Method 1: Split by line breaks (most common in PPTX)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if len(lines) > 1:
            portions = lines
        else:
            # Method 2: Split by bullet points or numbered lists
            import re

            bullet_pattern = r"[•·▪▫◦‣⁃]\s*"
            number_pattern = r"\d+\.\s+"

            # Try bullet points
            if re.search(bullet_pattern, text):
                parts = re.split(bullet_pattern, text)
                portions = [part.strip() for part in parts if part.strip()]
            # Try numbered lists
            elif re.search(number_pattern, text):
                parts = re.split(number_pattern, text)
                portions = [part.strip() for part in parts if part.strip()]
            else:
                # Method 3: Split by common separators
                separators = [" - ", " | ", " / ", ": ", "; "]
                for sep in separators:
                    if sep in text and len(text.split(sep)) > 1:
                        portions = [part.strip() for part in text.split(sep) if part.strip()]
                        break

                # Method 4: Split by sentences (for longer text)
                if not portions and len(text) > 100:
                    sentences = re.split(r"[.!?]+\s+", text)
                    if len(sentences) > 1:
                        cleaned_sentences = []
                        for i, sentence in enumerate(sentences):
                            sentence = sentence.strip()
                            if sentence:
                                # Add punctuation back if it was removed
                                if i < len(sentences) - 1 and not sentence.endswith((".", "!", "?")):
                                    sentence += "."
                                cleaned_sentences.append(sentence)
                        portions = cleaned_sentences

                # Method 5: Split long text into chunks (last resort)
                if not portions and len(text) > 200:
                    words = text.split()
                    chunk_size = max(10, len(words) // 3)  # Create roughly 3 chunks
                    chunks = []
                    for i in range(0, len(words), chunk_size):
                        chunk = " ".join(words[i : i + chunk_size])
                        chunks.append(chunk)
                    portions = chunks

        # Fallback: use full text as single portion
        if not portions:
            portions = [text]

        return portions

    def extract_from_pptx(self, pptx_path):
        """Extract text content from PPTX file."""
        try:
            presentation = Presentation(pptx_path)

            slides_data = []

            for slide_index, slide in enumerate(presentation.slides):
                slide_number = slide_index + 1

                # Extract text and images from slide
                texts = []
                images = []
                all_text = []
                text_id_counter = 0
                image_id_counter = 0

                for shape in slide.shapes:
                    try:
                        # Extract text
                        if hasattr(shape, "text_frame") and shape.text_frame.text.strip():
                            shape_text = shape.text_frame.text.strip()
                            all_text.append(shape_text)

                            # Create text array from individual portions
                            text_array = []
                            if hasattr(shape.text_frame, "paragraphs"):
                                # Extract from individual paragraphs and runs
                                for paragraph in shape.text_frame.paragraphs:
                                    if hasattr(paragraph, "runs") and paragraph.runs:
                                        for run in paragraph.runs:
                                            if run.text.strip():
                                                text_array.append(run.text.strip())
                                    elif paragraph.text.strip():
                                        # Split paragraph text intelligently
                                        portions = self.split_text_intelligently(paragraph.text.strip())
                                        text_array.extend(portions)
                            else:
                                # Fallback: Split shape text intelligently
                                text_chunks = self.split_text_intelligently(shape_text)
                                text_array.extend(text_chunks)

                            # Only add if there's actual text content
                            if shape_text or text_array:
                                # Get shape ID from name (format: "Google Shape;417;p131")
                                shape_id = None
                                shape_name = getattr(shape, "name", "")
                                if shape_name and ";" in shape_name:
                                    try:
                                        shape_id = shape_name.split(";")[1]  # Get the ID part
                                    except:
                                        pass

                                # Debug print shape properties

                                if shape_id is None:
                                    text_id_counter += 1
                                    shape_id = f"text_{text_id_counter}"

                                text_item = {
                                    "id": str(shape_id),  # Use ID from shape name
                                    "fullText": shape_text,
                                    "textArray": text_array,
                                }
                                texts.append(text_item)

                        # Extract images
                        if hasattr(shape, "image"):
                            # Get shape ID from name (format: "Google Shape;417;p131")
                            shape_id = None
                            shape_name = getattr(shape, "name", "")
                            if shape_name and ";" in shape_name:
                                try:
                                    shape_id = shape_name.split(";")[1]  # Get the ID part
                                except:
                                    pass

                            # Debug print image shape properties

                            if shape_id is None:
                                image_id_counter += 1
                                shape_id = f"image_{image_id_counter}"

                            image_item = {
                                "id": str(shape_id),  # Use ID from shape name
                                "name": f"slide_{slide_number}_image_{shape_id}.png",
                            }
                            images.append(image_item)

                    except Exception as e:
                        # Skip problematic shapes and continue
                        continue

                # Combine all text for slide type detection
                full_text = " ".join(all_text)

                slide_data = {
                    "slide_number": slide_number,
                    "texts": texts,
                    "images": images,
                }

                slides_data.append(slide_data)
                total_text_chunks = sum(len(text["textArray"]) for text in texts)

            return {
                "metadata": {
                    "file_name": Path(pptx_path).name,
                    "total_slides": len(slides_data),
                    "extraction_date": "2024-06-08T20:16:00Z",
                },
                "slides": slides_data,
            }

        except Exception as e:
            raise Exception(f"Error processing PPTX file: {e}")
