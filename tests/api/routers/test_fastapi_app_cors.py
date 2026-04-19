from __future__ import annotations

import importlib

import pytest
from fastapi.middleware.cors import CORSMiddleware


def _build_app():
    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)
    return fastapi_app.create_fastapi_app()


def _cors_middleware_kwargs(app) -> dict:
    middleware = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    return middleware.kwargs


def test_create_fastapi_app_uses_localhost_cors_defaults_in_local_env(monkeypatch) -> None:
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
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.delenv("ALLOW_ORIGIN_URL_AUTH", raising=False)

    with pytest.raises(ValueError, match="ALLOW_ORIGIN_URL_AUTH"):
        _build_app()


def test_create_fastapi_app_rejects_wildcard_cors_origin(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("ALLOW_ORIGIN_URL_AUTH", "*")

    with pytest.raises(ValueError, match="Wildcard CORS origins"):
        _build_app()


def test_create_fastapi_app_requires_session_secret(monkeypatch, mocker) -> None:
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
