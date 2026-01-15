"""
Tests for integration listing routes in the integrations API.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

# =============================================================================
# GET /available Tests
# =============================================================================


def test_list_available_no_connections(client: TestClient, integrations_dependency_overrides):
    """Test listing available integrations with no connections."""
    resp = client.get("/users/integrations/available")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Available integrations retrieved successfully"
    data = body["data"]
    assert "integrations" in data
    assert "total_available" in data
    assert "total_connected" in data
    assert data["total_connected"] == 0
    # All integrations should have is_connected=False
    for integration in data["integrations"]:
        assert integration["is_connected"] is False


def test_list_available_with_connections(client: TestClient, integrations_dependency_overrides):
    """Test listing available integrations with some connected."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]

    # Add a connected Teams integration
    integration_id = uuid4()
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        integration_type="teams",
        name="Company Teams",
        is_active=True,
    )
    integration_service.add_integration(integration)

    resp = client.get("/users/integrations/available")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["total_connected"] == 1

    # Find teams integration in the list
    teams_integration = next(
        (i for i in data["integrations"] if i["integration_type"] == "teams"),
        None,
    )
    assert teams_integration is not None
    assert teams_integration["is_connected"] is True
    assert len(teams_integration["connected_integrations"]) == 1


def test_list_available_multiple_same_type(client: TestClient, integrations_dependency_overrides):
    """Test listing with multiple connections of same type."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]

    # Add multiple Slack integrations
    for i in range(3):
        integration = FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            name=f"Slack Workspace {i + 1}",
            is_active=True,
        )
        integration_service.add_integration(integration)

    resp = client.get("/users/integrations/available")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["total_connected"] == 3

    # Find slack integration
    slack_integration = next(
        (i for i in data["integrations"] if i["integration_type"] == "slack"),
        None,
    )
    assert slack_integration is not None
    assert slack_integration["is_connected"] is True
    assert len(slack_integration["connected_integrations"]) == 3


def test_list_available_mixed_active_inactive(
    client: TestClient, integrations_dependency_overrides
):
    """Test that only active integrations are counted."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]

    # Add active integration
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="teams",
            name="Active Teams",
            is_active=True,
        )
    )

    # Add inactive integration
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            name="Inactive Slack",
            is_active=False,
        )
    )

    resp = client.get("/users/integrations/available")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    # Only active integration should be counted
    assert data["total_connected"] == 1
