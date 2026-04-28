"""Provider-neutral DTOs for email payload normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedEmailFolder:
    """Normalized folder payload consumed by sync service and writer layers.

    `fields` are normalized provider-neutral keys, so downstream code can ignore
    Outlook vs Gmail payload shape differences.
    """

    external_id: str | None
    provider: str
    fields: dict[str, Any]
    data_payload: dict[str, Any]
    debug_sample: dict[str, Any]


@dataclass(frozen=True)
class NormalizedEmailMessage:
    """Normalized message payload consumed by sync service and writer layers.

    The payload keeps external IDs and provider context so changes can still be
    traced back to the original connector for diagnostics.
    """

    external_id: str | None
    provider: str
    external_folder_id: str | None
    fields: dict[str, Any]
    data_payload: dict[str, Any]
    debug_sample: dict[str, Any]


@dataclass(frozen=True)
class EmailFolderUpsertBatchItem:
    """Single-item batch envelope for folder upsert work in tests and callers."""

    folder: NormalizedEmailFolder


@dataclass(frozen=True)
class EmailMessageUpsertBatchItem:
    """Single-item batch envelope for message upsert work in tests and callers."""

    message: NormalizedEmailMessage
