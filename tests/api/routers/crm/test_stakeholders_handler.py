"""Comprehensive tests for CRM Stakeholders API endpoints.

Tests cover:
- Response structure validation (all fields present with correct types)
- Business logic (filtering, sorting, deduplication)
- Edge cases (special characters, empty data, boundaries)
- Error handling (invalid IDs, not found scenarios)
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from src.api.routers.crm.contacts_handler import get_contact_read_service
from src.api.routers.crm.stakeholders_handler import _to_deal_job_response
from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    DecisionPower,
    StakeholderSource,
    StakeholderStance,
    StakeholderTemperature,
)

from tests.api.routers.crm.conftest import (
    FakeStakeholder,
    create_contact,
    create_stakeholder,
)


class TestCreateStakeholder:
    """Tests for POST /{deal_id}/stakeholders endpoint."""

    def test_deal_job_response_conversion_accepts_string_backed_job_fields(self):
        """Create response job mapping handles ORM jobs hydrated with string enum values."""
        job_id = uuid4()
        response = _to_deal_job_response(
            SimpleNamespace(
                job_id=job_id,
                job_type="deal_email_rematch",
                status="pending",
            )
        )

        assert response.job_id == job_id
        assert response.job_type == "deal_email_rematch"
        assert response.status == "pending"

    def test_deal_job_response_conversion_accepts_enum_backed_job_fields(self):
        """Create response job mapping still handles fake/unit jobs using enum values."""
        job_id = uuid4()
        response = _to_deal_job_response(
            SimpleNamespace(
                job_id=job_id,
                job_type=DealJobType.DEAL_EMAIL_REMATCH,
                status=JobStatus.PENDING,
            )
        )

        assert response.job_id == job_id
        assert response.job_type == "deal_email_rematch"
        assert response.status == "pending"

    def test_create_stakeholder_returns_nested_stakeholder_with_processing_jobs(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Manual create response exposes stakeholder plus reconnectable processing jobs."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={
                "email": "new.buyer@example.com",
                "name": "New Buyer",
                "job_title": "VP Engineering",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Stakeholder created successfully"
        assert body["data"]["stakeholder"]["email"] == "new.buyer@example.com"
        assert body["data"]["stakeholder"]["name"] == "New Buyer"
        assert [job["job_type"] for job in body["data"]["processing_jobs"]] == [
            "deal_email_rematch",
            "deal_calendar_rematch",
            "stakeholder_sync",
        ]
        assert {job["status"] for job in body["data"]["processing_jobs"]} == {"pending"}
        assert "stakeholder_research" not in {
            job["job_type"] for job in body["data"]["processing_jobs"]
        }
        assert body["data"]["research_job"] is None

    def test_create_name_only_stakeholder_returns_placeholder_without_jobs(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Name-only stakeholders are captured but skipped by email/domain workflows."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={"name": "Jane Buyer"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["stakeholder"]["email"] is None
        assert body["data"]["stakeholder"]["name"] == "Jane Buyer"
        assert body["data"]["processing_jobs"] == []
        assert body["data"]["research_job"] is None

    def test_create_stakeholder_rejects_legacy_identity_only_payload(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Create no longer accepts the old identity key; callers must classify input first."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={"identity": "Jane Buyer"},
        )

        assert resp.status_code == 422

    def test_create_stakeholder_rejects_external_links_only_payload(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Create does not infer stakeholder identity from generic external links."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={
                "external_links": [{"platform": "linkedin", "url": "https://linkedin.com/in/jane"}]
            },
        )

        assert resp.status_code == 422

    def test_create_linkedin_only_stakeholder_accepts_direct_linkedin_url(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
    ):
        """The add modal posts linkedin_url directly, so the API must not require external_links."""
        linkedin_url = "https://www.linkedin.com/in/direct-buyer"
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={"linkedin_url": linkedin_url},
        )

        assert resp.status_code == 201
        body = resp.json()
        stakeholder = body["data"]["stakeholder"]
        assert stakeholder["email"] is None
        assert stakeholder["linkedin_url"] == linkedin_url
        assert body["data"]["processing_jobs"] == []
        assert body["data"]["research_job"] is None

        stored_stakeholders = fake_stakeholders_service._stakeholders[test_deal_id]
        assert stored_stakeholders[-1].linkedin_url == linkedin_url

    def test_create_stakeholder_from_contact_id_uses_contact_profile_and_skips_cached_research(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Selected contacts can be added with only contact_id and reuse cached research."""
        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="selected@example.com",
            full_name="Selected Buyer",
            job_title="Chief Revenue Officer",
            phone="+1 555-0101",
            linkedin_url="https://linkedin.com/in/selected-buyer",
            contact_research="Existing contact research",
            last_researched_at=datetime.now(UTC),
        )
        fake_stakeholders_service.add_contact(contact)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={"contact_id": str(contact.contact_id)},
        )

        assert resp.status_code == 201
        body = resp.json()
        stakeholder = body["data"]["stakeholder"]
        assert stakeholder["contact_id"] == str(contact.contact_id)
        assert stakeholder["email"] == "selected@example.com"
        assert stakeholder["name"] == "Selected Buyer"
        assert stakeholder["job_title"] == "Chief Revenue Officer"
        assert stakeholder["phone_number"] == "+1 555-0101"
        assert stakeholder["linkedin_url"] == "https://linkedin.com/in/selected-buyer"
        assert [job["job_type"] for job in body["data"]["processing_jobs"]] == [
            "deal_email_rematch",
            "deal_calendar_rematch",
            "stakeholder_sync",
        ]
        assert body["data"]["research_job"] is None

    def test_create_stakeholder_from_contact_id_dispatches_contact_research_when_missing(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Contact-backed stakeholder creates should use canonical contact research."""
        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="selected@example.com",
            full_name="Selected Buyer",
            job_title="Chief Revenue Officer",
            phone="+1 555-0101",
            linkedin_url="https://linkedin.com/in/selected-buyer",
        )
        fake_stakeholders_service.add_contact(contact)
        research_job_id = uuid4()

        with patch(
            "src.workers.deal_worker.utils.contact_research_dispatch."
            "dispatch_contact_research_async",
            new_callable=AsyncMock,
        ) as dispatch_contact_research:
            dispatch_contact_research.return_value = str(research_job_id)
            resp = client.post(
                f"/users/crm/deals/{test_deal_id}/stakeholders",
                json={"contact_id": str(contact.contact_id)},
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["research_job"] == {
            "job_id": str(research_job_id),
            "job_type": "contact_research",
            "status": "pending",
        }
        dispatch_contact_research.assert_awaited_once_with(
            organisation_id=test_user_ids["organisation_id"],
            contact_id=contact.contact_id,
            user_id=test_user_ids["user_id"],
            trigger="stakeholder_created",
            force_refresh=False,
        )

    def test_create_stakeholder_without_contact_does_not_dispatch_research(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Unlinked stakeholders only queue processing jobs; research needs a contact."""
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={
                "email": "research.auto@example.com",
                "name": "Research Auto",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["research_job"] is None
        assert "stakeholder_research" not in {
            job["job_type"] for job in body["data"]["processing_jobs"]
        }

    def test_create_reactivates_deleted_stakeholder_with_processing_jobs(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Re-adding a deleted stakeholder restores the row and returns visible jobs."""
        removed = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="removed-again@example.com",
            name="Old Name",
            job_title="Old Title",
        )
        removed.is_deleted = True
        fake_stakeholders_service.add_stakeholder(removed)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={
                "email": "removed-again@example.com",
                "name": "Restored Name",
                "job_title": "Restored Title",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["stakeholder"]["participant_id"] == str(removed.participant_id)
        assert "stakeholder_id" not in body["data"]["stakeholder"]
        assert body["data"]["stakeholder"]["approval_status"] == "APPROVED"
        assert body["data"]["stakeholder"]["name"] == "Restored Name"
        assert body["data"]["stakeholder"]["job_title"] == "Restored Title"
        assert [job["job_type"] for job in body["data"]["processing_jobs"]] == [
            "deal_email_rematch",
            "deal_calendar_rematch",
            "stakeholder_sync",
        ]
        assert body["data"]["research_job"] is None

    def test_create_duplicate_active_stakeholder_updates_existing_row(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Duplicate create payloads update the existing stakeholder row."""
        active = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="already-active@example.com",
            name="Already Active",
        )
        fake_stakeholders_service.add_stakeholder(active)

        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders",
            json={
                "email": "already-active@example.com",
                "name": "Duplicate",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["data"]["stakeholder"]["participant_id"] == str(active.participant_id)
        assert body["data"]["stakeholder"]["name"] == "Duplicate"


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
        assert body["data"]["stakeholders"] == []

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
        assert len(body["data"]["stakeholders"]) == 1

        data = body["data"]["stakeholders"][0]
        # Verify required fields
        assert data["participant_id"] == str(stakeholder.participant_id)
        assert "stakeholder_id" not in data
        assert data["deal_id"] == str(test_deal_id)
        assert data["email"] == "john.doe@acme.com"
        assert data["name"] == "John Doe"
        assert data["job_title"] == "VP of Engineering"

        # Verify enum fields are string values
        assert data["source"] == "CRM"
        assert data["stance"] == "CHAMPION"
        assert data["temperature"] == "HOT"
        assert data["decision_power"] == "DECISION_MAKER"

        assert "engagement_score" not in data
        assert "champion_potential" not in data

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
        assert len(body["data"]["stakeholders"]) == 4

        emails = {s["email"] for s in body["data"]["stakeholders"]}
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
        assert len(body["data"]["stakeholders"]) == 1
        assert body["data"]["stakeholders"][0]["email"] == "active@example.com"

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
        assert len(body["data"]["stakeholders"]) == 5

        returned_names = {s["name"] for s in body["data"]["stakeholders"]}
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
        assert len(body["data"]["stakeholders"]) == 1

        data = body["data"]["stakeholders"][0]
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
        assert len(body["data"]["stakeholders"]) == 4

        returned_sources = {s["source"] for s in body["data"]["stakeholders"]}
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


class TestRemovedContactStakeholderRoutes:
    """Removed contact-picker routes should not be part of the public API."""

    def test_available_contacts_route_uses_contact_search(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        contact_id = uuid4()
        captured = {}

        class FakeContactSearchService:
            async def close(self):
                pass

            async def search_contacts(self, *, q, limit, cursor, company_id):
                captured.update(
                    {"q": q, "limit": limit, "cursor": cursor, "company_id": company_id}
                )
                return [
                    {
                        "contact_id": contact_id,
                        "full_name": "Sam Buyer",
                        "email": "sam@example.com",
                        "job_title": "CFO",
                        "company": {"id": None, "name": "Example", "domain": "example.com"},
                        "match_type": "name_prefix",
                    }
                ], None

        async def override_contact_search_service():
            yield FakeContactSearchService()

        client.app.dependency_overrides[get_contact_read_service] = override_contact_search_service

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/available-contacts",
            params={"query": "sam", "limit": 10},
        )

        assert resp.status_code == 200
        assert captured == {"q": "sam", "limit": 10, "cursor": None, "company_id": None}
        assert resp.json()["data"] == [
            {
                "contact_id": str(contact_id),
                "email": "sam@example.com",
                "first_name": None,
                "last_name": None,
                "full_name": "Sam Buyer",
                "job_title": "CFO",
                "company_name": "Example",
                "phone": None,
                "lifecycle_stage": None,
                "last_activity_at": None,
            }
        ]
        assert (
            "/users/crm/deals/{deal_id}/stakeholders/available-contacts"
            in client.app.openapi()["paths"]
        )

    def test_add_from_contacts_route_is_removed(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        resp = client.post(
            f"/users/crm/deals/{test_deal_id}/stakeholders/add-from-contacts",
            json={"contact_ids": [str(uuid4())]},
        )

        assert resp.status_code != 200
        assert (
            "/users/crm/deals/{deal_id}/stakeholders/add-from-contacts"
            not in client.app.openapi()["paths"]
        )


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
        assert data["participant_id"] == str(stakeholder.participant_id)
        assert "stakeholder_id" not in data
        assert data["deal_id"] == str(test_deal_id)
        assert data["email"] == "john.doe@acme.com"
        assert data["name"] == "John Doe"
        assert data["job_title"] == "VP of Engineering"
        assert data["department"] == "Engineering"
        assert data["stance"] == "CHAMPION"
        assert data["temperature"] == "HOT"
        assert data["decision_power"] == "DECISION_MAKER"
        assert "engagement_score" not in data
        assert "champion_potential" not in data
        assert data["source"] == "CRM"
        assert data["approval_status"] == "APPROVED"
        assert "engagement_history" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_returns_active_research_job_for_reconnect(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        fake_deal_job_service,
        test_deal_id,
        test_user_ids,
    ):
        """Stakeholder detail exposes active contact research job for SSE reconnect."""
        from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus

        from tests.api.routers.crm.conftest import FakeDealJob

        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="research.active@acme.com",
        )
        fake_stakeholders_service.add_contact(contact)
        stakeholder = FakeStakeholder(
            stakeholder_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="research.active@acme.com",
            name="Research Active",
            source=StakeholderSource.MANUAL,
            contact_id=contact.contact_id,
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)
        job = FakeDealJob(
            job_id=uuid4(),
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            job_type=DealJobType.CONTACT_RESEARCH,
            status=JobStatus.IN_PROGRESS,
        )
        job.job_metadata = {"contact_id": str(contact.contact_id)}
        fake_deal_job_service.add_job(job)

        resp = client.get(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        active_job = resp.json()["data"]["active_research_job"]
        assert active_job == {
            "job_id": str(job.job_id),
            "job_type": "contact_research",
            "status": "in_progress",
        }

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
                "decision_power": "DECISION_MAKER",
            },
            {
                "recorded_at": "2025-01-10T10:00:00Z",
                "stance": "SUPPORTER",
                "temperature": "WARM",
                "decision_power": "INFLUENCER",
            },
            {
                "recorded_at": "2025-01-01T10:00:00Z",
                "stance": "NEUTRAL",
                "temperature": "COLD",
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
        assert "engagement_score" not in data["engagement_history"][0]
        assert "champion_potential" not in data["engagement_history"][0]

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


class TestUpdateStakeholder:
    """Tests for PATCH /{deal_id}/stakeholders/{stakeholder_id} endpoint."""

    def test_updates_selected_fields(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """PATCH should persist editable stakeholder fields and enforce manual overrides."""
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="jane@acme.com",
            name="Jane Old",
            job_title="Manager",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.patch(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}",
            json={
                "name": "Jane Updated",
                "job_title": "Director of Engineering",
                "motivation": "Improve operational reliability",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Stakeholder updated"
        assert body["data"]["name"] == "Jane Updated"
        assert body["data"]["job_title"] == "Director of Engineering"
        assert body["data"]["motivation"] == "Improve operational reliability"

        updated = fake_stakeholders_service._get_stakeholder(
            test_deal_id,
            stakeholder.stakeholder_id,
        )
        assert updated is not None
        assert updated.stakeholder_data is not None
        assert set(updated.stakeholder_data["manual_overrides"]) >= {
            "name",
            "job_title",
            "motivation",
        }

    def test_returns_404_for_unknown_stakeholder(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Unknown stakeholder id should return 404 from update endpoint."""
        resp = client.patch(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{uuid4()}",
            json={"name": "Updated"},
        )

        assert resp.status_code == 404
        body = resp.json()
        assert body["status"] == "ERROR"
        assert body["message"] == "Stakeholder not found"

    def test_returns_400_for_empty_payload(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Empty update payload should be rejected as invalid request."""
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="empty@acme.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.patch(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}",
            json={},
        )

        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "ERROR"
        assert body["message"] == "No fields provided for update"

    def test_returns_422_for_invalid_decision_power(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Unknown decision power value should return validation error."""
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="invalid@acme.com",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.patch(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}",
            json={"decision_power": "NOT_A_ROLE"},
        )

        assert resp.status_code == 422


class TestRemoveStakeholder:
    """Tests for DELETE /{deal_id}/stakeholders/{stakeholder_id} endpoint."""

    def test_successfully_removes_stakeholder(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Verify stakeholder can be removed without legacy status gates."""
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="to-remove@example.com",
            name="To Remove",
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.delete(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Stakeholder removed"
        assert body["data"]["approval_status"] == "APPROVED"
        assert stakeholder.is_deleted is True

    def test_removes_legacy_pending_stakeholder(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Legacy pending rows are still removable after dropping approvals."""
        from src.domain.models.organisation_tables.sync.crm.stakeholders import ApprovalStatus

        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="pending@example.com",
        )
        stakeholder.approval_status = ApprovalStatus.PENDING
        fake_stakeholders_service.add_stakeholder(stakeholder)

        resp = client.delete(
            f"/users/crm/deals/{test_deal_id}/stakeholders/{stakeholder.stakeholder_id}"
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert stakeholder.is_deleted is True

    def test_returns_404_for_nonexistent_stakeholder(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify 404 when stakeholder doesn't exist."""
        nonexistent_id = uuid4()

        resp = client.delete(f"/users/crm/deals/{test_deal_id}/stakeholders/{nonexistent_id}")

        assert resp.status_code == 404

    def test_returns_422_for_invalid_stakeholder_id(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        test_deal_id,
    ):
        """Verify 422 for malformed stakeholder ID."""
        resp = client.delete(f"/users/crm/deals/{test_deal_id}/stakeholders/not-a-uuid")

        assert resp.status_code == 422


class TestTriggerStakeholderContactResearch:
    """Tests for POST /{deal_id}/stakeholders/{stakeholder_id}/research endpoint."""

    def test_returns_deal_job_response_for_manual_research(
        self,
        client: TestClient,
        stakeholders_dependency_overrides,
        fake_stakeholders_service,
        test_deal_id,
        test_user_ids,
    ):
        """Manual research returns the same generic job DTO used by other deal jobs."""
        contact = create_contact(
            organisation_id=test_user_ids["organisation_id"],
            email="research.target@example.com",
        )
        fake_stakeholders_service.add_contact(contact)
        stakeholder = create_stakeholder(
            deal_id=test_deal_id,
            organisation_id=test_user_ids["organisation_id"],
            email="research.target@example.com",
            contact_id=contact.contact_id,
        )
        fake_stakeholders_service.add_stakeholder(stakeholder)
        job_id = uuid4()

        with patch(
            "src.workers.deal_worker.utils.contact_research_dispatch."
            "dispatch_contact_research_async",
            new_callable=AsyncMock,
            return_value=str(job_id),
        ) as dispatch_research:
            resp = client.post(
                f"/users/crm/deals/{test_deal_id}/stakeholders/"
                f"{stakeholder.stakeholder_id}/research"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SUCCESS"
        assert body["message"] == "Research job dispatched"
        assert body["data"] == {
            "job_id": str(job_id),
            "job_type": "contact_research",
            "status": "pending",
        }
        dispatch_research.assert_awaited_once_with(
            organisation_id=test_user_ids["organisation_id"],
            contact_id=contact.contact_id,
            user_id=test_user_ids["user_id"],
            trigger="stakeholder_created",
            force_refresh=True,
        )
