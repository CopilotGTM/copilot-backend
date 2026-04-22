from src.service.integrations.crm.schema.hubspot_deal_mapping import (
    HUBSPOT_DEAL_CANONICAL_FIELDS,
    canonical_column_for,
    is_hubspot_deal_canonical,
    is_hubspot_deal_ignored,
)


def test_maps_hubspot_rollup_counts_to_existing_crmdeal_columns() -> None:
    assert canonical_column_for("num_associated_contacts") == "associated_contact_count"
    assert canonical_column_for("num_notes") == "note_count"
    assert is_hubspot_deal_canonical("num_associated_contacts") is True
    assert is_hubspot_deal_canonical("num_notes") is True


def test_does_not_canonicalize_next_step_or_last_contacted_yet() -> None:
    assert canonical_column_for("hs_next_step") is None
    assert canonical_column_for("notes_last_contacted") is None
    assert "hs_next_step" not in HUBSPOT_DEAL_CANONICAL_FIELDS
    assert "notes_last_contacted" not in HUBSPOT_DEAL_CANONICAL_FIELDS


def test_maps_both_hubspot_close_reason_fields_to_shared_closed_reason() -> None:
    assert canonical_column_for("closed_lost_reason") == "closed_reason"
    assert canonical_column_for("closed_won_reason") == "closed_reason"
    assert is_hubspot_deal_canonical("closed_lost_reason") is True
    assert is_hubspot_deal_canonical("closed_won_reason") is True


def test_ignores_close_date_variants_we_do_not_model() -> None:
    assert canonical_column_for("closed_won_date") is None
    assert canonical_column_for("closed_lost_date") is None
    assert is_hubspot_deal_ignored("closed_won_date") is True
    assert is_hubspot_deal_ignored("closed_lost_date") is True
