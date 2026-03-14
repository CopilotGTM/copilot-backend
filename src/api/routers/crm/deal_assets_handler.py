"""Deal Assets API handler - endpoints for managing deal file and link assets."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.deal_asset_models import (
    DealAssetFinalizeRequest,
    DealAssetItem,
    DealAssetLinkCreateRequest,
    DealAssetPresignRequest,
    DealAssetPresignResponse,
    DealAssetsPage,
    DealAssetUpdateRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.deal_asset_models import DealAsset
from src.infra.utils.logger import get_logger
from src.service.crm.deal_assets_service import DealAssetsService, DealAssetsServiceError
from src.service.crm.deal_assets_sse_service import DealAssetSSEService
from src.workers.deal_worker.client import send_deal_task_async
from sse_starlette.sse import EventSourceResponse

logger = get_logger("deal_assets_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deal_assets_router = APIRouter()


async def get_deal_assets_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealAssetsService instance for the current org.

    Ensures proper cleanup of database connections via try/finally pattern.
    """
    service = DealAssetsService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealAssetsService close failed", exc_info=True)


async def _map_asset_to_item(asset: DealAsset, service: DealAssetsService) -> DealAssetItem:
    """Map a DealAsset ORM object to a DealAssetItem response schema.

    Generates time-limited SAS URLs for download and thumbnails.
    """
    download_url = await service.generate_download_url(asset)
    thumb_small, thumb_medium = await service.generate_thumbnail_urls(asset)

    return DealAssetItem(
        asset_id=asset.asset_id,
        deal_id=asset.deal_id,
        name=asset.name,
        category=asset.category,
        direction=asset.direction,
        status=asset.status,
        filename=asset.filename,
        mimetype=asset.mimetype,
        size_bytes=asset.size_bytes,
        download_url=download_url,
        thumbnail_small_url=thumb_small,
        thumbnail_medium_url=thumb_medium,
        external_url=asset.external_url,
        external_source=asset.external_source,
        link_status=asset.link_status,
        link_checked_at=asset.link_checked_at,
        kb_source_id=asset.kb_source_id,
        source_type=asset.source_type,
        source_context=asset.source_context,
        deal_stage_when_added=asset.deal_stage_when_added,
        added_by_user_id=asset.added_by_user_id,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


@deal_assets_router.get(
    "/assets",
    response_model=GenericResponse[DealAssetsPage],
    summary="List all assets",
    description="Retrieve all assets across all deals for the organisation",
    dependencies=[Depends(rate_limit_user_read)],
)
async def list_all_assets(
    response: FastAPIResponse,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    service: DealAssetsService = Depends(get_deal_assets_service),
) -> GenericResponse[DealAssetsPage]:
    """List all non-deleted assets across all deals, with optional search."""
    try:
        assets, total_count = await service.list_all_assets(
            limit=limit,
            offset=offset,
            q=q,
        )
        items = [await _map_asset_to_item(asset, service) for asset in assets]

        return Response.success_response(
            message="Assets retrieved successfully",
            data=DealAssetsPage(items=items, total_count=total_count),
        )
    except DealAssetsServiceError as exc:
        logger.warning("All assets access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing all assets: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.get(
    "/{deal_id}/assets",
    response_model=GenericResponse[DealAssetsPage],
    summary="List deal assets",
    description="Retrieve all assets (files and links) for a specific deal",
    dependencies=[Depends(rate_limit_user_read)],
)
async def list_deal_assets(
    deal_id: UUID,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
) -> GenericResponse[DealAssetsPage]:
    """List all non-deleted assets for a deal, ordered by newest first."""
    try:
        assets, total_count = await service.list_assets(deal_id=deal_id)
        items = [await _map_asset_to_item(asset, service) for asset in assets]

        return Response.success_response(
            message="Assets retrieved successfully",
            data=DealAssetsPage(items=items, total_count=total_count),
        )
    except DealAssetsServiceError as exc:
        logger.warning("Deal assets access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing assets for deal %s: %s", deal_id, exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.get(
    "/{deal_id}/assets/stream",
    summary="Stream deal asset status updates",
    description="SSE stream for real-time deal asset processing status updates",
    dependencies=[Depends(rate_limit_user_read)],
)
async def stream_deal_asset_updates(
    deal_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
):
    """SSE endpoint for frontend to receive real-time asset processing updates."""
    service = DealAssetSSEService(organisation_id=user_ids.organisation_id)

    async def event_generator():
        try:
            async for event in service.stream_events(deal_id=deal_id):
                yield event
        finally:
            try:
                await service.close()
            except Exception:
                logger.debug("SSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())


@deal_assets_router.get(
    "/{deal_id}/assets/{asset_id}",
    response_model=GenericResponse[DealAssetItem],
    summary="Get deal asset",
    description="Retrieve a single asset by ID",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_deal_asset(
    deal_id: UUID,
    asset_id: UUID,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
) -> GenericResponse[DealAssetItem]:
    """Get a single asset with download and thumbnail URLs."""
    try:
        asset = await service.get_asset(deal_id=deal_id, asset_id=asset_id)
        item = await _map_asset_to_item(asset, service)

        return Response.success_response(message="Asset retrieved successfully", data=item)
    except DealAssetsServiceError as exc:
        logger.warning("Deal asset access error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error(
            "Unexpected error getting asset %s for deal %s: %s",
            asset_id,
            deal_id,
            exc,
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.post(
    "/{deal_id}/assets/upload/presign",
    response_model=GenericResponse[DealAssetPresignResponse],
    status_code=201,
    summary="Presign file upload",
    description="Generate a presigned URL for direct file upload to blob storage",
)
async def presign_file_upload(
    deal_id: UUID,
    request: DealAssetPresignRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetPresignResponse]:
    """Create a pending asset record and return a write SAS URL for upload."""
    try:
        asset, upload_url, expires_in = await service.presign_file_upload(
            deal_id=deal_id,
            user_id=user_ids.user_id,
            filename=request.filename,
            mimetype=request.mimetype,
            size_bytes=request.size_bytes,
            name=request.name,
            category=request.category,
            direction=request.direction,
        )
        await service.session.commit()

        return Response.created_response(
            message="Upload presigned successfully",
            data=DealAssetPresignResponse(
                asset_id=asset.asset_id,
                filename=asset.filename or request.filename,
                upload_url=upload_url,
                expires_in_seconds=expires_in,
                status=asset.status,
            ),
            response=response,
        )
    except DealAssetsServiceError as exc:
        logger.warning("Failed to presign upload: %s", exc.message, exc_info=True)
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error(
            "Unexpected error presigning upload for deal %s: %s",
            deal_id,
            exc,
            exc_info=True,
        )
        await service.session.rollback()
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.post(
    "/{deal_id}/assets/upload/finalize",
    response_model=GenericResponse[DealAssetItem],
    summary="Finalize file upload",
    description="Confirm that a presigned file upload has completed successfully",
)
async def finalize_file_upload(
    deal_id: UUID,
    request: DealAssetFinalizeRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    """Mark asset as processing and queue post-upload work (checksum + thumbnails)."""
    try:
        asset = await service.finalize_file_upload(
            deal_id=deal_id,
            asset_id=request.asset_id,
            checksum=request.checksum,
        )
        await service.session.commit()

        # Dispatch post-upload processing (checksum + thumbnails) to deal worker
        try:
            await send_deal_task_async(
                "deal_asset.post_upload",
                args=[str(user_ids.organisation_id), str(request.asset_id)],
                queue="deal",
                routing_key="deal",
            )
        except Exception:
            logger.warning(
                "Failed to queue post-upload task for asset %s — "
                "asset saved as processing, worker will retry",
                request.asset_id,
                exc_info=True,
            )

        item = await _map_asset_to_item(asset, service)
        return Response.success_response(message="Upload finalized successfully", data=item)
    except DealAssetsServiceError as exc:
        logger.warning("Failed to finalize upload: %s", exc.message, exc_info=True)
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error(
            "Unexpected error finalizing upload for deal %s: %s",
            deal_id,
            exc,
            exc_info=True,
        )
        await service.session.rollback()
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.post(
    "/{deal_id}/assets/links",
    response_model=GenericResponse[DealAssetItem],
    status_code=201,
    summary="Create external link",
    description="Add an external link as a deal asset",
)
async def create_deal_asset_link(
    deal_id: UUID,
    request: DealAssetLinkCreateRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    """Create an external link asset for a deal."""
    try:
        asset = await service.create_link(
            deal_id=deal_id,
            user_id=user_ids.user_id,
            name=request.name,
            external_url=request.external_url,
            external_source=request.external_source,
            category=request.category,
            direction=request.direction,
        )
        await service.session.commit()

        item = await _map_asset_to_item(asset, service)
        return Response.created_response(
            message="Link asset created successfully",
            data=item,
            response=response,
        )
    except DealAssetsServiceError as exc:
        logger.warning("Failed to create link asset: %s", exc.message, exc_info=True)
        await service.session.rollback()
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error(
            "Unexpected error creating link asset for deal %s: %s",
            deal_id,
            exc,
            exc_info=True,
        )
        await service.session.rollback()
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.patch(
    "/{deal_id}/assets/{asset_id}",
    response_model=GenericResponse[DealAssetItem],
    summary="Update deal asset",
    description="Partially update a deal asset's metadata",
)
async def update_deal_asset(
    deal_id: UUID,
    asset_id: UUID,
    request: DealAssetUpdateRequest,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    """Apply partial updates to an asset."""
    try:
        asset = await service.update_asset(
            deal_id=deal_id,
            asset_id=asset_id,
            updates=request.model_dump(exclude_unset=True),
        )
        await service.session.commit()

        item = await _map_asset_to_item(asset, service)
        return Response.success_response(message="Asset updated successfully", data=item)
    except DealAssetsServiceError as exc:
        await service.session.rollback()
        logger.warning("Failed to update asset: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error(
            "Unexpected error updating asset %s for deal %s: %s",
            asset_id,
            deal_id,
            exc,
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_assets_router.delete(
    "/{deal_id}/assets/{asset_id}",
    response_model=GenericResponse[DealAssetItem],
    summary="Delete deal asset",
    description="Soft delete a deal asset",
)
async def delete_deal_asset(
    deal_id: UUID,
    asset_id: UUID,
    response: FastAPIResponse,
    service: DealAssetsService = Depends(get_deal_assets_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealAssetItem]:
    """Soft delete an asset by setting is_deleted=True."""
    try:
        asset = await service.delete_asset(deal_id=deal_id, asset_id=asset_id)
        await service.session.commit()

        item = await _map_asset_to_item(asset, service)
        return Response.success_response(message="Asset deleted successfully", data=item)
    except DealAssetsServiceError as exc:
        await service.session.rollback()
        logger.warning("Failed to delete asset: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error(
            "Unexpected error deleting asset %s for deal %s: %s",
            asset_id,
            deal_id,
            exc,
            exc_info=True,
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
