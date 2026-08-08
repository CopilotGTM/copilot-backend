"""Regression gate for authenticated and deliberately public user routes."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI
from src.api.dependencies.organisation_dependencies import get_current_user
from src.infra.auth.user_auth import user_auth, user_bearer

PUBLIC_USER_ROUTES = {
    ("POST", "/users/auth/login"),
    ("POST", "/users/auth/reset-password"),
    ("POST", "/users/auth/refresh-token"),
    ("POST", "/users/auth/forgot-password"),
    ("GET", "/users/integrations/copilot-oauth/callback"),
    ("POST", "/users/integrations/google/webhook"),
    ("POST", "/users/integrations/hubspot/webhook"),
    ("POST", "/users/integrations/microsoft/webhook"),
    ("POST", "/users/integrations/microsoft/webhook/lifecycle"),
    ("GET", "/users/bots/auth/validate-state"),
    ("POST", "/users/bots/auth/callback"),
    ("POST", "/users/bots/teams/messages"),
    ("POST", "/users/bots/slack/events"),
    ("POST", "/users/bots/slack/interactions"),
    ("GET", "/users/bots/slack/oauth/callback"),
    ("GET", "/users/bots/whatsapp/webhook"),
    ("POST", "/users/bots/whatsapp/webhook"),
    ("POST", "/users/bots/google-chat/events"),
    ("POST", "/users/meeting-bot/recall/webhook"),
}


def _effective_routes(app: FastAPI) -> Iterable[Any]:
    for route in app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_route_contexts):
            yield from effective_route_contexts()
        else:
            yield route


def _has_user_authentication(dependant: Any) -> bool:
    if getattr(dependant, "call", None) in {get_current_user, user_auth, user_bearer}:
        return True
    return any(_has_user_authentication(child) for child in getattr(dependant, "dependencies", ()))


def _public_user_routes(app: FastAPI) -> set[tuple[str, str]]:
    public_routes: set[tuple[str, str]] = set()
    for route in _effective_routes(app):
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", ())
        dependant = getattr(route, "dependant", None)
        if not path.startswith("/users/") or dependant is None:
            continue
        if not _has_user_authentication(dependant):
            public_routes.update((method, path) for method in methods)
    return public_routes


def test_authentication_audit_is_not_an_api_runtime_dependency() -> None:
    """Authentication regression analysis belongs in tests, not API startup."""
    from src.api import fastapi_app

    assert "route_security" not in inspect.getsource(fastapi_app)


def test_current_user_route_graph_matches_the_reviewed_public_allowlist(monkeypatch) -> None:
    """The deployed app may expose only the provider and auth ingress routes under review."""

    from src.api import fastapi_app
    from src.infra.utils import app_state

    monkeypatch.setenv("ENV", "local")
    monkeypatch.setattr(app_state, "fastapp", FastAPI())
    monkeypatch.setattr(
        fastapi_app.security_secrets_config,
        "get_session_secret_sync",
        lambda: "test-session-secret",
    )
    monkeypatch.setattr(fastapi_app, "instrument_fastapi_app", lambda _app: None)
    monkeypatch.setattr(fastapi_app, "_voice_realtime_api_enabled", lambda: False)

    app = fastapi_app.create_fastapi_app()

    assert _public_user_routes(app) == PUBLIC_USER_ROUTES
