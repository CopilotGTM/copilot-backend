from src.service.integrations.crm.schema.classifier import classify
from src.service.integrations.crm.schema.schema_types import (
    CanonicalMatch,
    IgnoredMatch,
)


def test_classify_returns_canonical_match_for_shared_closed_reason_fields() -> None:
    classification = classify({"name": "closed_won_reason", "hubspotDefined": True})

    assert isinstance(classification, CanonicalMatch)
    assert classification.hubspot_property == "closed_won_reason"
    assert classification.canonical_column == "closed_reason"


def test_classify_returns_ignored_match_for_explicitly_ignored_close_dates() -> None:
    classification = classify({"name": "closed_won_date", "hubspotDefined": True})

    assert isinstance(classification, IgnoredMatch)
    assert classification.hubspot_property == "closed_won_date"
