from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True, frozen=True)
class SalesforceChangeEvent:
    replay_id: bytes
    schema_id: str
    record_ids: tuple[str, ...]
    change_type: str
    commit_timestamp: datetime | None
    commit_number: str | None
    transaction_key: str | None
    sequence_number: int | None
    payload: dict[str, Any]


SalesforceOpportunityChangeEvent = SalesforceChangeEvent
