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

    assert integration.connection_metadata["initial_sync_status"] == "queued"
    assert "initial_sync_placeholder" not in integration.connection_metadata
    assert integration.connection_metadata["schema_sync"]["deals"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["pipelines"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["users"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["companies"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["contacts"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["deals"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["tasks"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["notes"]["status"] == "queued"


def test_initial_sync_service_noops_for_hubspot_placeholder() -> None:
    service = InitialSyncService()
    integration = _make_integration(IntegrationType.HUBSPOT)

    with (
        patch(
            "src.service.integrations.initial_sync_service.build_hubspot_initial_sync_chain"
        ) as mock_build,
    ):
        service.trigger_initial_sync(integration.organisation_id, integration)

    mock_build.assert_called_once_with(
        organisation_id=integration.organisation_id,
        integration_id=integration.integration_id,
    )
    mock_build.return_value.apply_async.assert_called_once_with()
