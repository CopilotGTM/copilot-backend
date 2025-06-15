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
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if len(lines) > 1:
            portions = lines
        else:
            # Method 2: Split by bullet points or numbered lists
            import re
            bullet_pattern = r'[•·▪▫◦‣⁃]\s*'
            number_pattern = r'\d+\.\s+'
            
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
                separators = [' - ', ' | ', ' / ', ': ', '; ']
                for sep in separators:
                    if sep in text and len(text.split(sep)) > 1:
                        portions = [part.strip() for part in text.split(sep) if part.strip()]
                        break
                
                # Method 4: Split by sentences (for longer text)
                if not portions and len(text) > 100:
                    sentences = re.split(r'[.!?]+\s+', text)
                    if len(sentences) > 1:
                        cleaned_sentences = []
                        for i, sentence in enumerate(sentences):
                            sentence = sentence.strip()
                            if sentence:
                                # Add punctuation back if it was removed
                                if i < len(sentences) - 1 and not sentence.endswith(('.', '!', '?')):
                                    sentence += '.'
                                cleaned_sentences.append(sentence)
                        portions = cleaned_sentences
                
                # Method 5: Split long text into chunks (last resort)
                if not portions and len(text) > 200:
                    words = text.split()
                    chunk_size = max(10, len(words) // 3)  # Create roughly 3 chunks
                    chunks = []
                    for i in range(0, len(words), chunk_size):
                        chunk = ' '.join(words[i:i + chunk_size])
                        chunks.append(chunk)
                    portions = chunks
        
        # Fallback: use full text as single portion
        if not portions:
            portions = [text]
        
        return portions
    
    def extract_from_pptx(self, pptx_path):
        """Extract text content from PPTX file and return as Python dictionary."""
        try:
            # Validate file exists and is accessible
            if not os.path.exists(pptx_path):
                raise FileNotFoundError(f"PowerPoint file not found: {pptx_path}")
            
            # Validate file extension
            if not pptx_path.lower().endswith('.pptx'):
                raise ValueError(f"Invalid file format. Expected .pptx file: {pptx_path}")
            
            print(f"📖 Loading PPTX: {pptx_path}")
            
            try:
                presentation = Presentation(pptx_path)
            except Exception as e:
                raise Exception(f"Error opening PowerPoint file: {str(e)}")
            
            try:
                slide_count = len(presentation.slides)
                print(f"📊 Found {slide_count} slides")
            except Exception as e:
                print(f"⚠️ Warning: Could not determine slide count: {str(e)}")
                slide_count = 0
            
            slides_data = []
            
            for slide_index, slide in enumerate(presentation.slides):
                try:
                    slide_number = slide_index + 1
                    print(f"🔄 Processing slide {slide_number}...")
                    
                    # Extract text and images from slide
                    texts = []
                    images = []
                    all_text = []
                    text_id_counter = 0
                    image_id_counter = 0
                    
                    for shape in slide.shapes:
                        try:
                            # Extract text
                            if hasattr(shape, 'text_frame') and shape.text_frame.text.strip():
                                shape_text = shape.text_frame.text.strip()
                                all_text.append(shape_text)
                                
                                # Create text array from individual portions
                                text_array = []
                                if hasattr(shape.text_frame, 'paragraphs'):
                                    # Extract from individual paragraphs and runs
                                    for paragraph in shape.text_frame.paragraphs:
                                        if hasattr(paragraph, 'runs') and paragraph.runs:
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
                                    text_id_counter += 1
                                    text_item = {
                                        "id": f"text_{text_id_counter}",
                                        "fullText": shape_text,
                                        "textArray": text_array
                                    }
                                    texts.append(text_item)
                            
                            # Extract images
                            if hasattr(shape, 'image'):
                                image_id_counter += 1
                                image_item = {
                                    "id": f"image_{image_id_counter}",
                                    "name": f"slide_{slide_number}_image_{image_id_counter}.png"
                                }
                                images.append(image_item)
                                
                        except Exception as e:
                            print(f"⚠️ Warning: Error processing shape in slide {slide_number}: {str(e)}")
                            continue
                    
                    slide_data = {
                        "slide_number": slide_number,
                        "texts": texts,
                        "images": images
                    }
                    
                    slides_data.append(slide_data)
                    total_text_chunks = sum(len(text['textArray']) for text in texts)
                    print(f"   ✅ Extracted {len(texts)} text items with {total_text_chunks} chunks")
                    
                except Exception as e:
                    print(f"⚠️ Warning: Error processing slide {slide_index + 1}: {str(e)}")
                    continue
            
            # Create the final dictionary with current timestamp
            result = {
                "metadata": {
                    "file_name": Path(pptx_path).name,
                    "total_slides": len(slides_data),
                    "extraction_date": datetime.now().isoformat()
                },
                "slides": slides_data
            }
            
            return result
            
        except Exception as e:
            raise Exception(f"Error processing PPTX file: {str(e)}")