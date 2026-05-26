"""
Tests for integration listing routes in the integrations API.
"""

from uuid import uuid4

from fastapi.testclient import TestClient

# These endpoint tests focus on /users/integrations/available because listing logic
# drives both connected/inactive state and action-required scope guidance.


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


def test_list_available_salesforce_includes_admin_prerequisite_when_install_url_configured(
    client: TestClient, integrations_dependency_overrides, monkeypatch
) -> None:
    """Salesforce advertises the admin package install prerequisite from backend config."""
    from src.infra.config.configs import salesforce_integration_config

    install_url = "https://test.salesforce.com/packaging/installPackage.apexp?p0=04td"
    monkeypatch.setattr(
        salesforce_integration_config,
        "EXTERNAL_CLIENT_APP_INSTALL_URL",
        install_url,
        raising=False,
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    salesforce = next(
        (i for i in data["integrations"] if i["integration_type"] == "salesforce"),
        None,
    )
    assert salesforce is not None
    assert salesforce["admin_prerequisite"] == {
        "type": "install_url",
        "required_before_connect": True,
        "url": install_url,
        "label": "Install CopilotGTM in Salesforce",
    }


def test_list_available_salesforce_admin_prerequisite_is_null_when_install_url_absent(
    client: TestClient, integrations_dependency_overrides, monkeypatch
) -> None:
    """Salesforce keeps the generic field null until the package URL is configured."""
    from src.infra.config.configs import salesforce_integration_config

    monkeypatch.setattr(
        salesforce_integration_config,
        "EXTERNAL_CLIENT_APP_INSTALL_URL",
        "",
        raising=False,
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    salesforce = next(
        (i for i in data["integrations"] if i["integration_type"] == "salesforce"),
        None,
    )
    assert salesforce is not None
    assert salesforce["admin_prerequisite"] is None


def test_list_available_non_salesforce_admin_prerequisite_is_null(
    client: TestClient, integrations_dependency_overrides, monkeypatch
) -> None:
    """Provider prerequisites stay generic and opt-in per provider."""
    from src.infra.config.configs import salesforce_integration_config

    monkeypatch.setattr(
        salesforce_integration_config,
        "EXTERNAL_CLIENT_APP_INSTALL_URL",
        "https://test.salesforce.com/packaging/installPackage.apexp?p0=04td",
        raising=False,
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    non_salesforce = next(
        (i for i in data["integrations"] if i["integration_type"] != "salesforce"),
        None,
    )
    assert non_salesforce is not None
    assert non_salesforce["admin_prerequisite"] is None


def test_list_available_with_connections(client: TestClient, integrations_dependency_overrides):
    """Test listing available integrations with some connected."""
    # Contract: connected integrations appear as connected=true with instance details.
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    current_user = integrations_dependency_overrides["user_ids"]

    # Add a connected Teams integration
    integration_id = uuid4()
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=current_user.organisation_id,
        created_by_user_id=current_user.user_id,
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
    # Contract: multiple integrations of same type collapse into one type row with count.
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    current_user = integrations_dependency_overrides["user_ids"]

    # Add multiple Slack integrations
    for i in range(3):
        integration = FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
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
    current_user = integrations_dependency_overrides["user_ids"]

    # Add active integration
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
            integration_type="teams",
            name="Active Teams",
            is_active=True,
        )
    )

    # Add inactive integration
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
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


def test_list_available_includes_requires_reauth_instances(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Expired integrations are returned as action-required, not as connected."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    current_user = integrations_dependency_overrides["user_ids"]
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
            integration_type="gmail",
            name="Expired Gmail",
            is_active=False,
            requires_reauth=True,
            last_sync_status="auth_failed",
        )
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    gmail = next((i for i in data["integrations"] if i["integration_type"] == "gmail"), None)
    assert gmail is not None
    assert gmail["is_connected"] is False
    assert gmail["has_action_required"] is True
    assert gmail["connected_integrations"] == []
    assert len(gmail["action_required_integrations"]) == 1
    assert gmail["action_required_integrations"][0]["requires_reauth"] is True
    assert gmail["action_required_integrations"][0]["action_required"] == "reauth_expired"


def test_list_available_marks_missing_scopes_as_additional_permissions(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Active integrations with outdated scopes stay connected but surface reconnect guidance."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    current_user = integrations_dependency_overrides["user_ids"]
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
            integration_type="google_calendar",
            name="Google Calendar",
            is_active=True,
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    calendar = next(
        (i for i in data["integrations"] if i["integration_type"] == "google_calendar"),
        None,
    )
    assert calendar is not None
    assert calendar["is_connected"] is True
    assert calendar["has_action_required"] is True
    assert len(calendar["connected_integrations"]) == 1
    connected = calendar["connected_integrations"][0]
    assert connected["action_required"] == "additional_permissions"
    assert connected["missing_scopes"] == [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/meetings.space.readonly",
    ]
    assert connected["missing_scope_labels"] == [
        "Google Calendar invite access",
        "Google Meet access",
    ]


def test_list_available_marks_microsoft_missing_scopes_as_additional_permissions(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Microsoft integrations report exact missing scopes after scope changes."""
    # Contract: outlook_email scope drift surfaces reconnect guidance and exact labels.
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    current_user = integrations_dependency_overrides["user_ids"]
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
            integration_type="outlook_email",
            name="Outlook Email",
            is_active=True,
            scopes=["offline_access", "User.Read"],
        )
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    outlook_email = next(
        (i for i in data["integrations"] if i["integration_type"] == "outlook_email"),
        None,
    )
    assert outlook_email is not None
    assert outlook_email["is_connected"] is True
    assert outlook_email["has_action_required"] is True
    connected = outlook_email["connected_integrations"][0]
    assert connected["action_required"] == "additional_permissions"
    assert connected["missing_scopes"] == ["Mail.Read", "Mail.Send"]
    assert connected["missing_scope_labels"] == [
        "Outlook mail access",
        "Outlook mail send access",
    ]


def test_list_available_marks_outlook_calendar_write_scope_as_additional_permission(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Read-only Outlook Calendar connections must reconnect before sending invites."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    current_user = integrations_dependency_overrides["user_ids"]
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
            integration_type="outlook_calendar",
            name="Outlook Calendar",
            is_active=True,
            scopes=["offline_access", "User.Read", "Calendars.Read", "OnlineMeetings.Read"],
        )
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    outlook_calendar = next(
        (i for i in data["integrations"] if i["integration_type"] == "outlook_calendar"),
        None,
    )
    assert outlook_calendar is not None
    assert outlook_calendar["is_connected"] is True
    assert outlook_calendar["has_action_required"] is True
    connected = outlook_calendar["connected_integrations"][0]
    assert connected["action_required"] == "additional_permissions"
    assert connected["missing_scopes"] == ["Calendars.ReadWrite"]
    assert connected["missing_scope_labels"] == ["Outlook calendar write access"]


def test_list_available_hides_other_users_personal_integrations_even_if_legacy_org_scoped(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Personal providers should not leak action-required rows connected by another user."""
    from src.domain.models.organisation_tables.sync import IntegrationScope

    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    current_user = integrations_dependency_overrides["user_ids"]
    other_user_id = uuid4()
    integration_service = integrations_dependency_overrides["integration_service"]
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=other_user_id,
            integration_type="teams",
            name="Other User Teams",
            integration_email="teammate@example.com",
            integration_scope=IntegrationScope.ORGANIZATION,
            is_active=True,
            scopes=["offline_access", "User.Read"],
        )
    )
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
            integration_type="teams",
            name="My Teams",
            integration_email="me@example.com",
            integration_scope=IntegrationScope.ORGANIZATION,
            is_active=True,
            scopes=["offline_access", "User.Read"],
        )
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    teams = next((i for i in data["integrations"] if i["integration_type"] == "teams"), None)
    assert teams is not None
    emails = [
        instance["integration_email"]
        for instance in teams["connected_integrations"] + teams["action_required_integrations"]
    ]
    assert emails == ["me@example.com"]


def test_list_available_excludes_manually_disconnected_integrations_from_action_required(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Manual disconnect should not generate reconnect-required UI state."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=uuid4(),
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            name="Old Slack",
            is_active=False,
            requires_reauth=False,
            last_sync_error="Integration disconnected by user",
        )
    )

    resp = client.get("/users/integrations/available")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    slack = next((i for i in data["integrations"] if i["integration_type"] == "slack"), None)
    assert slack is not None
    assert slack["is_connected"] is False
    assert slack["has_action_required"] is False
    assert slack["action_required_integrations"] == []


def test_disconnect_clears_inactive_requires_reauth_without_provider_revoke(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Removing an expired inactive row clears the flag that keeps it in attention UI."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    current_user = integrations_dependency_overrides["user_ids"]
    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=current_user.organisation_id,
        created_by_user_id=current_user.user_id,
        integration_type="gmail",
        name="Expired Gmail",
        is_active=False,
        requires_reauth=True,
        last_sync_status="auth_failed",
        last_sync_error="Refresh token expired",
    )
    integration_service.add_integration(integration)

    resp = client.delete(f"/users/integrations/copilot-oauth/disconnect/{integration_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SUCCESS"
    assert integration.is_active is False
    assert integration.requires_reauth is False
    assert integration.last_sync_error == "Integration removed by user"
    assert integration_service.db._committed is True
    assert integration_service.revoked_integrations == []


def test_disconnect_rejects_inactive_integration_without_reauth(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Manually inactive rows still return the existing already-inactive error."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    current_user = integrations_dependency_overrides["user_ids"]
    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=current_user.organisation_id,
            created_by_user_id=current_user.user_id,
            integration_type="gmail",
            name="Inactive Gmail",
            is_active=False,
            requires_reauth=False,
            last_sync_error="Integration disconnected by user",
        )
    )

    resp = client.delete(f"/users/integrations/copilot-oauth/disconnect/{integration_id}")

    assert resp.status_code == 400, resp.text
    assert resp.json()["message"] == "Integration is already inactive"
    assert integration_service.db._committed is False
    assert integration_service.revoked_integrations == []


def test_disconnect_active_integration_revokes_and_clears_reauth(
    client: TestClient, integrations_dependency_overrides
) -> None:
    """Active disconnect keeps the existing revoke/deactivate behavior."""
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    current_user = integrations_dependency_overrides["user_ids"]
    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration = FakeKnowledgeIntegration(
        integration_id=integration_id,
        organisation_id=current_user.organisation_id,
        created_by_user_id=current_user.user_id,
        integration_type="gmail",
        name="Active Gmail",
        is_active=True,
        requires_reauth=True,
        connection_id="gmail_conn_123",
    )
    integration_service.add_integration(integration)

    resp = client.delete(f"/users/integrations/copilot-oauth/disconnect/{integration_id}")

    assert resp.status_code == 200, resp.text
    assert integration.is_active is False
    assert integration.requires_reauth is False
    assert integration.last_sync_error == "Integration disconnected by user"
    assert integration_service.db._committed is True
    assert integration_service.revoked_integrations == [integration_id]


def test_refresh_channels_accepts_slack(
    client: TestClient,
    integrations_dependency_overrides,
    monkeypatch,
) -> None:
    """Slack integrations can trigger the manual channel refresh task."""
    # Contract: only allowed integration types route into queue-based refresh flow.
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="slack",
            is_active=True,
            connection_id="slack_conn_123",
        )
    )

    async def _fake_send_task_async(*, task_name, args, queue, routing_key):
        assert task_name == "integrations.sync_messaging_channels_integration"
        assert args == [
            str(integrations_dependency_overrides["user_ids"].organisation_id),
            str(integration_id),
        ]
        assert queue == "integrations_priority"
        assert routing_key == "integrations_priority"
        return "task-123"

    monkeypatch.setattr(
        "src.api.routers.integrations.integration_routes.send_integrations_task_async",
        _fake_send_task_async,
    )

    resp = client.post(f"/users/integrations/{integration_id}/refresh-channels")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["task_id"] == "task-123"
    assert body["data"]["integration_id"] == str(integration_id)


def test_refresh_channels_rejects_non_messaging_integration(
    client: TestClient,
    integrations_dependency_overrides,
) -> None:
    """Only Slack and Teams integrations support channel refresh."""
    # Contract: Gmail and other non-messaging providers return actionable 400.
    from tests.api.routers.integrations.conftest import FakeKnowledgeIntegration

    integration_service = integrations_dependency_overrides["integration_service"]
    integration_id = uuid4()
    integration_service.add_integration(
        FakeKnowledgeIntegration(
            integration_id=integration_id,
            organisation_id=integrations_dependency_overrides["user_ids"].organisation_id,
            integration_type="gmail",
            is_active=True,
            connection_id="gmail_conn_123",
        )
    )

    resp = client.post(f"/users/integrations/{integration_id}/refresh-channels")

    assert resp.status_code == 400, resp.text
    assert resp.json()["status"] == "ERROR"
