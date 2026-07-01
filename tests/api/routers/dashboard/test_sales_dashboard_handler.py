from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.routers.dashboard import sales_dashboard_router
from src.api.schemas.sales_dashboard_models import SalesDashboardPreferences, SalesDashboardResponse


def test_sales_dashboard_route_returns_success(
    monkeypatch,
) -> None:
    async def fake_get_sales_dashboard(self, query, *, user_id):
        return SalesDashboardResponse(
            dashboard_id="sales",
            schema_version=1,
            title="Sales Dashboard",
            generated_at=datetime(2026, 5, 6, tzinfo=UTC),
            effective_filters={
                "date_range": "last_90_days",
                "from_date": "2026-02-05T00:00:00Z",
                "to_date": "2026-05-06T00:00:00Z",
                "pipeline_ids": [],
                "owner_ids": [],
            },
            sections=[],
            warnings=[],
        )

    user_ids = UserIDs(user_id=uuid4(), organisation_id=uuid4())
    app = FastAPI()
    app.include_router(sales_dashboard_router, prefix="/users/dashboard")

    def override_user() -> UserIDs:
        return user_ids

    async def override_org_session():
        yield object()

    async def override_master_session():
        yield object()

    async def override_rate_limit() -> None:
        return None

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_org_async_session] = override_org_session
    app.dependency_overrides[get_master_async_session] = override_master_session
    app.dependency_overrides[rate_limit_user_read] = override_rate_limit
    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service."
        "DashboardSnapshotService.get_sales_dashboard",
        fake_get_sales_dashboard,
    )

    with TestClient(app) as client:
        response = client.get("/users/dashboard/sales")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["dashboard_id"] == "sales"


def test_sales_dashboard_route_uses_snapshot_service(monkeypatch) -> None:
    async def fake_get_sales_dashboard(self, query, *, user_id):
        assert user_id == user_ids.user_id
        return SalesDashboardResponse(
            dashboard_id="sales",
            schema_version=1,
            title="Sales Dashboard",
            generated_at=datetime(2026, 5, 6, tzinfo=UTC),
            effective_filters={"date_range": "last_90_days"},
            sections=[],
            warnings=[],
        )

    user_ids = UserIDs(user_id=uuid4(), organisation_id=uuid4())
    app = FastAPI()
    app.include_router(sales_dashboard_router, prefix="/users/dashboard")

    def override_user() -> UserIDs:
        return user_ids

    async def override_org_session():
        yield object()

    async def override_master_session():
        yield object()

    async def override_rate_limit() -> None:
        return None

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_org_async_session] = override_org_session
    app.dependency_overrides[get_master_async_session] = override_master_session
    app.dependency_overrides[rate_limit_user_read] = override_rate_limit
    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service."
        "DashboardSnapshotService.get_sales_dashboard",
        fake_get_sales_dashboard,
    )

    with TestClient(app) as client:
        response = client.get("/users/dashboard/sales")

    assert response.status_code == 200
    assert response.json()["data"]["dashboard_id"] == "sales"


def test_sales_dashboard_preferences_routes_return_success(monkeypatch) -> None:
    async def fake_get_preferences(self):
        return SalesDashboardPreferences(filters={"date_range": "last_90_days"})

    async def fake_update_preferences(self, request):
        return SalesDashboardPreferences(
            filters=request.filters,
            hidden_widget_ids=request.hidden_widget_ids,
        )

    user_ids = UserIDs(user_id=uuid4(), organisation_id=uuid4())
    app = FastAPI()
    app.include_router(sales_dashboard_router, prefix="/users/dashboard")

    def override_user() -> UserIDs:
        return user_ids

    async def override_master_session():
        yield object()

    async def override_rate_limit() -> None:
        return None

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_master_async_session] = override_master_session
    app.dependency_overrides[rate_limit_user_read] = override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = override_rate_limit
    monkeypatch.setattr(
        "src.service.dashboard.sales_dashboard_service.SalesDashboardService.get_preferences",
        fake_get_preferences,
    )
    monkeypatch.setattr(
        "src.service.dashboard.sales_dashboard_service.SalesDashboardService.update_preferences",
        fake_update_preferences,
    )

    with TestClient(app) as client:
        get_response = client.get("/users/dashboard/sales/preferences")
        patch_response = client.patch(
            "/users/dashboard/sales/preferences",
            json={
                "filters": {"date_range": "last_30_days"},
                "hidden_widget_ids": ["stage_movement_flow"],
            },
        )

    assert get_response.status_code == 200
    assert get_response.json()["data"]["filters"]["date_range"] == "last_90_days"
    assert patch_response.status_code == 200
    assert patch_response.json()["data"]["filters"]["date_range"] == "last_30_days"
    assert patch_response.json()["data"]["hidden_widget_ids"] == ["stage_movement_flow"]
