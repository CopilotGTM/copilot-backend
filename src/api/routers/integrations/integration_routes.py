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
    IntegrationAdminPrerequisite,
    RefreshChannelsResponse,
)
from src.api.schemas.integrations.response_builders import build_integration_response
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import salesforce_integration_config
from src.infra.config.constant import IntegrationErrors
from src.infra.config.integrations_metadata import AvailabilityStatus, get_all_integration_metadata
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService
from src.workers.integrations_worker.client import send_integrations_task_async

logger = get_logger(__name__)
router = APIRouter(tags=["integrations"])


def _get_admin_prerequisite(
    integration_type: IntegrationType,
) -> IntegrationAdminPrerequisite | None:
    if integration_type != IntegrationType.SALESFORCE:
        return None

    install_url = salesforce_integration_config.EXTERNAL_CLIENT_APP_INSTALL_URL.strip()
    if not install_url:
        return None

    return IntegrationAdminPrerequisite(
        type="install_url",
        required_before_connect=True,
        url=install_url,
        label="Install CopilotGTM in Salesforce",
    )


async def get_integration_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency to create IntegrationService instance."""
    yield IntegrationService(db_session=session, user_ids=user_ids)


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
        # Pass user_id so the query returns ORG-scoped integrations (visible to all)
        # plus only this user's USER-scoped integrations (personal connections)
        visible_integrations = await service.list_available_integrations_for_user(
            user_id=user_ids.user_id,
        )

        connected_by_type: dict[IntegrationType, list] = {}
        action_required_by_type: dict[IntegrationType, list] = {}
        total_connected = 0

        for integration in visible_integrations:
            projected = build_integration_response(integration)
            int_type = projected.integration_type
            if integration.is_active:
                total_connected += 1
                if int_type not in connected_by_type:
                    connected_by_type[int_type] = []
                connected_by_type[int_type].append(projected)
            elif projected.action_required != "none":
                if int_type not in action_required_by_type:
                    action_required_by_type[int_type] = []
                action_required_by_type[int_type].append(projected)

        # Build response with all available integrations
        integrations = []

        # TODO(nikhil, 2026-Q2): Include a `can_manage` flag per integration so the
        # frontend can show read-only cards for org integrations when the user is not
        # an admin, and editable cards for the user's own integrations
        for int_type, metadata in get_all_integration_metadata().items():
            # Skip internal and deprecated integrations from the available list
            if int_type == IntegrationType.INTERNAL_CRM:
                continue
            if metadata.availability_status == AvailabilityStatus.DEPRECATED:
                continue

            # Get connected instances for this type
            connected_instances = connected_by_type.get(int_type, [])
            action_required_instances = action_required_by_type.get(int_type, [])
            is_connected = len(connected_instances) > 0
            has_action_required = bool(action_required_instances) or any(
                instance.action_required != "none" for instance in connected_instances
            )

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
                    has_action_required=has_action_required,
                    connected_integrations=connected_instances,
                    action_required_integrations=action_required_instances,
                    admin_prerequisite=_get_admin_prerequisite(int_type),
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
    summary="Refresh messaging channels for an integration",
    description="Trigger a manual refresh of Slack or Teams channels/chats for an integration.",
)
async def refresh_integration_channels(
    integration_id: UUID,
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[RefreshChannelsResponse]:
    """
    Trigger a manual refresh of Slack or Teams channels/chats.

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

        # Validate it's a supported messaging integration
        if integration.integration_type not in {
            IntegrationType.TEAMS,
            IntegrationType.SLACK,
        }:
            return Response.bad_request_error_response(
                message="Channel refresh is only supported for Slack and Teams integrations",
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
