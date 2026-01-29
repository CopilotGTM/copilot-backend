"""Teams Bot webhook handler.

This endpoint receives all messages from Microsoft Teams via Azure Bot Service.
The Bot Framework SDK handles authentication and message routing.

Endpoint: POST /bots/teams/messages
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from src.infra.config.configs import teams_config
from src.infra.utils.logger import get_logger
from src.service.bots import TeamsBotService

logger = get_logger(__name__)

router = APIRouter(tags=["teams-bot"])


# -----------------------------------------------------------------------------
# Service Dependency (follows service architecture pattern)
# -----------------------------------------------------------------------------
async def get_teams_bot_service():
    """Dependency that yields a TeamsBotService instance with cleanup."""
    service = TeamsBotService()
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("TeamsBotService close failed", exc_info=True)


# -----------------------------------------------------------------------------
# Webhook Endpoint
# -----------------------------------------------------------------------------
@router.post("/messages")
async def handle_teams_message(
    request: Request,
    service: TeamsBotService = Depends(get_teams_bot_service),
) -> Response:
    """
    Webhook endpoint for Microsoft Teams messages.

    This endpoint:
    1. Receives activities (messages, events) from Azure Bot Service
    2. Validates the Bot Framework authentication
    3. Processes the activity and returns a response

    Note: This endpoint does NOT use standard auth (no get_current_user).
    Authentication is handled by the Bot Framework SDK using the
    Authorization header with a JWT token from Azure Bot Service.
    """
    if not teams_config.TEAMS_ENABLED:
        logger.info("Teams webhook called but TEAMS_ENABLED is false")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Teams integration is not enabled",
        )

    # Get authorization header for Bot Framework validation
    auth_header = request.headers.get("Authorization", "")

    # Parse the incoming activity
    try:
        body = await request.json()
    except Exception as e:
        logger.error("Failed to parse Teams webhook body: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from e

    activity_type = body.get("type", "unknown")
    from_user = body.get("from", {}).get("name", "unknown")
    activity_id = body.get("id", "unknown")

    logger.info(
        "Teams webhook received: type=%s, from=%s, activity_id=%s",
        activity_type,
        from_user,
        activity_id,
    )

    try:
        await service.process_activity(body, auth_header)
        # Bot Framework expects 200 OK for successful processing
        return Response(status_code=status.HTTP_200_OK)

    except Exception as e:
        logger.error("Error processing Teams activity: %s", e, exc_info=True)
        # Still return 200 to avoid Azure Bot Service retries
        # Log the error for investigation
        return Response(status_code=status.HTTP_200_OK)


# -----------------------------------------------------------------------------
# Health Check Endpoint
# -----------------------------------------------------------------------------
@router.get("/health")
async def teams_health() -> dict:
    """Health check endpoint for Teams bot."""
    return {
        "status": "healthy",
        "teams_enabled": teams_config.TEAMS_ENABLED,
    }
