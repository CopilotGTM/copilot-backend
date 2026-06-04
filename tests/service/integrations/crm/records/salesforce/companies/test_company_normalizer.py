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


def test_normalize_accounts_uses_selected_custom_field_fallbacks() -> None:
    normalized, skipped = normalize_accounts(
        [
            {
                "Id": "001B",
                "Name": "CommLab India - Rapid eLearning Solutions**",
                "Website": None,
                "Domain__c": "commlabindia.com",
                "Description": None,
                "company_description_lusha_enriched__c": "Rapid eLearning provider",
                "Industry": None,
                "Cohort_Industry__c": "Education",
                "Type": None,
                "RecordType_name__c": "Prospect Account",
                "Customer_Stage__c": None,
                "Account_Status__c": None,
                "Lead_Status__c": "Non-Active Lead",
                "NumberOfEmployees": None,
                "Number_of_Employees__c": None,
                "LinkedIn_No_of_Employees__c": 200.0,
                "AnnualRevenue": None,
                "Annual_Revenue__c": "10000000",
                "BillingCountry": None,
                "ShippingCountry": None,
                "Country__c": "India",
                "LinkedIn_URL__c": "https://www.linkedin.com/company/commlab-india",
            }
        ]
    )

    assert skipped == 0
    company = normalized[0]
    assert company.name == "CommLab India - Rapid eLearning Solutions**"
    assert company.domain == "commlabindia.com"
    assert company.description == "Rapid eLearning provider"
    assert company.industry == "Education"
    assert company.company_type == "Prospect Account"
    assert company.lifecycle_stage == "Non-Active Lead"
    assert company.number_of_employees == 200
    assert company.annual_revenue == 10000000.0
    assert company.country == "India"
    assert company.linkedin_url == "https://www.linkedin.com/company/commlab-india"


def test_normalize_accounts_prefers_website_domain_over_custom_domain() -> None:
    normalized, skipped = normalize_accounts(
        [
            {
                "Id": "001C",
                "Name": "Acme",
                "Website": "https://www.acme.com/path",
                "Domain__c": "stale-custom-domain.example",
            }
        ]
    )

    assert skipped == 0
    assert normalized[0].domain == "acme.com"


def test_normalize_accounts_skips_rows_without_usable_id() -> None:
    normalized, skipped = normalize_accounts([{"Name": "Missing Id"}])

    assert normalized == []
    assert skipped == 1
