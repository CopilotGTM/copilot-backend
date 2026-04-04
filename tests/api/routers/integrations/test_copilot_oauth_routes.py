"""Tests for custom OAuth routes in the integrations API."""

from fastapi.testclient import TestClient
from src.infra.config.constant import IntegrationErrors


def test_copilot_authorize_success(client: TestClient, integrations_dependency_overrides) -> None:
    """Custom OAuth authorize returns an auth URL for supported integrations."""
    payload = {"integration_type": "gmail"}
    resp = client.post("/users/integrations/copilot-oauth/authorize", json=payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Authorization URL generated successfully."
    assert body["data"]["auth_url"] == "https://accounts.example.com/oauth/gmail"


def test_copilot_authorize_unavailable_integration(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Custom OAuth authorize rejects integrations that are not available."""
    integrations_dependency_overrides["copilot_oauth_service"].should_raise_unavailable = True

    payload = {"integration_type": "gmail"}
    resp = client.post("/users/integrations/copilot-oauth/authorize", json=payload)

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert body["message"] == IntegrationErrors.INTEGRATION_NOT_AVAILABLE


def test_copilot_authorize_invalid_provider(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Custom OAuth authorize returns a bad request for unsupported providers."""
    integrations_dependency_overrides["copilot_oauth_service"].should_raise_value_error = True

    payload = {"integration_type": "gmail"}
    resp = client.post("/users/integrations/copilot-oauth/authorize", json=payload)

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "No custom integration provider found" in body["message"]


def test_copilot_callback_returns_popup_completion_html(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Custom OAuth callback should redirect popup to the frontend bridge page."""
    resp = client.get(
        "/users/integrations/copilot-oauth/callback?code=abc&state=xyz",
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert "/integrations/oauth/popup-complete" in location
    assert "success=true" in location
    assert "integration_id=test-integration" in location
    assert "integration_type=" in location


def test_copilot_callback_error_returns_popup_completion_html(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Custom OAuth callback should redirect popup to the frontend bridge page on errors too."""
    integrations_dependency_overrides["copilot_callback_service"].should_raise_value_error = True

    resp = client.get(
        "/users/integrations/copilot-oauth/callback?code=abc&state=xyz",
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert "/integrations/oauth/popup-complete" in location
    assert "success=false" in location
    assert "Invalid+or+expired+state+parameter" in location
