"""Service for managing deal assets (files and links attached to deals)."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.deal_asset_models import DealAsset, DealAssetStatus
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger

logger = get_logger("deal_assets_service")

_WRITE_SAS_MINUTES = 15
_READ_SAS_MINUTES = 120


class DealAssetsServiceError(Exception):
    """Custom exception for deal assets service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealAssetsService:
    """Service for managing deal assets (files and links) within a deal.

    Called by API controllers to list, create, update, and delete assets.
    Each instance is scoped to a single organisation.
    """

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Dispose the underlying session when the service is released."""
        try:
            await self.org_session.close()
        except Exception:
            logger.debug("DealAssetsService close exception", exc_info=True)

    @property
    def session(self) -> AsyncSession:
        """Expose the underlying org session for legacy callers that expect it."""
        return self.org_session

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def list_assets(self, deal_id: UUID) -> tuple[list[DealAsset], int]:
        """Return all non-deleted, non-failed assets for a deal.

        Assets per deal are bounded, so no pagination is needed.
        """
        c = cast(Any, DealAsset).__table__.c

        base_where = and_(
            c.organisation_id == self.organisation_id,
            c.deal_id == deal_id,
            c.is_deleted.is_(False),
            c.status != DealAssetStatus.FAILED.value,
        )

        count_stmt = select(func.count()).select_from(DealAsset).where(base_where)
        count_result = await self.org_session.execute(count_stmt)
        total = count_result.scalar() or 0

        stmt = select(DealAsset).where(base_where).order_by(c.created_at.desc())
        result = await self.org_session.execute(stmt)
        assets: list[DealAsset] = list(result.scalars().all())

        return assets, total

    async def list_all_assets(
        self,
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
    ) -> tuple[list[DealAsset], int]:
        """Return all non-deleted, non-failed assets across all deals for the org."""
        c = cast(Any, DealAsset).__table__.c

        base_where = and_(
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
            c.status != DealAssetStatus.FAILED.value,
        )

        if q:
            base_where = and_(
                base_where,
                (c.name.ilike(f"%{q}%") | c.filename.ilike(f"%{q}%")),
            )

        count_stmt = select(func.count()).select_from(DealAsset).where(base_where)
        count_result = await self.org_session.execute(count_stmt)
        total = count_result.scalar() or 0

        stmt = (
            select(DealAsset)
            .where(base_where)
            .order_by(c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.org_session.execute(stmt)
        assets: list[DealAsset] = list(result.scalars().all())

        return assets, total

    async def get_asset(self, deal_id: UUID, asset_id: UUID) -> DealAsset:
        """Fetch a single asset after org + deal ownership filtering.

        The external asset id is scoped to both identifiers so one org cannot read
        another org's records even if IDs are guessed.
        """
        c = cast(Any, DealAsset).__table__.c

        stmt = select(DealAsset).where(
            c.asset_id == asset_id,
            c.deal_id == deal_id,
            c.organisation_id == self.organisation_id,
            c.is_deleted.is_(False),
        )
        result = await self.org_session.execute(stmt)
        asset = result.scalar_one_or_none()

        if not asset:
            raise DealAssetsServiceError(f"Asset {asset_id} not found", status_code=404)
        return asset

    async def presign_file_upload(
        self,
        deal_id: UUID,
        user_id: UUID,
        filename: str,
        mimetype: str,
        size_bytes: int,
        name: str | None = None,
        category: str | None = None,
        direction: str = "internal",
    ) -> tuple[DealAsset, str, int]:
        """Create a pending asset and return a write SAS URL for direct upload.

        Returns:
            (asset, upload_url, expires_in_seconds)
        """
        # Persist the pending DealAsset before generating upload credentials so a
        # partially completed upload still has an auditable record.
        asset_id = uuid4()
        storage_path = f"deal_assets/{user_id}/{asset_id}/{filename}"

        asset = DealAsset(
            asset_id=asset_id,
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            added_by_user_id=user_id,
            name=name or filename,
            category=category,
            direction=direction,
            status=DealAssetStatus.PENDING_UPLOAD.value,
            storage_path=storage_path,
            filename=filename,
            mimetype=mimetype,
            size_bytes=size_bytes,
        )

        self.org_session.add(asset)
        await self.org_session.flush()
        await self.org_session.refresh(asset)

        blob_storage = AzureOrganisationBlobStorage(self.organisation_id)
        upload_url = await blob_storage.generate_write_sas(storage_path, minutes=_WRITE_SAS_MINUTES)

        expires_in = _WRITE_SAS_MINUTES * 60

        logger.info(
            "Presigned upload for asset %s on deal %s by user %s",
            asset_id,
            deal_id,
            user_id,
        )
        return asset, upload_url, expires_in

    async def finalize_file_upload(
        self,
        deal_id: UUID,
        asset_id: UUID,
        checksum: str | None = None,
    ) -> DealAsset:
        """Mark a pending_upload asset as processing and queue post-upload work.

        The Celery post-upload task (checksum + thumbnails) transitions to active.
        """
        # This transition is a strict state machine gate: only PENDING can move
        # to PROCESSING, so callers cannot accidentally process stale rows twice.
        asset = await self.get_asset(deal_id, asset_id)

        if asset.status != DealAssetStatus.PENDING_UPLOAD.value:
            raise DealAssetsServiceError(
                f"Asset {asset_id} is not pending upload (status={asset.status})",
                status_code=409,
            )

        asset.status = DealAssetStatus.PROCESSING.value
        if checksum is not None:
            asset.checksum = checksum

        await self.org_session.flush()
        await self.org_session.refresh(asset)

        logger.info("Finalized upload for asset %s on deal %s", asset_id, deal_id)
        return asset

    async def create_link(
        self,
        deal_id: UUID,
        user_id: UUID,
        name: str,
        external_url: str,
        external_source: str | None = None,
        category: str | None = None,
        direction: str = "internal",
    ) -> DealAsset:
        """Create an active link asset without upload orchestration."""
        from src.domain.models.organisation_tables.deal_asset_models import DealAssetLinkStatus

        asset = DealAsset(
            asset_id=uuid4(),
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            added_by_user_id=user_id,
            name=name,
            category=category,
            direction=direction,
            status=DealAssetStatus.ACTIVE.value,
            external_url=external_url,
            external_source=external_source,
            link_status=DealAssetLinkStatus.UNKNOWN.value,
        )

        self.org_session.add(asset)
        await self.org_session.flush()
        await self.org_session.refresh(asset)

        logger.info(
            "Created link asset %s on deal %s by user %s",
            asset.asset_id,
            deal_id,
            user_id,
        )
        return asset

    async def update_asset(
        self, deal_id: UUID, asset_id: UUID, updates: dict[str, Any]
    ) -> DealAsset:
        """Apply partial updates on a validated, org-scoped asset."""
        asset = await self.get_asset(deal_id, asset_id)

        for key, value in updates.items():
            setattr(asset, key, value)

        await self.org_session.flush()
        await self.org_session.refresh(asset)

        return asset

    async def delete_asset(self, deal_id: UUID, asset_id: UUID) -> DealAsset:
        """Soft-delete an asset."""
        asset = await self.get_asset(deal_id, asset_id)

        asset.is_deleted = True

        await self.org_session.flush()
        await self.org_session.refresh(asset)

        logger.info("Soft-deleted asset %s on deal %s", asset_id, deal_id)
        return asset

    async def generate_download_url(self, asset: DealAsset) -> str | None:
        """Generate a time-limited read URL for the asset file.

        File links are only produced for blob-backed assets; link-type assets skip
        this path and return `None`.
        """
        if not asset.storage_path:
            return None

        blob_storage = AzureOrganisationBlobStorage(self.organisation_id)
        return await blob_storage.generate_read_sas(
            asset.storage_path,
            minutes=_READ_SAS_MINUTES,
            download_filename=asset.filename or asset.name,
        )

    async def generate_thumbnail_urls(self, asset: DealAsset) -> tuple[str | None, str | None]:
        """Generate read SAS URLs for the asset's small and medium thumbnails."""
        blob_storage = AzureOrganisationBlobStorage(self.organisation_id)

        small_url: str | None = None
        medium_url: str | None = None

        if asset.thumbnail_small_path:
            small_url = await blob_storage.generate_read_sas(
                asset.thumbnail_small_path, minutes=_READ_SAS_MINUTES
            )
        if asset.thumbnail_medium_path:
            medium_url = await blob_storage.generate_read_sas(
                asset.thumbnail_medium_path, minutes=_READ_SAS_MINUTES
            )

        return small_url, medium_url
