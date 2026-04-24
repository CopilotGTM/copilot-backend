from src.service.integrations.crm.schema.common.types import (
    CanonicalMatch,
    IgnoredMatch,
)
from src.service.integrations.crm.schema.hubspot.deals.classifier import classify


def test_classify_returns_canonical_match_for_shared_closed_reason_fields() -> None:
    classification = classify({"name": "closed_won_reason", "hubspotDefined": True})

    assert isinstance(classification, CanonicalMatch)
    assert classification.provider_field_key == "closed_won_reason"
    assert classification.canonical_column == "closed_reason"


def test_classify_returns_ignored_match_for_explicitly_ignored_close_dates() -> None:
    classification = classify({"name": "closed_won_date", "hubspotDefined": True})

    assert isinstance(classification, IgnoredMatch)
    assert classification.provider_field_key == "closed_won_date"
