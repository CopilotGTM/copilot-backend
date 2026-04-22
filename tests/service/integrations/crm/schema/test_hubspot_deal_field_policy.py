from src.service.integrations.crm.schema.hubspot_deal_field_policy import (
    HubSpotDealFieldMetadata,
    HubSpotDealValueStorageFilterOptions,
    classify_hubspot_deal_field,
    classify_hubspot_deal_field_for_value_storage,
)


def test_classifies_existing_canonical_fields_first() -> None:
    decision = classify_hubspot_deal_field(
        HubSpotDealFieldMetadata(
            provider_field_key="num_notes",
            hidden=False,
            read_only_value=True,
            group_name="deal_activity",
        )
    )

    assert decision.category == "canonical"
    assert decision.internal_name == "note_count"


def test_classifies_explicitly_ignored_fields() -> None:
    decision = classify_hubspot_deal_field(
        HubSpotDealFieldMetadata(
            provider_field_key="closed_won_date",
            hidden=False,
            read_only_value=True,
            group_name="dealinformation",
        )
    )

    assert decision.category == "ignored"


def test_suppresses_hidden_fields_before_anything_else() -> None:
    decision = classify_hubspot_deal_field(
        HubSpotDealFieldMetadata(
            provider_field_key="hs_campaign",
            hidden=True,
            read_only_value=False,
            group_name="dealinformation",
        )
    )

    assert decision.category == "suppressed_hidden"


def test_suppresses_read_only_fields_before_visibility_allowlist() -> None:
    decision = classify_hubspot_deal_field(
        HubSpotDealFieldMetadata(
            provider_field_key="notes_last_contacted",
            hidden=False,
            read_only_value=True,
            group_name="deal_activity",
        )
    )

    assert decision.category == "suppressed_read_only"


def test_suppresses_provider_noise_even_if_editable_and_not_hidden() -> None:
    decision = classify_hubspot_deal_field(
        HubSpotDealFieldMetadata(
            provider_field_key="hs_analytics_source",
            hidden=False,
            read_only_value=False,
            group_name="analyticsinformation",
        )
    )

    assert decision.category == "suppressed_provider_noise"


def test_exposes_only_allowlisted_custom_fields() -> None:
    decision = classify_hubspot_deal_field(
        HubSpotDealFieldMetadata(
            provider_field_key="hs_next_step",
            hidden=False,
            read_only_value=False,
            group_name="dealinformation",
        )
    )

    assert decision.category == "exposed_custom"
    assert decision.internal_name == "next_step"


def test_suppresses_non_allowlisted_editable_fields() -> None:
    decision = classify_hubspot_deal_field(
        HubSpotDealFieldMetadata(
            provider_field_key="some_future_editable_business_field",
            hidden=False,
            read_only_value=False,
            group_name="dealinformation",
        )
    )

    assert decision.category == "suppressed_unapproved"


def test_value_storage_filters_canonical_fields_by_default() -> None:
    decision = classify_hubspot_deal_field_for_value_storage(
        HubSpotDealFieldMetadata(
            provider_field_key="num_notes",
            hidden=False,
            read_only_value=True,
            group_name="deal_activity",
        )
    )

    assert decision.keep is False
    assert decision.category == "filtered_canonical"
    assert decision.internal_name == "note_count"


def test_value_storage_filters_hidden_fields_by_default() -> None:
    decision = classify_hubspot_deal_field_for_value_storage(
        HubSpotDealFieldMetadata(
            provider_field_key="hs_campaign",
            hidden=True,
            read_only_value=False,
            group_name="dealinformation",
        )
    )

    assert decision.keep is False
    assert decision.category == "filtered_hidden"


def test_value_storage_keeps_allowlisted_custom_fields() -> None:
    decision = classify_hubspot_deal_field_for_value_storage(
        HubSpotDealFieldMetadata(
            provider_field_key="hs_next_step",
            hidden=False,
            read_only_value=False,
            group_name="dealinformation",
        )
    )

    assert decision.keep is True
    assert decision.category == "global_default_custom"
    assert decision.internal_name == "next_step"


def test_value_storage_marks_surviving_non_global_fields_for_review() -> None:
    decision = classify_hubspot_deal_field_for_value_storage(
        HubSpotDealFieldMetadata(
            provider_field_key="future_deal_field",
            hidden=False,
            read_only_value=False,
            group_name="dealinformation",
            current_internal_key="future_deal_field",
        ),
        options=HubSpotDealValueStorageFilterOptions(
            exclude_canonical=True,
            exclude_ignored=True,
            exclude_hidden=True,
            exclude_read_only=True,
            exclude_provider_noise=True,
        ),
    )

    assert decision.keep is True
    assert decision.category == "review_custom"
    assert decision.internal_name == "future_deal_field"


def test_value_storage_keeps_provider_custom_fields() -> None:
    decision = classify_hubspot_deal_field_for_value_storage(
        HubSpotDealFieldMetadata(
            provider_field_key="custom_customer_priority",
            hidden=True,
            read_only_value=True,
            group_name="dealinformation",
            current_internal_key="custom_customer_priority",
            is_provider_custom=True,
        )
    )

    assert decision.keep is True
    assert decision.category == "provider_custom"
    assert decision.internal_name == "custom_customer_priority"
