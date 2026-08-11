from datetime import UTC, datetime

import pytest
from src.service.integrations.webhooks.salesforce.decoder import (
    decode_opportunity_change_event,
    decode_salesforce_change_event,
)


def test_decode_opportunity_change_event_fans_out_record_ids() -> None:
    event = decode_opportunity_change_event(
        replay_id=b"\x00\x01",
        schema_id="schema-1",
        payload={
            "ChangeEventHeader": {
                "recordIds": ["006xx0000000001AAA", "006xx0000000002AAA"],
                "changeType": "UPDATE",
                "commitTimestamp": 1717084800000,
                "commitNumber": "123456789",
                "transactionKey": "0001",
                "sequenceNumber": 2,
            }
        },
    )

    assert event.replay_id == b"\x00\x01"
    assert event.schema_id == "schema-1"
    assert event.record_ids == ("006xx0000000001AAA", "006xx0000000002AAA")
    assert event.change_type == "UPDATE"
    assert event.commit_timestamp == datetime(2024, 5, 30, 16, 0, tzinfo=UTC)
    assert event.commit_number == "123456789"
    assert event.transaction_key == "0001"
    assert event.sequence_number == 2


def test_decode_opportunity_change_event_rejects_invalid_record_id() -> None:
    with pytest.raises(ValueError, match="invalid record ID"):
        decode_opportunity_change_event(
            replay_id=b"\x00\x01",
            schema_id="schema-1",
            payload={
                "ChangeEventHeader": {
                    "recordIds": ["006xx0000000001AAA' OR Name LIKE '%"],
                    "changeType": "UPDATE",
                }
            },
        )


def test_decode_salesforce_change_event_accepts_account_ids() -> None:
    event = decode_salesforce_change_event(
        replay_id=b"\x00\x01",
        schema_id="schema-1",
        payload={
            "ChangeEventHeader": {
                "entityName": "Account",
                "recordIds": ["001xx0000000001AAA"],
                "changeType": "UPDATE",
            }
        },
        object_name="Account",
        id_prefix="001",
    )

    assert event.record_ids == ("001xx0000000001AAA",)
    assert event.change_type == "UPDATE"
    assert event.entity_name == "Account"


def test_decode_aggregate_change_event_uses_header_entity_name() -> None:
    event = decode_salesforce_change_event(
        replay_id=b"\x00\x01",
        schema_id="schema-1",
        payload={
            "ChangeEventHeader": {
                "entityName": "Opportunity",
                "recordIds": ["006xx0000000001AAA"],
                "changeType": "UPDATE",
            }
        },
        object_name=None,
        id_prefix=None,
    )

    assert event.entity_name == "Opportunity"
    assert event.record_ids == ("006xx0000000001AAA",)


def test_decode_aggregate_change_event_requires_entity_name() -> None:
    with pytest.raises(ValueError, match="has no entity name"):
        decode_salesforce_change_event(
            replay_id=b"\x00\x01",
            schema_id="schema-1",
            payload={
                "ChangeEventHeader": {
                    "recordIds": ["006xx0000000001AAA"],
                    "changeType": "UPDATE",
                }
            },
            object_name=None,
            id_prefix=None,
        )


def test_decode_salesforce_change_event_accepts_contact_ids() -> None:
    event = decode_salesforce_change_event(
        replay_id=b"\x00\x01",
        schema_id="schema-1",
        payload={
            "ChangeEventHeader": {
                "entityName": "Contact",
                "recordIds": ["003xx0000000001AAA"],
                "changeType": "UPDATE",
            }
        },
        object_name="Contact",
        id_prefix="003",
    )

    assert event.record_ids == ("003xx0000000001AAA",)


def test_decode_salesforce_change_event_accepts_user_ids() -> None:
    event = decode_salesforce_change_event(
        replay_id=b"\x00\x01",
        schema_id="schema-1",
        payload={
            "ChangeEventHeader": {
                "entityName": "User",
                "recordIds": ["005xx0000000001AAA"],
                "changeType": "UPDATE",
            }
        },
        object_name="User",
        id_prefix="005",
    )

    assert event.record_ids == ("005xx0000000001AAA",)


@pytest.mark.parametrize(
    ("object_name", "id_prefix", "record_id"),
    [
        ("Task", "00T", "00Txx0000000001AAA"),
        ("ContentNote", "069", "069xx0000000001AAA"),
        ("ContentDocumentLink", "06A", "06Axx0000000001AAA"),
    ],
)
def test_decode_salesforce_change_event_accepts_task_and_note_ids(
    object_name: str,
    id_prefix: str,
    record_id: str,
) -> None:
    event = decode_salesforce_change_event(
        replay_id=b"\x00\x01",
        schema_id="schema-1",
        payload={
            "ChangeEventHeader": {
                "entityName": object_name,
                "recordIds": [record_id],
                "changeType": "UPDATE",
            }
        },
        object_name=object_name,
        id_prefix=id_prefix,
    )

    assert event.record_ids == (record_id,)


def test_decode_salesforce_change_event_rejects_wrong_prefix() -> None:
    with pytest.raises(ValueError, match="invalid record ID"):
        decode_salesforce_change_event(
            replay_id=b"\x00\x01",
            schema_id="schema-1",
            payload={
                "ChangeEventHeader": {
                    "entityName": "Account",
                    "recordIds": ["003xx0000000001AAA"],
                    "changeType": "UPDATE",
                }
            },
            object_name="Account",
            id_prefix="001",
        )
