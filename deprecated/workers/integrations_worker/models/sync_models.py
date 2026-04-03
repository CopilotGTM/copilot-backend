from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class SyncJobRequest:
    organisation_id: UUID
    integration_id: UUID
    category: str
    provider: str
    provider_config_key: str | None = None
    incremental: bool = True
    sync_name: str | None = None
    sync_type: str | None = None
    sync_variant: str | None = None
    # Webhook response results for incremental sync optimization
    response_results: dict[str, int] | None = None


@dataclass
class SyncItemResult:
    status: str
    records_synced: int | None = None
    reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, removing None values for cleaner output."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class SyncJobResult:
    status: str
    items: dict[str, SyncItemResult] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with nested items properly serialized."""
        return {
            "status": self.status,
            "items": {k: v.to_dict() for k, v in self.items.items()},
            "extra": self.extra,
        }
