import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from src.api.schemas.sales_dashboard_models import (
    SalesDashboardPrimarySegment,
    SalesDashboardQueryParams,
    SalesDashboardResponse,
)
from src.domain.models.organisation_tables.dashboard_models import DashboardSnapshot
from src.service.dashboard.dashboard_snapshot_service import DashboardSnapshotService


def _sales_response() -> SalesDashboardResponse:
    return SalesDashboardResponse(
        dashboard_id="sales",
        schema_version=1,
        title="Sales Dashboard",
        generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        effective_filters={"date_range": "last_90_days"},
        sections=[],
        warnings=[],
    )


class FakeResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class FakeOrgSession:
    def __init__(self, row=None) -> None:
        self.row = row
        self.added = []
        self.commits = 0
        self.refreshes = []

    async def execute(self, stmt):
        return FakeResult(self.row)

    def add(self, row):
        self.row = row
        self.added.append(row)

    async def commit(self):
        self.commits += 1

    async def refresh(self, row):
        self.refreshes.append(row)


def test_regenerate_sales_snapshot_creates_sales_dashboard_row(monkeypatch) -> None:
    organisation_id = uuid4()
    user_id = uuid4()
    org_session = FakeOrgSession()

    class FakeSalesDashboardService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def get_sales_dashboard(self, query):
            assert isinstance(query, SalesDashboardQueryParams)
            return _sales_response()

    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service.SalesDashboardService",
        FakeSalesDashboardService,
    )

    snapshot = asyncio.run(
        DashboardSnapshotService(
            org_session=org_session,
            master_session=object(),
            organisation_id=organisation_id,
        ).regenerate_sales_snapshot(user_id=user_id)
    )

    assert snapshot.dashboard_key == "sales"
    assert snapshot.dashboard_name == "Sales Dashboard"
    assert snapshot.dashboard_type == "system"
    assert snapshot.schema_version == 1
    assert snapshot.created_by_user_id == user_id
    assert snapshot.latest_generation_status == "success"
    assert snapshot.latest_generation_error is None
    assert snapshot.latest_data["dashboard_id"] == "sales"
    assert snapshot.latest_data_generated_at == datetime(2026, 7, 1, tzinfo=UTC)
    assert snapshot.definition["enabled_widget_ids"]
    assert org_session.added == [snapshot]
    assert org_session.commits == 1


def test_regenerate_sales_snapshot_updates_existing_sales_row(monkeypatch) -> None:
    organisation_id = uuid4()
    existing = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="pending",
    )
    org_session = FakeOrgSession(existing)

    class FakeSalesDashboardService:
        def __init__(self, **kwargs):
            pass

        async def get_sales_dashboard(self, query):
            return _sales_response()

    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service.SalesDashboardService",
        FakeSalesDashboardService,
    )

    snapshot = asyncio.run(
        DashboardSnapshotService(
            org_session=org_session,
            master_session=object(),
            organisation_id=organisation_id,
        ).regenerate_sales_snapshot(user_id=uuid4())
    )

    assert snapshot is existing
    assert org_session.added == []
    assert snapshot.latest_generation_status == "success"
    assert snapshot.latest_data["title"] == "Sales Dashboard"
    assert org_session.commits == 1


def test_regenerate_sales_snapshot_marks_existing_row_failed(monkeypatch) -> None:
    organisation_id = uuid4()
    existing = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="success",
        latest_data={"dashboard_id": "sales"},
    )
    org_session = FakeOrgSession(existing)

    class FakeSalesDashboardService:
        def __init__(self, **kwargs):
            pass

        async def get_sales_dashboard(self, query):
            raise RuntimeError("dashboard render failed")

    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service.SalesDashboardService",
        FakeSalesDashboardService,
    )

    with pytest.raises(RuntimeError, match="dashboard render failed"):
        asyncio.run(
            DashboardSnapshotService(
                org_session=org_session,
                master_session=object(),
                organisation_id=organisation_id,
            ).regenerate_sales_snapshot(user_id=uuid4())
        )

    assert existing.latest_generation_status == "failed"
    assert existing.latest_generation_error == "dashboard render failed"
    assert existing.latest_data == {"dashboard_id": "sales"}
    assert org_session.commits == 1


def test_get_latest_sales_snapshot_data_returns_only_successful_payload() -> None:
    organisation_id = uuid4()
    successful = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="success",
        latest_data={"dashboard_id": "sales"},
    )

    data = asyncio.run(
        DashboardSnapshotService(
            org_session=FakeOrgSession(successful),
            master_session=object(),
            organisation_id=organisation_id,
        ).get_latest_sales_snapshot_data()
    )

    assert data == {"dashboard_id": "sales"}

    failed = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="failed",
        latest_data={"dashboard_id": "sales"},
    )

    data = asyncio.run(
        DashboardSnapshotService(
            org_session=FakeOrgSession(failed),
            master_session=object(),
            organisation_id=organisation_id,
        ).get_latest_sales_snapshot_data()
    )

    assert data is None


def test_get_sales_dashboard_uses_fresh_default_snapshot_without_rendering(monkeypatch) -> None:
    organisation_id = uuid4()
    generated_at = datetime(2026, 7, 1, tzinfo=UTC)
    snapshot = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="success",
        latest_data=_sales_response().model_dump(mode="json"),
        latest_data_generated_at=generated_at,
        refresh_interval_seconds=86400,
    )

    class FakeSalesDashboardService:
        def __init__(self, **kwargs):
            pass

        async def get_sales_dashboard(self, query):
            raise AssertionError("fresh default snapshot should avoid live render")

    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service.SalesDashboardService",
        FakeSalesDashboardService,
    )

    response = asyncio.run(
        DashboardSnapshotService(
            org_session=FakeOrgSession(snapshot),
            master_session=object(),
            organisation_id=organisation_id,
        ).get_sales_dashboard(
            SalesDashboardQueryParams(),
            user_id=uuid4(),
            now=generated_at + timedelta(hours=1),
        )
    )

    assert response.dashboard_id == "sales"
    assert response.generated_at == generated_at


def test_get_sales_dashboard_regenerates_stale_default_snapshot(monkeypatch) -> None:
    organisation_id = uuid4()
    old_generated_at = datetime(2026, 6, 30, tzinfo=UTC)
    fresh_generated_at = datetime(2026, 7, 1, tzinfo=UTC)
    snapshot = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="success",
        latest_data={**_sales_response().model_dump(mode="json"), "generated_at": old_generated_at},
        latest_data_generated_at=old_generated_at,
        refresh_interval_seconds=60,
    )

    class FakeSalesDashboardService:
        def __init__(self, **kwargs):
            pass

        async def get_sales_dashboard(self, query):
            response = _sales_response()
            return response.model_copy(update={"generated_at": fresh_generated_at})

    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service.SalesDashboardService",
        FakeSalesDashboardService,
    )

    response = asyncio.run(
        DashboardSnapshotService(
            org_session=FakeOrgSession(snapshot),
            master_session=object(),
            organisation_id=organisation_id,
        ).get_sales_dashboard(
            SalesDashboardQueryParams(),
            user_id=uuid4(),
            now=fresh_generated_at,
        )
    )

    assert response.generated_at == fresh_generated_at
    assert snapshot.latest_data_generated_at == fresh_generated_at


def test_get_sales_dashboard_renders_filtered_requests_without_updating_default_snapshot(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    snapshot = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="success",
        latest_data=_sales_response().model_dump(mode="json"),
        latest_data_generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        refresh_interval_seconds=86400,
    )
    original_payload = dict(snapshot.latest_data)
    owner_id = uuid4()

    class FakeSalesDashboardService:
        def __init__(self, **kwargs):
            pass

        async def get_sales_dashboard(self, query):
            assert query.owner_ids == [owner_id]
            response = _sales_response()
            return response.model_copy(update={"effective_filters": {"owner_ids": [str(owner_id)]}})

    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service.SalesDashboardService",
        FakeSalesDashboardService,
    )

    org_session = FakeOrgSession(snapshot)
    response = asyncio.run(
        DashboardSnapshotService(
            org_session=org_session,
            master_session=object(),
            organisation_id=organisation_id,
        ).get_sales_dashboard(
            SalesDashboardQueryParams(owner_ids=[owner_id]),
            user_id=uuid4(),
            now=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )

    assert response.effective_filters == {"owner_ids": [str(owner_id)]}
    assert snapshot.latest_data == original_payload
    assert org_session.commits == 0


def test_get_sales_dashboard_renders_non_default_segment_without_using_snapshot(
    monkeypatch,
) -> None:
    organisation_id = uuid4()
    snapshot = DashboardSnapshot(
        organisation_id=organisation_id,
        dashboard_key="sales",
        dashboard_name="Sales Dashboard",
        dashboard_type="system",
        schema_version=1,
        definition={},
        latest_generation_status="success",
        latest_data=_sales_response().model_dump(mode="json"),
        latest_data_generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        refresh_interval_seconds=86400,
    )

    class FakeSalesDashboardService:
        def __init__(self, **kwargs):
            pass

        async def get_sales_dashboard(self, query):
            assert query.primary_segment == SalesDashboardPrimarySegment.OWNER
            response = _sales_response()
            return response.model_copy(update={"effective_filters": {"primary_segment": "owner"}})

    monkeypatch.setattr(
        "src.service.dashboard.dashboard_snapshot_service.SalesDashboardService",
        FakeSalesDashboardService,
    )

    response = asyncio.run(
        DashboardSnapshotService(
            org_session=FakeOrgSession(snapshot),
            master_session=object(),
            organisation_id=organisation_id,
        ).get_sales_dashboard(
            SalesDashboardQueryParams(primary_segment=SalesDashboardPrimarySegment.OWNER),
            user_id=uuid4(),
            now=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )

    assert response.effective_filters == {"primary_segment": "owner"}
