"""
Integration management routes for CRUD operations.

Handles:
- Listing integrations
- Getting integration details
- Updating integration settings
- Checking integration status
- Triggering manual syncs (refresh button)
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations import (
    AvailableIntegration,
    AvailableIntegrationsResponse,
    IntegrationResponse,
    RefreshChannelsResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.constant import IntegrationErrors
from src.infra.config.integrations_metadata import get_all_integration_metadata
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService, NangoClient
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)
router = APIRouter(tags=["integrations"])


async def get_integration_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency to create IntegrationService instance."""
    nango = NangoClient()
    service = IntegrationService(db_session=session, user_ids=user_ids, nango_client=nango)
    try:
        yield service
    finally:
        await nango.close()
        if hasattr(service, "close") and callable(service.close):
            await service.close()


@router.get(
    "/available",
    response_model=GenericResponse[AvailableIntegrationsResponse],
    summary="List available integrations with connection status",
    description="Get all available integration types and show which ones the user has already connected.",
)
async def list_available_integrations(
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[AvailableIntegrationsResponse]:
    """
    List all available integration types with their connection status.

    Returns metadata for all supported integrations (HubSpot, Slack, Google Drive, etc.)
    along with all active connected instances for the organization.
    """
    try:
        # Get all active integrations for the organization
        connected_integrations = await service.list_all_active_integrations()

        # Group connected integrations by type
        connected_by_type = {}
        for integration in connected_integrations:
            int_type = integration.integration_type
            if int_type not in connected_by_type:
                connected_by_type[int_type] = []
            connected_by_type[int_type].append(integration)

        # Build response with all available integrations
        integrations = []
        total_connected = len(connected_integrations)

        for int_type, metadata in get_all_integration_metadata().items():
            if int_type == IntegrationType.INTERNAL_CRM:
                continue

            # Get connected instances for this type
            connected_instances = connected_by_type.get(int_type, [])
            is_connected = len(connected_instances) > 0

            integrations.append(
                AvailableIntegration(
                    integration_type=int_type,
                    name=metadata.name,
                    description=metadata.description,
                    logo_url=metadata.logo_url,
                    category=metadata.category,
                    category_name=metadata.category_name,
                    availability_status=metadata.availability_status,
                    is_connected=is_connected,
                    connected_integrations=[
                        IntegrationResponse.model_validate(conn) for conn in connected_instances
                    ],
                )
            )

        response_data = AvailableIntegrationsResponse(
            integrations=integrations,
            total_available=len(integrations),
            total_connected=total_connected,
        )

        logger.info(
            f"Listed {len(integrations)} available integrations "
            f"({total_connected} connected) for org {user_ids.organisation_id}"
        )

        return Response.success_response(
            message="Available integrations retrieved successfully", data=response_data
        )

    except Exception as e:
        logger.error(
            f"Failed to list available integrations for org {user_ids.organisation_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.INTEGRATION_LIST_FAILED, response=response
        )


@router.post(
    "/{integration_id}/refresh-channels",
    response_model=GenericResponse[RefreshChannelsResponse],
    summary="Refresh Teams channels for an integration",
    description="Trigger a manual refresh of Teams channels and chats for an integration.",
)
async def refresh_integration_channels(
    integration_id: UUID,
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[RefreshChannelsResponse]:
    """
    Trigger a manual refresh of Teams channels and chats.

    This endpoint validates the integration and dispatches a Celery task
    to refresh the channels. The task runs asynchronously and the task_id
    can be used to track progress.
    """
    try:
        # Get the integration
        integration = await service.get_integration(integration_id)

        if not integration:
            return Response.not_found_error_response(
                message="Integration not found",
                response=response,
            )

        # Validate it's a Teams integration
        if integration.integration_type != IntegrationType.TEAMS:
            return Response.bad_request_error_response(
                message="Channel refresh is only supported for Teams integrations",
                response=response,
            )

        # Validate it's active
        if not integration.is_active:
            return Response.bad_request_error_response(
                message="Integration is not active",
                response=response,
            )

        # Validate it's connected
        if not integration.connection_id:
            return Response.bad_request_error_response(
                message="Integration is not connected",
                response=response,
            )

        # Dispatch the Celery task
        task_id = await send_integrations_task_async(
            task_name="integrations.sync_messaging_channels_integration",
            args=[str(user_ids.organisation_id), str(integration_id)],
            queue="integrations_priority",
            routing_key="integrations_priority",
        )

        response_data = RefreshChannelsResponse(
            task_id=task_id,
            integration_id=integration_id,
            message="Channel refresh started successfully",
        )

        logger.info(
            "Channel refresh triggered: org=%s integration=%s task_id=%s",
            user_ids.organisation_id,
            integration_id,
            task_id,
        )

        return Response.success_response(
            message="Channel refresh started",
            data=response_data,
        )

    except Exception as e:
        logger.error(
            "Failed to trigger channel refresh: org=%s integration=%s error=%s",
            user_ids.organisation_id,
            integration_id,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to trigger channel refresh",
            response=response,
        )
