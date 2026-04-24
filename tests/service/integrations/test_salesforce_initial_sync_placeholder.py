from unittest.mock import MagicMock, patch
from uuid import uuid4

from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.initial_sync_service import InitialSyncService


def test_initial_sync_service_queues_salesforce_schema_pipeline_user_company_contact_deal_and_task_sync_metadata() -> (
    None
):
    service = InitialSyncService()
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_scope=IntegrationScope.ORGANIZATION,
        integration_type=IntegrationType.SALESFORCE,
        name="Test Integration",
        credentials={},
        connection_metadata={"provider_key": "salesforce"},
        is_active=True,
        connection_id=str(uuid4()),
    )

    service.apply_post_connect_state(integration)

    assert integration.connection_metadata["initial_sync_status"] == "queued"
    assert "initial_sync_placeholder" not in integration.connection_metadata
    assert integration.connection_metadata["schema_sync"]["opportunities"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["pipelines"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["users"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["companies"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["contacts"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["deals"]["status"] == "queued"
    assert integration.connection_metadata["record_sync"]["tasks"]["status"] == "queued"


def test_initial_sync_service_dispatches_salesforce_initial_sync_chain() -> None:
    service = InitialSyncService()
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_scope=IntegrationScope.ORGANIZATION,
        integration_type=IntegrationType.SALESFORCE,
        name="Test Integration",
        credentials={},
        connection_metadata={},
        is_active=True,
        connection_id=str(uuid4()),
    )
    chain_signature = MagicMock()

    with patch(
        "src.service.integrations.initial_sync_service.build_salesforce_initial_sync_chain",
        return_value=chain_signature,
    ) as mock_build:
        service.trigger_initial_sync(integration.organisation_id, integration)

    mock_build.assert_called_once_with(
        organisation_id=integration.organisation_id,
        integration_id=integration.integration_id,
    )
    chain_signature.apply_async.assert_called_once_with()
