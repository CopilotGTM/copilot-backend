from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SyncJobRequest:
    organisation_id: str
    integration_id: str
    category: str
    provider: str
    data_types: list[str]
    incremental: bool = True
    sync_name: str | None = None


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
