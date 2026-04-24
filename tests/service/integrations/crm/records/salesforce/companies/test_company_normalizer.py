from src.service.integrations.crm.records.salesforce.companies.company_normalizer import (
    normalize_accounts,
)


def test_normalize_accounts_maps_canonical_fields_and_derives_domain_from_website() -> None:
    normalized, skipped = normalize_accounts(
        [
            {
                "Id": "001A",
                "Name": "Acme",
                "Website": "https://acme.com/pricing",
                "Description": "Key account",
                "Industry": "Software",
                "Type": "Customer",
                "OwnerId": "005A",
                "NumberOfEmployees": 42,
                "AnnualRevenue": 1000.5,
                "Phone": "123",
                "BillingStreet": "1 Main St",
                "BillingCity": "Pune",
                "BillingState": "MH",
                "BillingCountry": "India",
                "BillingPostalCode": "411001",
                "CreatedDate": "2026-04-23T12:00:00.000+0000",
                "LastModifiedDate": "2026-04-23T12:30:00.000+0000",
                "Custom_Status__c": "VIP",
            }
        ]
    )

    assert skipped == 0
    assert len(normalized) == 1
    company = normalized[0]
    assert company.external_id == "001A"
    assert company.name == "Acme"
    assert company.domain == "acme.com"
    assert company.company_type == "Customer"
    assert company.owner_external_id == "005A"
    assert company.number_of_employees == 42
    assert company.annual_revenue == 1000.5
    assert company.city == "Pune"
    assert company.properties["salesforce_object"] == "Account"
    assert company.properties["raw_fields"]["Custom_Status__c"] == "VIP"


def test_normalize_accounts_skips_rows_without_usable_id() -> None:
    normalized, skipped = normalize_accounts([{"Name": "Missing Id"}])

    assert normalized == []
    assert skipped == 1
