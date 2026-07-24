"""Shared validation helpers for persisted automation configuration."""

from __future__ import annotations

import json
from typing import Any

_CREDENTIAL_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "secret",
    "password",
    "credential",
    "credentials",
}
_CREDENTIAL_KEY_SUFFIXES = (
    "_api_key",
    "_access_token",
    "_refresh_token",
    "_client_secret",
    "_password",
    "_secret",
    "_credential",
    "_credentials",
)


def validate_json_document(field_name: str, value: Any, *, max_bytes: int) -> None:
    """Reject values that cannot be safely persisted as bounded JSON."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain valid JSON values") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} bytes")


def contains_credentials(value: Any) -> bool:
    """Return whether nested configuration appears to contain secret material."""

    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if (
                normalized_key in _CREDENTIAL_KEYS
                or normalized_key.endswith(_CREDENTIAL_KEY_SUFFIXES)
                or contains_credentials(nested_value)
            ):
                return True
    elif isinstance(value, list):
        return any(contains_credentials(item) for item in value)
    return False
