from __future__ import annotations

import importlib


def _build_app(monkeypatch, mocker):
    monkeypatch.setenv("ENV", "local")

    import src.api.fastapi_app as fastapi_app
    import src.infra.utils.app_state as app_state

    importlib.reload(app_state)
    importlib.reload(fastapi_app)

    mocker.patch.object(
        fastapi_app.security_secrets_config,
        "get_session_secret_sync",
        return_value="test-session-secret",
    )
    mocker.patch.object(fastapi_app, "instrument_fastapi_app", return_value=None)

    return fastapi_app.create_fastapi_app()


def test_external_public_and_private_ingress_routes_are_versioned(monkeypatch, mocker) -> None:
    """Third-party integration contracts should expose canonical versioned paths."""
    app = _build_app(monkeypatch, mocker)

    runtime_paths = {route.path for route in app.routes}
    schema_paths = set(app.openapi()["paths"])

    assert "/public/v1/forms/{public_form_key}" in runtime_paths
    assert "/public/v1/forms/{public_form_key}/submissions" in runtime_paths
    assert "/private/v1/forms/submissions" in runtime_paths
    assert "/private/v1/crm/leads" in runtime_paths

    assert "/public/forms/{public_form_key}" not in runtime_paths
    assert "/private/forms/submissions" not in runtime_paths

    assert "/public/v1/forms/{public_form_key}" in schema_paths
    assert "/public/v1/forms/{public_form_key}/submissions" in schema_paths
    assert "/private/v1/forms/submissions" in schema_paths
    assert "/private/v1/crm/leads" in schema_paths

    assert "/public/forms/{public_form_key}" not in schema_paths
    assert "/private/forms/submissions" not in schema_paths
