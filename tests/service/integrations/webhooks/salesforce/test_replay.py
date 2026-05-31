import pytest
from src.service.integrations.webhooks.salesforce.replay import (
    decode_replay_id,
    encode_replay_id,
)


def test_replay_id_round_trip_preserves_opaque_bytes() -> None:
    replay_id = b"\x00\x01\xfa\xff"

    encoded = encode_replay_id(replay_id)

    assert encoded == "AAH6_w=="
    assert decode_replay_id(encoded) == replay_id


def test_decode_replay_id_rejects_invalid_base64() -> None:
    with pytest.raises(ValueError, match="Invalid Salesforce replay ID"):
        decode_replay_id("not base64")
