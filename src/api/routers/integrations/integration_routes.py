"""
Integration management routes for CRUD operations.

Handles:
- Listing integrations
- Getting integration details
- Updating integration settings
- Checking integration status
"""

from uuid import UUID

from fastapi import APIRouter, Depends
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
    IntegrationStatusResponse,
    IntegrationUpdate,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync import IntegrationScope
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
    service = IntegrationService(
        db_session=session, organisation_id=user_ids.organisation_id, nango_client=nango
    )
    try:
        yield service
    finally:
        await nango.close()


@router.get(
    "/available",
    response_model=GenericResponse[AvailableIntegrationsResponse],
    summary="List available integrations with connection status",
    description="Get all available integration types and show which ones the user has already connected.",
)
async def list_available_integrations(
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[AvailableIntegrationsResponse]:
    """
    Get list of all available integration types with their connection status.

    This endpoint combines:
    1. All available integration types from metadata
    2. User's connected integrations for each type

    Use this to show users what integrations they can connect and which ones are already connected.
    """
    try:
        # Get all user's connected integrations (org-wide + user's personal)
        connected_integrations = await service.list_integrations(
            user_id=user_ids.user_id, include_user_scoped=True
        )

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
            message=IntegrationErrors.AVAILABLE_INTEGRATIONS_FAILED
        )


@router.get(
    "/{integration_id}",
    response_model=GenericResponse[IntegrationResponse],
    summary="Get integration details",
    description="Get detailed information about a specific integration.",
)
async def get_integration(
    integration_id: UUID,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[IntegrationResponse]:
    """
    Get details for a specific integration.
    """
    try:
        integration = await service.get_integration(
            integration_id=integration_id,
        )

        if not integration:
            logger.warning(
                f"Integration {integration_id} not found for org {user_ids.organisation_id}"
            )
            return Response.not_found_response(message=IntegrationErrors.INTEGRATION_NOT_FOUND)

        # Check access: user can only access org-wide integrations or their own user-scoped ones
        if (
            integration.integration_scope == IntegrationScope.USER
            and integration.created_by_user_id != user_ids.user_id
        ):
            logger.warning(
                f"User {user_ids.user_id} attempted to access user-scoped integration "
                f"{integration_id} owned by {integration.created_by_user_id}"
            )
            return Response.forbidden_error_response(
                message="You don't have permission to access this integration"
            )

        response_data = IntegrationResponse.model_validate(integration)

        return Response.success_response(
            message="Integration details retrieved successfully", data=response_data
        )

    except Exception as e:
        logger.error(
            f"Failed to get integration {integration_id} for org {user_ids.organisation_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.INTEGRATION_DETAILS_FAILED
        )


@router.get(
    "/{integration_id}/status",
    response_model=GenericResponse[IntegrationStatusResponse],
    summary="Check integration status",
    description="Get lightweight status check for an integration (good for polling).",
)
async def check_integration_status(
    integration_id: UUID,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[IntegrationStatusResponse]:
    """
    Lightweight endpoint to check integration health and status.
    """
    try:
        integration = await service.get_integration(
            integration_id=integration_id,
        )

        if not integration:
            logger.warning(
                f"Integration {integration_id} not found for org {user_ids.organisation_id}"
            )
            return Response.not_found_response(message=IntegrationErrors.INTEGRATION_NOT_FOUND)

        # Check access: user can only access org-wide or their own user-scoped integrations
        if (
            integration.integration_scope == IntegrationScope.USER
            and integration.created_by_user_id != user_ids.user_id
        ):
            logger.warning(
                f"User {user_ids.user_id} attempted to check status of user-scoped integration "
                f"{integration_id} created by {integration.created_by_user_id}"
            )
            return Response.forbidden_error_response(
                message="You don't have permission to access this integration"
            )

        # Check if token needs reauthorization
        needs_reauth = False
        if integration.access_token_expires_at:
            needs_reauth = service.nango.is_token_expiring_soon(
                integration.access_token_expires_at,
                buffer_minutes=0,  # Already expired
            )

        response_data = IntegrationStatusResponse(
            integration_id=integration.integration_id,
            name=integration.name,
            integration_type=integration.integration_type,
            is_active=integration.is_active,
            last_synced_at=integration.last_synced_at,
            last_sync_status=integration.last_sync_status,
            needs_reauthorization=needs_reauth,
        )

        return Response.success_response(
            message="Integration status retrieved successfully", data=response_data
        )

    except Exception as e:
        logger.error(
            f"Failed to check integration status {integration_id} for org {user_ids.organisation_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.INTEGRATION_STATUS_CHECK_FAILED
        )


@router.patch(
    "/{integration_id}",
    response_model=GenericResponse[IntegrationResponse],
    summary="Update integration settings",
    description="Update integration configuration like name, sync frequency, or auto-sync setting.",
)
async def update_integration(
    integration_id: UUID,
    update_data: IntegrationUpdate,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[IntegrationResponse]:
    """
    Update integration settings.
    """
    try:
        # Verify integration exists and belongs to org
        existing = await service.get_integration(
            integration_id=integration_id,
        )

        if not existing:
            logger.warning(
                f"Integration {integration_id} not found for org {user_ids.organisation_id}"
            )
            return Response.not_found_response(message=IntegrationErrors.INTEGRATION_NOT_FOUND)

        # Check access: user can only update org-wide or their own user-scoped integrations
        if (
            existing.integration_scope == IntegrationScope.USER
            and existing.created_by_user_id != user_ids.user_id
        ):
            logger.warning(
                f"User {user_ids.user_id} attempted to update user-scoped integration "
                f"{integration_id} created by {existing.created_by_user_id}"
            )
            return Response.forbidden_error_response(
                message="You don't have permission to update this integration"
            )

        # Update integration
        updated = await service.update_integration(
            integration_id=integration_id,
            name=update_data.name,
            sync_frequency=update_data.sync_frequency,
            auto_sync_enabled=update_data.auto_sync_enabled,
        )

        logger.info(f"Updated integration {integration_id} for org {user_ids.organisation_id}")

        response_data = IntegrationResponse.model_validate(updated)

        return Response.success_response(
            message="Integration updated successfully", data=response_data
        )

    except ValueError as e:
        logger.warning(f"Validation error updating integration {integration_id}: {str(e)}")
        return Response.bad_request_response(message=str(e))
    except Exception as e:
        logger.error(
            f"Failed to update integration {integration_id} for org {user_ids.organisation_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.INTEGRATION_UPDATE_FAILED
        )
