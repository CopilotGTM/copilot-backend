"""Processing context shared across the extraction pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProcessingContext:
    """
    Shared context passed through the extraction pipeline.

    Contains all the information needed by processors to:
    - Generate correct blob storage paths
    - Create OrganisationDocument records
    - Track progress and status
    """

    organisation_id: str
    source_id: str
    user_id: str
    source_blob_path: str | None = None
    original_filename: str | None = None
    mimetype: str | None = None

    def __post_init__(self):
        """Validate required fields."""
        if not self.organisation_id:
            raise ValueError("organisation_id is required")
        if not self.source_id:
            raise ValueError("source_id is required")
        if not self.user_id:
            raise ValueError("user_id is required")
