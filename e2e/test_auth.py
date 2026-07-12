"""Deployed authentication smoke test for the dedicated E2E user."""

from __future__ import annotations

import httpx
import pytest

from e2e.conftest import e2e_access_token, required_env

pytestmark = pytest.mark.e2e


def test_e2e_user_can_log_in_to_deployed_dev() -> None:
    """Prove Key Vault credentials mint a token accepted by the deployed API."""
    with httpx.Client(timeout=15) as client:
        response = client.get(
            f"{required_env('E2E_API_BASE_URL').rstrip('/')}/users/auth/me",
            headers={"Authorization": f"Bearer {e2e_access_token()}"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "SUCCESS"
    assert payload["data"]["state"] == "ACTIVE"
    assert payload["data"]["email"] == required_env("E2E_USER_EMAIL")
