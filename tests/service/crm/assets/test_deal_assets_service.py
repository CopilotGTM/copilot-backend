"""Unit tests for DealAssetsService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetStatus,
)
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.service.crm.access.record_access_policy import CRMSystemActor
from src.service.crm.assets.deal_assets_service import (
    DealAssetsService,
    DealAssetsServiceError,
)


@pytest.fixture()
def org_id():
    """Build a stable organisation id for service-level assertions."""
    return uuid4()


@pytest.fixture()
def deal_id():
    """Build a stable deal id for service-level assertions."""
    return uuid4()


@pytest.fixture()
def user_id():
    """Build a stable user id for service-level assertions."""
    return uuid4()


@pytest.fixture()
def mock_session():
    """Mocked AsyncSession that records DB calls without real database traffic."""
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
    """Construct a service instance using the mocked org session."""
    return DealAssetsService(mock_session, org_id)


# ---------------------------------------------------------------------------
# TestPresignFileUpload
# ---------------------------------------------------------------------------


class TestPresignFileUpload:
    """Presign path assertions for upload URL and status transitions."""

    @pytest.mark.asyncio
    async def test_creates_pending_asset_and_returns_sas_url(
        self, service, deal_id, user_id, mock_session
    ):
        """Pending status must be stored before issuing upload URL and metadata."""
        fake_url = "https://storage.blob.core.windows.net/container/blob?sig=abc"
        service.storage.generate_write_sas = AsyncMock(return_value=fake_url)

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
        service.storage.generate_write_sas.assert_awaited_once_with(asset.storage_path, minutes=15)

    @pytest.mark.asyncio
    async def test_defaults_name_to_filename(self, service, deal_id, user_id):
        """A blank user-provided name should fall back to the uploaded filename."""
        service.storage.generate_write_sas = AsyncMock(return_value="https://example.com")

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
    """State transition assertions for finalizing uploaded assets."""

    @pytest.mark.asyncio
    async def test_transitions_pending_to_processing(self, service, deal_id, mock_session):
        """Pending uploads can transition to processing with checksum capture."""
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
        """Active assets must not be re-finalized; this should return a 409."""
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

    @pytest.mark.asyncio
    async def test_returns_processing_asset_for_safe_republication(
        self,
        service,
        deal_id,
        mock_session,
    ):
        """A client retry repairs broker publication without mutating the asset again."""
        asset_id = uuid4()
        processing_asset = DealAsset(
            asset_id=asset_id,
            organisation_id=service.organisation_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="file.txt",
            status=DealAssetStatus.PROCESSING.value,
            storage_path="deal_assets/test/file.txt",
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = processing_asset
        mock_session.execute.return_value = mock_result

        result = await service.finalize_file_upload(
            deal_id=deal_id,
            asset_id=asset_id,
        )

        assert result is processing_asset
        mock_session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestListAssets
# ---------------------------------------------------------------------------


class TestListAssets:
    """Deal asset reads expose blob-backed files only."""

    @pytest.mark.asyncio
    async def test_filters_out_rows_without_storage_paths(self, service, deal_id, mock_session):
        """Historical external-link rows stay stored but are absent from API reads."""
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        assets_result = MagicMock()
        assets_result.scalars.return_value.all.return_value = []
        mock_session.execute.side_effect = [count_result, assets_result]

        await service.list_assets(deal_id, limit=50, cursor=None)

        for call in mock_session.execute.await_args_list:
            compiled = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            assert "deal_assets.storage_path IS NOT NULL" in compiled

    @pytest.mark.asyncio
    async def test_returns_a_stable_cursor_page(self, service, deal_id, mock_session):
        """Cursor pages use created_at plus asset_id as a deterministic tie-breaker."""
        created_at = datetime.now(UTC)
        assets = [
            DealAsset(
                asset_id=uuid4(),
                organisation_id=service.organisation_id,
                deal_id=deal_id,
                added_by_user_id=uuid4(),
                name=f"file-{index}.pdf",
                storage_path=f"deal_assets/file-{index}.pdf",
                created_at=created_at,
            )
            for index in range(2)
        ]
        count_result = MagicMock()
        count_result.scalar.return_value = 2
        assets_result = MagicMock()
        assets_result.scalars.return_value.all.return_value = assets
        mock_session.execute.side_effect = [count_result, assets_result]

        items, next_cursor, total = await service.list_assets(
            deal_id,
            limit=1,
            cursor=None,
        )

        assert items == [assets[0]]
        assert total == 2
        assert next_cursor is not None
        assert decode_cursor(next_cursor) == (created_at, assets[0].asset_id)
        statement = mock_session.execute.await_args_list[1].args[0]
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "ORDER BY deal_assets.created_at DESC, deal_assets.asset_id DESC" in compiled
        assert "LIMIT 2" in compiled

    @pytest.mark.asyncio
    async def test_rejects_an_invalid_cursor(self, service, deal_id):
        """Malformed cursors become a client error instead of an internal error."""
        with pytest.raises(DealAssetsServiceError) as exc_info:
            await service.list_assets(deal_id, limit=50, cursor="not-a-cursor")

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cursor",
        [
            "",
            encode_cursor(datetime(2026, 1, 1), uuid4()),
        ],
    )
    async def test_rejects_empty_or_timezone_naive_cursors(self, service, deal_id, cursor):
        """Supplied cursors must contain a non-empty, timezone-aware position."""
        with pytest.raises(DealAssetsServiceError) as exc_info:
            await service.list_assets(deal_id, limit=50, cursor=cursor)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_org_wide_listing_filters_out_rows_without_storage_paths(
        self, service, mock_session
    ):
        """The organisation-wide asset API follows the same file-only contract."""
        service = DealAssetsService(
            mock_session,
            service.organisation_id,
            CRMSystemActor(source="test"),
        )
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        assets_result = MagicMock()
        assets_result.scalars.return_value.all.return_value = []
        mock_session.execute.side_effect = [count_result, assets_result]

        await service.list_all_assets()

        for call in mock_session.execute.await_args_list:
            compiled = str(call.args[0].compile(compile_kwargs={"literal_binds": True}))
            assert "deal_assets.storage_path IS NOT NULL" in compiled


# ---------------------------------------------------------------------------
# TestGetAsset
# ---------------------------------------------------------------------------


class TestGetAsset:
    """Lookup assertions for org + deal scoped asset reads."""

    @pytest.mark.asyncio
    async def test_raises_404_when_not_found(self, service, deal_id, mock_session):
        """Missing assets should map to a 404-style service error."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(DealAssetsServiceError) as exc_info:
            await service.get_asset(deal_id, uuid4())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_lookup_requires_a_storage_path(self, service, deal_id, mock_session):
        """Direct reads cannot expose historical external-link rows."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with pytest.raises(DealAssetsServiceError):
            await service.get_asset(deal_id, uuid4())

        statement = mock_session.execute.await_args.args[0]
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "deal_assets.storage_path IS NOT NULL" in compiled


# ---------------------------------------------------------------------------
# TestDeleteAsset
# ---------------------------------------------------------------------------


class TestDeleteAsset:
    """Delete assertions for soft-delete behavior."""

    @pytest.mark.asyncio
    async def test_soft_deletes_asset(self, service, deal_id, mock_session):
        """Delete should mark asset as deleted instead of hard-removing row."""
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


class TestGenerateDownloadUrl:
    """Download URL assertions for temporary links and content-disposition handling."""

    @pytest.mark.asyncio
    async def test_reuses_one_storage_client_for_all_urls(self, mock_session, org_id, deal_id):
        """A request reuses and closes one storage client for files and thumbnails."""
        asset = DealAsset(
            asset_id=uuid4(),
            organisation_id=org_id,
            deal_id=deal_id,
            added_by_user_id=uuid4(),
            name="unsafe.html",
            filename='unsafe"\r\n.html',
            status=DealAssetStatus.ACTIVE.value,
            storage_path="deal_assets/test/unsafe.html",
            thumbnail_small_path="deal_assets/test/thumb-small.jpg",
            thumbnail_medium_path="deal_assets/test/thumb-medium.jpg",
        )

        with patch(
            "src.service.crm.assets.deal_assets_service.AzureOrganisationBlobStorage"
        ) as mock_blob_cls:
            mock_blob = AsyncMock()
            mock_blob.generate_read_sas = AsyncMock(return_value="https://example.com/download")
            mock_blob_cls.return_value = mock_blob
            service = DealAssetsService(mock_session, org_id)

            url = await service.generate_download_url(asset)
            await service.generate_thumbnail_urls(asset)
            await service.close()

        assert url == "https://example.com/download"
        mock_blob_cls.assert_called_once_with(org_id)
        mock_blob.generate_read_sas.assert_any_await(
            asset.storage_path,
            minutes=30,
            download_filename=asset.filename,
        )
        assert mock_blob.generate_read_sas.await_count == 3
        mock_blob.close.assert_awaited_once()
