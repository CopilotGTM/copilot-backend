"""Comprehensive tests for CRM Stakeholders API endpoints.

Tests cover:
- Response structure validation (all fields present with correct types)
- Business logic (filtering, sorting, deduplication)
- Edge cases (special characters, empty data, boundaries)
- Error handling (invalid IDs, not found scenarios)
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)

from tests.api.routers.crm.conftest import (
    FakeContact,
    FakeStakeholder,
    create_contact,
    create_stakeholder,
)


class TestGetDealStakeholders:
    """Tests for GET /{deal_id}/stakeholders endpoint."""

    def test_returns_empty_list_for_deal_with_no_stakeholders(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify empty list is returned when deal has no stakeholders."""
        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Stakeholders retrieved"
        assert body["data"] == []

    def test_returns_all_stakeholder_fields_correctly(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify all stakeholder fields are present and correctly typed in response."""
        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="john.doe@acme.com",
            name="John Doe",
            job_title="VP of Engineering",
            source=StakeholderSource.CRM,
            stance=StakeholderStance.CHAMPION,
            temperature=StakeholderTemperature.HOT,
            decision_power=DecisionPower.DECISION_MAKER,
            engagement_score=85.5,
            champion_potential=0.92,
            motivation="Looking to modernize tech stack",
            pain_points=["Legacy systems", "Slow deployments"],
            keywords=["automation", "cloud", "DevOps"],
            last_contact_at=datetime.now(UTC) - timedelta(days=3),
            external_links=[{"platform": "linkedin", "url": "https://linkedin.com/in/johndoe"}],
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1

        data = body["data"][0]
        # Verify required fields
        assert data["stakeholder_id"] == str(stakeholder.stakeholder_id)
        assert data["deal_id"] == str(test_deal_id)
        assert data["email"] == "john.doe@acme.com"
        assert data["name"] == "John Doe"
        assert data["job_title"] == "VP of Engineering"

        # Verify enum fields are string values
        assert data["source"] == "CRM"
        assert data["stance"] == "CHAMPION"
        assert data["temperature"] == "HOT"
        assert data["decision_power"] == "DECISION_MAKER"

        # Verify numeric fields
        assert data["engagement_score"] == 85.5
        assert data["champion_potential"] == 0.92

        # Verify list fields
        assert data["pain_points"] == ["Legacy systems", "Slow deployments"]
        assert data["keywords"] == ["automation", "cloud", "DevOps"]
        assert len(data["external_links"]) == 1
        assert data["external_links"][0]["platform"] == "linkedin"

    def test_returns_multiple_stakeholders_for_deal(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify multiple stakeholders are returned for a single deal."""
        stakeholders_data = [
            {"email": "ceo@company.com", "name": "Alice CEO", "job_title": "CEO"},
            {"email": "cto@company.com", "name": "Bob CTO", "job_title": "CTO"},
            {"email": "cfo@company.com", "name": "Carol CFO", "job_title": "CFO"},
            {"email": "vp.sales@company.com", "name": "Dan VP Sales", "job_title": "VP Sales"},
        ]

        for data in stakeholders_data:
            stakeholder = create_stakeholder(
                deal_id=test_deal_id, organisation_id=test_user_ids["organisation_id"], **data
            )
            fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 4

        emails = {s["email"] for s in body["data"]}
        assert emails == {
            "ceo@company.com",
            "cto@company.com",
            "cfo@company.com",
            "vp.sales@company.com",
        }

    def test_excludes_soft_deleted_stakeholders(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify soft-deleted stakeholders are not included in response."""
        # Active stakeholder
        active = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="active@example.com",
            name="Active User",
        )
        fake_stakeholders_service.add_stakeholder(active)

        # Deleted stakeholder
        deleted = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="deleted@example.com",
            name="Deleted User",
        )
        deleted.is_deleted = True
        fake_stakeholders_service.add_stakeholder(deleted)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["email"] == "active@example.com"

    def test_handles_stakeholders_with_special_characters_in_name(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify stakeholders with special characters in name are handled correctly."""
        special_names = [
            "José García-López",
            "François O'Brien",
            "李明 (Li Ming)",
            "Müller & Associates",
            'Name with "quotes"',
        ]

        for name in special_names:
            stakeholder = create_stakeholder(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                name=name,
            )
            fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 5

        returned_names = {s["name"] for s in body["data"]}
        assert returned_names == set(special_names)

    def test_handles_stakeholders_with_null_optional_fields(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify stakeholders with null optional fields are returned correctly."""
        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="minimal@example.com",
            name=None,
            job_title=None,
            motivation=None,
            last_contact_at=None,
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1

        data = body["data"][0]
        assert data["email"] == "minimal@example.com"
        assert data["name"] is None
        assert data["job_title"] is None
        assert data["motivation"] is None
        assert data["last_contact_at"] is None

    def test_handles_stakeholders_from_different_sources(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify stakeholders from different sources have correct source values."""
        sources = [
            StakeholderSource.CRM,
            StakeholderSource.MANUAL,
            StakeholderSource.CALENDAR_DISCOVERY,
            StakeholderSource.EMAIL_DISCOVERY,
        ]

        for source in sources:
            stakeholder = create_stakeholder(
                deal_id=test_deal_id,
                organisation_id=test_user_ids["organisation_id"],
                source=source,
            )
            fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 4

        returned_sources = {s["source"] for s in body["data"]}
        assert returned_sources == {"CRM", "MANUAL", "CALENDAR_DISCOVERY", "EMAIL_DISCOVERY"}

    def test_returns_422_for_invalid_deal_id_format(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
    ):
        """Verify 422 error for malformed deal ID."""
        # Note: Empty string "" returns 404 (route not found) rather than 422
        invalid_ids = ["not-a-uuid", "12345", "null", "undefined"]

        for invalid_id in invalid_ids:
            resp = client.get(f"/users/crm/deals/{invalid_id}/stakeholders")
            assert resp.status_code == 422, f"Expected 422 for deal_id={invalid_id}"


class TestListAvailableContacts:
    """Tests for GET /{deal_id}/stakeholders/available-contacts endpoint."""

    def test_returns_empty_list_when_no_contacts_exist(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify empty list when no contacts are available."""
        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["data"] == []

    def test_returns_all_contact_fields_correctly(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify all contact fields are present in response."""
        contact = FakeContact(
            contact_id=uuid4(),
            organisation_id=test_user_ids["organisation_id"],
            email="contact@company.com",
            full_name="Jane Contact",
            job_title="Senior Manager",
            company_name="Acme Corp",
            external_id="hubspot-123456",
        )
        fake_stakeholders_service.add_contact(contact)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1

        data = body["data"][0]
        assert data["contact_id"] == str(contact.contact_id)
        assert data["external_id"] == "hubspot-123456"
        assert data["email"] == "contact@company.com"
        assert data["full_name"] == "Jane Contact"
        assert data["job_title"] == "Senior Manager"
        assert data["company_name"] == "Acme Corp"

    def test_excludes_contacts_already_added_as_stakeholders(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify contacts that are already stakeholders are excluded."""
        # Add stakeholder with specific email
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="existing@example.com",
            name="Existing Stakeholder",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        # Add contacts - one matching stakeholder email, one new
        existing_contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="existing@example.com",  # Same as stakeholder
            full_name="Existing Contact",
        )
        new_contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="new@example.com",
            full_name="New Contact",
        )
        fake_stakeholders_service.add_contact(existing_contact)
        fake_stakeholders_service.add_contact(new_contact)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["email"] == "new@example.com"

    def test_excludes_contacts_with_case_insensitive_email_match(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify email matching is case-insensitive."""
        # Add stakeholder with lowercase email
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="john@example.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        # Add contact with different case
        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="JOHN@EXAMPLE.COM",  # Different case
            full_name="John Contact",
        )
        fake_stakeholders_service.add_contact(contact)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 0

    def test_excludes_contacts_without_email(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify contacts without email are excluded."""
        contact_no_email = FakeContact(
            contact_id=uuid4(),
            organisation_id=test_user_ids["organisation_id"],
            email=None,
            full_name="No Email Contact",
        )
        contact_with_email = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="has.email@example.com",
            full_name="Has Email Contact",
        )
        fake_stakeholders_service.add_contact(contact_no_email)
        fake_stakeholders_service.add_contact(contact_with_email)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["email"] == "has.email@example.com"

    def test_filters_contacts_by_name_query(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify contacts can be filtered by name search query."""
        contacts = [
            {"full_name": "Alice Johnson", "email": "alice@example.com"},
            {"full_name": "Bob Smith", "email": "bob@example.com"},
            {"full_name": "Alice Williams", "email": "alice.w@example.com"},
        ]

        for data in contacts:
            contact = create_contact(organisation_id=test_user_ids["organisation_id"], **data)
            fake_stakeholders_service.add_contact(contact)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts",
            params={"query": "alice"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 2

        names = {c["full_name"] for c in body["data"]}
        assert names == {"Alice Johnson", "Alice Williams"}

    def test_filters_contacts_by_email_query(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify contacts can be filtered by email search query."""
        contacts = [
            {"full_name": "Alice", "email": "alice@acme.com"},
            {"full_name": "Bob", "email": "bob@acme.com"},
            {"full_name": "Carol", "email": "carol@other.com"},
        ]

        for data in contacts:
            contact = create_contact(organisation_id=test_user_ids["organisation_id"], **data)
            fake_stakeholders_service.add_contact(contact)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts",
            params={"query": "acme"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 2

    def test_filters_contacts_by_job_title_query(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify contacts can be filtered by job title search query."""
        contacts = [
            {"full_name": "Alice", "email": "alice@example.com", "job_title": "Software Engineer"},
            {"full_name": "Bob", "email": "bob@example.com", "job_title": "Product Manager"},
            {"full_name": "Carol", "email": "carol@example.com", "job_title": "Senior Engineer"},
        ]

        for data in contacts:
            contact = create_contact(organisation_id=test_user_ids["organisation_id"], **data)
            fake_stakeholders_service.add_contact(contact)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts",
            params={"query": "engineer"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 2

        titles = {c["job_title"] for c in body["data"]}
        assert "Software Engineer" in titles
        assert "Senior Engineer" in titles

    def test_query_is_case_insensitive(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify search query is case-insensitive."""
        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="test@example.com",
            full_name="John DOE",
        )
        fake_stakeholders_service.add_contact(contact)

        # Test different cases
        for query in ["john", "JOHN", "John", "jOhN"]:
            resp = client.get(
                f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts",
                params={"query": query},
            )
            assert resp.status_code == 200
            assert len(resp.json()["data"]) == 1, f"Query '{query}' should match"

    def test_respects_limit_parameter(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify limit parameter restricts number of results."""
        # Add 10 contacts
        for i in range(10):
            contact = create_contact(
                organisation_id=test_user_ids["organisation_id"],
                email=f"contact{i}@example.com",
                full_name=f"Contact {i}",
            )
            fake_stakeholders_service.add_contact(contact)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts",
            params={"limit": 3},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 3

    def test_default_limit_is_applied(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify default limit of 50 is applied."""
        # Add 60 contacts
        for i in range(60):
            contact = create_contact(
                organisation_id=test_user_ids["organisation_id"],
                email=f"contact{i}@example.com",
            )
            fake_stakeholders_service.add_contact(contact)

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 50  # Default limit


class TestAddContactsAsStakeholders:
    """Tests for POST /{deal_id}/stakeholders/add-from-contacts endpoint."""

    def test_successfully_adds_single_contact_as_stakeholder(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify single contact can be added as stakeholder."""
        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="new.stakeholder@example.com",
            full_name="New Stakeholder",
            job_title="Director",
        )
        fake_stakeholders_service.add_contact(contact)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": [str(contact.contact_id)]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert len(body["data"]["added"]) == 1
        assert body["data"]["skipped"] == []

        added = body["data"]["added"][0]
        assert added["email"] == "new.stakeholder@example.com"
        assert added["name"] == "New Stakeholder"
        assert "stakeholder_id" in added

    def test_successfully_adds_multiple_contacts_as_stakeholders(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify multiple contacts can be added at once."""
        contacts = []
        for i in range(3):
            contact = create_contact(
                organisation_id=test_user_ids["organisation_id"],
                email=f"contact{i}@example.com",
                full_name=f"Contact {i}",
            )
            fake_stakeholders_service.add_contact(contact)
            contacts.append(contact)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": [str(c.contact_id) for c in contacts]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]["added"]) == 3
        assert body["data"]["skipped"] == []

    def test_skips_contacts_already_added_as_stakeholders(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify existing stakeholders are skipped (not duplicated)."""
        # Add existing stakeholder
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="existing@example.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        # Add contact with same email
        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="existing@example.com",
            full_name="Existing Contact",
        )
        fake_stakeholders_service.add_contact(contact)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": [str(contact.contact_id)]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["added"] == []
        assert len(body["data"]["skipped"]) == 1
        assert body["data"]["skipped"][0]["contact_id"] == str(contact.contact_id)
        assert "already exists" in body["data"]["skipped"][0]["reason"]

    def test_skips_contacts_without_email(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify contacts without email are skipped."""
        contact = FakeContact(
            contact_id=uuid4(),
            organisation_id=test_user_ids["organisation_id"],
            email=None,
            full_name="No Email Contact",
        )
        fake_stakeholders_service.add_contact(contact)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": [str(contact.contact_id)]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["added"] == []
        assert len(body["data"]["skipped"]) == 1

    def test_skips_unknown_contact_ids(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify unknown contact IDs are reported as skipped."""
        unknown_id = uuid4()

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": [str(unknown_id)]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["added"] == []
        assert len(body["data"]["skipped"]) == 1
        assert body["data"]["skipped"][0]["contact_id"] == str(unknown_id)
        assert "not found" in body["data"]["skipped"][0]["reason"]

    def test_handles_mixed_valid_and_invalid_contacts(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify mix of valid, invalid, and existing contacts are handled correctly."""
        # Valid contact
        valid_contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="valid@example.com",
            full_name="Valid Contact",
        )
        fake_stakeholders_service.add_contact(valid_contact)

        # Contact without email
        no_email_contact = FakeContact(
            contact_id=uuid4(),
            organisation_id=test_user_ids["organisation_id"],
            email=None,
            full_name="No Email",
        )
        fake_stakeholders_service.add_contact(no_email_contact)

        # Existing stakeholder
        existing_stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="existing@example.com",
        )
        fake_stakeholders_service.add_stakeholder(existing_stakeholder)

        existing_contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="existing@example.com",
        )
        fake_stakeholders_service.add_contact(existing_contact)

        # Unknown ID
        unknown_id = uuid4()

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={
                "contact_ids": [
                    str(valid_contact.contact_id),
                    str(no_email_contact.contact_id),
                    str(existing_contact.contact_id),
                    str(unknown_id),
                ]
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]["added"]) == 1  # Only valid_contact
        assert len(body["data"]["skipped"]) == 3  # no_email, existing, unknown

    def test_prevents_duplicate_emails_in_same_request(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify same email added twice in one request doesn't create duplicates."""
        contact1 = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="same@example.com",
            full_name="Contact 1",
        )
        contact2 = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="same@example.com",  # Same email
            full_name="Contact 2",
        )
        fake_stakeholders_service.add_contact(contact1)
        fake_stakeholders_service.add_contact(contact2)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": [str(contact1.contact_id), str(contact2.contact_id)]},
        )

        assert resp.status_code == 200
        body = resp.json()
        # First one should be added, second skipped as duplicate
        assert len(body["data"]["added"]) == 1
        assert len(body["data"]["skipped"]) == 1

    def test_returns_422_for_empty_contact_ids_list(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify 422 error when contact_ids list is empty."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": []},
        )

        assert resp.status_code == 422  # Validation error - min_length=1

    def test_returns_422_for_invalid_deal_id(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
    ):
        """Verify 422 error for malformed deal ID."""
        resp = client.post(
            "/users/crm/deals/not-a-uuid/stakeholders/add-from-contacts",
            json={"contact_ids": [str(uuid4())]},
        )

        assert resp.status_code == 422

    def test_returns_422_for_invalid_contact_id_format(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify 422 error for malformed contact IDs."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": ["not-a-uuid"]},
        )

        assert resp.status_code == 422


class TestGetStakeholderDetail:
    """Tests for GET /{deal_id}/stakeholders/{stakeholder_id} endpoint."""

    def test_returns_stakeholder_detail_with_all_fields(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify full stakeholder detail is returned with all fields."""
        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="john.doe@acme.com",
            name="John Doe",
            job_title="VP of Engineering",
            department="Engineering",
            source=StakeholderSource.CRM,
            stance=StakeholderStance.CHAMPION,
            temperature=StakeholderTemperature.HOT,
            decision_power=DecisionPower.DECISION_MAKER,
            engagement_score=85.5,
            champion_potential=92.0,
            motivation="Looking to modernize tech stack",
            pain_points=["Legacy systems", "Slow deployments"],
            keywords=["automation", "cloud"],
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Stakeholder detail retrieved"

        data = body["data"]
        assert data["stakeholder_id"] == str(stakeholder.stakeholder_id)
        assert data["deal_id"] == str(test_deal_id)
        assert data["email"] == "john.doe@acme.com"
        assert data["name"] == "John Doe"
        assert data["job_title"] == "VP of Engineering"
        assert data["department"] == "Engineering"
        assert data["stance"] == "CHAMPION"
        assert data["temperature"] == "HOT"
        assert data["decision_power"] == "DECISION_MAKER"
        assert data["engagement_score"] == 85.5
        assert data["champion_potential"] == 92.0
        assert data["source"] == "CRM"
        assert data["approval_status"] == "APPROVED"
        assert "engagement_history" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_returns_engagement_history(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify engagement history is returned showing metric changes over time."""
        history = [
            {
                "recorded_at": "2025-01-15T10:00:00Z",
                "stance": "CHAMPION",
                "temperature": "HOT",
                "engagement_score": 90.0,
                "champion_potential": 95.0,
                "decision_power": "DECISION_MAKER",
            },
            {
                "recorded_at": "2025-01-10T10:00:00Z",
                "stance": "SUPPORTER",
                "temperature": "WARM",
                "engagement_score": 70.0,
                "champion_potential": 75.0,
                "decision_power": "INFLUENCER",
            },
            {
                "recorded_at": "2025-01-01T10:00:00Z",
                "stance": "NEUTRAL",
                "temperature": "COLD",
                "engagement_score": 30.0,
                "champion_potential": 40.0,
                "decision_power": "UNKNOWN",
            },
        ]

        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="evolving@example.com",
            name="Evolving Stakeholder",
            stance=StakeholderStance.CHAMPION,
            temperature=StakeholderTemperature.HOT,
            engagement_score=90.0,
            champion_potential=95.0,
            decision_power=DecisionPower.DECISION_MAKER,
            stakeholder_engagement_history=history,
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]

        # Verify engagement history is present and ordered (newest first)
        assert len(data["engagement_history"]) == 3
        assert data["engagement_history"][0]["temperature"] == "HOT"
        assert data["engagement_history"][1]["temperature"] == "WARM"
        assert data["engagement_history"][2]["temperature"] == "COLD"

        # Verify we can trace the journey from COLD to HOT
        assert data["engagement_history"][2]["stance"] == "NEUTRAL"
        assert data["engagement_history"][1]["stance"] == "SUPPORTER"
        assert data["engagement_history"][0]["stance"] == "CHAMPION"

    def test_returns_empty_engagement_history_for_new_stakeholder(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify new stakeholders have empty engagement history."""
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="new@example.com",
            name="New Stakeholder",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["engagement_history"] == []

    def test_returns_404_for_nonexistent_stakeholder(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify 404 is returned when stakeholder doesn't exist."""
        nonexistent_id = uuid4()

        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/{nonexistent_id}")

        assert resp.status_code == 404
        body = resp.json()
        assert body["status"] == "ERROR"
        assert body["message"] == "Stakeholder not found"

    def test_returns_404_for_stakeholder_in_different_deal(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify 404 is returned when stakeholder exists but in different deal."""
        other_deal_id = uuid4()
        stakeholder = create_stakeholder(
            deal_id=other_deal_id,  # Different deal
            organisation_id=test_user_ids["organisation_id"],
            email="other@example.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        # Try to fetch from wrong deal
        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 404

    def test_returns_422_for_invalid_deal_id(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
    ):
        """Verify 422 is returned for malformed deal ID."""
        resp = client.get("/users/crm/deals/not-a-uuid/stakeholders/" + str(uuid4()))

        assert resp.status_code == 422

    def test_returns_422_for_invalid_stakeholder_id(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify 422 is returned for malformed stakeholder ID."""
        resp = client.get(f"/users/crm/deals/{test_deal_id}/stakeholders/not-a-uuid")

        assert resp.status_code == 422
