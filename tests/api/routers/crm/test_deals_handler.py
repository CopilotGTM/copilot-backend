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
from src.api.schemas.crm_deal_write_models import DealCreateRequest
from src.api.schemas.crm_models import DealsListParams
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStatus

from tests.api.routers.crm.conftest import (
    FakeCompany,
    FakeDealJob,
    create_deal,
)


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
                added=[SimpleNamespace(participant_id=participant_id)],
            )
        ),
        trigger_deal_mapping_if_ready=AsyncMock(),
    )
    job_visibility_service = SimpleNamespace(
        create_stakeholder_processing_jobs=AsyncMock(return_value=[]),
    )
    dispatch_research = AsyncMock(return_value=None)

    class FakeStakeholdersService:
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
        "StakeholdersService",
        FakeStakeholdersService,
    )
    monkeypatch.setattr(
        deals_handler_module,
        "StakeholderJobVisibilityService",
        FakeStakeholderJobVisibilityService,
        raising=False,
    )
    monkeypatch.setattr(
        deals_handler_module,
        "_dispatch_auto_stakeholder_research",
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
        participant_id=participant_id,
        deal_id=deal.deal_id,
        user_id=write_service.actor_user_id,
    )
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once_with(deal)
    assert response.status == "SUCCESS"


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
            q=None,
        ),
        service=fake_deals_service,
        _rl_read=None,
    )

    assert result.status == "SUCCESS"
    assert [item.name for item in result.data.items] == ["CRM-only Owner Deal"]


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
        currency="USD",
        deal_status=CRMDealStatus.OPEN,
        probability=0.5,
        close_date=None,
        expected_close_date=None,
        stage_name="Discovery",
        stage_label="Discovery",
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
    # Status key is the enum value (e.g., "open" or "OPEN")
    assert data["by_status"].get("OPEN", data["by_status"].get("open", 0)) == 2
    assert data["by_status"].get("WON", data["by_status"].get("won", 0)) == 1
    assert data["total_amount"] == 60000
    assert data["average_amount"] == 20000


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
    assert data["currency"] == "USD"
    assert data["currency_symbol"] == "$"


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
    mocker.patch(
        "src.api.routers.crm.deals_handler._get_active_deal_custom_field_keys",
        new=AsyncMock(return_value=["our_revenue_inr", "next_follow_up_date"]),
    )

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
    custom_keys_mock = mocker.patch(
        "src.api.routers.crm.deals_handler._get_active_deal_custom_field_keys",
        new=AsyncMock(return_value=["our_revenue_inr", "next_follow_up_date"]),
    )

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
    mocker.patch(
        "src.api.routers.crm.deals_handler._get_active_deal_custom_field_definitions",
        new=AsyncMock(
            return_value=[
                SimpleNamespace(
                    key="custom_start_at",
                    label="Start At",
                    data_type="datetime",
                    required=False,
                    options={},
                    validation={},
                ),
                SimpleNamespace(
                    key="custom_budget",
                    label="Budget",
                    data_type="currency",
                    required=False,
                    options={},
                    validation={},
                ),
            ]
        ),
    )

    resp = client.get(f"/users/crm/deals/{deal.deal_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    definitions = {item["key"]: item for item in data["custom_column_definitions"]}
    assert definitions["custom_start_at"]["data_type"] == "datetime"
    assert definitions["custom_start_at"]["currency_code"] is None
    assert definitions["custom_budget"]["data_type"] == "currency"
    assert definitions["custom_budget"]["currency_code"] == "INR"
    assert definitions["custom_budget"]["currency_symbol"] == "₹"


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
            job_type=DealJobType.STAKEHOLDER_RESEARCH,
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
# POST /process-meetings - Trigger Meeting Processing Tests
# =============================================================================


def test_process_meetings_success(client: TestClient, deals_dependency_overrides, mocker):
    """Test successfully triggering meeting processing."""
    mocker.patch(
        "src.api.routers.crm.deals_handler.send_deal_task_async",
        return_value="mock-task-id",
    )

    resp = client.post("/users/crm/process-meetings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["task_id"] == "mock-task-id"


def test_process_meetings_celery_error(client: TestClient, deals_dependency_overrides, mocker):
    """Test meeting processing when celery fails."""
    mocker.patch(
        "src.api.routers.crm.deals_handler.send_deal_task_async",
        side_effect=Exception("Celery connection failed"),
    )

    resp = client.post("/users/crm/process-meetings")
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"


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
