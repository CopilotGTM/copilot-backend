"""Tests for Recall webhook signature verification."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from src.service.integrations.meeting_recording.providers.recall_webhook_verifier import (
    RecallWebhookVerificationError,
    verify_recall_webhook_signature,
)


def _build_signature(secret: str, webhook_id: str, timestamp: str, payload: bytes) -> str:
    """Build a valid test signature using the same scheme as Recall/Svix."""
    secret_key = base64.b64decode(secret.removeprefix("whsec_"))
    signed_content = f"{webhook_id}.{timestamp}.{payload.decode('utf-8')}".encode()
    signature = hmac.new(secret_key, signed_content, hashlib.sha256).digest()
    return f"v1,{base64.b64encode(signature).decode('utf-8')}"


class TestRecallWebhookVerification:
    def test_verifies_current_workspace_headers(self) -> None:
        secret = "whsec_c2lnbmluZy1zZWNyZXQtdmFsdWU="
        payload = b'{"event":"transcript.done"}'
        webhook_id = "msg_123"
        timestamp = "1731705121"
        signature = _build_signature(secret, webhook_id, timestamp, payload)

        verified = verify_recall_webhook_signature(
            secret=secret,
            headers={
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": signature,
            },
            payload=payload,
        )

        assert verified is True

    def test_verifies_svix_header_variants(self) -> None:
        secret = "whsec_c2lnbmluZy1zZWNyZXQtdmFsdWU="
        payload = b'{"event":"bot.done"}'
        webhook_id = "msg_456"
        timestamp = "1731705122"
        signature = _build_signature(secret, webhook_id, timestamp, payload)

        verified = verify_recall_webhook_signature(
            secret=secret,
            headers={
                "svix-id": webhook_id,
                "svix-timestamp": timestamp,
                "svix-signature": signature,
            },
            payload=payload,
        )

        assert verified is True

    def test_verifies_secret_with_surrounding_whitespace(self) -> None:
        secret = "whsec_c2lnbmluZy1zZWNyZXQtdmFsdWU="
        payload = b'{"event":"transcript.done"}'
        webhook_id = "msg_123"
        timestamp = "1731705121"
        signature = _build_signature(secret, webhook_id, timestamp, payload)

        verified = verify_recall_webhook_signature(
            secret=f"\n{secret}\n",
            headers={
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": signature,
            },
            payload=payload,
        )

        assert verified is True

    def test_rejects_invalid_signature(self) -> None:
        with pytest.raises(RecallWebhookVerificationError):
            verify_recall_webhook_signature(
                secret="whsec_c2lnbmluZy1zZWNyZXQtdmFsdWU=",
                headers={
                    "webhook-id": "msg_123",
                    "webhook-timestamp": "1731705121",
                    "webhook-signature": "v1,invalid",
                },
                payload=b'{"event":"transcript.done"}',
            )

    def test_rejects_missing_headers(self) -> None:
        with pytest.raises(RecallWebhookVerificationError):
            verify_recall_webhook_signature(
                secret="whsec_c2lnbmluZy1zZWNyZXQtdmFsdWU=",
                headers={},
                payload=b"{}",
            )
