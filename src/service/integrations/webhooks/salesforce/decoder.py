from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.service.integrations.webhooks.salesforce.types import (
    SalesforceChangeEvent,
    SalesforceOpportunityChangeEvent,
)


def decode_opportunity_change_event(
    *,
    replay_id: bytes,
    schema_id: str,
    payload: dict[str, Any],
) -> SalesforceOpportunityChangeEvent:
    return decode_salesforce_change_event(
        replay_id=replay_id,
        schema_id=schema_id,
        payload=payload,
        object_name="Opportunity",
        id_prefix="006",
    )


def decode_salesforce_change_event(
    *,
    replay_id: bytes,
    schema_id: str,
    payload: dict[str, Any],
    object_name: str,
    id_prefix: str,
) -> SalesforceChangeEvent:
    header = dict(payload.get("ChangeEventHeader") or {})
    entity_name = _optional_str(header.get("entityName"))
    if entity_name is not None and entity_name != object_name:
        raise ValueError(f"Salesforce {object_name} CDC event has mismatched entityName")

    record_ids = tuple(
        _validate_record_id(record_id, object_name=object_name, id_prefix=id_prefix)
        for record_id in header.get("recordIds") or ()
        if record_id
    )
    if not record_ids:
        raise ValueError(f"Salesforce {object_name} CDC event has no record IDs")

    change_type = str(header.get("changeType") or "").strip()
    if not change_type:
        raise ValueError(f"Salesforce {object_name} CDC event has no change type")

    commit_timestamp = _decode_commit_timestamp(header.get("commitTimestamp"))

    sequence_number_value = header.get("sequenceNumber")
    sequence_number = int(sequence_number_value) if sequence_number_value is not None else None

    return SalesforceChangeEvent(
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


def _validate_record_id(value: Any, *, object_name: str, id_prefix: str) -> str:
    record_id = str(value).strip()
    id_re = re.compile(rf"^{re.escape(id_prefix)}[A-Za-z0-9]{{12}}(?:[A-Za-z0-9]{{3}})?$")
    if not id_re.fullmatch(record_id):
        raise ValueError(f"Salesforce {object_name} CDC event has invalid record ID")
    return record_id
