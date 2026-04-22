"""Shared DTOs used across CRM record syncs.

Each per-entity sync result extends `RecordSyncResult` and implements
`to_metadata()` so the Tier 3 task can stamp identical shapes into sync
history and `KnowledgeIntegration.connection_metadata`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class RecordSyncResult:
    """Base summary for any record-sync run.

    Subclasses add per-entity counts (pipelines_created, stages_updated, …).
    `to_metadata()` is overridden by subclasses; the base shape here guarantees
    the provenance fields every phase shares.
    """

    provider: str
    object_type: str
    organisation_id: UUID
    integration_id: UUID
    started_at: datetime
    completed_at: datetime

    def base_metadata(self) -> dict[str, Any]:
        """Common provenance fields every record-sync metadata payload carries."""
        return {
            "entity": "record",
            "provider": self.provider,
            "object_type": self.object_type,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }
