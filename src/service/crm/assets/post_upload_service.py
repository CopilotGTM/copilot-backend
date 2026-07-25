"""Finalize deal-asset files after their blobs have been committed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetStatus,
)
from src.infra.config.configs import thumbnail_config
from src.infra.database.sessions import acquire_org_session
from src.infra.media.thumbnail_generator import generate_thumbnails
from src.infra.messaging.event_models import DealAssetEventUpdate
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("deal_asset_post_upload_service")


class PostUploadStatus(str, Enum):
    """Terminal and retryable outcomes understood by the worker task."""

    PROCESSED = "processed"
    ALREADY_PROCESSED = "already_processed"
    RETRYABLE_FAILURE = "retryable_failure"
    INVALID = "invalid"


@dataclass(frozen=True)
class PostUploadOutcome:
    """Committed asset state and the event that may now be published."""

    status: PostUploadStatus
    event: DealAssetEventUpdate | None = None
    error: str | None = None


@dataclass(frozen=True)
class AssetData:
    """Fields needed while no database session is held."""

    asset_id: str
    deal_id: str
    storage_path: str | None
    mimetype: str
    checksum: str | None
    status: str
    thumbnail_small_path: str | None
    updated_at: str | None


@dataclass(frozen=True)
class ProcessingResult:
    """Blob-derived fields ready to commit."""

    checksum: str
    thumbnail_small_path: str | None
    thumbnail_medium_path: str | None


class DealAssetPostUploadService:
    """Compute blob metadata and commit one final asset state.

    Event publication deliberately remains in the task so consumers never see a
    state that the database transaction has not committed.
    """

    def __init__(self, organisation_id: str):
        self.organisation_id = organisation_id
        self.org_uuid = UUID(organisation_id)
        self.storage = AzureOrganisationBlobStorage(organisation_id=self.org_uuid)

    def process(self, asset_id: str) -> PostUploadOutcome:
        """Finalize an asset and describe the worker's next action."""
        try:
            asset_data = self._load_asset_data(asset_id)
            if asset_data is None:
                return PostUploadOutcome(PostUploadStatus.INVALID, error="Asset not found")

            if asset_data.status == DealAssetStatus.ACTIVE.value and asset_data.checksum:
                return PostUploadOutcome(
                    PostUploadStatus.ALREADY_PROCESSED,
                    event=self._event_from_data(asset_data),
                )

            if not asset_data.storage_path:
                message = "Asset has no storage path"
                return PostUploadOutcome(
                    PostUploadStatus.INVALID,
                    event=self._mark_failed(asset_id, message),
                    error=message,
                )

            try:
                result = self._process_asset(asset_data)
                event = self._finalize(asset_id, result)
            except Exception as exc:
                logger.exception(
                    "Deal asset post-upload failed",
                    extra={
                        "event": "deal_asset_post_upload_failed",
                        "organisation_id": self.organisation_id,
                        "asset_id": asset_id,
                    },
                )
                message = str(exc)
                return PostUploadOutcome(
                    PostUploadStatus.RETRYABLE_FAILURE,
                    event=self._mark_failed(asset_id, message),
                    error=message,
                )

            if event is None:
                return PostUploadOutcome(
                    PostUploadStatus.INVALID,
                    error="Asset disappeared before finalization",
                )
            return PostUploadOutcome(PostUploadStatus.PROCESSED, event=event)
        finally:
            self._close_storage()

    def _load_asset_data(self, asset_id: str) -> AssetData | None:
        for session in acquire_org_session(self.org_uuid):
            asset = session.get(DealAsset, asset_id)
            if asset is None:
                return None
            return self._snapshot(asset)
        return None

    def _process_asset(self, data: AssetData) -> ProcessingResult:
        checksum = data.checksum or run_async(self.storage.compute_checksum(data.storage_path))
        if not checksum:
            raise RuntimeError("Checksum computation returned no value")

        small_path = None
        medium_path = None
        if thumbnail_config.is_supported(data.mimetype.lower()):
            try:
                created = run_async(
                    generate_thumbnails(
                        storage=self.storage,
                        original_blob_path=data.storage_path,
                        mimetype=data.mimetype,
                    )
                )
                if created:
                    small_path = created.get("thumb_small")
                    medium_path = created.get("thumb_medium")
            except Exception:
                logger.exception(
                    "Thumbnail generation failed for deal asset %s",
                    data.asset_id,
                )

        return ProcessingResult(
            checksum=checksum,
            thumbnail_small_path=small_path,
            thumbnail_medium_path=medium_path,
        )

    def _finalize(self, asset_id: str, result: ProcessingResult) -> DealAssetEventUpdate | None:
        committed: AssetData | None = None
        for session in acquire_org_session(self.org_uuid):
            asset = session.get(DealAsset, asset_id)
            if asset is None:
                return None

            asset.checksum = result.checksum
            if result.thumbnail_small_path:
                asset.thumbnail_small_path = result.thumbnail_small_path
            if result.thumbnail_medium_path:
                asset.thumbnail_medium_path = result.thumbnail_medium_path
            asset.status = DealAssetStatus.ACTIVE.value
            session.merge(asset)
            session.commit()
            committed = self._snapshot(asset)
            break

        return self._event_from_data(committed) if committed is not None else None

    def _mark_failed(self, asset_id: str, error_message: str) -> DealAssetEventUpdate | None:
        committed: AssetData | None = None
        for session in acquire_org_session(self.org_uuid):
            asset = session.get(DealAsset, asset_id)
            if asset is None:
                return None
            asset.status = DealAssetStatus.FAILED.value
            session.merge(asset)
            session.commit()
            committed = self._snapshot(asset)
            break

        return (
            self._event_from_data(committed, error_message=error_message[:500])
            if committed is not None
            else None
        )

    @staticmethod
    def _snapshot(asset: DealAsset) -> AssetData:
        """Detach event and media fields before releasing the database session."""
        return AssetData(
            asset_id=str(asset.asset_id),
            deal_id=str(asset.deal_id),
            storage_path=asset.storage_path,
            mimetype=asset.mimetype or "",
            checksum=asset.checksum,
            status=asset.status,
            thumbnail_small_path=asset.thumbnail_small_path,
            updated_at=asset.updated_at.isoformat() if asset.updated_at else None,
        )

    def _event_from_data(
        self,
        data: AssetData,
        *,
        error_message: str | None = None,
    ) -> DealAssetEventUpdate:
        return DealAssetEventUpdate(
            asset_id=data.asset_id,
            deal_id=data.deal_id,
            status=data.status,
            updated_at=data.updated_at,
            error_message=error_message,
            thumbnail_small_url=self._generate_thumbnail_url(data.thumbnail_small_path),
        )

    def _generate_thumbnail_url(self, path: str | None) -> str | None:
        if not path:
            return None
        try:
            return run_async(self.storage.generate_read_sas(path, minutes=30))
        except Exception:
            logger.debug("Thumbnail SAS generation failed path=%s", path, exc_info=True)
            return None

    def _close_storage(self) -> None:
        try:
            close_coro = self.storage.close()
            if close_coro is not None:
                run_async(close_coro)
        except Exception:
            logger.debug("Failed to close storage client", exc_info=True)
