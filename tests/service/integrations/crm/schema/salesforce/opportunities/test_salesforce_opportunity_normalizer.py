from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldDataType
from src.service.integrations.crm.schema.common.types import ProviderCustomMatch
from src.service.integrations.crm.schema.salesforce.opportunities.normalizer import (
    normalize_salesforce_field,
)


def test_normalize_salesforce_picklist_field_to_select() -> None:
    raw = {
        "name": "Risk_Level__c",
        "label": "Risk Level",
        "type": "picklist",
        "custom": True,
        "picklistValues": [
            {"value": "High", "label": "High", "active": True},
            {"value": "Low", "label": "Low", "active": False},
        ],
    }

    normalized = normalize_salesforce_field(
        raw,
        ProviderCustomMatch(provider_field_key=raw["name"], is_provider_custom=True),
    )

    assert normalized.provider_field_key == "Risk_Level__c"
    assert normalized.label == "Risk Level"
    assert normalized.data_type == CRMCustomFieldDataType.SELECT
    assert [opt.value for opt in normalized.options] == ["High"]


def test_normalize_salesforce_formula_field_to_formula_type() -> None:
    raw = {
        "name": "Projected_Margin__c",
        "label": "Projected Margin",
        "type": "percent",
        "custom": True,
        "calculated": True,
        "calculatedFormula": "Amount * 0.25",
    }

    normalized = normalize_salesforce_field(
        raw,
        ProviderCustomMatch(provider_field_key=raw["name"], is_provider_custom=True),
    )

    assert normalized.data_type == CRMCustomFieldDataType.FORMULA
    assert normalized.formula == {"source": "salesforce", "expression": "Amount * 0.25"}
