"""
Tests for CRM Deals API endpoints.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import Response as FastAPIResponse
from fastapi.testclient import TestClient
from src.api.routers.crm import deals_handler as deals_handler_module
from src.api.schemas.crm_deal_write_models import DealCreateRequest, DealUpdateRequest
from src.api.schemas.crm_models import DealsListParams
from src.api.schemas.crm_schema_models import CRMEntitySchemaResponse, CRMFieldSchemaItem
from src.domain.models.organisation_tables.sync.crm.custom_fields import CRMCustomFieldEntity
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus

from tests.api.routers.crm.conftest import (
    FakeCompany,
    FakeDealJob,
    create_deal,
)


def test_deal_write_response_includes_updated_business_fields() -> None:
    """Write endpoints should return the fields a refreshed row/detail needs."""
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme Deal",
        amount=125000.0,
        predicted_amount=81250.0,
        currency="inr",
        probability=65.0,
        forecast_category="Commit",
        pipeline_id=uuid4(),
        pipeline_name="Enterprise",
        pipeline_label="Enterprise Pipeline",
        stage_id=uuid4(),
        stage_name="Proposal",
        stage_label="Proposal Stage",
        deal_status=CRMDealStatus.OPEN,
        owner_id=uuid4(),
        owner_name="Ada Owner",
        owner_email="ada@example.com",
        company_id=uuid4(),
        company_name="Acme Corp",
        company_domain="acme.com",
        company_industry="Manufacturing",
        company_website="https://acme.com",
        company_linkedin_url="https://linkedin.com/company/acme",
        company_country="United States",
        primary_contact_id=uuid4(),
        primary_contact_name="Jane Buyer",
        primary_contact_email="jane@acme.com",
        source_created_at=datetime(2026, 1, 1, tzinfo=UTC),
        close_date=datetime(2026, 3, 1, tzinfo=UTC),
        expected_close_date=datetime(2026, 3, 15, tzinfo=UTC),
        closed_won_at=datetime(2026, 3, 2, tzinfo=UTC),
        closed_lost_at=None,
        closed_reason="Signed annual agreement",
        stage_entered_at=datetime(2026, 2, 1, tzinfo=UTC),
        stage_updated_at=datetime(2026, 2, 2, tzinfo=UTC),
        description="Enterprise expansion",
        deal_type="Expansion",
        priority="High",
        lead_source="Referral",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        synced_at=datetime(2026, 1, 2, 1, tzinfo=UTC),
    )

    response = deals_handler_module._map_deal_write_response(deal).model_dump()

    assert response["amount"] == 125000.0
    assert response["predicted_amount"] == 81250.0
    assert response["currency"] == "INR"
    assert response["currency_symbol"] == "₹"
    assert response["deal_status"] == "open"
    assert response["probability"] == 65.0
    assert response["forecast_category"] == "Commit"
    assert response["pipeline_name"] == "Enterprise"
    assert response["pipeline_label"] == "Enterprise Pipeline"
    assert response["stage_name"] == "Proposal"
    assert response["stage_label"] == "Proposal Stage"
    assert response["owner_user_id"] == deal.owner_id
    assert response["owner_name"] == "Ada Owner"
    assert response["owner_email"] == "ada@example.com"
    assert response["owner_details"] == [
        {
            "owner_id": deal.owner_id,
            "name": "Ada Owner",
            "email": "ada@example.com",
        }
    ]
    assert response["is_internal"] is True
    assert response["company_industry"] == "Manufacturing"
    assert response["company_website"] == "https://acme.com"
    assert response["company_linkedin_url"] == "https://linkedin.com/company/acme"
    assert response["company_country"] == "United States"
    assert response["primary_contact_id"] == deal.primary_contact_id
    assert response["primary_contact_name"] == "Jane Buyer"
    assert response["primary_contact_email"] == "jane@acme.com"
    assert response["source_created_at"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert response["closed_won_at"] == datetime(2026, 3, 2, tzinfo=UTC)
    assert response["closed_lost_at"] is None
    assert response["closed_reason"] == "Signed annual agreement"
    assert response["stage_entered_at"] == datetime(2026, 2, 1, tzinfo=UTC)
    assert response["stage_updated_at"] == datetime(2026, 2, 2, tzinfo=UTC)
    assert response["deal_type"] == "Expansion"
    assert response["priority"] == "High"
    assert response["lead_source"] == "Referral"
    assert response["description"] == "Enterprise expansion"
    assert response["synced_at"] == datetime(2026, 1, 2, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_crm_schema_returns_deal_schema_service_payload() -> None:
    """The schema endpoint should stay a thin wrapper over DealSchemaService."""
    payload = CRMEntitySchemaResponse(
        entity="deal",
        fields=[
            CRMFieldSchemaItem(
                entity="deal",
                key="name",
                label="Deal name",
                type="string",
                order=1,
                source="standard",
                ownership="system",
                editable=True,
                write_path="name",
                write_policy="always",
                internal_write=True,
                internal_write_validation={"required": False, "option_membership": False},
            )
        ],
    )
    service = SimpleNamespace(get_schema=AsyncMock(return_value=payload))

    result = await deals_handler_module.get_crm_schema(
        response=FastAPIResponse(),
        entity=CRMCustomFieldEntity.DEAL,
        service=service,
        _rl_read=None,
    )

    service.get_schema.assert_awaited_once_with()
    assert result.status == "SUCCESS"
    assert result.data == payload


@pytest.mark.asyncio
async def test_create_deal_adds_selected_contacts_through_stakeholder_service(
    monkeypatch,
) -> None:
    contact_id = uuid4()
    participant_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme Deal",
        pipeline_id=uuid4(),
        stage_id=uuid4(),
        deal_status="open",
        owner_id=None,
        company_id=None,
        company_name=None,
        company_domain=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session = SimpleNamespace(
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )
    write_service = SimpleNamespace(
        session=session,
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
        create_deal=AsyncMock(return_value=deal),
    )
    stakeholder_service = SimpleNamespace(
        add_contacts_as_stakeholders=AsyncMock(
            return_value=SimpleNamespace(
                total_added=1,
                added=[SimpleNamespace(participant_id=participant_id, contact_id=contact_id)],
            )
        ),
        trigger_deal_mapping_if_ready=AsyncMock(),
    )
    job_visibility_service = SimpleNamespace(
        create_stakeholder_processing_jobs=AsyncMock(return_value=[]),
    )
    dispatch_research = AsyncMock(return_value=None)

    class FakeStakeholderWriteService:
        def __init__(self, session_arg, organisation_id, actor_user_id):
            assert session_arg is session
            assert organisation_id == write_service.organisation_id
            assert actor_user_id == write_service.actor_user_id

        async def add_contacts_as_stakeholders(self, deal_id, contact_ids):
            return await stakeholder_service.add_contacts_as_stakeholders(deal_id, contact_ids)

        async def trigger_deal_mapping_if_ready(self, deal_id):
            return await stakeholder_service.trigger_deal_mapping_if_ready(deal_id)

    class FakeStakeholderJobVisibilityService:
        def __init__(self, session_arg, organisation_id, actor_user_id):
            assert session_arg is session
            assert organisation_id == write_service.organisation_id
            assert actor_user_id == write_service.actor_user_id

        async def create_stakeholder_processing_jobs(self, deal_id, stakeholder_id):
            return await job_visibility_service.create_stakeholder_processing_jobs(
                deal_id,
                stakeholder_id,
            )

    monkeypatch.setattr(
        deals_handler_module,
        "StakeholderWriteService",
        FakeStakeholderWriteService,
    )
    monkeypatch.setattr(
        deals_handler_module,
        "StakeholderJobVisibilityService",
        FakeStakeholderJobVisibilityService,
        raising=False,
    )
    monkeypatch.setattr(
        deals_handler_module,
        "_contact_research_job_response",
        dispatch_research,
        raising=False,
    )

    response = await deals_handler_module.create_deal(
        request=DealCreateRequest(
            name="Acme Deal",
            contact_participants=[{"contact_id": str(contact_id)}],
        ),
        response=FastAPIResponse(),
        service=write_service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    write_service.create_deal.assert_awaited_once()
    stakeholder_service.add_contacts_as_stakeholders.assert_awaited_once_with(
        deal.deal_id,
        [contact_id],
    )
    stakeholder_service.trigger_deal_mapping_if_ready.assert_not_awaited()
    job_visibility_service.create_stakeholder_processing_jobs.assert_awaited_once_with(
        deal.deal_id,
        participant_id,
    )
    dispatch_research.assert_awaited_once_with(
        organisation_id=write_service.organisation_id,
        contact_id=contact_id,
        user_id=write_service.actor_user_id,
    )
    assert session.commit.await_count == 2
    session.refresh.assert_awaited_once_with(deal)
    create_payload = write_service.create_deal.await_args.args[0]
    assert "contact_participants" not in create_payload
    assert response.status == "SUCCESS"


@pytest.mark.asyncio
async def test_create_deal_accepts_primary_contact_snapshot() -> None:
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme Deal",
        pipeline_id=uuid4(),
        stage_id=uuid4(),
        deal_status="open",
        owner_id=None,
        company_id=None,
        company_name=None,
        company_domain=None,
        primary_contact_name="Jane Buyer",
        primary_contact_email="jane@acme.com",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session = SimpleNamespace(
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )
    write_service = SimpleNamespace(
        session=session,
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
        create_deal=AsyncMock(return_value=deal),
    )

    response = await deals_handler_module.create_deal(
        request=DealCreateRequest(
            name="Acme Deal",
            primary_contact_name="  Jane Buyer  ",
            primary_contact_email=" jane@acme.com ",
        ),
        response=FastAPIResponse(),
        service=write_service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    write_service.create_deal.assert_awaited_once()
    payload = write_service.create_deal.await_args.args[0]
    assert payload["primary_contact_name"] == "Jane Buyer"
    assert payload["primary_contact_email"] == "jane@acme.com"
    assert response.status == "SUCCESS"


@pytest.mark.asyncio
async def test_create_deal_keeps_success_when_stakeholder_job_visibility_fails(
    monkeypatch,
) -> None:
    contact_id = uuid4()
    participant_id = uuid4()
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme Deal",
        pipeline_id=uuid4(),
        stage_id=uuid4(),
        deal_status="open",
        owner_id=None,
        company_id=None,
        company_name=None,
        company_domain=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session = SimpleNamespace(
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )
    write_service = SimpleNamespace(
        session=session,
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
        create_deal=AsyncMock(return_value=deal),
    )
    stakeholder_service = SimpleNamespace(
        add_contacts_as_stakeholders=AsyncMock(
            return_value=SimpleNamespace(
                total_added=1,
                added=[SimpleNamespace(participant_id=participant_id, contact_id=contact_id)],
            )
        ),
    )
    dispatch_research = AsyncMock(return_value=None)

    class FakeStakeholderWriteService:
        def __init__(self, *_args):
            pass

        async def add_contacts_as_stakeholders(self, deal_id, contact_ids):
            return await stakeholder_service.add_contacts_as_stakeholders(deal_id, contact_ids)

    class FailingStakeholderJobVisibilityService:
        def __init__(self, *_args):
            pass

        async def create_stakeholder_processing_jobs(self, _deal_id, _stakeholder_id):
            raise RuntimeError("job visibility failed")

    monkeypatch.setattr(
        deals_handler_module,
        "StakeholderWriteService",
        FakeStakeholderWriteService,
    )
    monkeypatch.setattr(
        deals_handler_module,
        "StakeholderJobVisibilityService",
        FailingStakeholderJobVisibilityService,
        raising=False,
    )
    monkeypatch.setattr(
        deals_handler_module,
        "_contact_research_job_response",
        dispatch_research,
        raising=False,
    )

    response = await deals_handler_module.create_deal(
        request=DealCreateRequest(
            name="Acme Deal",
            contact_participants=[{"contact_id": str(contact_id)}],
        ),
        response=FastAPIResponse(),
        service=write_service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert response.status == "SUCCESS"
    assert response.data.deal_id == deal.deal_id
    session.refresh.assert_awaited_once_with(deal)
    session.rollback.assert_awaited_once()
    dispatch_research.assert_awaited_once_with(
        organisation_id=write_service.organisation_id,
        contact_id=contact_id,
        user_id=write_service.actor_user_id,
    )


# =============================================================================
# Deal list endpoint tests
# =============================================================================


def test_list_deals_empty(client: TestClient, deals_dependency_overrides):
    """Test listing deals when none exist."""
    resp = client.get("/users/crm/deals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["message"] == "Deals retrieved successfully"
    data = body["data"]
    assert data["items"] == []
    assert data["total_count"] == 0
    assert data["total_pages"] == 0


def test_list_deals_success(client: TestClient, deals_dependency_overrides):
    """Test listing deals with data."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add some deals
    for i in range(3):
        deal = create_deal(org_id, name=f"Deal {i}")
        deals_service.add_deal(deal)

    resp = client.get("/users/crm/deals")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert len(data["items"]) == 3


def test_list_deals_does_not_expose_deal_health(client: TestClient, deals_dependency_overrides):
    """Deal health is detail/AI context, not a deal-list column."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]
    deals_service.add_deal(
        create_deal(
            org_id,
            name="Health Hidden Deal",
            deal_summary_data={
                "data": {
                    "deal_health": "AT_RISK",
                    "deal_health_reason": "Procurement is blocked.",
                }
            },
        )
    )

    resp = client.get("/users/crm/deals")

    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert "deal_health" not in item
    assert "deal_health_reason" not in item


def test_list_deals_pagination(client: TestClient, deals_dependency_overrides):
    """Test pagination with limit and page number."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add more deals than the limit
    for i in range(5):
        deal = create_deal(org_id, name=f"Deal {i}")
        deals_service.add_deal(deal)

    # First page
    resp1 = client.get("/users/crm/deals", params={"limit": 2})
    assert resp1.status_code == 200
    body1 = resp1.json()
    data1 = body1["data"]
    assert len(data1["items"]) == 2
    assert data1["total_count"] == 5
    assert data1["total_pages"] == 3

    # Second page
    resp2 = client.get("/users/crm/deals", params={"limit": 2, "page": 2})
    assert resp2.status_code == 200
    body2 = resp2.json()
    data2 = body2["data"]
    assert len(data2["items"]) == 2


def test_list_deals_fetch_all(client: TestClient, deals_dependency_overrides):
    """Test fetch_all mode returns all deals."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add deals
    for i in range(10):
        deal = create_deal(org_id, name=f"Deal {i}")
        deals_service.add_deal(deal)

    resp = client.get("/users/crm/deals", params={"fetch_all": True})
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 10
    assert data["total_count"] == 10


def test_list_deals_filter_by_status(client: TestClient, deals_dependency_overrides):
    """Test filtering deals by status."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add deals with different statuses
    deals_service.add_deal(create_deal(org_id, name="Open Deal", deal_status=CRMDealStatus.OPEN))
    deals_service.add_deal(create_deal(org_id, name="Won Deal", deal_status=CRMDealStatus.WON))
    deals_service.add_deal(create_deal(org_id, name="Lost Deal", deal_status=CRMDealStatus.LOST))

    # Filter by open status - enum values are lowercase
    resp = client.get("/users/crm/deals?statuses=open")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["deal_status"] == "open"


def test_list_deals_filter_by_amount_range(client: TestClient, deals_dependency_overrides):
    """Test filtering deals by amount range."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add deals with different amounts
    deals_service.add_deal(create_deal(org_id, name="Small Deal", amount=5000))
    deals_service.add_deal(create_deal(org_id, name="Medium Deal", amount=15000))
    deals_service.add_deal(create_deal(org_id, name="Large Deal", amount=50000))

    # Filter by amount range
    resp = client.get("/users/crm/deals", params={"min_amount": 10000, "max_amount": 30000})
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "Medium Deal"


def test_list_deals_search_query(client: TestClient, deals_dependency_overrides):
    """Test searching deals by name."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add deals
    deals_service.add_deal(create_deal(org_id, name="Acme Corporation Deal"))
    deals_service.add_deal(create_deal(org_id, name="Beta Industries Deal"))
    deals_service.add_deal(create_deal(org_id, name="Acme Subsidiary Deal"))

    # Search for "Acme"
    resp = client.get("/users/crm/deals", params={"q": "acme"})
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 2
    for item in data["items"]:
        assert "acme" in item["name"].lower()


def test_list_deals_with_owner(client: TestClient, deals_dependency_overrides):
    """Test deals include owner details."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add deal with owner
    deal = create_deal(
        org_id,
        name="Deal with Owner",
        owner_id=uuid4(),
        owner_name="John Sales",
        owner_email="john@example.com",
    )
    deals_service.add_deal(deal)

    resp = client.get("/users/crm/deals")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert len(item["owner_details"]) == 1
    assert item["owner_details"][0]["name"] == "John Sales"


def test_list_deals_includes_clickable_account_and_primary_contact_details(
    client: TestClient, deals_dependency_overrides
):
    """List rows should expose account/contact IDs in owner-style detail arrays."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]
    company_id = uuid4()
    contact_id = uuid4()
    deals_service.add_deal(
        create_deal(
            org_id,
            name="Relationship Deal",
            company_id=company_id,
            company_name="Acme Corp",
            company_domain="acme.com",
            primary_contact_id=contact_id,
            primary_contact_name="Jane Buyer",
            primary_contact_email="jane@acme.com",
        )
    )

    resp = client.get("/users/crm/deals")

    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["account_details"] == [
        {
            "company_id": str(company_id),
            "name": "Acme Corp",
            "domain": "acme.com",
        }
    ]
    assert item["primary_contact_details"] == [
        {
            "contact_id": str(contact_id),
            "name": "Jane Buyer",
            "email": "jane@acme.com",
        }
    ]


def test_list_deals_includes_default_column_fields(client: TestClient, deals_dependency_overrides):
    """List rows should expose fields referenced by backend default deal columns."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]
    contact_id = uuid4()
    last_activity_at = datetime(2026, 6, 30, 15, 45, tzinfo=UTC)
    deals_service.add_deal(
        create_deal(
            org_id,
            name="Default Column Deal",
            primary_contact_id=contact_id,
            primary_contact_name="Jane Buyer",
            primary_contact_email="jane@example.com",
            last_activity_at=last_activity_at,
        )
    )

    resp = client.get("/users/crm/deals")

    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["primary_contact_id"] == str(contact_id)
    assert item["primary_contact_name"] == "Jane Buyer"
    assert item["primary_contact_email"] == "jane@example.com"
    assert item["last_activity_at"] == "2026-06-30T15:45:00Z"


def test_list_deals_search_matches_company_name(client: TestClient, deals_dependency_overrides):
    """Router test fake should mirror production q search across deal and company names."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deals_service.add_deal(create_deal(org_id, name="Renewal", company_name="Acme Corp"))
    deals_service.add_deal(create_deal(org_id, name="Unrelated", company_name="Globex"))

    resp = client.get("/users/crm/deals", params={"q": " acme "})
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    assert [item["company_name"] for item in items] == ["Acme Corp"]


def test_list_deals_filters_by_expected_close_date(client: TestClient, deals_dependency_overrides):
    """Forecast close-date filters should use expected_close_date, not actual close_date."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deals_service.add_deal(
        create_deal(
            org_id,
            name="July Forecast",
            expected_close_date=datetime(2026, 7, 15, tzinfo=UTC),
            close_date=None,
        )
    )
    deals_service.add_deal(
        create_deal(
            org_id,
            name="August Forecast",
            expected_close_date=datetime(2026, 8, 15, tzinfo=UTC),
            close_date=None,
        )
    )
    deals_service.add_deal(
        create_deal(
            org_id,
            name="Actually Closed In July",
            expected_close_date=datetime(2026, 8, 15, tzinfo=UTC),
            close_date=datetime(2026, 7, 10, tzinfo=UTC),
        )
    )

    resp = client.get(
        "/users/crm/deals",
        params={
            "min_expected_close_date": "2026-07-01T00:00:00Z",
            "max_expected_close_date": "2026-07-31T23:59:59Z",
        },
    )

    assert resp.status_code == 200, resp.text
    names = [item["name"] for item in resp.json()["data"]["items"]]
    assert names == ["July Forecast"]


def test_search_deals_http_uses_seeded_fake_service(client: TestClient, deals_dependency_overrides):
    """The HTTP search route should work with the standard deals dependency fake."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deals_service.add_deal(create_deal(org_id, name="Renewal", company_name="Acme Corp"))
    deals_service.add_deal(create_deal(org_id, name="Unrelated", company_name="Globex"))

    resp = client.get("/users/crm/deals/search", params={"q": "acme", "limit": 20})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["has_more"] is False
    assert data["next_cursor"] is None
    assert [item["company_name"] for item in data["items"]] == ["Acme Corp"]


def test_search_deals_http_cursor_returns_next_page(client: TestClient, deals_dependency_overrides):
    """The HTTP search fake should support the same forward cursor flow as production."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deals_service.add_deal(create_deal(org_id, name="Acme Alpha", company_name="Acme"))
    deals_service.add_deal(create_deal(org_id, name="Acme Beta", company_name="Acme"))

    first = client.get("/users/crm/deals/search", params={"q": "acme", "limit": 1})
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert [item["name"] for item in first_data["items"]] == ["Acme Alpha"]
    assert first_data["has_more"] is True
    assert first_data["next_cursor"]

    second = client.get(
        "/users/crm/deals/search",
        params={"q": "acme", "limit": 1, "cursor": first_data["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_data = second.json()["data"]
    assert [item["name"] for item in second_data["items"]] == ["Acme Beta"]
    assert second_data["has_more"] is False
    assert second_data["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_deals_default_includes_crm_only_owner_deals(
    fake_deals_service, test_user_ids, monkeypatch
):
    """Default Deals is a pipeline view, not current-app-user-only."""
    org_id = test_user_ids["organisation_id"]

    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="CRM-only Owner Deal",
            owner_id=uuid4(),
            owner_name="CRM Owner",
            owner_email="crm-owner@example.com",
        )
    )
    monkeypatch.setattr(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        AsyncMock(return_value={}),
    )

    result = await deals_handler_module.list_deals(
        response=FastAPIResponse(),
        params=DealsListParams(
            limit=40,
            page=1,
            statuses=None,
            pipeline_ids=None,
            owner_ids=None,
            min_amount=None,
            max_amount=None,
            min_close_date=None,
            max_close_date=None,
            min_expected_close_date=None,
            max_expected_close_date=None,
            q=None,
        ),
        service=fake_deals_service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert [item.name for item in result.data.items] == ["CRM-only Owner Deal"]


@pytest.mark.asyncio
async def test_list_deals_includes_stage_and_pipeline_ids(
    fake_deals_service, test_user_ids, monkeypatch
):
    """List rows should include stable IDs needed for filters and stage actions."""
    org_id = test_user_ids["organisation_id"]
    stage_id = uuid4()
    pipeline_id = uuid4()
    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Pipeline Action Deal",
            predicted_amount=42000.0,
            stage_id=stage_id,
            pipeline_id=pipeline_id,
        )
    )
    monkeypatch.setattr(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        AsyncMock(return_value={}),
    )

    result = await deals_handler_module.list_deals(
        response=FastAPIResponse(),
        params=DealsListParams(
            limit=40,
            page=1,
            statuses=None,
            pipeline_ids=None,
            owner_ids=None,
            min_amount=None,
            max_amount=None,
            min_close_date=None,
            max_close_date=None,
            min_expected_close_date=None,
            max_expected_close_date=None,
            q=None,
        ),
        service=fake_deals_service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert result.data.items[0].predicted_amount == 42000.0
    assert result.data.items[0].stage_id == stage_id
    assert result.data.items[0].pipeline_id == pipeline_id


@pytest.mark.asyncio
async def test_search_deals_returns_cursor_page(mocker) -> None:
    deal_id = uuid4()
    integration_id = uuid4()
    now = datetime.now(UTC)
    deal = SimpleNamespace(
        deal_id=deal_id,
        integration_id=integration_id,
        name="Acme Renewal",
        amount=25000.0,
        predicted_amount=12500.0,
        currency="usd",
        deal_status="open",
        stage_id=uuid4(),
        stage_name="Negotiation",
        pipeline_id=uuid4(),
        pipeline_name="Sales",
        company_id=uuid4(),
        company_name="Acme",
        company_domain="acme.com",
        owner_name="Jane Smith",
        owner_email="jane@example.com",
        owner_id=uuid4(),
        created_at=now,
        updated_at=now,
    )
    service = SimpleNamespace(
        search_deals=AsyncMock(return_value=([deal], "next-token")),
        session=object(),
        organisation_id=uuid4(),
    )
    integration_types_mock = mocker.patch(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        new=AsyncMock(return_value={integration_id: True}),
    )

    result = await deals_handler_module.search_deals(
        response=FastAPIResponse(),
        q="acme",
        limit=20,
        cursor=None,
        service=service,
        _rl_read=None,
    )

    service.search_deals.assert_awaited_once_with(q="acme", limit=20, cursor=None)
    assert result.status == "SUCCESS"
    assert result.data is not None
    assert result.data.items[0].deal_id == deal_id
    assert result.data.items[0].predicted_amount == 12500.0
    assert result.data.items[0].currency == "USD"
    assert result.data.items[0].stage_id == deal.stage_id
    assert result.data.items[0].pipeline_id == deal.pipeline_id
    assert result.data.items[0].is_internal is True
    assert result.data.next_cursor == "next-token"
    assert result.data.has_more is True
    integration_types_mock.assert_awaited_once_with(
        service.session,
        service.organisation_id,
        {integration_id},
    )


@pytest.mark.asyncio
async def test_search_deals_invalid_cursor_returns_service_status() -> None:
    service = SimpleNamespace(
        search_deals=AsyncMock(
            side_effect=deals_handler_module.DealReadServiceError("Invalid cursor", 400)
        )
    )
    response = FastAPIResponse()

    result = await deals_handler_module.search_deals(
        response=response,
        q="acme",
        limit=20,
        cursor="not-a-cursor",
        service=service,
        _rl_read=None,
    )

    assert result.status == "ERROR"
    assert result.message == "Invalid cursor"
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_list_deal_owners_hydrates_selected_owner_outside_search(
    fake_deals_service, test_user_ids
):
    org_id = test_user_ids["organisation_id"]
    selected_owner_id = uuid4()

    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Selected Owner Deal",
            owner_id=selected_owner_id,
            owner_name="Selected Owner",
            owner_email="selected@example.com",
        )
    )
    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Search Match Deal",
            owner_id=uuid4(),
            owner_name="Search Match",
            owner_email="match@example.com",
        )
    )

    result = await deals_handler_module.list_deal_owners(
        response=FastAPIResponse(),
        limit=200,
        q="match",
        owner_ids=[selected_owner_id],
        service=fake_deals_service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    owners = result.data.items
    owner_ids = {owner.owner_id for owner in owners}
    assert selected_owner_id in owner_ids
    assert any(owner.name == "Search Match" for owner in owners)


@pytest.mark.asyncio
async def test_list_deal_owners_trims_search_in_router_fake(fake_deals_service, test_user_ids):
    org_id = test_user_ids["organisation_id"]
    owner_id = uuid4()

    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Whitespace Search Deal",
            owner_id=owner_id,
            owner_name="Search Match",
            owner_email="match@example.com",
        )
    )

    result = await deals_handler_module.list_deal_owners(
        response=FastAPIResponse(),
        limit=200,
        q="  match  ",
        owner_ids=None,
        service=fake_deals_service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert [owner.owner_id for owner in result.data.items] == [owner_id]


@pytest.mark.asyncio
async def test_get_current_user_deal_owners_resolves_preselect_owner_ids(
    fake_deals_service, test_user_ids
):
    org_id = test_user_ids["organisation_id"]
    current_user_id = test_user_ids["user_id"]
    id_owner_id = current_user_id
    email_owner_id = uuid4()

    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Owner ID Match",
            owner_id=id_owner_id,
            owner_name="Current User",
            owner_email="other@example.com",
        )
    )
    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Owner Email Match",
            owner_id=email_owner_id,
            owner_name="Email Bridge Owner",
            owner_email="test@example.com",
        )
    )
    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Unrelated Owner",
            owner_id=uuid4(),
            owner_email="other@example.com",
        )
    )

    result = await deals_handler_module.get_current_user_deal_owners(
        response=FastAPIResponse(),
        user_ids=SimpleNamespace(
            user_id=current_user_id,
            organisation_id=org_id,
            user_email="test@example.com",
        ),
        service=fake_deals_service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    owner_ids = {owner.owner_id for owner in result.data.items}
    assert owner_ids == {id_owner_id, email_owner_id}


@pytest.mark.asyncio
async def test_get_current_user_deal_owners_trims_user_email(fake_deals_service, test_user_ids):
    org_id = test_user_ids["organisation_id"]
    current_user_id = test_user_ids["user_id"]
    email_owner_id = uuid4()

    fake_deals_service.add_deal(
        create_deal(
            org_id,
            name="Owner Email Match",
            owner_id=email_owner_id,
            owner_name="Email Bridge Owner",
            owner_email="test@example.com",
        )
    )

    result = await deals_handler_module.get_current_user_deal_owners(
        response=FastAPIResponse(),
        user_ids=SimpleNamespace(
            user_id=current_user_id,
            organisation_id=org_id,
            user_email=" Test@Example.com ",
        ),
        service=fake_deals_service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert [owner.owner_id for owner in result.data.items] == [email_owner_id]


def test_list_deals_returns_currency_symbol_and_normalized_code(
    client: TestClient, deals_dependency_overrides
):
    """Deal list should return uppercase currency code and matching symbol."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="INR Deal", currency="inr")
    deals_service.add_deal(deal)

    resp = client.get("/users/crm/deals")
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["currency"] == "INR"
    assert item["currency_symbol"] == "₹"


def test_list_deals_currency_normalization_handles_code_with_symbol(
    client: TestClient, deals_dependency_overrides
):
    """Currency should normalize mixed "INR (₹)" input to canonical code and symbol."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="INR Decorated", currency="INR (₹)")
    deals_service.add_deal(deal)

    resp = client.get("/users/crm/deals")
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"]["items"][0]
    assert item["currency"] == "INR"
    assert item["currency_symbol"] == "₹"


def test_list_deals_hides_active_import_job_links(
    client: TestClient, deals_dependency_overrides, monkeypatch
):
    """List endpoint should hide import links when an active import job exists."""
    deals_service = deals_dependency_overrides["deals_service"]
    deal_id = uuid4()
    deal = SimpleNamespace(
        deal_id=deal_id,
        integration_id=None,
        name="Reconnect Deal",
        amount=1000.0,
        predicted_amount=None,
        currency="USD",
        deal_status=CRMDealStatus.OPEN,
        probability=0.5,
        close_date=None,
        expected_close_date=None,
        stage_id=None,
        stage_name="Discovery",
        stage_label="Discovery",
        pipeline_id=None,
        pipeline_name="Default",
        pipeline_label="Default",
        owner_name=None,
        owner_email=None,
        owner_id=None,
        company_id=None,
        company_name=None,
        company_domain=None,
        deal_summary_data=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        synced_at=datetime.now(UTC),
    )
    deals_service.session = object()
    deals_service.list_deals = AsyncMock(return_value=([deal], 1))
    deals_service.get_deal_team_members_batch = AsyncMock(return_value={})

    monkeypatch.setattr(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        AsyncMock(return_value={}),
    )

    resp = client.get("/users/crm/deals")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["active_import_job"] is None


# =============================================================================
# Deal statistics endpoint tests
# =============================================================================


def test_deals_stats_empty(client: TestClient, deals_dependency_overrides):
    """Test stats when no deals exist."""
    resp = client.get("/users/crm/deals/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["total"] == 0
    assert data["by_status"] == {}
    assert data["by_pipeline"] == {}


def test_deals_stats_success(client: TestClient, deals_dependency_overrides):
    """Test stats with deal data."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add deals
    deals_service.add_deal(create_deal(org_id, deal_status=CRMDealStatus.OPEN, amount=10000))
    deals_service.add_deal(create_deal(org_id, deal_status=CRMDealStatus.OPEN, amount=20000))
    deals_service.add_deal(create_deal(org_id, deal_status=CRMDealStatus.WON, amount=30000))

    resp = client.get("/users/crm/deals/stats")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["total"] == 3
    assert data["by_status"] == {"open": 2, "won": 1}
    assert data["total_amount"] == 60000
    assert data["average_amount"] == 20000


@pytest.mark.asyncio
async def test_deals_stats_normalizes_enum_status_keys() -> None:
    """Stats should expose the same lowercase status values as deal rows."""
    service = SimpleNamespace(
        get_deals_stats=AsyncMock(
            return_value={
                "total": 2,
                "by_status": {CRMDealStatus.OPEN: 1, CRMDealStatus.WON: 1},
                "by_pipeline": {},
                "total_amount": 1000.0,
                "average_amount": 500.0,
            }
        )
    )

    result = await deals_handler_module.get_deals_stats(
        response=FastAPIResponse(),
        service=service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert result.data.by_status == {"open": 1, "won": 1}


def test_deals_stats_by_pipeline(client: TestClient, deals_dependency_overrides):
    """Test stats count by pipeline."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add deals with different pipelines
    deals_service.add_deal(create_deal(org_id, pipeline_name="sales"))
    deals_service.add_deal(create_deal(org_id, pipeline_name="sales"))
    deals_service.add_deal(create_deal(org_id, pipeline_name="enterprise"))

    resp = client.get("/users/crm/deals/stats")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["by_pipeline"]["sales"] == 2
    assert data["by_pipeline"]["enterprise"] == 1


# =============================================================================
# Deal detail endpoint tests
# =============================================================================


def test_get_deal_success(client: TestClient, deals_dependency_overrides):
    """Test getting a deal by ID."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="Test Deal")
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["name"] == "Test Deal"
    assert str(data["deal_id"]) == str(deal.deal_id)
    assert data["deal_status"] == "open"
    assert data["currency"] == "USD"
    assert data["currency_symbol"] == "$"


@pytest.mark.asyncio
async def test_get_deal_returns_detail_field_definitions():
    """Deal detail should advertise the ordered Get Details field/edit contract."""
    org_id = uuid4()
    deal = create_deal(org_id, name="Metadata Deal")
    service = SimpleNamespace(
        session=None,
        organisation_id=org_id,
        get_deal_by_id=AsyncMock(return_value=deal),
        get_deal_team_members_batch=AsyncMock(return_value={}),
        get_company_by_id=AsyncMock(return_value=None),
    )
    job_service = SimpleNamespace(get_active_deal_jobs_status=AsyncMock(return_value=None))

    result = await deals_handler_module.get_deal(
        deal_id=deal.deal_id,
        response=FastAPIResponse(),
        service=service,
        deal_job_service=job_service,
        include_empty_custom_columns=True,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    data = result.data.model_dump()
    detail_fields = data["detail_field_definitions"]
    fields_by_key = {field["key"]: field for field in detail_fields}

    assert [field["order"] for field in detail_fields] == [
        *range(1, 10),
        *range(12, 37),
    ]
    assert "custom_columns" not in fields_by_key
    assert "deal_summary_data" not in fields_by_key
    assert "active_deal_jobs" not in fields_by_key
    assert "associated_contact_count" not in fields_by_key
    assert "associated_company_count" not in fields_by_key
    assert "note_count" not in fields_by_key
    assert "deal_health" not in fields_by_key
    assert "deal_health_reason" not in fields_by_key
    assert fields_by_key["name"] == {
        "key": "name",
        "label": "Deal name",
        "data_type": "string",
        "order": 1,
        "section": "core",
        "filter_control": "text",
        "editable": True,
        "patch_field": "name",
        "edit_control": "text",
        "edit_policy": "always",
        "locked_reason": None,
    }
    assert fields_by_key["owner_details"]["editable"] is False
    assert fields_by_key["owner_details"]["edit_policy"] == "owner_assignment"
    assert fields_by_key["stage_name"]["edit_policy"] == "stage_action"
    assert fields_by_key["close_date"]["editable"] is False
    assert fields_by_key["close_date"]["patch_field"] is None
    assert fields_by_key["close_date"]["edit_policy"] == "lifecycle_action"
    assert fields_by_key["company_name"]["edit_policy"] == "account_action"
    assert fields_by_key["primary_contact_name"]["edit_policy"] == "primary_contact_action"


@pytest.mark.asyncio
async def test_deal_detail_field_edit_policies_match_write_contracts():
    """Every predefined detail field should advertise the correct write path."""
    org_id = uuid4()
    deal = create_deal(org_id, name="Metadata Deal")
    service = SimpleNamespace(
        session=None,
        organisation_id=org_id,
        get_deal_by_id=AsyncMock(return_value=deal),
        get_deal_team_members_batch=AsyncMock(return_value={}),
        get_company_by_id=AsyncMock(return_value=None),
    )
    job_service = SimpleNamespace(get_active_deal_jobs_status=AsyncMock(return_value=None))

    result = await deals_handler_module.get_deal(
        deal_id=deal.deal_id,
        response=FastAPIResponse(),
        service=service,
        deal_job_service=job_service,
        include_empty_custom_columns=True,
        _rl_read=None,
    )

    fields_by_key = {
        field["key"]: field
        for field in result.data.model_dump()["detail_field_definitions"]  # type: ignore[union-attr]
    }

    assert {key for key, field in fields_by_key.items() if field["edit_policy"] == "always"} == set(
        DealUpdateRequest.model_fields
    ) - {"custom_properties", "custom_columns"}
    assert {
        key for key, field in fields_by_key.items() if field["edit_policy"] == "account_action"
    } == {
        "company_name",
        "company_domain",
        "company_industry",
        "company_website",
        "company_linkedin_url",
        "company_country",
    }
    assert {
        key
        for key, field in fields_by_key.items()
        if field["edit_policy"] == "primary_contact_action"
    } == {"primary_contact_name", "primary_contact_email"}
    assert {
        key for key, field in fields_by_key.items() if field["edit_policy"] == "stage_action"
    } == {"deal_status", "stage_name", "pipeline_name"}
    assert {
        key for key, field in fields_by_key.items() if field["edit_policy"] == "lifecycle_action"
    } == {"close_date", "closed_won_at", "closed_lost_at", "closed_reason"}
    assert {key for key, field in fields_by_key.items() if field["edit_policy"] == "system"} == {
        "predicted_amount",
        "deal_age_days",
        "last_activity_at",
        "stage_entered_at",
        "stage_updated_at",
        "crm_url",
    }


def test_deal_update_request_accepts_all_advertised_inline_fields() -> None:
    """Inline-editable deal fields must match the public PATCH request model."""
    editable_patch = {
        "name": "Updated deal",
        "amount": 75000.0,
        "currency": "inr",
        "source_created_at": datetime(2026, 3, 10, 0, 0, 0, tzinfo=UTC),
        "expected_close_date": datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC),
        "description": "Updated context",
        "deal_type": "Expansion",
        "priority": "High",
        "lead_source": "Referral",
        "probability": 65,
        "forecast_category": "Commit",
    }

    request = DealUpdateRequest(**editable_patch)
    dumped = request.model_dump(exclude_unset=True)

    assert set(dumped) == set(editable_patch)
    assert dumped["currency"] == "INR"


def test_normalize_deal_status_returns_schema_safe_default() -> None:
    """Deal response models require a non-null status even for sparse rows."""
    assert deals_handler_module._normalize_deal_status(None) == "open"


@pytest.mark.asyncio
async def test_update_deal_primary_contact_passes_explicit_clear_intent() -> None:
    deal = SimpleNamespace(
        deal_id=uuid4(),
        name="Acme Deal",
        pipeline_id=uuid4(),
        stage_id=uuid4(),
        deal_status="open",
        owner_id=None,
        company_id=None,
        company_name=None,
        company_domain=None,
        primary_contact_id=None,
        primary_contact_name=None,
        primary_contact_email=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service = SimpleNamespace(
        update_primary_contact=AsyncMock(return_value=deal),
        session=SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock()),
    )

    result = await deals_handler_module.update_deal_primary_contact(
        deal_id=deal.deal_id,
        request=deals_handler_module.DealPrimaryContactUpdateRequest(
            primary_contact_name=None,
            primary_contact_email=None,
        ),
        response=FastAPIResponse(),
        service=service,
        _crm_write_guard=None,
        _rl_write=None,
    )

    assert result.status == "SUCCESS"
    service.update_primary_contact.assert_awaited_once_with(
        deal_id=deal.deal_id,
        primary_contact_name=None,
        primary_contact_email=None,
        clear_primary_contact=True,
    )


def test_get_deal_returns_expanded_detail_fields(client: TestClient, deals_dependency_overrides):
    """Deal detail should include expanded core/timeline/metadata fields."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    now = datetime.now(UTC)
    deal = create_deal(
        org_id,
        name="Expanded Deal",
        predicted_amount=32000.0,
        forecast_category="Commit",
        expected_close_date=now,
        closed_reason="Budget shift",
        crm_url="https://crm.example/deal/123",
        deal_type="Expansion",
        priority="High",
        lead_source="Inbound",
        source_modified_at=now,
        associated_contact_count=3,
        associated_company_count=1,
        note_count=5,
    )
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["predicted_amount"] == 32000.0
    assert data["forecast_category"] == "Commit"
    assert data["expected_close_date"] is not None
    assert data["closed_reason"] == "Budget shift"
    assert data["crm_url"] == "https://crm.example/deal/123"
    assert data["deal_type"] == "Expansion"
    assert data["priority"] == "High"
    assert data["lead_source"] == "Inbound"
    assert data["source_modified_at"] is not None
    assert data["associated_contact_count"] == 3
    assert data["associated_company_count"] == 1
    assert data["note_count"] == 5


def test_get_deal_not_found(client: TestClient, deals_dependency_overrides):
    """Test getting non-existent deal."""
    fake_id = uuid4()
    resp = client.get(f"/users/crm/deals/{fake_id}")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_get_deal_with_owner(client: TestClient, deals_dependency_overrides):
    """Test deal includes owner details."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(
        org_id,
        name="Deal with Owner",
        owner_id=uuid4(),
        owner_name="Jane Manager",
        owner_email="jane@example.com",
    )
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert len(data["owner_details"]) == 1
    assert data["owner_details"][0]["name"] == "Jane Manager"
    assert data["owner_details"][0]["email"] == "jane@example.com"


def test_get_deal_with_team(client: TestClient, deals_dependency_overrides):
    """Test deal includes team members."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="Team Deal")
    deals_service.add_deal(deal)

    # Add team members
    deals_service.add_team_member(
        deal.deal_id,
        {
            "user_id": uuid4(),
            "name": "Team Member 1",
            "email": "member1@example.com",
            "role": "collaborator",
        },
    )
    deals_service.add_team_member(
        deal.deal_id,
        {
            "user_id": uuid4(),
            "name": "Team Member 2",
            "email": "member2@example.com",
            "role": "split_owner",
        },
    )

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert len(data["deal_team"]) == 2


def test_get_deal_includes_custom_columns_for_internal(
    client: TestClient, deals_dependency_overrides, mocker
):
    """Internal deals should expose custom imported properties as custom_columns."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    integration_id = uuid4()
    deal = create_deal(org_id, name="Internal Custom Deal")
    deal.integration_id = integration_id
    deal.properties = {
        "is_internal_crm": True,
        "created_via": "import",
        "custom_scope": "Geo & Infra",
        "our_revenue_inr": "25000000",
    }
    deals_service.add_deal(deal)

    mocker.patch(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        new=AsyncMock(return_value={integration_id: True}),
    )

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["is_internal"] is True
    assert data["custom_columns"] == {
        "custom_scope": "Geo & Infra",
        "our_revenue_inr": "25000000",
    }


def test_get_deal_includes_empty_custom_columns_from_definitions(
    client: TestClient, deals_dependency_overrides, mocker
):
    """Internal deal detail should include active custom keys even when values are missing."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    integration_id = uuid4()
    deal = create_deal(org_id, name="Internal Custom Deal With Empty Fields")
    deal.integration_id = integration_id
    deal.properties = {
        "is_internal_crm": True,
        "created_via": "import",
        "our_revenue_inr": "25000000",
    }
    deals_service.add_deal(deal)

    mocker.patch(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        new=AsyncMock(return_value={integration_id: True}),
    )

    class FakeDealSchemaService:
        def __init__(self, *_args):
            pass

        async def active_internal_custom_field_keys(self):
            return ["our_revenue_inr", "next_follow_up_date"]

        async def deal_detail_custom_column_definitions(self, *, deal_currency_code):
            return []

    mocker.patch("src.api.routers.crm.deals_handler.DealSchemaService", FakeDealSchemaService)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["is_internal"] is True
    assert data["custom_columns"] == {
        "our_revenue_inr": "25000000",
        "next_follow_up_date": None,
    }


def test_get_deal_custom_columns_populated_only_mode(
    client: TestClient, deals_dependency_overrides, mocker
):
    """When include_empty_custom_columns=false, only populated custom values should be returned."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    integration_id = uuid4()
    deal = create_deal(org_id, name="Internal Custom Deal Populated Only")
    deal.integration_id = integration_id
    deal.properties = {
        "is_internal_crm": True,
        "created_via": "import",
        "our_revenue_inr": "25000000",
    }
    deals_service.add_deal(deal)

    mocker.patch(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        new=AsyncMock(return_value={integration_id: True}),
    )
    custom_keys_mock = AsyncMock(return_value=["our_revenue_inr", "next_follow_up_date"])

    class FakeDealSchemaService:
        def __init__(self, *_args):
            pass

        async def active_internal_custom_field_keys(self):
            return await custom_keys_mock()

        async def deal_detail_custom_column_definitions(self, *, deal_currency_code):
            return []

    mocker.patch("src.api.routers.crm.deals_handler.DealSchemaService", FakeDealSchemaService)

    resp = client.get(
        f"/users/crm/deals/{deal.deal_id}",
        params={"include_empty_custom_columns": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["custom_columns"] == {
        "our_revenue_inr": "25000000",
    }
    custom_keys_mock.assert_not_called()


def test_get_deal_includes_custom_column_definitions_with_types(
    client: TestClient, deals_dependency_overrides, mocker
):
    """Internal deal detail should include custom field type metadata for frontend controls."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    integration_id = uuid4()
    deal = create_deal(org_id, name="Internal Typed Custom Deal", currency="INR")
    deal.integration_id = integration_id
    deal.properties = {"custom_start_at": "2026-02-10T10:00:00Z", "custom_budget": "2500000"}
    deals_service.add_deal(deal)

    mocker.patch(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        new=AsyncMock(return_value={integration_id: True}),
    )

    class FakeDealSchemaService:
        def __init__(self, *_args):
            pass

        async def active_internal_custom_field_keys(self):
            return ["custom_start_at", "custom_budget"]

        async def deal_detail_custom_column_definitions(self, *, deal_currency_code):
            assert deal_currency_code == "INR"
            return [
                deals_handler_module.DealCustomColumnDefinition(
                    key="custom_start_at",
                    label="Start At",
                    data_type="datetime",
                    required=False,
                    options={},
                    validation={},
                ),
                deals_handler_module.DealCustomColumnDefinition(
                    key="custom_budget",
                    label="Budget",
                    data_type="currency",
                    required=False,
                    options={},
                    validation={},
                    currency_code="INR",
                    currency_symbol="₹",
                ),
            ]

    mocker.patch("src.api.routers.crm.deals_handler.DealSchemaService", FakeDealSchemaService)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    definitions = {item["key"]: item for item in data["custom_column_definitions"]}
    assert definitions["custom_start_at"]["data_type"] == "datetime"
    assert definitions["custom_start_at"]["currency_code"] is None
    assert definitions["custom_budget"]["data_type"] == "currency"
    assert definitions["custom_budget"]["currency_code"] == "INR"
    assert definitions["custom_budget"]["currency_symbol"] == "₹"


@pytest.mark.asyncio
async def test_get_deal_uses_schema_service_for_custom_detail_metadata(mocker) -> None:
    """Deal detail compatibility metadata should be backed by DealSchemaService."""
    org_id = uuid4()
    integration_id = uuid4()
    deal = create_deal(org_id, name="Schema Backed Deal", currency="INR")
    deal.integration_id = integration_id
    deal.properties = {}
    service = SimpleNamespace(
        session=object(),
        organisation_id=org_id,
        get_deal_by_id=AsyncMock(return_value=deal),
        get_deal_team_members_batch=AsyncMock(return_value={}),
        get_company_by_id=AsyncMock(return_value=None),
    )
    job_service = SimpleNamespace(get_active_deal_jobs_status=AsyncMock(return_value=None))

    class FakeDealSchemaService:
        def __init__(self, session, organisation_id):
            assert session is service.session
            assert organisation_id == org_id

        async def active_internal_custom_field_keys(self):
            return ["procurement_stage"]

        async def deal_detail_custom_column_definitions(self, *, deal_currency_code):
            assert deal_currency_code == "INR"
            return [
                deals_handler_module.DealCustomColumnDefinition(
                    key="procurement_stage",
                    label="Procurement Stage",
                    data_type="select",
                    required=True,
                    options={"values": [{"id": "legal", "value": "legal"}]},
                    validation={},
                    editable=True,
                    patch_field="custom_columns.procurement_stage",
                    edit_control="select",
                    edit_policy="custom_columns",
                )
            ]

    mocker.patch(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        new=AsyncMock(return_value={integration_id: True}),
    )
    mocker.patch(
        "src.api.routers.crm.deals_handler.DealSchemaService",
        FakeDealSchemaService,
    )

    result = await deals_handler_module.get_deal(
        deal_id=deal.deal_id,
        response=FastAPIResponse(),
        service=service,
        deal_job_service=job_service,
        include_empty_custom_columns=True,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    data = result.data.model_dump()  # type: ignore[union-attr]
    assert data["custom_columns"] == {"procurement_stage": None}
    assert data["custom_column_definitions"][0]["key"] == "procurement_stage"
    assert data["custom_column_definitions"][0]["edit_control"] == "select"


def test_get_deal_omits_custom_columns_for_external(
    client: TestClient, deals_dependency_overrides, mocker
):
    """External deals should not expose internal custom details."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    integration_id = uuid4()
    deal = create_deal(org_id, name="External Deal")
    deal.integration_id = integration_id
    deal.properties = {"custom_scope": "Should Not Surface"}
    deals_service.add_deal(deal)

    mocker.patch(
        "src.api.routers.crm.deals_handler._get_integration_types_map",
        new=AsyncMock(return_value={integration_id: False}),
    )

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["is_internal"] is False
    assert data["custom_columns"] is None


def test_get_deal_with_company_industry(client: TestClient, deals_dependency_overrides):
    """Test deal includes company industry."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Add company
    company = FakeCompany(
        company_id=uuid4(),
        organisation_id=org_id,
        name="Acme Corp",
        industry="Technology",
    )
    deals_service.add_company(company)

    # Add deal with company
    deal = create_deal(
        org_id,
        name="Company Deal",
        company_id=company.company_id,
        company_name="Acme Corp",
    )
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["company_industry"] == "Technology"


def test_get_deal_uses_deal_company_profile_snapshot_when_company_lookup_misses(
    client: TestClient, deals_dependency_overrides
):
    """Deal detail should not drop profile fields already present on the deal snapshot."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(
        org_id,
        name="Snapshot Company Deal",
        company_id=uuid4(),
        company_name="Acme Corp",
        company_domain="acme.com",
        company_industry="Manufacturing",
        company_website="https://acme.com",
        company_linkedin_url="https://linkedin.com/company/acme",
        company_country="United States",
    )
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["company_industry"] == "Manufacturing"
    assert data["company_website"] == "https://acme.com"
    assert data["company_linkedin_url"] == "https://linkedin.com/company/acme"
    assert data["company_country"] == "United States"


def test_get_deal_company_row_nulls_override_stale_deal_profile_snapshot(
    client: TestClient, deals_dependency_overrides
):
    """Linked account clears should not be hidden by older deal snapshot values."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]
    company_id = uuid4()

    deals_service.add_company(
        FakeCompany(
            company_id=company_id,
            organisation_id=org_id,
            name="Acme Corp",
            domain="acme.com",
            industry=None,
            website=None,
            linkedin_url=None,
            country=None,
        )
    )
    deal = create_deal(
        org_id,
        name="Cleared Company Deal",
        company_id=company_id,
        company_name="Acme Corp",
        company_domain="acme.com",
        company_industry="Old Industry",
        company_website="https://old.example",
        company_linkedin_url="https://linkedin.com/company/old",
        company_country="Old Country",
    )
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["company_industry"] is None
    assert data["company_website"] is None
    assert data["company_linkedin_url"] is None
    assert data["company_country"] is None


def test_get_deal_with_summary_data(client: TestClient, deals_dependency_overrides):
    """Test deal includes AI summary data."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    summary_data = {
        "health": "at_risk",
        "health_reason": "No activity in 30 days",
        "summary": "Deal needs attention",
    }
    deal = create_deal(org_id, name="Summary Deal", deal_summary_data=summary_data)
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["deal_summary_data"] == summary_data


def test_get_deal_with_active_jobs(client: TestClient, deals_dependency_overrides):
    """Test deal includes active job status."""
    deals_service = deals_dependency_overrides["deals_service"]
    job_service = deals_dependency_overrides["job_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="Job Deal")
    deals_service.add_deal(deal)

    # Add an active job - use the conftest imports to ensure same enum
    from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus

    from tests.api.routers.crm.conftest import FakeDealJob as ConfTestFakeDealJob

    job = ConfTestFakeDealJob(
        job_id=uuid4(),
        deal_id=deal.deal_id,
        organisation_id=org_id,
        job_type=DealJobType.DEAL_SUMMARY,
        status=JobStatus.IN_PROGRESS,
    )
    job_service.add_job(job)

    # Verify job was added correctly
    assert deal.deal_id in job_service._deal_jobs, (
        f"Job not added to _deal_jobs for deal {deal.deal_id}"
    )
    assert len(job_service._deal_jobs[deal.deal_id]) == 1, "Expected 1 job"

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert "active_deal_jobs" in data
    assert data["active_deal_jobs"]["deal_summary"]["status"] == "in_progress"


def test_get_deal_with_active_stakeholder_processing_jobs(
    client: TestClient, deals_dependency_overrides
):
    """Deal detail exposes active stakeholder processing jobs for reconnect."""
    deals_service = deals_dependency_overrides["deals_service"]
    job_service = deals_dependency_overrides["job_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="Stakeholder Job Deal")
    deals_service.add_deal(deal)

    from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus

    from tests.api.routers.crm.conftest import FakeDealJob as ConfTestFakeDealJob

    job_service.add_job(
        ConfTestFakeDealJob(
            job_id=uuid4(),
            deal_id=deal.deal_id,
            organisation_id=org_id,
            job_type=DealJobType.DEAL_EMAIL_REMATCH,
            status=JobStatus.PENDING,
        )
    )
    job_service.add_job(
        ConfTestFakeDealJob(
            job_id=uuid4(),
            deal_id=deal.deal_id,
            organisation_id=org_id,
            job_type=DealJobType.STAKEHOLDER_SYNC,
            status=JobStatus.IN_PROGRESS,
        )
    )
    job_service.add_job(
        ConfTestFakeDealJob(
            job_id=uuid4(),
            deal_id=deal.deal_id,
            organisation_id=org_id,
            job_type=DealJobType.CONTACT_RESEARCH,
            status=JobStatus.IN_PROGRESS,
        )
    )

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")

    assert resp.status_code == 200
    active_jobs = resp.json()["data"]["active_deal_jobs"]
    assert [job["job_type"] for job in active_jobs["stakeholder_processing"]] == [
        "deal_email_rematch",
        "stakeholder_sync",
    ]


def test_get_deal_age_calculation(client: TestClient, deals_dependency_overrides):
    """Test deal age is calculated correctly."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    # Use a fixed historical create timestamp so age assertions are deterministic.
    deal = create_deal(
        org_id,
        name="Age Test Deal",
        source_created_at=datetime.now(UTC) - timedelta(days=10),
    )
    deals_service.add_deal(deal)

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert data["deal_age_days"] == 10


# =============================================================================
# POST /deals/{deal_id}/deal-summary/generate - Generate AI Summary Tests
# =============================================================================


def test_generate_summary_new_job(client: TestClient, deals_dependency_overrides):
    """Test initiating a new summary job."""
    deals_service = deals_dependency_overrides["deals_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="Summary Test Deal")
    deals_service.add_deal(deal)

    resp = client.post(f"/users/crm/deals/{deal.deal_id}/deal-summary/generate")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert "initiated" in body["message"].lower()
    data = body["data"]
    assert "job_id" in data
    # job_type is converted to string - may include enum name or just value
    assert "deal_summary" in data["job_type"].lower()
    assert "pending" in data["status"].lower()


def test_generate_summary_existing_job(client: TestClient, deals_dependency_overrides):
    """Test returns existing job if already in progress."""
    deals_service = deals_dependency_overrides["deals_service"]
    job_service = deals_dependency_overrides["job_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="Existing Job Deal")
    deals_service.add_deal(deal)

    # Add an existing running job
    from src.domain.models.organisation_tables.job_models import DealJobType, JobStatus

    existing_job = FakeDealJob(
        job_id=uuid4(),
        deal_id=deal.deal_id,
        organisation_id=org_id,
        job_type=DealJobType.DEAL_SUMMARY,
        status=JobStatus.IN_PROGRESS,
    )
    job_service.add_job(existing_job)

    resp = client.post(f"/users/crm/deals/{deal.deal_id}/deal-summary/generate")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "already in progress" in body["message"].lower()
    assert str(body["data"]["job_id"]) == str(existing_job.job_id)


def test_generate_summary_service_error(client: TestClient, deals_dependency_overrides):
    """Test error handling when job service fails."""
    deals_service = deals_dependency_overrides["deals_service"]
    job_service = deals_dependency_overrides["job_service"]
    org_id = deals_dependency_overrides["user_ids"]["organisation_id"]

    deal = create_deal(org_id, name="Error Deal")
    deals_service.add_deal(deal)

    # Configure job service to error
    job_service._should_error = True
    job_service._error_message = "Job creation failed"
    job_service._error_status = 500

    resp = client.post(f"/users/crm/deals/{deal.deal_id}/deal-summary/generate")
    assert resp.status_code == 500, resp.text


# =============================================================================
# SSE stream endpoint tests
# =============================================================================

# Note: SSE endpoints are difficult to test with synchronous TestClient
# as they create long-lived streaming connections. Full SSE testing
# requires async test infrastructure with proper timeout handling.
# The endpoint is verified to exist and be routable via integration tests.


def test_generic_deal_job_stream_returns_sse(
    client: TestClient, deals_dependency_overrides, monkeypatch
):
    """Generic deal job stream endpoint returns SSE using DealJobSSEService."""
    deal_id = uuid4()
    job_id = uuid4()
    created_services = []

    class FakeDealJobSSEService:
        """Fake SSE service that exposes resume handling for list-stream assertions."""

        def __init__(self, organisation_id, created_job_id, user_id):
            self.organisation_id = organisation_id
            self.job_id = created_job_id
            self.user_id = user_id
            self.resume_from_id = None
            created_services.append(self)

        async def ensure_user_access(self, deal_id=None):
            """Allow stream access for this success-path test."""
            return True

        async def stream_events(self, *, resume_from_id=None):
            """Emit a single completion frame to satisfy endpoint contract."""
            self.resume_from_id = resume_from_id
            yield {"event": "complete", "data": "{}"}

        async def close(self):
            """Close hook required by DealJobSSEService contract."""
            return None

    monkeypatch.setattr(
        "src.api.routers.crm.deals_handler.DealJobSSEService",
        FakeDealJobSSEService,
    )

    resp = client.get(
        f"/users/crm/deals/{deal_id}/jobs/{job_id}/stream",
        headers={"Last-Event-ID": "12-0"},
    )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert created_services[0].job_id == job_id
    assert created_services[0].resume_from_id == "12-0"


def test_generic_deal_job_stream_rejects_cross_user_job(
    client: TestClient, deals_dependency_overrides, monkeypatch
):
    """Generic deal job stream endpoint uses DealJobSSEService authorization."""
    deal_id = uuid4()
    job_id = uuid4()

    class FakeDealJobSSEService:
        """Fake SSE service that denies access to validate endpoint error handling."""

        def __init__(self, organisation_id, created_job_id, user_id):
            self.organisation_id = organisation_id
            self.job_id = created_job_id
            self.user_id = user_id

        async def ensure_user_access(self, deal_id=None):
            """Deny access so endpoint returns authorization error."""
            return False

    monkeypatch.setattr(
        "src.api.routers.crm.deals_handler.DealJobSSEService",
        FakeDealJobSSEService,
    )

    resp = client.get(f"/users/crm/deals/{deal_id}/jobs/{job_id}/stream")

    assert resp.status_code == 403


def test_generic_deal_job_stream_returns_404_for_missing_job(
    client: TestClient, deals_dependency_overrides, monkeypatch
):
    """Missing job streams should fail fast instead of waiting on an empty Redis key."""
    deal_id = uuid4()
    job_id = uuid4()

    class FakeDealJobSSEService:
        """Fake SSE service that reports unknown job status."""

        def __init__(self, organisation_id, created_job_id, user_id):
            self.organisation_id = organisation_id
            self.job_id = created_job_id
            self.user_id = user_id

        async def ensure_user_access(self, deal_id=None):
            """Return None to represent a missing/unavailable job."""
            return None

    monkeypatch.setattr(
        "src.api.routers.crm.deals_handler.DealJobSSEService",
        FakeDealJobSSEService,
    )

    resp = client.get(f"/users/crm/deals/{deal_id}/jobs/{job_id}/stream")

    assert resp.status_code == 404


def test_generic_deal_job_stream_rejects_job_from_different_deal(
    client: TestClient, deals_dependency_overrides, monkeypatch
):
    """Generic deal job stream must authorize against the requested deal_id."""
    deal_id = uuid4()
    job_id = uuid4()
    checked_deal_ids = []

    class FakeDealJobSSEService:
        """Fake SSE service that records the requested deal_id for auth checks."""

        def __init__(self, organisation_id, created_job_id, user_id):
            self.organisation_id = organisation_id
            self.job_id = created_job_id
            self.user_id = user_id

        async def ensure_user_access(self, deal_id=None):
            """Track the requested deal_id and deny access for assertion branch."""
            checked_deal_ids.append(deal_id)
            return False

    monkeypatch.setattr(
        "src.api.routers.crm.deals_handler.DealJobSSEService",
        FakeDealJobSSEService,
    )

    resp = client.get(f"/users/crm/deals/{deal_id}/jobs/{job_id}/stream")

    assert resp.status_code == 403
    assert checked_deal_ids == [deal_id]
