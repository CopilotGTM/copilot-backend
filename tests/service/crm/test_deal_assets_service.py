"""Unit tests for DealAssetsService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetLinkStatus,
    DealAssetStatus,
)
from src.service.crm.deal_assets_service import DealAssetsService, DealAssetsServiceError


@pytest.fixture()
def org_id():
    return uuid4()


@pytest.fixture()
def deal_id():
    return uuid4()


@pytest.fixture()
def user_id():
    return uuid4()


@pytest.fixture()
def mock_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture()
def service(mock_session, org_id):
    return DealAssetsService(mock_session, org_id)


# ---------------------------------------------------------------------------
# TestPresignFileUpload
# ---------------------------------------------------------------------------


class TestPresignFileUpload:
    @pytest.mark.asyncio
    async def test_creates_pending_asset_and_returns_sas_url(
        self, service, deal_id, user_id, mock_session
    ):
        fake_url = "https://storage.blob.core.windows.net/container/blob?sig=abc"

        with patch(
            "src.service.crm.deal_assets_service.AzureOrganisationBlobStorage"
        ) as mock_blob_cls:
            mock_blob = AsyncMock()
            mock_blob.generate_write_sas = AsyncMock(return_value=fake_url)
            mock_blob_cls.return_value = mock_blob

            asset, url, expires = await service.presign_file_upload(
                deal_id=deal_id,
                user_id=user_id,
                filename="report.pdf",
                mimetype="application/pdf",
                size_bytes=1024,
                name="Q4 Report",
            )

        assert asset.status == DealAssetStatus.PENDING_UPLOAD.value
        assert asset.filename == "report.pdf"
        assert "deal_assets/" in asset.storage_path
        assert url == fake_url
        assert expires == 15 * 60
        mock_session.add.assert_called_once_with(asset)
        mock_blob.generate_write_sas.assert_awaited_once_with(asset.storage_path, minutes=15)

    @pytest.mark.asyncio
    async def test_defaults_name_to_filename(self, service, deal_id, user_id):
        with patch(
            "src.service.crm.deal_assets_service.AzureOrganisationBlobStorage"
        ) as mock_blob_cls:
            mock_blob = AsyncMock()
            mock_blob.generate_write_sas = AsyncMock(return_value="https://example.com")
            mock_blob_cls.return_value = mock_blob

            asset, _, _ = await service.presign_file_upload(
                deal_id=deal_id,
                user_id=user_id,
                filename="deck.pptx",
                mimetype="application/vnd.ms-powerpoint",
                size_bytes=2048,
                name=None,
            )

        assert asset.name == "deck.pptx"


# ---------------------------------------------------------------------------
# TestFinalizeFileUpload
# ---------------------------------------------------------------------------


class TestFinalizeFileUpload:
    @pytest.mark.asyncio
    async def test_transitions_pending_to_processing(self, service, deal_id, mock_session):
        asset_id = uuid4()
        pending_asset = DealAsset(
            asset_id=asset_id,
            organisation_id=service.organisation_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="file.txt",
            status=DealAssetStatus.PENDING_UPLOAD.value,
            storage_path="deal_assets/test/file.txt",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = pending_asset
        mock_session.execute.return_value = mock_result

        result = await service.finalize_file_upload(
            deal_id=deal_id, asset_id=asset_id, checksum="sha256:abc123"
        )

        assert result.status == DealAssetStatus.PROCESSING.value
        assert result.checksum == "sha256:abc123"

    @pytest.mark.asyncio
    async def test_rejects_already_active(self, service, deal_id, mock_session):
        asset_id = uuid4()
        active_asset = DealAsset(
            asset_id=asset_id,
            organisation_id=service.organisation_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="file.txt",
            status=DealAssetStatus.ACTIVE.value,
            storage_path="deal_assets/test/file.txt",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = active_asset
        mock_session.execute.return_value = mock_result

        with pytest.raises(DealAssetsServiceError) as exc_info:
            await service.finalize_file_upload(deal_id=deal_id, asset_id=asset_id)

        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# TestCreateLink
# ---------------------------------------------------------------------------


class TestCreateLink:
    @pytest.mark.asyncio
    async def test_creates_active_link_asset(self, service, deal_id, user_id, mock_session):
        asset = await service.create_link(
            deal_id=deal_id,
            user_id=user_id,
            name="Google Doc",
            external_url="https://docs.google.com/document/d/abc",
        )

        assert asset.status == DealAssetStatus.ACTIVE.value
        assert asset.external_url == "https://docs.google.com/document/d/abc"
        assert asset.storage_path is None
        assert asset.link_status == DealAssetLinkStatus.UNKNOWN.value
        mock_session.add.assert_called_once_with(asset)


# ---------------------------------------------------------------------------
# TestGetAsset
# ---------------------------------------------------------------------------


class TestGetAsset:
    @pytest.mark.asyncio
    async def test_raises_404_when_not_found(self, service, deal_id, mock_session):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(DealAssetsServiceError) as exc_info:
            await service.get_asset(deal_id, uuid4())

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestDeleteAsset
# ---------------------------------------------------------------------------


class TestDeleteAsset:
    @pytest.mark.asyncio
    async def test_soft_deletes_asset(self, service, deal_id, mock_session):
        asset_id = uuid4()
        existing_asset = DealAsset(
            asset_id=asset_id,
            organisation_id=service.organisation_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="old-file.pdf",
            status=DealAssetStatus.ACTIVE.value,
            storage_path="deal_assets/test/old-file.pdf",
            is_deleted=False,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_asset
        mock_session.execute.return_value = mock_result

        result = await service.delete_asset(deal_id=deal_id, asset_id=asset_id)

        assert result.is_deleted is True
