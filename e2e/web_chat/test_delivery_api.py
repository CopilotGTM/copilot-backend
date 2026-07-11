"""Deployed web-chat API boundary checks that do not require worker completion."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from e2e.web_chat.support import (
    auth_headers,
    base_url,
    require_live_delivery_target,
    session_id,
)

pytestmark = pytest.mark.e2e


def test_send_message_rejects_invalid_authentication() -> None:
    """Prove the deployed endpoint rejects a caller before accepting chat work."""
    with httpx.Client(timeout=15) as client:
        response = client.post(
            f"{base_url()}/users/chats/send_message",
            headers={"Authorization": "Bearer e2e-invalid-token"},
            json={"session_id": str(uuid4()), "content": "E2E authentication probe"},
        )

    assert response.status_code == 401, response.text


def test_send_message_rejects_empty_content() -> None:
    """Prove deployed request validation rejects an input that cannot create a job."""
    require_live_delivery_target()

    with httpx.Client(timeout=15) as client:
        response = client.post(
            f"{base_url()}/users/chats/send_message",
            headers=auth_headers(),
            json={"session_id": str(session_id()), "content": "   "},
        )

    assert response.status_code == 422, response.text


def test_send_message_rejects_session_outside_authenticated_user() -> None:
    """Prove session ownership is enforced before durable chat work is created."""
    require_live_delivery_target()

    with httpx.Client(timeout=15) as client:
        response = client.post(
            f"{base_url()}/users/chats/send_message",
            headers=auth_headers(),
            json={"session_id": str(uuid4()), "content": "E2E session boundary probe"},
        )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["status"] == "ERROR"
    assert payload["message"] == "Chat session not found"
