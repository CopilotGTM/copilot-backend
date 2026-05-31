from __future__ import annotations

import base64
import binascii


def encode_replay_id(replay_id: bytes) -> str:
    if not replay_id:
        raise ValueError("Salesforce replay ID cannot be empty")
    return base64.urlsafe_b64encode(replay_id).decode("ascii")


def decode_replay_id(encoded: str) -> bytes:
    try:
        replay_id = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("Invalid Salesforce replay ID") from exc
    if not replay_id:
        raise ValueError("Invalid Salesforce replay ID")
    return replay_id
