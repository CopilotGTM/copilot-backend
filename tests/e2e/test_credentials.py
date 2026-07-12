"""Regression checks for the deployed E2E credential bootstrap."""

from __future__ import annotations

import httpx
import pytest

from e2e import conftest


def test_access_token_logs_in_with_dedicated_e2e_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the contract that CI stores credentials while tokens remain process-local."""
    monkeypatch.setenv("E2E_API_BASE_URL", "https://devapi.example.com/")
    monkeypatch.setenv("E2E_USER_EMAIL", "e2e@example.com")
    monkeypatch.setenv("E2E_USER_PASSWORD", "password-from-key-vault")
    captured_request: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        """Capture the login request without sending the test password over HTTP."""
        captured_request.update(url=url, **kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"data": {"tokens": {"access_token": "short-lived-token"}}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    assert conftest.e2e_access_token() == "short-lived-token"
    assert captured_request["url"] == "https://devapi.example.com/users/auth/login"
    assert captured_request["json"] == {
        "email": "e2e@example.com",
        "password": "password-from-key-vault",
    }


def test_required_env_fails_closed_when_ci_requires_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent a green deployment when its E2E credentials were not injected."""
    monkeypatch.delenv("E2E_USER_EMAIL", raising=False)
    monkeypatch.setenv("E2E_REQUIRE_CONFIG", "1")

    with pytest.raises(pytest.fail.Exception, match="E2E_USER_EMAIL"):
        conftest.required_env("E2E_USER_EMAIL")
