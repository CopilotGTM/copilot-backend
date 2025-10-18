from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BaseParser(ABC):
    """Abstract base class for document parsers."""

    def __init__(self, organisation_id: str, user_id: str, storage_backend: Any):
        """
        Initialize the parser.

        Args:
            organisation_id: The organisation ID
            user_id: The user ID who uploaded the document
            storage_backend: Storage backend instance (e.g., AzureOrganisationBlobStorage)
        """
        self.organisation_id = organisation_id
        self.user_id = user_id
        self.storage_backend = storage_backend

    @abstractmethod
    def parse(
        self,
        file_path: str | Path,
        document_url: str | None = None,
        source_id: str | None = None,
        source_blob_path: str | None = None,
        original_filename: str | None = None,
        source_size_bytes: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Parse the document and return processed data.

        Args:
            file_path: Path to the document file
            document_url: Optional URL of the document
            source_id: Optional source id for the uploaded document
            source_blob_path: Optional blob path of the original file in storage
            original_filename: Optional original filename provided by user
            source_size_bytes: Optional file size in bytes from OrganisationSource.size_bytes

        Returns:
            List of processed document chunks/pages
        """
        pass

    @classmethod
    def supported_mimetypes(cls) -> list[str]:
        """Optional classmethod listing supported MIME types.

        Parsers may override this to enable lightweight factory registration
        without instantiation. If not overridden, factories may fall back to
        instantiating the parser and calling an instance method.
        """
        return []

    def get_supported_mimetypes(self) -> list[str]:
        """Backwards-compatible instance method for supported MIME types."""
        # If the classmethod is provided by subclass, prefer that
        cls = self.__class__
        m = getattr(cls, "supported_mimetypes", None)
        if callable(m):
            try:
                return list(m())
            except Exception:
                pass
        return []
