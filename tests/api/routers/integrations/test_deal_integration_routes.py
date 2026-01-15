"""
Tests for deal integration routes in the integrations API.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

# =============================================================================
# GET /deals/{deal_id}/available Tests
# =============================================================================


def test_deal_available_no_connections(client: TestClient, integrations_dependency_overrides):
    """Test listing available deal integrations with no connections."""
    deal_id = uuid4()
    resp = client.get(f"/users/integrations/deals/{deal_id}/available")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert "integrations" in data
    assert data["total_connected"] == 0


def test_deal_available_with_connections(client: TestClient, integrations_dependency_overrides):
    """Test listing available deal integrations with Teams connected."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()

    # Add Teams integration
    integration = FakeKnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        integration_type="teams",
        name="Company Teams",
        is_active=True,
    )
    integration_service.add_integration(integration)

    resp = client.get(f"/users/integrations/deals/{deal_id}/available")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["total_connected"] == 1

    # Teams should be shown as connected
    teams = next(
        (i for i in data["integrations"] if i["integration_type"] == "teams"),
        None,
    )
    assert teams is not None
    assert teams["is_connected"] is True


def test_deal_available_with_linked_channels(client: TestClient, integrations_dependency_overrides):
    """Test listing deal integrations shows linked channels."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    integration_id = uuid4()

    # Add Teams integration
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        integration_type="teams",
        name="Company Teams",
        is_active=True,
    )
    integration_service.add_integration(integration)

    # Add linked channel
    channel_id = uuid4()
    integration_service.deal_channels[str(deal_id)] = [
        {
            "channel_id": channel_id,
            "external_channel_id": "ch_123",
            "name": "Sales Channel",
            "type": "channel",
            "integration_id": integration_id,
            "integration_type": "teams",
            "integration_name": "Company Teams",
            "parent_name": "Sales Team",
        }
    ]

    resp = client.get(f"/users/integrations/deals/{deal_id}/available")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]

    # Find teams integration
    teams = next(
        (i for i in data["integrations"] if i["integration_type"] == "teams"),
        None,
    )
    assert teams is not None
    assert len(teams["linked_channels"]) == 1
    assert teams["linked_channels"][0]["name"] == "Sales Channel"


# =============================================================================
# POST /deals/{deal_id}/channels Tests
# =============================================================================


def test_link_channel_success(client: TestClient, integrations_dependency_overrides):
    """Test successfully linking a channel to a deal."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    integration_id = uuid4()

    # Add integration
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        integration_type="teams",
        is_active=True,
    )
    integration_service.add_integration(integration)

    payload = {
        "id": "external_ch_123",
        "name": "Sales Channel",
        "type": "channel",
        "integration_id": str(integration_id),
        "parent_id": "team_123",
        "parent_name": "Sales Team",
    }
    resp = client.post(f"/users/integrations/deals/{deal_id}/channels", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Channel linked successfully"
    data = body["data"]
    assert "channel_id" in data
    assert data["name"] == "Sales Channel"
    assert str(data["deal_id"]) == str(deal_id)


def test_link_channel_missing_fields(client: TestClient, integrations_dependency_overrides):
    """Test linking channel with missing required fields."""
    deal_id = uuid4()
    payload = {
        "id": "ch_123",
        # Missing name, type, integration_id
    }
    resp = client.post(f"/users/integrations/deals/{deal_id}/channels", json=payload)
    assert resp.status_code == 422, resp.text


def test_link_channel_invalid_integration_id(client: TestClient, integrations_dependency_overrides):
    """Test linking channel with invalid integration_id format."""
    deal_id = uuid4()
    payload = {
        "id": "ch_123",
        "name": "Test Channel",
        "type": "channel",
        "integration_id": "not-a-uuid",
    }
    resp = client.post(f"/users/integrations/deals/{deal_id}/channels", json=payload)
    assert resp.status_code == 422, resp.text


# =============================================================================
# DELETE /deals/{deal_id}/channels/{channel_id} Tests
# =============================================================================


def test_remove_channel_success(client: TestClient, integrations_dependency_overrides):
    """Test successfully removing a channel from a deal."""
    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    channel_id = uuid4()

    # Add linked channel
    integration_service.deal_channels[str(deal_id)] = [
        {
            "channel_id": channel_id,
            "external_channel_id": "ch_123",
            "name": "Sales Channel",
            "type": "channel",
            "integration_id": uuid4(),
        }
    ]

    resp = client.delete(f"/users/integrations/deals/{deal_id}/channels/{channel_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "removed" in body["message"].lower()


def test_remove_channel_not_found(client: TestClient, integrations_dependency_overrides):
    """Test removing non-existent channel link."""
    deal_id = uuid4()
    channel_id = uuid4()

    resp = client.delete(f"/users/integrations/deals/{deal_id}/channels/{channel_id}")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_remove_channel_wrong_deal(client: TestClient, integrations_dependency_overrides):
    """Test removing channel from wrong deal."""
    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id_1 = uuid4()
    deal_id_2 = uuid4()
    channel_id = uuid4()

    # Add channel to deal_1
    integration_service.deal_channels[str(deal_id_1)] = [
        {
            "channel_id": channel_id,
            "external_channel_id": "ch_123",
            "name": "Sales Channel",
            "type": "channel",
            "integration_id": uuid4(),
        }
    ]

    # Try to remove from deal_2
    resp = client.delete(f"/users/integrations/deals/{deal_id_2}/channels/{channel_id}")
    assert resp.status_code == 404, resp.text


# =============================================================================
# GET /deals/{integration_id}/channels Tests
# =============================================================================


def test_list_channels_success(client: TestClient, integrations_dependency_overrides):
    """Test listing channels for an integration."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()

    # Add integration
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        integration_type="teams",
        is_active=True,
    )
    integration_service.add_integration(integration)

    # Add channels
    integration_service.add_channels(
        integration_id,
        [
            {"id": "ch_1", "name": "General", "type": "channel", "parent_name": "Engineering"},
            {"id": "ch_2", "name": "Sales", "type": "channel", "parent_name": "Sales Team"},
            {"id": "ch_3", "name": "Marketing", "type": "channel", "parent_name": "Marketing"},
        ],
    )

    resp = client.get(f"/users/integrations/deals/{integration_id}/channels")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["total"] == 3
    assert len(data["channels"]) == 3


def test_list_channels_with_deal_filter(client: TestClient, integrations_dependency_overrides):
    """Test listing channels filters out already linked ones."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    deal_id = uuid4()

    # Add integration
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        integration_type="teams",
        is_active=True,
    )
    integration_service.add_integration(integration)

    # Add channels
    integration_service.add_channels(
        integration_id,
        [
            {"id": "ch_1", "name": "General", "type": "channel"},
            {"id": "ch_2", "name": "Sales", "type": "channel"},
            {"id": "ch_3", "name": "Marketing", "type": "channel"},
        ],
    )

    # Link one channel to the deal
    integration_service.deal_channels[str(deal_id)] = [
        {
            "channel_id": uuid4(),
            "external_channel_id": "ch_1",  # This one is linked
            "name": "General",
            "type": "channel",
            "integration_id": integration_id,
        }
    ]

    # List with deal_id filter
    resp = client.get(
        f"/users/integrations/deals/{integration_id}/channels",
        params={"deal_id": str(deal_id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    # Should only return 2 channels (ch_1 is filtered out)
    assert data["total"] == 2
    channel_ids = [ch["id"] for ch in data["channels"]]
    assert "ch_1" not in channel_ids
    assert "ch_2" in channel_ids
    assert "ch_3" in channel_ids


def test_list_channels_integration_not_found(client: TestClient, integrations_dependency_overrides):
    """Test listing channels for non-existent integration."""
    integration_id = uuid4()
    resp = client.get(f"/users/integrations/deals/{integration_id}/channels")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


def test_list_channels_empty(client: TestClient, integrations_dependency_overrides):
    """Test listing channels when integration has none."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()

    # Add integration with no channels
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
        integration_type="teams",
        is_active=True,
    )
    integration_service.add_integration(integration)

    resp = client.get(f"/users/integrations/deals/{integration_id}/channels")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["total"] == 0
    assert data["channels"] == []
