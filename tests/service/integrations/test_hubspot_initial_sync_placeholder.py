from unittest.mock import patch
from uuid import uuid4

from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.initial_sync_service import InitialSyncService


def _make_integration(integration_type: IntegrationType) -> KnowledgeIntegration:
    return KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_scope=IntegrationScope.ORGANIZATION,
        integration_type=integration_type,
        name="Test Integration",
        credentials={},
        connection_metadata={},
        is_active=True,
        connection_id=str(uuid4()),
    )


def test_initial_sync_service_applies_hubspot_placeholder_metadata() -> None:
    service = InitialSyncService()
    integration = _make_integration(IntegrationType.HUBSPOT)

    service.apply_post_connect_state(integration)

    assert integration.connection_metadata["initial_sync_status"] == "not_implemented"
    assert (
        "CRM sync dispatch will be added later"
        in integration.connection_metadata["initial_sync_placeholder"]
    )


def test_initial_sync_service_noops_for_hubspot_placeholder() -> None:
    service = InitialSyncService()
    integration = _make_integration(IntegrationType.HUBSPOT)

    with (
        patch("src.service.integrations.initial_sync_service.send_integrations_task") as mock_send,
        patch("src.service.integrations.initial_sync_service.logger") as mock_logger,
    ):
        service.trigger_initial_sync(integration.organisation_id, integration)

    mock_send.assert_not_called()
    mock_logger.warning.assert_not_called()
    mock_logger.info.assert_called()
