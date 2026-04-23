from src.service.integrations.crm.schema.salesforce.opportunities.mapping import (
    SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS,
)


def test_maps_salesforce_opportunity_stage_and_owner_fields_to_crmdeal_columns() -> None:
    assert SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS["StageName"] == "stage_external_id"
    assert SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS["OwnerId"] == "owner_external_id"


def test_maps_salesforce_amount_and_forecast_fields_to_existing_crmdeal_columns() -> None:
    assert SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS["Amount"] == "amount"
    assert SALESFORCE_OPPORTUNITY_CANONICAL_FIELDS["ExpectedRevenue"] == "predicted_amount"
