from datetime import UTC, datetime

from src.service.integrations.crm.records.salesforce.contacts.contact_normalizer import (
    normalize_contacts,
)


def test_normalize_contacts_maps_canonical_fields_and_lowercases_email() -> None:
    normalized, skipped = normalize_contacts(
        [
            {
                "Id": "003A",
                "FirstName": "Alex",
                "LastName": "Morgan",
                "Email": " Owner@Acme.com ",
                "Title": "VP Sales",
                "Department": "Sales",
                "Phone": "123",
                "MobilePhone": "456",
                "LinkedIn_URL__c": "https://www.linkedin.com/in/alex-morgan",
                "AccountId": "001A",
                "OwnerId": "005A",
                "MailingStreet": "1 Main St",
                "MailingCity": "Pune",
                "MailingState": "MH",
                "MailingPostalCode": "411001",
                "MailingCountry": "India",
                "CreatedDate": "2026-04-23T12:00:00.000+0000",
                "LastModifiedDate": "2026-04-23T12:30:00.000+0000",
                "LastActivityDate": "2026-04-22",
                "Custom_Status__c": "VIP",
            }
        ]
    )

    assert skipped == 0
    assert len(normalized) == 1
    contact = normalized[0]
    assert contact.external_id == "003A"
    assert contact.email == "owner@acme.com"
    assert contact.full_name == "Alex Morgan"
    assert contact.job_title == "VP Sales"
    assert contact.linkedin_url == "https://www.linkedin.com/in/alex-morgan"
    assert contact.company_external_id == "001A"
    assert contact.owner_external_id == "005A"
    assert contact.city == "Pune"
    assert contact.last_activity_at == datetime(2026, 4, 22, tzinfo=UTC)
    assert contact.properties["salesforce_object"] == "Contact"
    assert contact.properties["salesforce_department"] == "Sales"
    assert contact.properties["raw_fields"]["Custom_Status__c"] == "VIP"


def test_normalize_contacts_skips_rows_without_usable_id() -> None:
    normalized, skipped = normalize_contacts([{"Email": "missing@example.com"}])

    assert normalized == []
    assert skipped == 1
