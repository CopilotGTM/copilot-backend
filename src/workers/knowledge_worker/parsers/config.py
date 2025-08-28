from __future__ import annotations

import os
from typing import Dict, Any


class ParserConfig:
    """Configuration settings for document parsers (PDF-focused)."""

    # Embedding settings
    MAX_EMBEDDING_TEXT_LENGTH = int(os.getenv("MAX_EMBEDDING_TEXT_LENGTH", "8000"))
    EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "1536"))

    # Content processing settings
    MAX_CHUNK_SIZE = int(os.getenv("MAX_CHUNK_SIZE", "4000"))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
    MIN_CONTENT_LENGTH = int(os.getenv("MIN_CONTENT_LENGTH", "10"))
    MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "200"))

    # Retry and timeout settings
    PARSER_TIMEOUT_SECONDS = int(os.getenv("PARSER_TIMEOUT_SECONDS", "300"))
    MAX_PARSER_RETRIES = int(os.getenv("MAX_PARSER_RETRIES", "3"))

    @classmethod
    def get_parser_settings(cls, parser_type: str) -> Dict[str, Any]:
        """Return common settings (PDF is the only active parser)."""
        return {
            'max_chunk_size': cls.MAX_CHUNK_SIZE,
            'chunk_overlap': cls.CHUNK_OVERLAP,
            'min_content_length': cls.MIN_CONTENT_LENGTH,
            'max_summary_length': cls.MAX_SUMMARY_LENGTH,
            'max_embedding_text_length': cls.MAX_EMBEDDING_TEXT_LENGTH,
        }

    @classmethod
    def validate_config(cls) -> bool:
        """Validate configuration settings."""
        errors = []

        if cls.MAX_CHUNK_SIZE <= 0:
            errors.append("MAX_CHUNK_SIZE must be positive")

        if cls.CHUNK_OVERLAP >= cls.MAX_CHUNK_SIZE:
            errors.append("CHUNK_OVERLAP must be less than MAX_CHUNK_SIZE")

        if cls.MIN_CONTENT_LENGTH <= 0:
            errors.append("MIN_CONTENT_LENGTH must be positive")

        if errors:
            raise ValueError(f"Configuration validation failed: {'; '.join(errors)}")

        return True


# Validate configuration on import
ParserConfig.validate_config()
