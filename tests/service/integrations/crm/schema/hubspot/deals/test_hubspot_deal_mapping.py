from src.service.integrations.crm.schema.hubspot.deals.mapping import (
    HUBSPOT_DEAL_CANONICAL_FIELDS,
    is_hubspot_deal_canonical,
    is_hubspot_deal_ignored,
)


def test_maps_hubspot_rollup_counts_to_existing_crmdeal_columns() -> None:
    assert HUBSPOT_DEAL_CANONICAL_FIELDS["num_associated_contacts"] == "associated_contact_count"
    assert HUBSPOT_DEAL_CANONICAL_FIELDS["num_notes"] == "note_count"


def test_does_not_canonicalize_next_step_or_last_contacted_yet() -> None:
    assert not is_hubspot_deal_canonical("hs_next_step")
    assert not is_hubspot_deal_canonical("notes_last_contacted")


def test_maps_both_hubspot_close_reason_fields_to_shared_closed_reason() -> None:
    assert HUBSPOT_DEAL_CANONICAL_FIELDS["closed_won_reason"] == "closed_reason"
    assert HUBSPOT_DEAL_CANONICAL_FIELDS["closed_lost_reason"] == "closed_reason"


def test_ignores_close_date_variants_we_do_not_model() -> None:
    assert is_hubspot_deal_ignored("closed_won_date")
    assert is_hubspot_deal_ignored("closed_lost_date")
