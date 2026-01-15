"""
Tests for OAuth routes in the integrations API.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

# =============================================================================
# POST /oauth/authorize Tests
# =============================================================================


def test_authorize_success(client: TestClient, integrations_dependency_overrides):
    """Test successful OAuth authorization initiation."""
    payload = {"integration_type": "teams"}
    resp = client.post("/users/integrations/oauth/authorize", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Connect session created successfully."
    data = body["data"]
    assert "session_token" in data
    assert data["integration_type"] == "teams"


def test_authorize_with_integration_name(client: TestClient, integrations_dependency_overrides):
    """Test OAuth authorization with custom integration name."""
    payload = {
        "integration_type": "slack",
        "integration_name": "My Slack Workspace",
    }
    resp = client.post("/users/integrations/oauth/authorize", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"


def test_authorize_invalid_integration_type(client: TestClient, integrations_dependency_overrides):
    """Test OAuth authorization with invalid integration type."""
    payload = {"integration_type": "invalid_type"}
    resp = client.post("/users/integrations/oauth/authorize", json=payload)
    assert resp.status_code == 422, resp.text


def test_authorize_unavailable_integration(client: TestClient, integrations_dependency_overrides):
    """Test OAuth authorization for unavailable integration."""
    # Configure the fake service to raise unavailable error
    integrations_dependency_overrides["oauth_service"].should_raise_unavailable = True

    payload = {"integration_type": "teams"}
    resp = client.post("/users/integrations/oauth/authorize", json=payload)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_authorize_nango_error_400(client: TestClient, integrations_dependency_overrides):
    """Test OAuth authorization when Nango returns 400 error."""
    oauth_service = integrations_dependency_overrides["oauth_service"]
    oauth_service.should_raise_nango_error = True
    oauth_service.nango_error_status = 400
    oauth_service.nango_error_payload = {"error": {"message": "Provider not configured"}}

    payload = {"integration_type": "teams"}
    resp = client.post("/users/integrations/oauth/authorize", json=payload)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_authorize_nango_error_500(client: TestClient, integrations_dependency_overrides):
    """Test OAuth authorization when Nango returns 500 error."""
    oauth_service = integrations_dependency_overrides["oauth_service"]
    oauth_service.should_raise_nango_error = True
    oauth_service.nango_error_status = 500
    oauth_service.nango_error_payload = None

    payload = {"integration_type": "teams"}
    resp = client.post("/users/integrations/oauth/authorize", json=payload)
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# =============================================================================
# DELETE /oauth/disconnect/{integration_id} Tests
# =============================================================================


def test_disconnect_success(client: TestClient, integrations_dependency_overrides):
    """Test successful integration disconnection."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        is_active=True,
        connection_id="conn_123",
    )
    integration_service.add_integration(integration)

    resp = client.delete(f"/users/integrations/oauth/disconnect/{integration_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "disconnected" in body["message"].lower()


def test_disconnect_not_found(client: TestClient, integrations_dependency_overrides):
    """Test disconnecting non-existent integration."""
    integration_id = uuid4()
    resp = client.delete(f"/users/integrations/oauth/disconnect/{integration_id}")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_disconnect_already_inactive(client: TestClient, integrations_dependency_overrides):
    """Test disconnecting already inactive integration."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        is_active=False,  # Already inactive
        connection_id="conn_123",
    )
    integration_service.add_integration(integration)

    resp = client.delete(f"/users/integrations/oauth/disconnect/{integration_id}")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_disconnect_no_connection_id(client: TestClient, integrations_dependency_overrides):
    """Test disconnecting integration without connection_id."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        is_active=True,
        connection_id=None,  # No connection ID
    )
    integration_service.add_integration(integration)

    resp = client.delete(f"/users/integrations/oauth/disconnect/{integration_id}")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


# =============================================================================
# POST /oauth/webhooks/nango Tests
# =============================================================================


def test_webhook_auth_success(client: TestClient, integrations_dependency_overrides):
    """Test successful auth webhook processing."""
    payload = {
        "type": "auth",
        "success": True,
        "connectionId": "conn_123",
        "providerConfigKey": "teams",
    }
    headers = {"X-Nango-Signature": "valid_signature"}
    resp = client.post(
        "/users/integrations/oauth/webhooks/nango",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def test_webhook_invalid_signature(client: TestClient, integrations_dependency_overrides):
    """Test webhook with invalid signature."""
    webhook_service = integrations_dependency_overrides["webhook_service"]
    webhook_service.valid_signature = False

    payload = {
        "type": "auth",
        "success": True,
        "connectionId": "conn_123",
    }
    headers = {"X-Nango-Signature": "invalid_signature"}
    resp = client.post(
        "/users/integrations/oauth/webhooks/nango",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 401, resp.text


def test_webhook_missing_signature(client: TestClient, integrations_dependency_overrides):
    """Test webhook without signature header."""
    webhook_service = integrations_dependency_overrides["webhook_service"]
    webhook_service.valid_signature = False

    payload = {
        "type": "auth",
        "success": True,
        "connectionId": "conn_123",
    }
    # No X-Nango-Signature header
    resp = client.post("/users/integrations/oauth/webhooks/nango", json=payload)
    assert resp.status_code == 401, resp.text


def test_webhook_unsuccessful_payload(client: TestClient, integrations_dependency_overrides):
    """Test webhook with unsuccessful payload."""
    payload = {
        "type": "auth",
        "success": False,  # Unsuccessful
        "error": "Authorization failed",
    }
    headers = {"X-Nango-Signature": "valid_signature"}
    resp = client.post(
        "/users/integrations/oauth/webhooks/nango",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


def test_webhook_sync_type(client: TestClient, integrations_dependency_overrides):
    """Test sync webhook type is handled at app level."""
    payload = {
        "type": "sync",
        "success": True,
        "syncId": "sync_123",
    }
    headers = {"X-Nango-Signature": "valid_signature"}
    resp = client.post(
        "/users/integrations/oauth/webhooks/nango",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def test_webhook_unknown_type(client: TestClient, integrations_dependency_overrides):
    """Test webhook with unknown type."""
    payload = {
        "type": "unknown_type",
        "success": True,
    }
    headers = {"X-Nango-Signature": "valid_signature"}
    resp = client.post(
        "/users/integrations/oauth/webhooks/nango",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
