from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.webhooks.salesforce.metadata import (
    SalesforceCdcMetadataError,
    get_enabled_salesforce_cdc_metadata,
    get_salesforce_cdc_metadata_for_channel,
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


def test_salesforce_cdc_metadata_reads_all_enabled_channels() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {
                "opportunity": {"enabled": True, "channel": "/data/OpportunityChangeEvent"},
                "account": {"enabled": True, "channel": "/data/AccountChangeEvent"},
                "contact": {"enabled": True, "channel": "/data/ContactChangeEvent"},
                "user": {"enabled": True, "channel": "/data/UserChangeEvent"},
                "task": {"enabled": True, "channel": "/data/TaskChangeEvent"},
                "content_note": {"enabled": True, "channel": "/data/ContentNoteChangeEvent"},
                "content_document_link": {
                    "enabled": True,
                    "channel": "/data/ContentDocumentLinkChangeEvent",
                },
            },
        },
    )

    metadata = get_enabled_salesforce_cdc_metadata(integration)

    assert [entry.object_key for entry in metadata] == [
        "opportunity",
        "account",
        "contact",
        "user",
        "task",
        "content_note",
        "content_document_link",
    ]
    assert [entry.channel for entry in metadata] == [
        "/data/OpportunityChangeEvent",
        "/data/AccountChangeEvent",
        "/data/ContactChangeEvent",
        "/data/UserChangeEvent",
        "/data/TaskChangeEvent",
        "/data/ContentNoteChangeEvent",
        "/data/ContentDocumentLinkChangeEvent",
    ]


def test_salesforce_cdc_metadata_resolves_task_and_note_channels() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {
                "task": {"enabled": True, "channel": "/data/TaskChangeEvent"},
                "content_note": {"enabled": True, "channel": "/data/ContentNoteChangeEvent"},
                "content_document_link": {
                    "enabled": True,
                    "channel": "/data/ContentDocumentLinkChangeEvent",
                },
            },
        },
    )

    task_metadata = get_salesforce_cdc_metadata_for_channel(
        integration,
        channel="/data/TaskChangeEvent",
    )
    note_metadata = get_salesforce_cdc_metadata_for_channel(
        integration,
        channel="/data/ContentNoteChangeEvent",
    )
    link_metadata = get_salesforce_cdc_metadata_for_channel(
        integration,
        channel="/data/ContentDocumentLinkChangeEvent",
    )

    assert (task_metadata.object_key, task_metadata.object_name, task_metadata.id_prefix) == (
        "task",
        "Task",
        "00T",
    )
    assert (note_metadata.object_key, note_metadata.object_name, note_metadata.id_prefix) == (
        "content_note",
        "ContentNote",
        "069",
    )
    assert (link_metadata.object_key, link_metadata.object_name, link_metadata.id_prefix) == (
        "content_document_link",
        "ContentDocumentLink",
        "06A",
    )


def test_salesforce_cdc_metadata_resolves_user_channel() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={
            "salesforce_organization_id": "00Dxx0000000001",
            "salesforce_cdc": {
                "user": {"enabled": True, "channel": "/data/UserChangeEvent"},
            },
        },
    )

    metadata = get_salesforce_cdc_metadata_for_channel(
        integration,
        channel="/data/UserChangeEvent",
    )

    assert (metadata.object_key, metadata.object_name, metadata.id_prefix) == (
        "user",
        "User",
        "005",
    )


def test_salesforce_cdc_metadata_rejects_unsupported_channel() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={"instance_url": "https://acme.my.salesforce.com"},
        connection_metadata={"salesforce_organization_id": "00Dxx0000000001"},
    )

    with pytest.raises(SalesforceCdcMetadataError, match="Unsupported Salesforce CDC channel"):
        get_salesforce_cdc_metadata_for_channel(integration, channel="/data/LeadChangeEvent")


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
