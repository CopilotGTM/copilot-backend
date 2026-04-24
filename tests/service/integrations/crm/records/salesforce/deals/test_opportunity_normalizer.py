from __future__ import annotations

from datetime import UTC, datetime

from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.integrations.crm.records.salesforce.deals.opportunity_normalizer import (
    normalize_opportunities,
)


def test_normalizer_maps_salesforce_opportunity_to_internal_deal_contract() -> None:
    normalized, skipped = normalize_opportunities(
        [
            {
                "Id": "006A",
                "Name": "Enterprise Expansion",
                "Amount": "125000",
                "CurrencyIsoCode": "USD",
                "CloseDate": "2026-04-30",
                "StageName": "Closed Won",
                "RecordTypeId": "012PIPE",
                "OwnerId": "005OWNER",
                "AccountId": "001COMP",
                "CreatedById": "005CREATOR",
                "LastModifiedById": "005EDITOR",
                "CreatedDate": "2026-04-01T10:00:00+00:00",
                "LastModifiedDate": "2026-04-10T11:00:00+00:00",
                "IsClosed": True,
                "IsWon": True,
                "Probability": 100,
                "ExpectedRevenue": 125000,
                "ForecastCategoryName": "Closed",
                "LeadSource": "Partner",
                "Description": "Expansion cycle",
                "Custom_Field__c": "custom-value",
            }
        ],
        instance_url="https://acme.my.salesforce.com",
        primary_contact_by_deal={"006A": "003CONTACT"},
        current_stage_entered_at_by_deal={
            "006A": datetime(2026, 4, 12, 9, 0, tzinfo=UTC),
        },
        contact_count_by_deal={"006A": 2},
    )

    assert skipped == 0
    deal = normalized[0]
    assert deal.deal_status == CRMDealStatus.WON
    assert deal.pipeline_external_id == "012PIPE"
    assert deal.stage_external_id == "012PIPE:Closed Won"
    assert deal.primary_contact_external_id == "003CONTACT"
    assert deal.associated_contact_count == 2
    assert deal.closed_won_at == datetime(2026, 4, 12, 9, 0, tzinfo=UTC)
    assert deal.crm_url.endswith("/Opportunity/006A/view")
    assert deal.properties["raw_fields"]["Custom_Field__c"] == "custom-value"
