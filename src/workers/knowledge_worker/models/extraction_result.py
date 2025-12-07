"""Extraction result model for error handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.domain.models.organisation_tables.knowledge_models import OrganisationDocument


@dataclass
class ExtractionResult:
    """
    Result type for extraction operations.

    Provides a clean way to handle success/failure without exceptions
    for expected failure cases (unsupported format, empty content, etc.).

    Usage:
        result = service.execute(...)
        if result.is_success:
            print(f"Stored {len(result.document_ids)} documents")
        else:
            print(f"Failed: {result.error_message}")
    """

    success: bool
    document_ids: list[str] | None = None
    error_message: str | None = None
    chunks_processed: int = 0
    chunks_stored: int = 0
    documents: list[Any] | None = field(default=None)  # OrganisationDocument list

    @classmethod
    def ok(
        cls,
        document_ids: list[str],
        chunks_processed: int = 0,
        documents: list[OrganisationDocument] | None = None,
    ) -> ExtractionResult:
        """Create a successful result."""
        return cls(
            success=True,
            document_ids=document_ids,
            chunks_processed=chunks_processed,
            chunks_stored=len(document_ids),
            documents=documents,
        )

    @classmethod
    def fail(cls, message: str) -> ExtractionResult:
        """Create a failure result."""
        return cls(
            success=False,
            error_message=message,
        )

    @property
    def is_success(self) -> bool:
        """Check if extraction succeeded."""
        return self.success

    @property
    def is_failure(self) -> bool:
        """Check if extraction failed."""
        return not self.success

    @property
    def stored_count(self) -> int:
        """Number of documents successfully stored."""
        return len(self.document_ids) if self.document_ids else 0

    def __repr__(self) -> str:
        if self.success:
            return f"ExtractionResult(success=True, stored={self.stored_count})"
        return f"ExtractionResult(success=False, error='{self.error_message}')"
