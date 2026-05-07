import asyncio
from types import SimpleNamespace
from uuid import uuid4

from src.api.schemas.sales_dashboard_models import UpdateSalesDashboardPreferencesRequest
from src.service.dashboard.preferences import (
    SALES_DASHBOARD_PREFERENCES_PATH,
    extract_sales_dashboard_preferences,
    merge_sales_dashboard_preferences,
)
from src.service.dashboard.sales_dashboard_service import SalesDashboardService


def test_extract_missing_preferences_returns_empty_shape() -> None:
    assert extract_sales_dashboard_preferences({}) == {}


def test_merge_sales_dashboard_preferences_preserves_other_preferences() -> None:
    dashboard_id = str(uuid4())
    existing = {
        "dashboard": {
            "preferences_by_dashboard_id": {
                dashboard_id: {"filters": {"date_range": "last_30_days"}}
            }
        },
        "notifications": {"email": True},
    }

    updated = merge_sales_dashboard_preferences(
        existing,
        {"filters": {"date_range": "last_90_days"}},
    )

    assert SALES_DASHBOARD_PREFERENCES_PATH == ("dashboard", "sales")
    assert updated["dashboard"]["sales"]["filters"]["date_range"] == "last_90_days"
    assert (
        updated["dashboard"]["preferences_by_dashboard_id"][dashboard_id]["filters"]["date_range"]
        == "last_30_days"
    )
    assert updated["notifications"]["email"] is True


class FakeScalarResult:
    def __init__(self, row) -> None:
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class FakeMasterSession:
    def __init__(self, user) -> None:
        self.user = user
        self.committed = False
        self.refreshed = None

    async def execute(self, _statement):
        return FakeScalarResult(self.user)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, row) -> None:
        self.refreshed = row


def test_service_updates_sales_dashboard_preferences_without_removing_other_branches() -> None:
    user = SimpleNamespace(
        preferences={
            "dashboard": {
                "preferences_by_dashboard_id": {
                    "private-board": {"filters": {"date_range": "last_30_days"}}
                }
            },
            "notifications": {"email": True},
        }
    )
    master_session = FakeMasterSession(user)
    service = SalesDashboardService(
        org_session=None,
        master_session=master_session,
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    response = asyncio.run(
        service.update_preferences(
            UpdateSalesDashboardPreferencesRequest(
                filters={"date_range": "last_90_days"},
                hidden_widget_ids=["stage_movement_flow"],
            )
        )
    )

    assert response.filters["date_range"] == "last_90_days"
    assert user.preferences["dashboard"]["sales"]["hidden_widget_ids"] == ["stage_movement_flow"]
    assert (
        user.preferences["dashboard"]["preferences_by_dashboard_id"]["private-board"]["filters"][
            "date_range"
        ]
        == "last_30_days"
    )
    assert user.preferences["notifications"]["email"] is True
    assert master_session.committed is True
    assert master_session.refreshed is user
