"""Recall webhook signature verification.

This helper is shared with API ingress. It accepts both current `webhook-*` and
legacy `svix-*` headers because providers can lag on rollout and we still need to
accept older sends without dropping callbacks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Mapping

# Recall/Svix recommends rejecting old signed deliveries. Five minutes allows
# normal proxy and worker clock skew without making captured requests reusable.
RECALL_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60


class RecallWebhookVerificationError(ValueError):
    """Raised when a Recall webhook signature is missing or invalid."""


def verify_recall_webhook_signature(
    *,
    secret: str,
    headers: Mapping[str, str],
    payload: bytes | None,
    now: float | None = None,
    tolerance_seconds: int = RECALL_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
) -> bool:
    """Verify Recall webhook signatures for current and legacy header variants.

    Supports the current `webhook-*` headers as well as the older `svix-*`
    variants documented by Recall. The secret is expected to be the `whsec_...`
    value from Recall.
    """
    secret = secret.strip()
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    # Keep header reads case-insensitive; proxy/CDN layers can mutate case and
    # still preserve values.
    message_id = normalized_headers.get("webhook-id") or normalized_headers.get("svix-id")
    timestamp = normalized_headers.get("webhook-timestamp") or normalized_headers.get(
        "svix-timestamp"
    )
    signature_header = normalized_headers.get("webhook-signature") or normalized_headers.get(
        "svix-signature"
    )

    if not secret or not secret.startswith("whsec_"):
        raise RecallWebhookVerificationError("missing_or_invalid_secret")
    if not message_id or not timestamp or not signature_header:
        raise RecallWebhookVerificationError("missing_signature_headers")

    try:
        signed_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise RecallWebhookVerificationError("invalid_timestamp") from exc
    current_time = time.time() if now is None else now
    if tolerance_seconds < 0 or abs(current_time - signed_at) > tolerance_seconds:
        raise RecallWebhookVerificationError("timestamp_outside_tolerance")

    payload_bytes = payload or b""
    try:
        secret_key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RecallWebhookVerificationError("invalid_secret_encoding") from exc
    # Recall signs the exact UTF-8 JSON body that was delivered over HTTP, not
    # a parsed/re-serialized payload.
    signed_content = f"{message_id}.{timestamp}.{payload_bytes.decode('utf-8')}".encode()
    expected_signature = base64.b64encode(
        hmac.new(secret_key, signed_content, hashlib.sha256).digest()
    ).decode("utf-8")

    for versioned_signature in signature_header.split():
        # Signature transport may include multiple schemes; skip malformed entries
        # instead of hard-failing the entire verification.
        try:
            version, candidate_signature = versioned_signature.split(",", 1)
        except ValueError:
            continue
        if version != "v1":
            continue
        try:
            candidate_bytes = base64.b64decode(candidate_signature)
            expected_bytes = base64.b64decode(expected_signature)
        except binascii.Error as exc:
            raise RecallWebhookVerificationError("invalid_signature_encoding") from exc
        if len(candidate_bytes) != len(expected_bytes):
            continue
        if hmac.compare_digest(expected_bytes, candidate_bytes):
            return True

    raise RecallWebhookVerificationError("signature_mismatch")
