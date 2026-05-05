"""Tests for app-level and public lead form CORS configuration.

These tests cover two CORS contracts in one place: environment-driven global
defaults for authenticated app traffic, and per-form origin allow-lists used by
public lead form embeds.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from src.api.middleware.public_form_cors import PublicLeadFormCORSMiddleware


def _build_app():
    """Reload app modules so env changes in the test are picked up."""
    # Each test sets or clears env vars; reloading guarantees those changes flow
    # into app setup instead of being memoized by already-imported singletons.
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)
    return fastapi_app.create_fastapi_app()


def _cors_middleware_kwargs(app) -> dict:
    """Return kwargs for the app's CORSMiddleware instance."""
    # Test needs the configured CORS surface area directly so we assert defaults
    # and validation errors without coupling to route names or internals.
    middleware = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    return middleware.kwargs


def _public_form_cors_app(monkeypatch, allowed_origins: list[str]) -> FastAPI:
    """Build a minimal app with PublicLeadFormCORSMiddleware attached."""

    # A minimal app keeps this test focused on middleware behavior and avoids
    # unrelated auth or dependency interactions from the main application factory.
    async def _allowed_origins(public_form_key: str) -> list[str]:
        assert public_form_key == "lf_demo123"
        return allowed_origins

    monkeypatch.setattr(
        "src.api.middleware.public_form_cors.resolve_public_form_allowed_origins",
        _allowed_origins,
    )

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://app.copilotgtm.com"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(PublicLeadFormCORSMiddleware)

    @app.get("/public/v1/forms/{public_form_key}")
    async def _get_public_form(public_form_key: str) -> dict[str, str]:
        return {"public_form_key": public_form_key}

    return app


def test_create_fastapi_app_uses_localhost_cors_defaults_in_local_env(monkeypatch) -> None:
    """Verify local env uses localhost defaults and exposes the CORS allow-list."""
    # LOCAL env uses the hard-coded dev origins to keep frontend integration
    # predictable during local development without requiring deployment vars.
    monkeypatch.setenv("ENV", "local")
    monkeypatch.delenv("ALLOW_ORIGIN_URL_AUTH", raising=False)

    app = _build_app()
    cors_kwargs = _cors_middleware_kwargs(app)

    assert cors_kwargs["allow_origins"] == [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://localhost:8000",
    ]


def test_create_fastapi_app_requires_explicit_cors_origins_outside_local(monkeypatch) -> None:
    """Require ALLOW_ORIGIN_URL_AUTH for non-local envs to avoid insecure defaults."""
    # In non-local environments, missing ALLOW_ORIGIN_URL_AUTH is a hard
    # deployment safety failure so credentials-based CORS never defaults.
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.delenv("ALLOW_ORIGIN_URL_AUTH", raising=False)

    with pytest.raises(ValueError, match="ALLOW_ORIGIN_URL_AUTH"):
        _build_app()


def test_create_fastapi_app_rejects_wildcard_cors_origin(monkeypatch) -> None:
    """Reject wildcard origins because credentialed CORS with sessions is unsafe."""
    # Wildcard origins are rejected because this API uses credentials and session
    # cookies, and '*' would create an insecure browser-authenticated surface.
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("ALLOW_ORIGIN_URL_AUTH", "*")

    with pytest.raises(ValueError, match="Wildcard CORS origins"):
        _build_app()


def test_create_fastapi_app_requires_session_secret(monkeypatch, mocker) -> None:
    """Fail app startup if the session secret is unavailable."""
    # Session middleware initialization must fail fast when secrets are missing so
    # startup cannot proceed with an insecure cookie key.
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("ALLOW_ORIGIN_URL_AUTH", "https://devapp.copilotgtm.com")

    import src.api.fastapi_app as fastapi_app

    mocker.patch.object(
        fastapi_app.security_secrets_config,
        "get_session_secret_sync",
        side_effect=RuntimeError("missing session secret"),
    )

    with pytest.raises(RuntimeError, match="missing session secret"):
        _build_app()


def test_public_form_cors_allows_configured_customer_origin_preflight(monkeypatch) -> None:
    """Allow CORS preflight when the public form key allows the request origin."""
    # Public forms with a configured origin must permit browser preflight so
    # embedded form iframes can complete the GET/POST flow reliably.
    app = _public_form_cors_app(monkeypatch, ["https://customer.example"])
    client = TestClient(app)

    response = client.options(
        "/public/v1/forms/lf_demo123",
        headers={
            "Origin": "https://customer.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://customer.example"
    assert response.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    assert response.headers["access-control-allow-headers"] == "content-type"


def test_public_form_cors_adds_headers_to_configured_customer_origin_response(
    monkeypatch,
) -> None:
    """Inject only allow-origin header for allowed origins on normal GET responses."""
    # For a real request from an allowed origin, we only expect an Allow-Origin
    # echo with minimal header injection to avoid changing unrelated response body
    # behavior.
    app = _public_form_cors_app(monkeypatch, ["https://customer.example"])
    client = TestClient(app)

    response = client.get(
        "/public/v1/forms/lf_demo123",
        headers={"Origin": "https://customer.example"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://customer.example"
    assert response.json() == {"public_form_key": "lf_demo123"}


def test_public_form_cors_blocks_unlisted_customer_origin_preflight(monkeypatch) -> None:
    """Return 403 on preflight when origin is not allow-listed for the form key."""
    # Origins not in the allow-list must fail at preflight and should not get any
    # reflective CORS header (browser then refuses the actual request).
    app = _public_form_cors_app(monkeypatch, ["https://customer.example"])
    client = TestClient(app)

    response = client.options(
        "/public/v1/forms/lf_demo123",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
