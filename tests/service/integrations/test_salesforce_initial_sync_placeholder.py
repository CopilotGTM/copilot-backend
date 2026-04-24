from uuid import uuid4

from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.service.integrations.initial_sync_service import InitialSyncService


def test_initial_sync_service_applies_salesforce_placeholder_metadata() -> None:
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

    assert integration.connection_metadata["initial_sync_status"] == "not_implemented"
    assert (
        "Salesforce custom auth connected successfully"
        in integration.connection_metadata["initial_sync_placeholder"]
    )
