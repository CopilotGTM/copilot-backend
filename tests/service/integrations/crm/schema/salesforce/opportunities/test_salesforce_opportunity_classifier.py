import pytest
from src.service.integrations.crm.schema.common.types import (
    CanonicalMatch,
    ProviderCustomMatch,
)
from src.service.integrations.crm.schema.salesforce.opportunities.classifier import (
    MalformedSalesforceField,
    classify_salesforce_opportunity_field,
)


def test_classify_salesforce_returns_canonical_match_for_stage_name() -> None:
    classification = classify_salesforce_opportunity_field({"name": "StageName", "custom": False})

    assert isinstance(classification, CanonicalMatch)
    assert classification.provider_field_key == "StageName"
    assert classification.canonical_column == "stage_external_id"


def test_classify_salesforce_marks_custom_field_as_provider_custom() -> None:
    classification = classify_salesforce_opportunity_field(
        {"name": "Partner_Notes__c", "custom": True}
    )

    assert isinstance(classification, ProviderCustomMatch)
    assert classification.provider_field_key == "Partner_Notes__c"
    assert classification.is_provider_custom is True


def test_classify_salesforce_marks_standard_noncanonical_field_as_provider_defined() -> None:
    classification = classify_salesforce_opportunity_field({"name": "IsClosed", "custom": False})

    assert isinstance(classification, ProviderCustomMatch)
    assert classification.is_provider_custom is False


def test_classify_salesforce_rejects_missing_field_name() -> None:
    with pytest.raises(MalformedSalesforceField):
        classify_salesforce_opportunity_field({"label": "Missing name"})
