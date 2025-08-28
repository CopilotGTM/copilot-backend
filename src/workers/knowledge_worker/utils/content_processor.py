from __future__ import annotations

import re
from typing import List, Dict, Any, Optional
from pathlib import Path

from src.infra.utils.logger import get_logger

logger = get_logger("content_processor")


class ContentProcessor:
    """Utility class for processing and cleaning document content."""
    
    @staticmethod
    def clean_text(text: str) -> str:
        """
        Clean and normalize text content.
        
        Args:
            text: Raw text content
            
        Returns:
            Cleaned text
        """
        if not text:
            return ""
        
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text)
        
        # Remove control characters but keep line breaks
        text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)
        
        # Strip leading/trailing whitespace
        text = text.strip()
        
        return text
    
    @staticmethod
    def extract_text_elements(elements: List[Dict[str, Any]]) -> str:
        """
        Extract text content from slide/document elements.
        
        Args:
            elements: List of document elements
            
        Returns:
            Concatenated text content
        """
        text_parts = []
        
        def extract_from_element(element: Dict[str, Any]):
            """Recursively extract text from an element."""
            element_type = element.get('type', '')
            
            if element_type == 'text':
                text = element.get('text', '')
                if text:
                    text_parts.append(text)
            
            elif element_type == 'table':
                # Extract text from table cells
                rows = element.get('rows', [])
                for row in rows:
                    cells = row.get('cells', [])
                    for cell in cells:
                        cell_text = cell.get('text', '')
                        if cell_text:
                            text_parts.append(cell_text)
            
            elif element_type == 'group':
                # Process nested elements
                nested_elements = element.get('elements', [])
                for nested_element in nested_elements:
                    extract_from_element(nested_element)
        
        for element in elements:
            extract_from_element(element)
        
        combined_text = ' '.join(text_parts)
        return ContentProcessor.clean_text(combined_text)
    
    @staticmethod
    def chunk_content(content: str, max_chunk_size: int = 8000, overlap: int = 200) -> List[str]:
        """
        Split content into chunks for processing.
        
        Args:
            content: Text content to chunk
            max_chunk_size: Maximum size of each chunk
            overlap: Number of characters to overlap between chunks
            
        Returns:
            List of content chunks
        """
        if len(content) <= max_chunk_size:
            return [content]
        
        chunks = []
        start = 0
        
        while start < len(content):
            end = start + max_chunk_size
            
            # If this isn't the last chunk, try to break at a word boundary
            if end < len(content):
                # Look for the last space within the chunk
                last_space = content.rfind(' ', start, end)
                if last_space > start:
                    end = last_space
            
            chunk = content[start:end].strip()
            if chunk:
                chunks.append(chunk)
            
            # Move start position with overlap
            start = max(start + 1, end - overlap)
            
            # Prevent infinite loop
            if start >= len(content):
                break
        
        return chunks
    
    @staticmethod
    def generate_summary(content: str, max_length: int = 200) -> str:
        """
        Generate a brief summary of content.
        
        Args:
            content: Text content to summarize
            max_length: Maximum length of summary
            
        Returns:
            Content summary
        """
        if len(content) <= max_length:
            return content
        
        # Take first few sentences up to max_length
        sentences = re.split(r'[.!?]+', content)
        summary_parts = []
        current_length = 0
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            if current_length + len(sentence) + 1 <= max_length:
                summary_parts.append(sentence)
                current_length += len(sentence) + 1
            else:
                break
        
        summary = '. '.join(summary_parts)
        if summary and not summary.endswith('.'):
            summary += '...'
        
        return summary
    
    @staticmethod
    def extract_metadata_from_filename(filename: str) -> Dict[str, Any]:
        """
        Extract metadata from filename.
        
        Args:
            filename: Document filename
            
        Returns:
            Dictionary of extracted metadata
        """
        path = Path(filename)
        
        metadata = {
            'filename': path.name,
            'extension': path.suffix.lower(),
            'name_without_extension': path.stem,
            'file_type': ContentProcessor._get_file_type(path.suffix),
        }
        
        # Try to extract date from filename
        date_match = re.search(r'(\d{4}[-_]\d{2}[-_]\d{2})', path.stem)
        if date_match:
            metadata['extracted_date'] = date_match.group(1)
        
        # Try to extract version from filename
        version_match = re.search(r'v(\d+(?:\.\d+)*)', path.stem, re.IGNORECASE)
        if version_match:
            metadata['extracted_version'] = version_match.group(1)
        
        return metadata
    
    @staticmethod
    def _get_file_type(extension: str) -> str:
        """Get file type category from extension."""
        extension = extension.lower()
        
        type_mapping = {
            '.pdf': 'document',
            '.doc': 'document',
            '.docx': 'document',
            '.ppt': 'presentation',
            '.pptx': 'presentation',
            '.xls': 'spreadsheet',
            '.xlsx': 'spreadsheet',
            '.txt': 'text',
            '.md': 'text',
            '.html': 'text',
            '.csv': 'data',
            '.json': 'data',
            '.xml': 'data',
        }
        
        return type_mapping.get(extension, 'unknown')
    
    @staticmethod
    def validate_content(content: str, min_length: int = 10) -> bool:
        """
        Validate that content meets minimum requirements.
        
        Args:
            content: Content to validate
            min_length: Minimum content length
            
        Returns:
            True if content is valid, False otherwise
        """
        if not content or not isinstance(content, str):
            return False
        
        cleaned_content = ContentProcessor.clean_text(content)
        return len(cleaned_content) >= min_length
