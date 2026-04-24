from uuid import uuid4

from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
    CRMCustomFieldSource,
)


def test_internal_custom_field_defaults_to_internal_source() -> None:
    field = CRMCustomFieldDefinition(
        organisation_id=uuid4(),
        entity_type=CRMCustomFieldEntity.DEAL,
        key="priority",
        label="Priority",
        data_type=CRMCustomFieldDataType.TEXT,
    )

    assert field.field_source == CRMCustomFieldSource.INTERNAL
    assert field.integration_id is None
    assert field.provider_name is None
    assert field.provider_field_key is None
    assert field.is_provider_custom is None
    assert field.data_payload is None
    assert field.source_modified_at is None
    assert field.last_seen_at is None


def test_provider_custom_field_supports_integration_provenance() -> None:
    integration_id = uuid4()

    field = CRMCustomFieldDefinition(
        organisation_id=uuid4(),
        entity_type=CRMCustomFieldEntity.DEAL,
        key="dealstage",
        label="Deal Stage",
        data_type=CRMCustomFieldDataType.SELECT,
        field_source=CRMCustomFieldSource.PROVIDER,
        integration_id=integration_id,
        provider_name="hubspot",
        provider_field_key="dealstage",
        is_provider_custom=False,
        data_payload={"type": "enumeration"},
    )

    assert field.field_source == CRMCustomFieldSource.PROVIDER
    assert field.integration_id == integration_id
    assert field.provider_name == "hubspot"
    assert field.provider_field_key == "dealstage"
    assert field.is_provider_custom is False
    assert field.data_payload == {"type": "enumeration"}
