"""Shared environment gates for deployed E2E suites."""

from __future__ import annotations

import os
from functools import cache

import httpx
import pytest


def required_env(name: str) -> str:
    """Return a required E2E setting or skip when live credentials are absent."""
    value = os.getenv(name)
    if value:
        return value

    if os.getenv("E2E_REQUIRE_CONFIG") == "1":
        pytest.fail(f"{name} is required for deployed E2E tests")
    pytest.skip(f"{name} is required for deployed E2E tests")


@cache
def e2e_access_token_for(
    email_environment_name: str,
    password_environment_name: str,
) -> str:
    """Log in one configured actor and retain its token only in process memory."""
    url = f"{required_env('E2E_API_BASE_URL').rstrip('/')}/users/auth/login"
    response = httpx.post(
        url,
        json={
            "email": required_env(email_environment_name),
            "password": required_env(password_environment_name),
        },
        timeout=15,
    )
    if response.status_code != 200:
        pytest.fail(f"E2E user login failed with HTTP {response.status_code}")

    try:
        access_token = response.json()["data"]["tokens"]["access_token"]
    except (KeyError, TypeError, ValueError):
        pytest.fail("E2E user login response did not contain an access token")

    if not isinstance(access_token, str) or not access_token:
        pytest.fail("E2E user login returned an empty access token")
    return access_token


def e2e_access_token() -> str:
    """Return the default E2E actor token used by existing suites."""
    return e2e_access_token_for("E2E_USER_EMAIL", "E2E_USER_PASSWORD")
