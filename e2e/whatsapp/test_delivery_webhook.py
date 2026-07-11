"""Deployed WhatsApp webhook authentication and request-contract checks."""

from __future__ import annotations

import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest

from e2e.conftest import required_env
from e2e.whatsapp.support import require_live_delivery_target

pytestmark = pytest.mark.e2e


def _base_url() -> str:
    return required_env("E2E_API_BASE_URL").rstrip("/")


def _app_secret() -> str:
    return required_env("E2E_WHATSAPP_APP_SECRET")


def _sign(body: bytes) -> str:
    digest = hmac.new(_app_secret().encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _headers(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _sign(body),
    }


def _webhook_url() -> str:
    return f"{_base_url()}/users/bots/whatsapp/webhook"


def _meta_text_payload(*, message_id: str | None = None, text: str = "E2E hello") -> dict:
    phone_number_id = required_env("E2E_WHATSAPP_PHONE_NUMBER_ID")
    recipient_wa_id = required_env("E2E_WHATSAPP_RECIPIENT_WA_ID")
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e2e-business-account",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "E2E WhatsApp User"},
                                    "wa_id": recipient_wa_id,
                                }
                            ],
                            "messages": [
                                {
                                    "from": recipient_wa_id,
                                    "id": message_id or f"wamid.e2e.{uuid4()}",
                                    "timestamp": "1760000000",
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_whatsapp_verification_challenge_returns_plain_text() -> None:
    """Prove Meta can complete webhook ownership verification."""
    verify_token = required_env("E2E_WHATSAPP_VERIFY_TOKEN")
    challenge = f"challenge-{uuid4()}"

    with httpx.Client(timeout=15) as client:
        response = client.get(
            _webhook_url(),
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": verify_token,
                "hub.challenge": challenge,
            },
        )

    assert response.status_code == 200
    assert response.text == challenge
    assert response.headers["content-type"].startswith("text/plain")


def test_whatsapp_webhook_rejects_invalid_signature() -> None:
    """Prove unsigned or incorrectly signed provider traffic is rejected."""
    body = json.dumps(_meta_text_payload()).encode("utf-8")

    with httpx.Client(timeout=15) as client:
        response = client.post(
            _webhook_url(),
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=invalid",
            },
        )

    assert response.status_code == 401


def test_whatsapp_webhook_rejects_invalid_json_with_valid_signature() -> None:
    """Prove authenticated malformed payloads fail at the JSON boundary."""
    body = b"{not-json"

    with httpx.Client(timeout=15) as client:
        response = client.post(_webhook_url(), content=body, headers=_headers(body))

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid request body"


def test_whatsapp_text_webhook_accepts_meta_payload_shape() -> None:
    """Prove a signed Meta text callback is accepted by the deployed endpoint."""
    require_live_delivery_target()
    payload = _meta_text_payload(text=f"E2E delivery contract {uuid4()}")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    with httpx.Client(timeout=30) as client:
        response = client.post(_webhook_url(), content=body, headers=_headers(body))

    assert response.status_code == 200
    assert response.content == b""


def test_whatsapp_duplicate_webhook_retry_returns_success() -> None:
    """Prove Meta can safely retry the same signed callback."""
    require_live_delivery_target()
    payload = _meta_text_payload(
        message_id=f"wamid.e2e.duplicate.{uuid4()}",
        text="E2E duplicate delivery contract",
    )
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = _headers(body)

    with httpx.Client(timeout=30) as client:
        first = client.post(_webhook_url(), content=body, headers=headers)
        retry = client.post(_webhook_url(), content=body, headers=headers)

    assert first.status_code == 200
    assert retry.status_code == 200
    assert first.content == b""
    assert retry.content == b""
