"""Tests for custom OAuth routes in the integrations API."""

from fastapi.testclient import TestClient
from src.api.routers.integrations.copilot_oauth_routes import _provider_error_message
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
    assert body["data"]["attempt_id"] == "attempt-gmail"


def test_copilot_authorize_forwards_oauth_base_url(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    payload = {
        "integration_type": "salesforce",
        "oauth_base_url": "https://ameyo--gtmcopilot.sandbox.my.salesforce.com",
    }

    resp = client.post("/users/integrations/copilot-oauth/authorize", json=payload)

    assert resp.status_code == 200
    service = integrations_dependency_overrides["copilot_oauth_service"]
    assert service.last_initiate_kwargs["integration_type"].value == "salesforce"
    assert (
        service.last_initiate_kwargs["oauth_base_url"]
        == "https://ameyo--gtmcopilot.sandbox.my.salesforce.com"
    )


def test_provider_error_message_is_bounded() -> None:
    message = _provider_error_message("invalid_request\n", "\t" + "x" * 1000)

    assert len(message) == 300
    assert message.startswith("invalid_request: ")
    assert "\n" not in message
    assert "\t" not in message


def test_copilot_authorize_rejects_oauth_base_url_for_non_salesforce(
    client: TestClient,
) -> None:
    payload = {
        "integration_type": "gmail",
        "oauth_base_url": "https://accounts.google.com",
    }

    resp = client.post("/users/integrations/copilot-oauth/authorize", json=payload)

    assert resp.status_code == 422, resp.text
    assert "oauth_base_url is only supported for Salesforce integrations" in resp.text


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
    assert "attempt_id=test-connection" in location


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
    assert "attempt_id=" not in location


def test_copilot_callback_provider_error_returns_popup_completion_html(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Provider-denied callbacks should close the popup with a useful error."""
    resp = client.get(
        "/users/integrations/copilot-oauth/callback"
        "?error=access_denied&error_description=user%20denied&state=valid-state",
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert "/integrations/oauth/popup-complete" in location
    assert "success=false" in location
    assert "access_denied%3A+user+denied" in location
    assert "attempt_id=attempt-from-valid-state" in location


def test_copilot_callback_missing_code_returns_popup_completion_html(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Direct callback hits should not expose FastAPI validation JSON in the popup."""
    resp = client.get(
        "/users/integrations/copilot-oauth/callback?state=valid-state",
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert "/integrations/oauth/popup-complete" in location
    assert "success=false" in location
    assert "OAuth+callback+did+not+include+an+authorization+code" in location
    assert "attempt_id=attempt-from-valid-state" in location


def test_copilot_callback_error_includes_attempt_id_when_state_is_signed(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Recoverable callback failures should still identify the active popup attempt."""
    integrations_dependency_overrides["copilot_callback_service"].should_raise_value_error = True

    resp = client.get(
        "/users/integrations/copilot-oauth/callback?code=abc&state=valid-state",
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert "/integrations/oauth/popup-complete" in location
    assert "success=false" in location
    assert "attempt_id=attempt-from-valid-state" in location


def test_copilot_callback_preserves_backend_error_message(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Callback redirect should carry the backend rejection message through to the popup page."""
    callback_service = integrations_dependency_overrides["copilot_callback_service"]
    callback_service.should_raise_value_error = True
    callback_service.value_error_message = (
        "Gmail permission was not granted. Please reconnect and allow Gmail access."
    )

    resp = client.get(
        "/users/integrations/copilot-oauth/callback?code=abc&state=xyz",
        follow_redirects=False,
    )

    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert "/integrations/oauth/popup-complete" in location
    assert "success=false" in location
    assert "Gmail+permission+was+not+granted." in location
