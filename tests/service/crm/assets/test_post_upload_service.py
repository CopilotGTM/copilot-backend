"""Regression tests for the deal-asset post-upload state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import src.service.crm.assets.post_upload_service as post_upload
from src.domain.models.organisation_tables.deal_asset_models import DealAssetStatus
from src.service.crm.assets.post_upload_service import (
    DealAssetPostUploadService,
    PostUploadStatus,
)

ORGANISATION_ID = UUID("11111111-1111-1111-1111-111111111111")
ASSET_ID = UUID("22222222-2222-2222-2222-222222222222")
DEAL_ID = UUID("33333333-3333-3333-3333-333333333333")


class _Session:
    def __init__(self, asset: SimpleNamespace | None):
        self.asset = asset
        self.commits = 0

    def get(self, model, asset_id):  # noqa: ANN001
        return self.asset if asset_id in {ASSET_ID, str(ASSET_ID)} else None

    def merge(self, asset: SimpleNamespace) -> None:
        self.asset = asset

    def commit(self) -> None:
        self.commits += 1


class _FailingCommitSession(_Session):
    def commit(self) -> None:
        raise RuntimeError("database unavailable")


def _asset(**overrides) -> SimpleNamespace:
    values = {
        "asset_id": ASSET_ID,
        "deal_id": DEAL_ID,
        "storage_path": "deal_assets/file.pdf",
        "mimetype": "application/pdf",
        "checksum": None,
        "status": DealAssetStatus.PROCESSING.value,
        "thumbnail_small_path": None,
        "thumbnail_medium_path": None,
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service(storage) -> DealAssetPostUploadService:  # noqa: ANN001
    service = DealAssetPostUploadService.__new__(DealAssetPostUploadService)
    service.organisation_id = str(ORGANISATION_ID)
    service.org_uuid = ORGANISATION_ID
    service.storage = storage
    return service


def test_already_processed_asset_returns_active_event_without_blob_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset(status=DealAssetStatus.ACTIVE.value, checksum="sha256")
    session = _Session(asset)
    monkeypatch.setattr(post_upload, "acquire_org_session", lambda _: [session])
    storage = SimpleNamespace(
        compute_checksum=AsyncMock(),
        generate_read_sas=AsyncMock(),
        close=AsyncMock(),
    )

    outcome = _service(storage).process(str(ASSET_ID))

    assert outcome.status is PostUploadStatus.ALREADY_PROCESSED
    assert outcome.event is not None
    assert outcome.event.status == DealAssetStatus.ACTIVE.value
    storage.compute_checksum.assert_not_awaited()
    assert session.commits == 0


def test_checksum_failure_commits_failed_state_and_requests_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    session = _Session(asset)
    monkeypatch.setattr(post_upload, "acquire_org_session", lambda _: [session])
    storage = SimpleNamespace(
        compute_checksum=AsyncMock(side_effect=OSError("blob unavailable")),
        close=AsyncMock(),
    )

    outcome = _service(storage).process(str(ASSET_ID))

    assert outcome.status is PostUploadStatus.RETRYABLE_FAILURE
    assert outcome.event is not None
    assert outcome.event.status == DealAssetStatus.FAILED.value
    assert "blob unavailable" in (outcome.event.error_message or "")
    assert asset.status == DealAssetStatus.FAILED.value
    assert asset.checksum is None
    assert session.commits == 1


def test_thumbnail_failure_is_nonfatal_after_checksum_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset()
    session = _Session(asset)
    monkeypatch.setattr(post_upload, "acquire_org_session", lambda _: [session])
    monkeypatch.setattr(post_upload.thumbnail_config, "is_supported", lambda _: True)
    monkeypatch.setattr(
        post_upload,
        "generate_thumbnails",
        AsyncMock(side_effect=OSError("thumbnail unavailable")),
    )
    storage = SimpleNamespace(
        compute_checksum=AsyncMock(return_value="sha256"),
        generate_read_sas=AsyncMock(),
        close=AsyncMock(),
    )

    outcome = _service(storage).process(str(ASSET_ID))

    assert outcome.status is PostUploadStatus.PROCESSED
    assert outcome.event is not None
    assert outcome.event.status == DealAssetStatus.ACTIVE.value
    assert asset.status == DealAssetStatus.ACTIVE.value
    assert asset.checksum == "sha256"
    assert session.commits == 1


def test_missing_storage_path_is_terminal_failed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset(storage_path=None)
    session = _Session(asset)
    monkeypatch.setattr(post_upload, "acquire_org_session", lambda _: [session])
    storage = SimpleNamespace(close=AsyncMock())

    outcome = _service(storage).process(str(ASSET_ID))

    assert outcome.status is PostUploadStatus.INVALID
    assert outcome.event is not None
    assert outcome.event.status == DealAssetStatus.FAILED.value
    assert asset.status == DealAssetStatus.FAILED.value
    assert session.commits == 1


def test_missing_asset_is_terminal_without_blob_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(None)
    monkeypatch.setattr(post_upload, "acquire_org_session", lambda _: [session])
    storage = SimpleNamespace(compute_checksum=AsyncMock(), close=AsyncMock())

    outcome = _service(storage).process(str(ASSET_ID))

    assert outcome.status is PostUploadStatus.INVALID
    assert outcome.event is None
    storage.compute_checksum.assert_not_awaited()


def test_finalization_failure_commits_failed_state_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = _asset(checksum="sha256", mimetype="unsupported/type")
    sessions = iter(
        [
            _Session(asset),
            _FailingCommitSession(asset),
            _Session(asset),
        ]
    )
    monkeypatch.setattr(
        post_upload,
        "acquire_org_session",
        lambda _: [next(sessions)],
    )
    storage = SimpleNamespace(close=AsyncMock())

    outcome = _service(storage).process(str(ASSET_ID))

    assert outcome.status is PostUploadStatus.RETRYABLE_FAILURE
    assert outcome.event is not None
    assert outcome.event.status == DealAssetStatus.FAILED.value
    assert asset.status == DealAssetStatus.FAILED.value


def test_thumbnail_url_is_generated_after_database_session_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow Azure SAS work must not consume a tenant database connection."""
    asset = _asset(
        checksum="sha256",
        mimetype="text/plain",
        thumbnail_small_path="deal_assets/thumb.jpg",
    )
    session = _Session(asset)
    session_active = False

    def acquire(_organisation_id):
        nonlocal session_active
        session_active = True
        try:
            yield session
        finally:
            session_active = False

    observed_session_states: list[bool] = []

    async def generate_read_sas(*_args, **_kwargs):
        observed_session_states.append(session_active)
        return "https://blob.example/thumb"

    monkeypatch.setattr(post_upload, "acquire_org_session", acquire)
    monkeypatch.setattr(post_upload.thumbnail_config, "is_supported", lambda _: False)
    storage = SimpleNamespace(
        compute_checksum=AsyncMock(),
        generate_read_sas=generate_read_sas,
        close=AsyncMock(),
    )

    outcome = _service(storage).process(str(ASSET_ID))

    assert outcome.status is PostUploadStatus.PROCESSED
    assert observed_session_states == [False]
