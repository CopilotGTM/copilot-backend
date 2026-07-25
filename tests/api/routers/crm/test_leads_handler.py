"""Regression tests for CRM lead router endpoints with dependency overrides.

These tests drive the leads router through create/read/participant/convert flows and
verify behavior without touching real database sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.routers.crm.leads.leads_handler import (
    get_lead_conversion_service,
    get_lead_participant_service,
    get_lead_read_service,
    get_lead_write_service,
    require_leads_convert_scope,
    require_leads_create_scope,
    require_leads_update_scope,
)
from src.api.schemas.crm_lead_models import CreateLeadRequest, UpdateLeadRequest
from src.domain.models.organisation_tables.sync.crm.leads import (
    CRMLeadParticipantType,
    CRMLeadSourceType,
    CRMLeadStatus,
)
from src.service.crm.leads.contracts import LeadServiceError
from src.service.crm.leads.conversion_service import LeadConversionService
from src.service.crm.leads.write_service import LeadWriteService


class FakeLead:
    """In-memory lead shape used by router tests.

    It mirrors the minimum fields the router and response serializers touch so tests
    can assert endpoint contracts while bypassing persistence.
    """

    def __init__(
        self,
        organisation_id: UUID,
        *,
        lead_id: UUID | None = None,
        status: CRMLeadStatus = CRMLeadStatus.NEW,
        full_name: str | None = "Jane Buyer",
        email: str | None = "jane@example.com",
        company_name: str | None = "Example Inc",
        source_type: CRMLeadSourceType = CRMLeadSourceType.MANUAL,
    ) -> None:
        self.lead_id = lead_id or uuid4()
        self.organisation_id = organisation_id
        self.status = status
        self.source_type = source_type
        self.source_channel = None
        self.source_name = None
        self.source_ref = None
        self.priority = None
        self.score = None
        self.first_name = None
        self.last_name = None
        self.full_name = full_name
        self.email = email
        self.phone = None
        self.job_title = None
        self.linkedin_url = None
        self.company_name = company_name
        self.company_domain = None
        self.company_website = None
        self.industry = None
        self.country = None
        self.region = None
        self.city = None
        self.message = None
        self.interest = None
        self.budget_amount = None
        self.budget_currency = None
        self.owner_id = None
        self.owner_name = None
        self.owner_email = None
        self.assigned_at = None
        self.contact_id = None
        self.company_id = None
        self.converted_deal_id = None
        self.converted_at = None
        self.disqualified_reason = None
        self.archive_reason = None
        self.form_id = None
        self.form_version_id = None
        self.form_submission_id = None
        self.utm_source = None
        self.utm_medium = None
        self.utm_campaign = None
        self.utm_term = None
        self.utm_content = None
        self.attribution = {}
        self.consent = {}
        self.properties = {}
        self.metrics = {}
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
        self.captured_at = self.created_at
        self.last_activity_at = None
        self.last_contacted_at = None
        self.qualified_at = None
        self.disqualified_at = None
        self.is_deleted = False


class FakeLeadParticipant:
    """In-memory lead participant fixture used by convert-path tests."""

    def __init__(self, lead_id: UUID, organisation_id: UUID) -> None:
        self.participant_id = uuid4()
        self.lead_id = lead_id
        self.organisation_id = organisation_id
        self.participant_type = CRMLeadParticipantType.CONTACT
        self.relationship_role = "primary_contact"
        self.is_primary = True
        self.related_local_id = None
        self.name = "Jane Buyer"
        self.email = "jane@example.com"
        self.phone = None
        self.job_title = "VP Sales"
        self.company_name = "Example Inc"
        self.company_domain = "example.com"
        self.source = "manual"
        self.properties = {}
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


class FakeLeadReadService:
    """Fake service implementation for `LeadReadService`.

    Router tests inject this service to keep endpoint behavior deterministic and
    avoid organization-level DB dependencies.
    """

    def __init__(self, organisation_id: UUID) -> None:
        self.organisation_id = organisation_id
        self.leads: dict[UUID, FakeLead] = {}
        self.participants: dict[UUID, list[FakeLeadParticipant]] = {}

    async def close(self) -> None:
        """No-op close used to match the real service lifecycle."""
        pass

    async def list_leads(self, **kwargs):
        """Return all stored leads plus count, matching the service contract."""
        return list(self.leads.values()), len(self.leads)

    async def create_lead(self, payload):
        """Build and store a lead from payload fields for create-endpoint assertions."""
        lead = FakeLead(
            self.organisation_id,
            full_name=payload.get("full_name"),
            email=payload.get("email"),
            company_name=payload.get("company_name"),
        )
        self.leads[lead.lead_id] = lead
        return lead

    async def get_lead(self, lead_id: UUID):
        """Return lead and participants for the requested lead id."""
        return self.leads[lead_id], self.participants.get(lead_id, [])

    async def add_participant(self, lead_id: UUID, payload):
        """Attach one participant record to an in-memory lead."""
        participant = FakeLeadParticipant(lead_id, self.organisation_id)
        participant.email = payload.get("email")
        participant.name = payload.get("name")
        self.participants.setdefault(lead_id, []).append(participant)
        return participant

    async def convert_lead(self, lead_id: UUID, payload):
        """Mark a lead converted and return the synthetic deal mapping payload."""
        lead = self.leads[lead_id]
        deal_id = uuid4()
        lead.status = CRMLeadStatus.CONVERTED
        lead.converted_deal_id = deal_id
        lead.converted_at = datetime.now(UTC)
        return {
            "lead": lead,
            "deal_id": deal_id,
            "copied_participant_count": len(self.participants.get(lead_id, [])),
        }


def _iter_route_dependencies(dependencies):
    for dependency in dependencies:
        yield dependency
        yield from _iter_route_dependencies(getattr(dependency, "dependencies", []))


def _install_lead_route_overrides(app, *, user_override, service_override, rate_limit_override):
    original_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_current_user] = user_override
    app.dependency_overrides[get_lead_read_service] = service_override
    app.dependency_overrides[get_lead_write_service] = service_override
    app.dependency_overrides[get_lead_participant_service] = service_override
    app.dependency_overrides[get_lead_conversion_service] = service_override
    app.dependency_overrides[rate_limit_user_read] = rate_limit_override
    app.dependency_overrides[rate_limit_user_write] = rate_limit_override
    app.dependency_overrides[require_leads_create_scope] = rate_limit_override
    app.dependency_overrides[require_leads_update_scope] = rate_limit_override
    app.dependency_overrides[require_leads_convert_scope] = rate_limit_override
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dependency in _iter_route_dependencies(dependant.dependencies):
            call = getattr(dependency, "call", None)
            if (
                getattr(call, "__module__", None) == "src.api.dependencies.crm_write_guard"
                and getattr(call, "__name__", None) == "dependency"
            ):
                app.dependency_overrides[call] = rate_limit_override
    return original_overrides


def test_create_and_get_lead(client: TestClient, test_user_ids):
    """Create a lead through the public router endpoint and read it back."""
    service = FakeLeadReadService(test_user_ids["organisation_id"])

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        yield service

    async def _override_rate_limit():
        return None

    from src.infra.utils.app_state import fastapp as app

    original_overrides = _install_lead_route_overrides(
        app,
        user_override=_override_user,
        service_override=_override_service,
        rate_limit_override=_override_rate_limit,
    )
    try:
        create_resp = client.post(
            "/users/crm/leads",
            json={
                "full_name": "Jane Buyer",
                "email": "jane@example.com",
                "company_name": "Example Inc",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        lead_id = create_resp.json()["data"]["lead_id"]

        detail_resp = client.get(f"/users/crm/leads/{lead_id}")
        assert detail_resp.status_code == 200, detail_resp.text
        data = detail_resp.json()["data"]
        assert data["email"] == "jane@example.com"
        assert data["participants"] == []
    finally:
        app.dependency_overrides = original_overrides


def test_add_lead_participant(client: TestClient, test_user_ids):
    """Add a participant to an existing lead and assert payload propagation."""
    service = FakeLeadReadService(test_user_ids["organisation_id"])
    lead = FakeLead(test_user_ids["organisation_id"])
    service.leads[lead.lead_id] = lead

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        yield service

    async def _override_rate_limit():
        return None

    from src.infra.utils.app_state import fastapp as app

    original_overrides = _install_lead_route_overrides(
        app,
        user_override=_override_user,
        service_override=_override_service,
        rate_limit_override=_override_rate_limit,
    )
    try:
        resp = client.post(
            f"/users/crm/leads/{lead.lead_id}/participants",
            json={
                "participant_type": "contact",
                "relationship_role": "primary_contact",
                "is_primary": True,
                "name": "Jane Buyer",
                "email": "jane@example.com",
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["email"] == "jane@example.com"
        assert data["relationship_role"] == "primary_contact"
    finally:
        app.dependency_overrides = original_overrides


def test_convert_lead_to_deal(client: TestClient, test_user_ids, monkeypatch):
    """Convert a lead to a deal and assert participant copy count and status."""
    service = FakeLeadReadService(test_user_ids["organisation_id"])
    lead = FakeLead(test_user_ids["organisation_id"])
    service.leads[lead.lead_id] = lead
    service.participants[lead.lead_id] = [FakeLeadParticipant(lead.lead_id, lead.organisation_id)]
    hydration_dispatch = AsyncMock(return_value=["task-1"])
    monkeypatch.setattr(
        "src.api.routers.crm.leads.leads_handler.dispatch_contact_communication_hydration_async",
        hydration_dispatch,
        raising=False,
    )

    def _override_user():
        return UserIDs(
            user_id=test_user_ids["user_id"],
            organisation_id=test_user_ids["organisation_id"],
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        yield service

    async def _override_rate_limit():
        return None

    from src.infra.utils.app_state import fastapp as app

    original_overrides = _install_lead_route_overrides(
        app,
        user_override=_override_user,
        service_override=_override_service,
        rate_limit_override=_override_rate_limit,
    )
    try:
        resp = client.post(
            f"/users/crm/leads/{lead.lead_id}/convert",
            json={
                "deal_name": "Example Inc - Enterprise",
                "amount": 50000,
                "currency": "USD",
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["lead_id"] == str(lead.lead_id)
        assert data["status"] == "converted"
        assert data["converted_deal_id"] == str(lead.converted_deal_id)
        assert data["copied_participant_count"] == 1
        hydration_dispatch.assert_awaited_once_with(
            organisation_id=test_user_ids["organisation_id"],
            deal_ids=[lead.converted_deal_id],
            trigger="lead_conversion_committed",
        )
    finally:
        app.dependency_overrides = original_overrides


def test_create_and_update_lead_request_models_reject_converted_status():
    """Only the conversion endpoint may set a lead to converted."""
    with pytest.raises(ValidationError) as create_exc:
        CreateLeadRequest.model_validate({"full_name": "Jane Buyer", "status": "converted"})

    with pytest.raises(ValidationError) as update_exc:
        UpdateLeadRequest.model_validate({"status": "converted"})

    assert create_exc.value.errors()[0]["loc"] == ("status",)
    assert update_exc.value.errors()[0]["loc"] == ("status",)


def test_convert_lead_payload_carries_lead_owner_when_not_overridden(test_user_ids):
    """Building a deal payload should default owner to lead owner when not overridden."""
    lead = FakeLead(test_user_ids["organisation_id"])
    lead.owner_id = test_user_ids["user_id"]

    payload = LeadConversionService._build_deal_payload_from_lead(lead, {})

    assert payload["owner_user_id"] == test_user_ids["user_id"]


def test_convert_lead_payload_keeps_conversion_metadata_out_of_custom_fields(test_user_ids):
    """Conversion metadata must not require a matching deal custom field definition."""
    lead = FakeLead(test_user_ids["organisation_id"])

    payload = LeadConversionService._build_deal_payload_from_lead(lead, {})

    assert "custom_properties" not in payload


@pytest.mark.asyncio
async def test_lead_service_rejects_converted_status_outside_conversion(test_user_ids):
    """Service-level guard protects non-router callers from direct converted writes."""
    service = LeadWriteService(
        org_session=object(),  # type: ignore[arg-type]
        organisation_id=test_user_ids["organisation_id"],
    )

    try:
        await service.create_lead({"status": CRMLeadStatus.CONVERTED})
    except LeadServiceError as exc:
        assert exc.status_code == 422
        assert exc.data == {"code": "converted_status_not_allowed", "field": "status"}
    else:
        raise AssertionError("create_lead accepted converted status")

    try:
        await service.update_lead(uuid4(), {"status": CRMLeadStatus.CONVERTED})
    except LeadServiceError as exc:
        assert exc.status_code == 422
        assert exc.data == {"code": "converted_status_not_allowed", "field": "status"}
    else:
        raise AssertionError("update_lead accepted converted status")
