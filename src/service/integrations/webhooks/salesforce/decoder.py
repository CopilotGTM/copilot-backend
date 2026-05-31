from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.service.integrations.webhooks.salesforce.types import SalesforceOpportunityChangeEvent

_OPPORTUNITY_ID_RE = re.compile(r"^006[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")


def decode_opportunity_change_event(
    *,
    replay_id: bytes,
    schema_id: str,
    payload: dict[str, Any],
) -> SalesforceOpportunityChangeEvent:
    header = dict(payload.get("ChangeEventHeader") or {})
    record_ids = tuple(
        _validate_opportunity_id(record_id)
        for record_id in header.get("recordIds") or ()
        if record_id
    )
    if not record_ids:
        raise ValueError("Salesforce Opportunity CDC event has no record IDs")

    change_type = str(header.get("changeType") or "").strip()
    if not change_type:
        raise ValueError("Salesforce Opportunity CDC event has no change type")

    commit_timestamp = _decode_commit_timestamp(header.get("commitTimestamp"))

    sequence_number_value = header.get("sequenceNumber")
    sequence_number = int(sequence_number_value) if sequence_number_value is not None else None

    return SalesforceOpportunityChangeEvent(
        replay_id=replay_id,
        schema_id=schema_id,
        record_ids=record_ids,
        change_type=change_type,
        commit_timestamp=commit_timestamp,
        commit_number=_optional_str(header.get("commitNumber")),
        transaction_key=_optional_str(header.get("transactionKey")),
        sequence_number=sequence_number,
        payload=payload,
    )


def _decode_commit_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_opportunity_id(value: Any) -> str:
    record_id = str(value).strip()
    if not _OPPORTUNITY_ID_RE.fullmatch(record_id):
        raise ValueError("Salesforce Opportunity CDC event has invalid record ID")
    return record_id
