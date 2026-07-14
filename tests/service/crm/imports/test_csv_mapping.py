from src.service.crm.imports.csv_mapping import suggest_csv_mappings


def _by_header(headers: list[str], rows: list[dict[str, str]] | None = None):
    if rows is None:
        return {header: suggest_csv_mappings([header], [])[0] for header in headers}
    return {item.header: item for item in suggest_csv_mappings(headers, rows or [])}


def test_common_aliases_map_to_supported_crm_fields() -> None:
    suggestions = _by_header(
        [
            "Company",
            "Account Name",
            "Website",
            "Domain",
            "Account Phone",
            "Account City",
            "Account State",
            "Account Country",
            "Account Address",
            "Account Postal Code",
            "First Name",
            "Last Name",
            "Full Name",
            "Email",
            "Title",
            "LinkedIn",
            "Phone",
            "Mobile Phone",
            "Contact Website",
            "Contact Address",
            "Contact City",
            "Contact State",
            "Contact Country",
            "Contact Postal Code",
            "Company LinkedIn",
            "Account LinkedIn",
        ]
    )

    assert suggestions["Company"].target_field == "account.name"
    assert suggestions["Account Name"].target_field == "account.name"
    assert suggestions["Website"].target_field == "account.website"
    assert suggestions["Domain"].target_field == "account.domain"
    assert suggestions["Account Phone"].target_field == "account.phone"
    assert suggestions["Account City"].target_field == "account.city"
    assert suggestions["Account State"].target_field == "account.state"
    assert suggestions["Account Country"].target_field == "account.country"
    assert suggestions["Account Address"].target_field == "account.address"
    assert suggestions["Account Postal Code"].target_field == "account.postal_code"
    assert suggestions["First Name"].target_field == "contact.first_name"
    assert suggestions["Last Name"].target_field == "contact.last_name"
    assert suggestions["Full Name"].target_field == "contact.name"
    assert suggestions["Email"].target_field == "contact.email"
    assert suggestions["Title"].target_field == "contact.job_title"
    assert suggestions["LinkedIn"].target_field == "contact.linkedin_url"
    assert suggestions["Phone"].target_field == "contact.phone"
    assert suggestions["Mobile Phone"].target_field == "contact.mobile_phone"
    assert suggestions["Contact Website"].target_field == "contact.website"
    assert suggestions["Contact Address"].target_field == "contact.address"
    assert suggestions["Contact City"].target_field == "contact.city"
    assert suggestions["Contact State"].target_field == "contact.state"
    assert suggestions["Contact Country"].target_field == "contact.country"
    assert suggestions["Contact Postal Code"].target_field == "contact.postal_code"
    assert suggestions["Company LinkedIn"].target_field == "account.linkedin_url"
    assert suggestions["Account LinkedIn"].target_field == "account.linkedin_url"
    assert suggestions["Email"].confidence == "medium"
    assert suggestions["Email"].reason_code == "header_alias"


def test_deal_aliases_map_to_supported_crm_fields() -> None:
    suggestions = _by_header(
        [
            "Deal Name",
            "Opportunity Name",
            "Amount",
            "Deal Value",
            "Currency",
            "Expected Close Date",
            "Deal Type",
            "Priority",
            "Lead Source",
            "Probability",
            "Forecast Category",
            "Description",
            "Created Date",
        ]
    )

    assert suggestions["Deal Name"].target_field == "deal.name"
    assert suggestions["Opportunity Name"].target_field == "deal.name"
    assert suggestions["Amount"].target_field == "deal.amount"
    assert suggestions["Deal Value"].target_field == "deal.amount"
    assert suggestions["Currency"].target_field == "deal.currency"
    assert suggestions["Expected Close Date"].target_field == "deal.expected_close_date"
    assert suggestions["Deal Type"].target_field == "deal.deal_type"
    assert suggestions["Priority"].target_field == "deal.priority"
    assert suggestions["Lead Source"].target_field == "deal.lead_source"
    assert suggestions["Probability"].target_field == "deal.probability"
    assert suggestions["Forecast Category"].target_field == "deal.forecast_category"
    assert suggestions["Description"].target_field == "deal.description"
    assert suggestions["Created Date"].target_field == "deal.source_created_at"


def test_sample_heuristics_map_identity_values_without_aliases() -> None:
    suggestions = _by_header(
        ["Primary", "Site", "Profile", "Org Profile"],
        [
            {
                "Primary": "jane@example.com",
                "Site": "https://acme.com/pricing",
                "Profile": "https://www.linkedin.com/in/jane-doe",
                "Org Profile": "https://linkedin.com/company/acme",
            }
        ],
    )

    assert suggestions["Primary"].target_field == "contact.email"
    assert suggestions["Primary"].confidence == "medium"
    assert suggestions["Primary"].reason_code == "sample_email"
    assert suggestions["Site"].target_field == "account.website"
    assert suggestions["Site"].reason_code == "sample_website"
    assert suggestions["Profile"].target_field == "contact.linkedin_url"
    assert suggestions["Profile"].reason_code == "sample_person_linkedin"
    assert suggestions["Org Profile"].target_field == "account.linkedin_url"
    assert suggestions["Org Profile"].reason_code == "sample_account_linkedin"


def test_alias_and_sample_agreement_is_high_confidence() -> None:
    suggestions = _by_header(
        ["Email", "Website", "Company LinkedIn"],
        [
            {
                "Email": "jane@example.com",
                "Website": "acme.com",
                "Company LinkedIn": "linkedin.com/company/acme",
            }
        ],
    )

    assert suggestions["Email"].confidence == "high"
    assert suggestions["Email"].reason_code == "alias_and_sample"
    assert suggestions["Website"].confidence == "high"
    assert suggestions["Company LinkedIn"].confidence == "high"


def test_unknown_headers_are_unmapped_with_short_reason_code() -> None:
    suggestions = _by_header(["Favorite Color"], [{"Favorite Color": "blue"}])

    assert suggestions["Favorite Color"].target_field is None
    assert suggestions["Favorite Color"].confidence == "unmapped"
    assert suggestions["Favorite Color"].reason_code == "no_match"


def test_duplicate_alias_targets_are_left_unmapped_instead_of_guessed() -> None:
    """Equal header matches must not silently map two columns to one CRM field."""
    suggestions = _by_header(["Company", "Account Name"], [])

    assert suggestions["Company"].target_field is None
    assert suggestions["Account Name"].target_field is None
    assert suggestions["Company"].reason_code == "duplicate_or_ambiguous_target"


def test_custom_field_keys_map_but_duplicate_labels_remain_unmapped() -> None:
    suggestions = suggest_csv_mappings(
        ["renewal_region", "Region"],
        [{"renewal_region": "APAC", "Region": "APAC"}],
        targets=[
            {
                "target_field": "deal.custom.renewal_region",
                "label": "Region",
                "custom_field_key": "renewal_region",
                "value_type": "text",
            },
            {
                "target_field": "deal.custom.sales_region",
                "label": "Region",
                "custom_field_key": "sales_region",
                "value_type": "text",
            },
        ],
    )

    assert suggestions[0].target_field == "deal.custom.renewal_region"
    assert suggestions[1].target_field is None
