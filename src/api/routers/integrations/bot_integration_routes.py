"""Authenticated management endpoints for CopilotGTM bots."""

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations.bot_integration_schemas import (
    AvailableBotIntegrationsResponse,
    SlackBotConnectResponse,
    WhatsAppBotConnectResponse,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.integrations.bot_integration_service import BotIntegrationService

logger = get_logger(__name__)
router = APIRouter(prefix="/bots", tags=["integrations"])


async def get_bot_integration_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_master_async_session),
) -> BotIntegrationService:
    return BotIntegrationService(session=session, user_ids=user_ids)


@router.get(
    "/available",
    response_model=GenericResponse[AvailableBotIntegrationsResponse],
    summary="List available CopilotGTM bots",
)
async def list_available_bots(
    response: FastAPIResponse,
    service: BotIntegrationService = Depends(get_bot_integration_service),
) -> GenericResponse[AvailableBotIntegrationsResponse]:
    try:
        data = await service.list_available_bots()
        return Response.success_response(
            message="Available bots retrieved successfully",
            data=data,
        )
    except Exception:
        logger.exception("Failed to list available bot integrations")
        return Response.internal_server_error_response(
            message="Failed to retrieve available bots",
            response=response,
        )


@router.get(
    "/slack/connect",
    response_model=GenericResponse[SlackBotConnectResponse],
    summary="Get Slack bot install URL",
)
async def get_slack_bot_connect_url(
    response: FastAPIResponse,
    service: BotIntegrationService = Depends(get_bot_integration_service),
) -> GenericResponse[SlackBotConnectResponse]:
    try:
        data = await service.get_slack_connect_url()
        return Response.success_response(
            message="Slack bot connect URL generated successfully",
            data=data,
        )
    except Exception:
        logger.exception("Failed to generate Slack bot connect URL")
        return Response.internal_server_error_response(
            message="Failed to start Slack bot connection",
            response=response,
        )


@router.get(
    "/whatsapp/connect",
    response_model=GenericResponse[WhatsAppBotConnectResponse],
    summary="Get WhatsApp bot QR connect URL",
)
async def get_whatsapp_bot_connect_url(
    response: FastAPIResponse,
    service: BotIntegrationService = Depends(get_bot_integration_service),
) -> GenericResponse[WhatsAppBotConnectResponse]:
    try:
        data = await service.get_whatsapp_connect_url()
        return Response.success_response(
            message="WhatsApp bot connect URL generated successfully",
            data=data,
        )
    except Exception:
        logger.exception("Failed to generate WhatsApp bot connect URL")
        return Response.internal_server_error_response(
            message="Failed to start WhatsApp bot connection",
            response=response,
        )
