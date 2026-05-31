from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.salesforce.metadata import (
    SalesforceCdcMetadataError,
    get_salesforce_opportunity_cdc_metadata,
)


def test_salesforce_cdc_metadata_reads_opportunity_channel() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {
                "opportunity": {
                    "enabled": True,
                    "channel": "/data/OpportunityChangeEvent",
                    "pubsub_endpoint": "api.pubsub.salesforce.com:443",
                }
            },
        },
    )

    metadata = get_salesforce_opportunity_cdc_metadata(integration)

    assert metadata.salesforce_organization_id == "00Dxx0000000001"
    assert metadata.instance_url == "https://acme.my.salesforce.com"
    assert metadata.channel == "/data/OpportunityChangeEvent"
    assert metadata.pubsub_endpoint == "api.pubsub.salesforce.com:443"


def test_salesforce_cdc_metadata_rejects_disabled_opportunity_channel() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={"salesforce_organization_id": "00Dxx0000000001"},
    )

    with pytest.raises(SalesforceCdcMetadataError, match="Opportunity CDC is not enabled"):
        get_salesforce_opportunity_cdc_metadata(integration)


def test_salesforce_cdc_metadata_rejects_non_salesforce_pubsub_endpoint() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {
                "opportunity": {
                    "enabled": True,
                    "channel": "/data/OpportunityChangeEvent",
                    "pubsub_endpoint": "169.254.169.254:443",
                }
            },
        },
    )

    with pytest.raises(SalesforceCdcMetadataError, match="Unsupported Salesforce Pub/Sub endpoint"):
        get_salesforce_opportunity_cdc_metadata(integration)


def test_salesforce_cdc_metadata_accepts_salesforce_pubsub_7443_port() -> None:
    integration = KnowledgeIntegration(
        integration_id=uuid4(),
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://example.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {
                "opportunity": {
                    "enabled": True,
                    "channel": "/data/OpportunityChangeEvent",
                    "pubsub_endpoint": "api.pubsub.salesforce.com:7443",
                }
            },
        },
    )

    metadata = get_salesforce_opportunity_cdc_metadata(integration)

    assert metadata.pubsub_endpoint == "api.pubsub.salesforce.com:7443"


def test_salesforce_cdc_metadata_rejects_non_https_instance_url() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "http://acme.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {"opportunity": {"enabled": True}},
        },
    )

    with pytest.raises(SalesforceCdcMetadataError, match="Unsupported Salesforce instance URL"):
        get_salesforce_opportunity_cdc_metadata(integration)


def test_salesforce_cdc_metadata_rejects_non_salesforce_instance_url() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://169.254.169.254"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {"opportunity": {"enabled": True}},
        },
    )

    with pytest.raises(SalesforceCdcMetadataError, match="Unsupported Salesforce instance URL"):
        get_salesforce_opportunity_cdc_metadata(integration)
