"""Post-upload processing service for deal assets.

Handles checksum computation and thumbnail generation for uploaded deal asset files.
Follows the fresh session pattern — sessions acquired only for DB ops and released
before long-running Azure blob operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.domain.models.organisation_tables.deal_asset_models import (
    DealAsset,
    DealAssetStatus,
)
from src.infra.config.configs import thumbnail_config
from src.infra.database.sessions import acquire_org_session
from src.infra.media.thumbnail_generator import generate_thumbnails
from src.infra.messaging.deal_asset_status_publish import publish_deal_asset_status
from src.infra.messaging.event_models import DealAssetEventUpdate
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger

logger = get_logger("deal_asset_post_upload_service")


@dataclass
class AssetData:
    """Data extracted from deal asset record for processing."""

    asset_id: str
    deal_id: str
    storage_path: str
    mimetype: str
    checksum: str | None


@dataclass
class ProcessingResult:
    """Result of asset processing."""

    checksum: str | None
    thumbnail_small_path: str | None
    thumbnail_medium_path: str | None
    failed: bool = False


class DealAssetPostUploadService:
    """Checksum computation and thumbnail generation for deal asset files.

    Mirrors the chat attachment PostUploadService pattern:
    Phase 1 — load asset from DB (session released)
    Phase 2 — Azure blob ops (checksum + thumbnails, no session held)
    Phase 3 — update DB with results + publish SSE event (session released)
    """

    def __init__(self, organisation_id: str):
        self.organisation_id = organisation_id
        self.org_uuid = UUID(organisation_id)
        self.storage = AzureOrganisationBlobStorage(organisation_id=self.org_uuid)

    def process(self, asset_id: str) -> bool:
        """Run post-upload processing for a deal asset.

        Returns True on success, False on failure.
        """
        try:
            asset_data = self._load_asset_data(asset_id)
            if not asset_data:
                return False

            result = self._process_asset(asset_data)
            self._finalize(asset_id, result)
            return not result.failed

        except Exception as e:
            logger.error(
                "Deal asset post-upload failed org=%s asset=%s error=%s",
                self.organisation_id,
                asset_id,
                e,
            )
            self._mark_failed(asset_id, str(e))
            return False

        finally:
            self._close_storage()

    # ── Phase 1: Load from DB ────────────────────────────────────────

    def _load_asset_data(self, asset_id: str) -> AssetData | None:
        for session in acquire_org_session(self.org_uuid):
            asset = session.get(DealAsset, asset_id)
            if not asset:
                logger.warning("Deal asset %s not found for processing", asset_id)
                return None

            if asset.status == DealAssetStatus.ACTIVE.value and asset.checksum:
                logger.info("Deal asset %s already processed", asset_id)
                return None

            if not asset.storage_path:
                logger.warning("Deal asset %s has no storage_path", asset_id)
                return None

            return AssetData(
                asset_id=str(asset.asset_id),
                deal_id=str(asset.deal_id),
                storage_path=asset.storage_path,
                mimetype=asset.mimetype or "",
                checksum=asset.checksum,
            )

        return None

    # ── Phase 2: Azure blob operations (no DB session) ───────────────

    def _process_asset(self, data: AssetData) -> ProcessingResult:
        checksum = data.checksum
        thumbnail_small_path = None
        thumbnail_medium_path = None

        # Compute checksum
        try:
            if data.storage_path and not checksum:
                checksum = run_async(self.storage.compute_checksum(data.storage_path))
        except Exception:
            logger.exception("Checksum computation failed for asset %s", data.asset_id)

        # Generate thumbnails for supported mime types
        try:
            mt = (data.mimetype or "").lower()
            if thumbnail_config.is_supported(mt):
                created = run_async(
                    generate_thumbnails(
                        storage=self.storage,
                        original_blob_path=data.storage_path,
                        mimetype=data.mimetype,
                    )
                )
                if created:
                    thumbnail_small_path = created.get("thumb_small")
                    thumbnail_medium_path = created.get("thumb_medium")
        except Exception:
            # Thumbnails are optional — don't fail the whole upload
            logger.exception("Thumbnail generation failed for asset %s", data.asset_id)

        return ProcessingResult(
            checksum=checksum,
            thumbnail_small_path=thumbnail_small_path,
            thumbnail_medium_path=thumbnail_medium_path,
        )

    # ── Phase 3: Update DB + publish event ───────────────────────────

    def _finalize(self, asset_id: str, result: ProcessingResult) -> None:
        asset_data_for_publish: dict | None = None
        thumb_path: str | None = None

        for session in acquire_org_session(self.org_uuid):
            asset = session.get(DealAsset, asset_id)
            if not asset:
                logger.warning("Deal asset %s not found for finalization", asset_id)
                return

            if result.checksum:
                asset.checksum = result.checksum
            if result.thumbnail_small_path:
                asset.thumbnail_small_path = result.thumbnail_small_path
            if result.thumbnail_medium_path:
                asset.thumbnail_medium_path = result.thumbnail_medium_path

            asset.status = DealAssetStatus.ACTIVE.value
            session.merge(asset)
            session.commit()

            thumb_path = asset.thumbnail_small_path

            asset_data_for_publish = {
                "asset_id": str(asset.asset_id),
                "deal_id": str(asset.deal_id),
                "status": DealAssetStatus.ACTIVE.value,
                "updated_at": (asset.updated_at.isoformat() if asset.updated_at else None),
            }

        # Generate thumbnail SAS URL outside session
        if asset_data_for_publish:
            thumbnail_url = self._generate_thumbnail_url(thumb_path)
            event = DealAssetEventUpdate(
                asset_id=asset_data_for_publish["asset_id"],
                deal_id=asset_data_for_publish["deal_id"],
                status=asset_data_for_publish["status"],
                updated_at=asset_data_for_publish["updated_at"],
                thumbnail_small_url=thumbnail_url,
            )
            publish_deal_asset_status(self.organisation_id, event)

    def _mark_failed(self, asset_id: str, error_message: str) -> None:
        try:
            for session in acquire_org_session(self.org_uuid):
                asset = session.get(DealAsset, asset_id)
                if not asset:
                    return

                asset.status = DealAssetStatus.FAILED.value
                session.merge(asset)
                session.commit()

                event = DealAssetEventUpdate(
                    asset_id=str(asset.asset_id),
                    deal_id=str(asset.deal_id),
                    status=DealAssetStatus.FAILED.value,
                    updated_at=(asset.updated_at.isoformat() if asset.updated_at else None),
                    error_message=error_message[:500],
                )
                publish_deal_asset_status(self.organisation_id, event)
        except Exception:
            logger.debug(
                "Failed to persist FAILED state for asset %s",
                asset_id,
                exc_info=True,
            )

    # ── Helpers ──────────────────────────────────────────────────────

    def _generate_thumbnail_url(self, path: str | None) -> str | None:
        if not path:
            return None
        try:
            return run_async(self.storage.generate_read_sas(path, minutes=120))
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
