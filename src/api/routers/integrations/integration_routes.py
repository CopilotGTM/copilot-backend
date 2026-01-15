"""
Integration management routes for CRUD operations.

Handles:
- Listing integrations
- Getting integration details
- Updating integration settings
- Checking integration status
- Triggering manual syncs (refresh button)
"""

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
)
from src.api.utils.http_response import Response
from src.infra.config.constant import IntegrationErrors
from src.infra.config.integrations_metadata import get_all_integration_metadata
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService, NangoClient

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
