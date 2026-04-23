from __future__ import annotations

import re

from src.service.integrations.crm.schema.common.types import ProviderCustomMatch
from src.service.integrations.crm.schema.hubspot.deals.normalizer import normalize


def test_normalize_generates_db_safe_key_for_long_hubspot_property_name() -> None:
    raw = {
        "name": "hs_line_item_global_term_hs_recurring_billing_start_date_enabled",
        "label": "Global Term Line Item Recurring Billing Start Date Enabled",
        "type": "bool",
        "fieldType": "booleancheckbox",
        "hubspotDefined": True,
    }

    normalized = normalize(
        raw,
        ProviderCustomMatch(
            provider_field_key=raw["name"],
            is_provider_custom=False,
        ),
    )

    assert (
        normalized.provider_field_key
        == "hs_line_item_global_term_hs_recurring_billing_start_date_enabled"
    )
    assert normalized.key != normalized.provider_field_key
    assert len(normalized.key) <= 63
    assert re.fullmatch(r"^[a-z][a-z0-9_]{1,62}$", normalized.key)


def test_normalize_truncates_hubspot_description_to_db_limit() -> None:
    raw = {
        "name": "meetings_tool_referrer_campaign",
        "label": "Meetings Tool Referrer Campaign",
        "description": "x" * 6000,
        "type": "string",
        "fieldType": "text",
        "hubspotDefined": True,
    }

    normalized = normalize(
        raw,
        ProviderCustomMatch(
            provider_field_key=raw["name"],
            is_provider_custom=False,
        ),
    )

    assert normalized.description == "x" * 5000
    assert normalized.data_payload["description"] == "x" * 6000
