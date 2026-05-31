from uuid import uuid4

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.integrations.copilot_callback_service import CopilotCallbackService


def test_salesforce_callback_persists_provider_identity_metadata() -> None:
    integration = KnowledgeIntegration(
        organisation_id=uuid4(),
        created_by_user_id=uuid4(),
        integration_type=IntegrationType.SALESFORCE,
        name="Salesforce",
        credentials={},
        connection_metadata={"existing": "kept"},
    )
    provider_user_info = {
        "salesforce_user_id": "005xx0000000001",
        "salesforce_organization_id": "00Dxx0000000001",
        "salesforce_instance_url": "https://acme.my.salesforce.com/",
        "salesforce_username": "owner@example.com",
    }

    CopilotCallbackService()._apply_custom_token_payload(
        integration,
        integration_type=IntegrationType.SALESFORCE,
        provider_key="salesforce",
        token_payload={"access_token": "access-token", "scope": "refresh_token api"},
        provider_user_info=provider_user_info,
    )

    assert integration.connection_metadata["existing"] == "kept"
    assert integration.connection_metadata["salesforce_user_id"] == "005xx0000000001"
    assert integration.connection_metadata["salesforce_organization_id"] == "00Dxx0000000001"
    assert integration.connection_metadata["salesforce_instance_url"] == (
        "https://acme.my.salesforce.com"
    )
    assert integration.connection_metadata["salesforce_username"] == "owner@example.com"
    assert integration.connection_metadata["salesforce_cdc"] == {
        "opportunity": {
            "enabled": True,
            "channel": "/data/OpportunityChangeEvent",
        },
        "account": {
            "enabled": True,
            "channel": "/data/AccountChangeEvent",
        },
        "contact": {
            "enabled": True,
            "channel": "/data/ContactChangeEvent",
        },
        "user": {
            "enabled": True,
            "channel": "/data/UserChangeEvent",
        },
        "task": {
            "enabled": True,
            "channel": "/data/TaskChangeEvent",
        },
        "content_note": {
            "enabled": True,
            "channel": "/data/ContentNoteChangeEvent",
        },
        "content_document_link": {
            "enabled": True,
            "channel": "/data/ContentDocumentLinkChangeEvent",
        },
    }
