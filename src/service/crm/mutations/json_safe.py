"""JSON-safe normalization for CRM mutation audit payloads.

CRM write services hand this module ordinary Python domain values. The audit
table stores JSON, so this module is the single place that converts supported
values and rejects payloads that should never land in an immutable ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from src.service.crm.mutations.contracts import CRMMutationEventServiceError

# Keep audit rows useful without allowing a pasted note or provider blob to
# dominate the JSON column. The truncated shape preserves evidence of loss.
MAX_AUDIT_STRING_LENGTH = 5000

# Field-name based filtering is deliberately conservative: a false positive
# fails the write loudly, while a leaked credential would be immutable audit data.
_SENSITIVE_FIELD_TOKENS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "oauth",
    "password",
    "refresh_token",
    "secret",
    "token",
)


def changed_field(old: Any, new: Any) -> dict[str, Any] | None:
    """Return an old/new payload only when a field actually changed.

    Entity write services use the returned value while building a
    changed-fields dict; skipped `None` values keep no-op assignments out of
    the audit ledger.
    """
    if old == new:
        return None
    return {"old": old, "new": new}


def normalize_changed_fields(
    changed_fields: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Normalize old/new field changes into JSON-safe audit values.

    The audit contract requires every field to carry both sides of the diff.
    Missing `old` or `new` usually means a caller is trying to record a partial
    patch rather than the immutable before/after evidence used by reviewers.
    """
    normalized: dict[str, dict[str, Any]] = {}
    for field_name, change in changed_fields.items():
        _raise_if_sensitive_field(field_name)
        if "old" not in change or "new" not in change:
            raise CRMMutationEventServiceError(
                f"CRM mutation field '{field_name}' must include old and new values",
                status_code=400,
            )
        normalized[field_name] = {
            "old": normalize_json_value(change["old"], field_name=field_name),
            "new": normalize_json_value(change["new"], field_name=field_name),
        }
    return normalized


def normalize_json_value(value: Any, *, field_name: str | None = None) -> Any:
    """Convert supported CRM audit values into JSON-safe primitives.

    Unsupported types are rejected instead of stringified because audit payloads
    need stable shapes for idempotency hashing and future workflow filters.
    """
    if value is None:
        return value
    if isinstance(value, Enum):
        return normalize_json_value(value.value, field_name=field_name)
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        # Nested objects keep their caller-provided keys as strings because JSON
        # object keys cannot safely preserve UUID, enum, or Decimal identity.
        return {
            str(key): normalize_json_value(nested, field_name=str(key))
            for key, nested in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        # Strings are handled above; treating them as sequences here would split
        # customer-visible values into characters and corrupt audit evidence.
        return [normalize_json_value(item, field_name=field_name) for item in value]

    raise CRMMutationEventServiceError(
        f"CRM mutation field '{field_name or '<unknown>'}' contains an unsupported value",
        status_code=400,
    )


def _normalize_string(value: str) -> str | dict[str, Any]:
    """Return short strings as-is and long strings with explicit truncation metadata."""
    if len(value) <= MAX_AUDIT_STRING_LENGTH:
        return value
    return {
        "value": value[:MAX_AUDIT_STRING_LENGTH],
        "truncated": True,
        "original_length": len(value),
    }


def _raise_if_sensitive_field(field_name: str) -> None:
    """Reject field names that are likely to contain credentials or bearer material."""
    normalized = field_name.lower().replace("-", "_")
    if any(token in normalized for token in _SENSITIVE_FIELD_TOKENS):
        raise CRMMutationEventServiceError(
            f"CRM mutation field '{field_name}' is not audit-safe",
            status_code=400,
        )
