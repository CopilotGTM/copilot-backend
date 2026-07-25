"""Tests for Deal Assets API handler.

Verifies HTTP layer behaviour: status codes, JSON structure, and correct
delegation to the service layer.  The service is fully mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.crm_record_access import (
    require_deal_collaborate,
    require_deal_control,
    require_deal_view,
)
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.routers.crm.deal_assets_handler import get_deal_assets_service
from src.service.crm.assets.deal_assets_service import DealAssetsServiceError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_ID = uuid4()
_USER_ID = uuid4()
_DEAL_ID = uuid4()
_ASSET_ID = uuid4()
_NOW = datetime.now(tz=UTC)


def _make_fake_asset(
    asset_id=None,
    deal_id=None,
    **overrides,
):
    """Return a plain object that quacks like a DealAsset ORM instance."""
    asset = MagicMock()
    asset.asset_id = asset_id or _ASSET_ID
    asset.deal_id = deal_id or _DEAL_ID
    asset.organisation_id = _ORG_ID
    asset.name = overrides.get("name", "proposal.pdf")
    asset.category = overrides.get("category", "proposal")
    asset.direction = overrides.get("direction", "internal")
    asset.status = overrides.get("status", "active")
    asset.filename = overrides.get("filename", "proposal.pdf")
    asset.mimetype = overrides.get("mimetype", "application/pdf")
    asset.size_bytes = overrides.get("size_bytes", 12345)
    asset.external_url = overrides.get("external_url")
    asset.external_source = overrides.get("external_source")
    asset.link_status = overrides.get("link_status")
    asset.link_checked_at = overrides.get("link_checked_at")
    asset.kb_source_id = overrides.get("kb_source_id")
    asset.source_type = overrides.get("source_type", "manual_upload")
    asset.source_context = overrides.get("source_context")
    asset.deal_stage_when_added = overrides.get("deal_stage_when_added")
    asset.added_by_user_id = overrides.get("added_by_user_id", _USER_ID)
    asset.created_at = overrides.get("created_at", _NOW)
    asset.updated_at = overrides.get("updated_at", _NOW)
    asset.storage_path = overrides.get("storage_path")
    asset.thumbnail_small_path = overrides.get("thumbnail_small_path")
    asset.thumbnail_medium_path = overrides.get("thumbnail_medium_path")
    asset.is_deleted = overrides.get("is_deleted", False)
    return asset


def _build_mock_service():
    """Build an AsyncMock that satisfies the DealAssetsService protocol."""
    svc = AsyncMock()
    # generate_download_url / generate_thumbnail_urls are called by _map_asset_to_item
    svc.generate_download_url = AsyncMock(return_value=None)
    svc.generate_thumbnail_urls = AsyncMock(return_value=(None, None))
    # session mock for commit/rollback calls in the handler
    svc.session = AsyncMock()
    svc.close = AsyncMock()
    return svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_service():
    return _build_mock_service()


@pytest.fixture()
def assets_dependency_overrides(client: TestClient, mock_service):
    """Override auth + service dependencies for deal-assets endpoints."""
    from src.infra.utils.app_state import fastapp as app

    def _override_user():
        return UserIDs(
            user_id=_USER_ID,
            organisation_id=_ORG_ID,
            user_name="Test User",
            user_email="test@example.com",
        )

    async def _override_service():
        try:
            yield mock_service
        finally:
            await mock_service.close()

    async def _override_rate_limit():
        return None

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_deal_assets_service] = _override_service
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    app.dependency_overrides[require_deal_view] = _override_rate_limit
    app.dependency_overrides[require_deal_collaborate] = _override_rate_limit
    app.dependency_overrides[require_deal_control] = _override_rate_limit
    yield mock_service
    app.dependency_overrides = {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_PREFIX = "/users/crm/deals"


def test_list_deal_assets(client: TestClient, assets_dependency_overrides):
    """GET /{deal_id}/assets returns 200 with items list."""
    svc = assets_dependency_overrides
    fake_asset = _make_fake_asset()
    svc.list_assets.return_value = ([fake_asset], 1)

    resp = client.get(f"{_PREFIX}/{_DEAL_ID}/assets")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["total_count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["asset_id"] == str(fake_asset.asset_id)


def test_get_deal_asset(client: TestClient, assets_dependency_overrides):
    """GET /{deal_id}/assets/{asset_id} returns 200."""
    svc = assets_dependency_overrides
    fake_asset = _make_fake_asset()
    svc.get_asset.return_value = fake_asset

    resp = client.get(f"{_PREFIX}/{_DEAL_ID}/assets/{_ASSET_ID}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["asset_id"] == str(_ASSET_ID)
    assert body["data"]["name"] == "proposal.pdf"


def test_get_deal_asset_not_found(client: TestClient, assets_dependency_overrides):
    """GET /{deal_id}/assets/{asset_id} returns 404 when service raises."""
    svc = assets_dependency_overrides
    svc.get_asset.side_effect = DealAssetsServiceError("Asset not found", status_code=404)

    missing_id = uuid4()
    resp = client.get(f"{_PREFIX}/{_DEAL_ID}/assets/{missing_id}")

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["status"] == "ERROR"
    assert "not found" in body["message"].lower()


def test_presign_upload(client: TestClient, assets_dependency_overrides):
    """POST /{deal_id}/assets/upload/presign returns 201 with upload_url."""
    svc = assets_dependency_overrides
    fake_asset = _make_fake_asset(status="pending_upload")
    svc.presign_file_upload.return_value = (
        fake_asset,
        "https://blob.example.com/upload?sig=abc",
        900,
    )

    payload = {
        "filename": "deck.pptx",
        "mimetype": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "size_bytes": 50000,
    }
    resp = client.post(f"{_PREFIX}/{_DEAL_ID}/assets/upload/presign", json=payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    data = body["data"]
    assert data["upload_url"] == "https://blob.example.com/upload?sig=abc"
    assert data["expires_in_seconds"] == 900
    assert data["status"] == "pending_upload"


def test_finalize_upload(client: TestClient, assets_dependency_overrides):
    """POST /{deal_id}/assets/upload/finalize returns 200 with processing status."""
    svc = assets_dependency_overrides
    fake_asset = _make_fake_asset(status="processing")
    svc.finalize_file_upload.return_value = fake_asset

    with patch(
        "src.api.routers.crm.deal_assets_handler.send_deal_task_async",
        new_callable=AsyncMock,
    ) as mock_send:
        mock_send.return_value = "task-id-123"
        payload = {"asset_id": str(_ASSET_ID)}
        resp = client.post(f"{_PREFIX}/{_DEAL_ID}/assets/upload/finalize", json=payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["status"] == "processing"
    mock_send.assert_awaited_once()


def test_finalize_upload_reports_queue_failure(client: TestClient, assets_dependency_overrides):
    """A committed upload is retryable by the client when task publication fails."""
    svc = assets_dependency_overrides
    svc.finalize_file_upload.return_value = _make_fake_asset(status="processing")

    with patch(
        "src.api.routers.crm.deal_assets_handler.send_deal_task_async",
        new_callable=AsyncMock,
        side_effect=RuntimeError("broker unavailable"),
    ):
        response = client.post(
            f"{_PREFIX}/{_DEAL_ID}/assets/upload/finalize",
            json={"asset_id": str(_ASSET_ID)},
        )

    assert response.status_code == 503
    assert response.json()["status"] == "ERROR"
    svc.session.commit.assert_awaited_once()


def test_create_link(client: TestClient, assets_dependency_overrides):
    """POST /{deal_id}/assets/links returns 201."""
    svc = assets_dependency_overrides
    fake_asset = _make_fake_asset(
        external_url="https://docs.google.com/spreadsheet/123",
        external_source="google_drive",
        filename=None,
        mimetype=None,
        size_bytes=None,
        storage_path=None,
    )
    svc.create_link.return_value = fake_asset

    payload = {
        "name": "Budget Spreadsheet",
        "external_url": "https://docs.google.com/spreadsheet/123",
        "external_source": "google_drive",
    }
    resp = client.post(f"{_PREFIX}/{_DEAL_ID}/assets/links", json=payload)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["external_url"] == "https://docs.google.com/spreadsheet/123"


def test_update_asset(client: TestClient, assets_dependency_overrides):
    """PATCH /{deal_id}/assets/{asset_id} returns 200."""
    svc = assets_dependency_overrides
    fake_asset = _make_fake_asset(name="Updated Name", category="contract")
    svc.update_asset.return_value = fake_asset

    payload = {"name": "Updated Name", "category": "contract"}
    resp = client.patch(f"{_PREFIX}/{_DEAL_ID}/assets/{_ASSET_ID}", json=payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["name"] == "Updated Name"
    assert body["data"]["category"] == "contract"


def test_delete_asset(client: TestClient, assets_dependency_overrides):
    """DELETE /{deal_id}/assets/{asset_id} returns 200."""
    svc = assets_dependency_overrides
    fake_asset = _make_fake_asset(is_deleted=True)
    svc.delete_asset.return_value = fake_asset

    resp = client.delete(f"{_PREFIX}/{_DEAL_ID}/assets/{_ASSET_ID}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["data"]["asset_id"] == str(_ASSET_ID)
