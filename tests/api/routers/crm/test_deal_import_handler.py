from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write


@pytest.fixture
def deal_import_dependency_overrides(client: TestClient, test_user_ids):
    user_ids = UserIDs(
        user_id=test_user_ids["user_id"],
        organisation_id=test_user_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )

    async def _override_user():
        return user_ids

    async def _override_rate_limit():
        return None

    from src.infra.utils.app_state import fastapp as app

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield user_ids
    app.dependency_overrides = {}


def test_get_deal_import_template_returns_coming_soon(
    client: TestClient, deal_import_dependency_overrides
):
    response = client.get("/users/crm/deals/import/template")

    assert response.status_code == 409
    assert response.json()["message"] == (
        "Deal CSV import is coming soon. The previous import flow has been removed."
    )


def test_create_deal_import_upload_url_returns_coming_soon(
    client: TestClient, deal_import_dependency_overrides
):
    response = client.post(
        "/users/crm/deals/import/upload-url",
        json={"filename": "deals.csv"},
    )

    assert response.status_code == 409
    assert response.json()["message"] == (
        "Deal CSV import is coming soon. The previous import flow has been removed."
    )


def test_process_deal_import_returns_coming_soon(
    client: TestClient, deal_import_dependency_overrides
):
    user_id = deal_import_dependency_overrides.user_id
    response = client.post(
        "/users/crm/deals/import/process",
        json={"storage_path": f"crm_imports/deals/{user_id}/f.csv"},
    )

    assert response.status_code == 409
    assert response.json()["message"] == (
        "Deal CSV import is coming soon. The previous import flow has been removed."
    )


def test_deal_import_stream_returns_coming_soon(
    client: TestClient, deal_import_dependency_overrides
):
    response = client.get(f"/users/crm/deals/import/jobs/{uuid4()}/events")

    assert response.status_code == 409
    assert response.json()["message"] == (
        "Deal CSV import is coming soon. The previous import flow has been removed."
    )
