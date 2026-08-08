from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


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

    schema_paths = set(app.openapi()["paths"])

    assert "/public/v1/forms/{public_form_key}" in schema_paths
    assert "/public/v1/forms/{public_form_key}/submissions" in schema_paths
    assert "/private/v1/forms/submissions" in schema_paths
    assert "/private/v1/crm/leads" in schema_paths

    assert "/public/forms/{public_form_key}" not in schema_paths
    assert "/private/forms/submissions" not in schema_paths

    client = TestClient(app)

    assert client.get("/public/v1/forms/test-form").status_code != 404
    assert client.post("/public/v1/forms/test-form/submissions", json={}).status_code != 404
    assert client.post("/private/v1/forms/submissions", json={}).status_code != 404
    assert client.post("/private/v1/crm/leads", json={}).status_code != 404

    assert client.get("/public/forms/test-form").status_code == 404
    assert client.post("/private/forms/submissions", json={}).status_code == 404


def test_user_agent_definition_routes_are_versioned(monkeypatch, mocker) -> None:
    app = _build_app(monkeypatch, mocker)
    paths = app.openapi()["paths"]

    assert set(paths["/users/v1/agents"]) >= {"get", "post"}
    assert set(paths["/users/v1/agents/{agent_id}"]) >= {"get", "put", "delete"}
    assert "/users/v1/agent-capability-catalog" in paths
    assert "/users/v1/agents/{agent_id}/capabilities" in paths
    assert set(paths["/users/v1/agent-templates"]) == {"get"}
    assert set(paths["/users/v1/agent-templates/{template_id}"]) == {"get"}
    assert set(paths["/users/v1/agent-templates/{template_id}/use"]) == {"post"}
    assert "Location" in paths["/users/v1/agents"]["post"]["responses"]["201"]["headers"]
    assert (
        "Location"
        in paths["/users/v1/agent-templates/{template_id}/use"]["post"]["responses"]["201"][
            "headers"
        ]
    )
    assert "409" in paths["/users/v1/agents/{agent_id}"]["put"]["responses"]
    assert "409" in paths["/users/v1/agents/{agent_id}"]["delete"]["responses"]

    # The reminder-backed scheduled-agent surface remains separate during migration.
    assert "/users/agents" in paths


def test_user_agent_trigger_routes_are_versioned(monkeypatch, mocker) -> None:
    app = _build_app(monkeypatch, mocker)
    paths = app.openapi()["paths"]

    assert "/users/v1/automation-trigger-catalog" in paths
    assert set(paths["/users/v1/automation-trigger-options/search"]) == {"post"}
    assert set(paths["/users/v1/automation-trigger-options/resolve"]) == {"post"}
    assert set(paths["/users/v1/agents/{agent_id}/triggers"]) >= {"get", "post"}
    assert set(paths["/users/v1/agents/{agent_id}/triggers/{trigger_id}"]) >= {
        "get",
        "put",
        "delete",
    }
    assert "/users/v1/agents/{agent_id}/triggers/{trigger_id}/refresh-target" in paths


def test_user_agent_run_routes_are_versioned(monkeypatch, mocker) -> None:
    app = _build_app(monkeypatch, mocker)
    paths = app.openapi()["paths"]

    assert "/users/v1/agent-runs" in paths
    assert "/users/v1/agent-runs/{run_id}" in paths
