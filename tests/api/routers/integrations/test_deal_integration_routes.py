"""Tests for deal integration routes in the integrations API."""

from uuid import uuid4

from fastapi.testclient import TestClient


def test_deal_available_no_connections(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Listing available deal integrations returns no connected providers by default."""
    deal_id = uuid4()

    resp = client.get(f"/users/integrations/deals/{deal_id}/available")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["total_connected"] == 0


def test_deal_available_with_connected_slack(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Slack appears as connected when the user has an active Slack integration."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()

    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            name="Revenue Slack",
            is_active=True,
        )
    )

    resp = client.get(f"/users/integrations/deals/{deal_id}/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    slack = next(
        (item for item in data["integrations"] if item["integration_type"] == "slack"),
        None,
    )
    assert slack is not None
    assert slack["is_connected"] is True
    assert len(slack["connected_integrations"]) == 1


def test_deal_available_groups_linked_channels_by_type(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Deal availability groups linked channels under their provider type."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    slack_integration_id = uuid4()

    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=slack_integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            name="Revenue Slack",
            is_active=True,
        )
    )
    integration_service.deal_channels[str(deal_id)] = [
        {
            "channel_id": uuid4(),
            "external_channel_id": "T123:C456",
            "name": "deal-room",
            "type": "private",
            "integration_id": slack_integration_id,
            "integration_type": "slack",
            "integration_name": "Revenue Slack",
            "parent_name": "Revenue Workspace",
        }
    ]

    resp = client.get(f"/users/integrations/deals/{deal_id}/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    slack = next(
        (item for item in data["integrations"] if item["integration_type"] == "slack"),
        None,
    )
    assert slack is not None
    assert len(slack["linked_channels"]) == 1
    assert slack["linked_channels"][0]["name"] == "deal-room"


def test_link_channel_success(client: TestClient, integrations_dependency_overrides) -> None:
    """A synced messaging channel can be linked to a deal."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    integration_id = uuid4()

    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            name="Revenue Slack",
            is_active=True,
        )
    )

    payload = {
        "id": "T123:C456",
        "name": "deal-room",
        "type": "private",
        "integration_id": str(integration_id),
        "parent_id": "T123",
        "parent_name": "Revenue Workspace",
    }

    resp = client.post(f"/users/integrations/deals/{deal_id}/channels", json=payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Channel linked successfully"
    data = body["data"]
    assert data["external_channel_id"] == "T123:C456"
    assert data["name"] == "deal-room"
    assert str(data["deal_id"]) == str(deal_id)


def test_link_channel_success_without_integration_id(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Older clients can still link channels without sending integration_id."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    integration_id = uuid4()

    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            name="Revenue Slack",
            is_active=True,
        )
    )

    payload = {
        "id": "T123:C456",
        "name": "deal-room",
        "type": "private",
        "parent_id": "T123",
        "parent_name": "Revenue Workspace",
    }

    resp = client.post(f"/users/integrations/deals/{deal_id}/channels", json=payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["integration_id"] == str(integration_id)


def test_link_channel_missing_fields(client: TestClient, integrations_dependency_overrides) -> None:
    """Validation errors are returned for malformed channel-link payloads."""
    deal_id = uuid4()

    resp = client.post(
        f"/users/integrations/deals/{deal_id}/channels",
        json={"id": "T123:C456"},
    )

    assert resp.status_code == 422, resp.text


def test_link_channel_invalid_integration_id(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Integration IDs must be valid UUIDs."""
    deal_id = uuid4()
    payload = {
        "id": "T123:C456",
        "name": "deal-room",
        "type": "private",
        "integration_id": "not-a-uuid",
    }

    resp = client.post(f"/users/integrations/deals/{deal_id}/channels", json=payload)

    assert resp.status_code == 422, resp.text


def test_remove_channel_success(client: TestClient, integrations_dependency_overrides) -> None:
    """A linked channel can be removed from a deal."""
    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    channel_id = uuid4()

    integration_service.deal_channels[str(deal_id)] = [
        {
            "channel_id": channel_id,
            "external_channel_id": "T123:C456",
            "name": "deal-room",
            "type": "private",
            "integration_id": uuid4(),
        }
    ]

    resp = client.delete(f"/users/integrations/deals/{deal_id}/channels/{channel_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "removed" in body["message"].lower()


def test_remove_channel_not_found(client: TestClient, integrations_dependency_overrides) -> None:
    """Removing a missing link returns 404."""
    resp = client.delete(f"/users/integrations/deals/{uuid4()}/channels/{uuid4()}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["status"] == "ERROR"


def test_remove_channel_wrong_deal(client: TestClient, integrations_dependency_overrides) -> None:
    """Channel links are scoped to the deal they were created under."""
    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id_1 = uuid4()
    deal_id_2 = uuid4()
    channel_id = uuid4()

    integration_service.deal_channels[str(deal_id_1)] = [
        {
            "channel_id": channel_id,
            "external_channel_id": "T123:C456",
            "name": "deal-room",
            "type": "private",
            "integration_id": uuid4(),
        }
    ]

    resp = client.delete(f"/users/integrations/deals/{deal_id_2}/channels/{channel_id}")

    assert resp.status_code == 404, resp.text


def test_list_user_channels_for_slack(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Channel listing resolves by integration type, not integration_id."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    integration_id = uuid4()

    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            is_active=True,
        )
    )
    integration_service.add_channels(
        integration_id,
        [
            {
                "id": "T123:C111",
                "name": "general",
                "type": "public",
                "parent_name": "Revenue Workspace",
                "member_count": 8,
            },
            {
                "id": "T123:C222",
                "name": "deal-room",
                "type": "private",
                "parent_name": "Revenue Workspace",
                "member_count": 5,
            },
        ],
    )

    resp = client.get(f"/users/integrations/deals/{deal_id}/channels/slack")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["total"] == 2
    assert {channel["id"] for channel in data["channels"]} == {"T123:C111", "T123:C222"}


def test_list_user_channels_excludes_already_linked(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Already-linked channels are filtered out for the current deal."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    integration_id = uuid4()

    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="teams",
            is_active=True,
        )
    )
    integration_service.add_channels(
        integration_id,
        [
            {"id": "team-1:chan-1", "name": "General", "type": "public"},
            {"id": "team-1:chan-2", "name": "Sales", "type": "public"},
            {"id": "team-1:chan-3", "name": "Marketing", "type": "public"},
        ],
    )
    integration_service.deal_channels[str(deal_id)] = [
        {
            "channel_id": uuid4(),
            "external_channel_id": "team-1:chan-1",
            "name": "General",
            "type": "public",
            "integration_id": integration_id,
            "integration_type": "teams",
        }
    ]

    resp = client.get(f"/users/integrations/deals/{deal_id}/channels/teams")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 2
    assert {channel["id"] for channel in data["channels"]} == {"team-1:chan-2", "team-1:chan-3"}


def test_list_user_channels_requires_connected_integration(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Listing channels fails when the user has no active integration of that type."""
    resp = client.get(f"/users/integrations/deals/{uuid4()}/channels/slack")

    assert resp.status_code == 404, resp.text
    assert resp.json()["status"] == "ERROR"


def test_list_user_channels_empty(client: TestClient, integrations_dependency_overrides) -> None:
    """Connected integrations with no synced channels return an empty list."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    deal_id = uuid4()
    integration_id = uuid4()

    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            is_active=True,
        )
    )

    resp = client.get(f"/users/integrations/deals/{deal_id}/channels/slack")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["channels"] == []
