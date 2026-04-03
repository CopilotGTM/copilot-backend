"""
Deal Integration management routes.

Handles:
- Listing available integrations for deals
- Linking channels to deals
- Removing channels from deals
"""

from typing import Any
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
    ChannelLinkRequest,
    ChannelLinkResponse,
    ChannelListResponse,
    EmptyResponse,
    IntegrationResponse,
    LinkedChannel,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.config.integrations_metadata import get_all_integration_metadata
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService

logger = get_logger(__name__)
router = APIRouter(tags=["deal-integrations"])


async def get_integration_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency to create IntegrationService instance."""
    yield IntegrationService(db_session=session, user_ids=user_ids)


@router.get(
    "/deals/{deal_id}/available",
    response_model=GenericResponse[AvailableIntegrationsResponse],
    summary="List available integrations for deals",
    description="Get available integrations specifically for deal context (Slack, Teams).",
)
async def list_available_deal_integrations(
    deal_id: UUID,
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[AvailableIntegrationsResponse]:
    """
    List available integrations for deals (Slack, Teams).

    Returns metadata for supported deal integrations along with connected instances
    linked to the specified deal.
    """
    try:
        logger.info(
            "Listing available deal integrations",
            extra={"deal_id": str(deal_id)},
        )
        # Get active integrations for the deal
        connected_integrations = await service.list_all_active_integrations(
            user_id=user_ids.user_id
        )

        # Get linked channels for the deal
        linked_channels = await service.get_deal_linked_channels(deal_id)

        # Group linked channels by integration type
        linked_channels_by_type: dict[str, list[dict[str, Any]]] = {}
        for channel in linked_channels:
            int_type = channel["integration_type"]
            if int_type not in linked_channels_by_type:
                linked_channels_by_type[int_type] = []
            linked_channels_by_type[int_type].append(channel)

        # Group connected integrations by type
        connected_by_type: dict[str, list[KnowledgeIntegration]] = {}
        for integration in connected_integrations:
            int_type = integration.integration_type
            if int_type not in connected_by_type:
                connected_by_type[int_type] = []
            connected_by_type[int_type].append(integration)

        # Build response with selected integrations (Slack, Teams)
        integrations = []
        total_connected = len(connected_integrations)

        # Only include Teams for now
        target_integrations = ["teams"]
        all_metadata = get_all_integration_metadata()

        for int_type in target_integrations:
            if int_type in all_metadata:
                metadata = all_metadata[int_type]
                # Get connected instances for this type
                connected_instances = connected_by_type.get(int_type, [])
                is_connected = len(connected_instances) > 0

                # Get linked channels for this type
                type_linked_channels = linked_channels_by_type.get(int_type, [])

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
                        linked_channels=[LinkedChannel(**ch) for ch in type_linked_channels],
                    )
                )

        response_data = AvailableIntegrationsResponse(
            integrations=integrations,
            total_available=len(integrations),
            total_connected=total_connected,
        )

        return Response.success_response(
            message="Available deal integrations retrieved successfully",
            data=response_data,
        )

    except Exception as e:
        logger.error("Error listing available deal integrations: %s", str(e), exc_info=True)
        return Response.error_response(
            message="Failed to retrieve available deal integrations",
            status_code=500,
            response=response,
        )


@router.post(
    "/deals/{deal_id}/channels",
    response_model=GenericResponse[ChannelLinkResponse],
    summary="Link channel to deal",
    description="Link a messaging channel (Slack/Teams) to a deal for syncing.",
)
async def link_channel_to_deal(
    deal_id: UUID,
    channel_data: ChannelLinkRequest,
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[ChannelLinkResponse]:
    """
    Link a channel to a deal.

    Args:
        deal_id: UUID of the deal
        channel_data: Channel details (id, name, type, integration_id)
        service: IntegrationService instance
        user_ids: Current authenticated user context

    Returns:
        Success message
    """
    try:
        result = await service.link_channel_to_deal(deal_id, channel_data)

        logger.info(
            f"Linked channel {channel_data.id} to deal {deal_id} for user {user_ids.user_id}"
        )

        response_data = ChannelLinkResponse(
            channel_id=result["channel_id"],
            deal_id=result["deal_id"],
            external_channel_id=result["external_channel_id"],
            name=result["name"],
            type=result["type"],
            integration_id=result["integration_id"],
        )

        return Response.success_response(
            message="Channel linked successfully",
            data=response_data,
        )
    except ValueError as e:
        return Response.error_response(message=str(e), status_code=404, response=response)
    except Exception as e:
        logger.error(
            f"Failed to link channel to deal {deal_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to link channel", response=response
        )


@router.delete(
    "/deals/{deal_id}/channels/{channel_id}",
    response_model=GenericResponse[EmptyResponse],
    summary="Remove channel from deal",
    description="Unlink a messaging channel from a deal.",
)
async def remove_channel_from_deal(
    deal_id: UUID,
    channel_id: UUID,
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[EmptyResponse]:
    """
    Remove a channel link from a deal.

    Args:
        deal_id: UUID of the deal
        channel_id: UUID of the channel (internal ID)
        service: IntegrationService instance
        user_ids: Current authenticated user context

    Returns:
        Success message
    """
    try:
        logger.info(f"User {user_ids.user_id} removing channel {channel_id} from deal {deal_id}")
        success = await service.remove_channel_from_deal(deal_id, channel_id)
        if success:
            return Response.success_response(message="Channel removed successfully")
        else:
            return Response.error_response(
                message="Channel link not found", status_code=404, response=response
            )
    except Exception as e:
        logger.error(
            f"Failed to remove channel {channel_id} from deal {deal_id}: {str(e)}", exc_info=True
        )
        return Response.internal_server_error_response(
            message="Failed to remove channel", response=response
        )


@router.get(
    "/deals/{integration_id}/channels",
    response_model=GenericResponse[ChannelListResponse],
    summary="List channels for an integration",
    description="Fetch available channels/teams for a specific integration (e.g., Teams, Slack).",
)
async def list_integration_channels(
    integration_id: UUID,
    response: FastAPIResponse,
    deal_id: UUID | None = None,
    service: IntegrationService = Depends(get_integration_service),
) -> GenericResponse[ChannelListResponse]:
    """
    List channels for a specific integration.

    Args:
        integration_id: UUID of the integration
        deal_id: Optional UUID of the deal to filter out already linked channels
        service: IntegrationService instance

    Returns:
        List of channels
    """
    try:
        channels = await service.list_integration_channels(integration_id, deal_id)

        response_data = ChannelListResponse(
            channels=channels,
            total=len(channels),
        )

        return Response.success_response(
            message="Channels retrieved successfully",
            data=response_data,
        )
    except ValueError as e:
        return Response.error_response(message=str(e), status_code=404, response=response)
    except Exception as e:
        logger.error(
            f"Failed to list channels for integration {integration_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message="Failed to retrieve channels", response=response
        )
