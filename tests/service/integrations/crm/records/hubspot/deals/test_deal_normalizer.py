from __future__ import annotations

import pytest
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus
from src.service.integrations.crm.records.hubspot.deals.deal_normalizer import normalize_deals


@pytest.mark.parametrize(
    ("properties", "expected_status"),
    [
        ({"hs_is_closed_won": "true"}, CRMDealStatus.WON),
        ({"hs_is_closed_lost": "true"}, CRMDealStatus.LOST),
        ({"hs_is_closed": "true"}, CRMDealStatus.CLOSED),
        ({}, CRMDealStatus.OPEN),
    ],
)
def test_normalizer_derives_deal_status_from_hubspot_closed_flags(
    properties: dict[str, str],
    expected_status: CRMDealStatus,
) -> None:
    normalized, skipped = normalize_deals(
        [
            {
                "id": "123",
                "properties": {
                    "dealname": "Enterprise renewal",
                    **properties,
                },
            }
        ]
    )

    assert skipped == 0
    assert normalized[0].deal_status == expected_status
