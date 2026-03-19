"""Teams Bot webhook handler.

This endpoint receives all messages from Microsoft Teams via Azure Bot Service.
Authentication is validated using the Bot Framework JWT in the Authorization header.

Endpoint: POST /bots/teams/messages
"""

import contextlib

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from src.infra.config.configs import teams_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.bots.teams_bot_service import TeamsBotService

logger = get_logger(__name__)

router = APIRouter(tags=["teams-bot"])


@router.post("/messages")
async def handle_teams_message(
    request: Request,
) -> Response:
    """
    Webhook endpoint for Microsoft Teams messages.

    This endpoint:
    1. Receives activities (messages, events) from Azure Bot Service
    2. Validates the Bot Framework authentication
    3. Processes the activity and returns a response

    Note: This endpoint does NOT use standard auth (no get_current_user).
    Authentication is validated using the Bot Framework JWT in the
    Authorization header.
    """
    if not teams_config.TEAMS_ENABLED:
        logger.info("Teams webhook called but TEAMS_ENABLED is false")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Teams integration is not enabled",
        )

    # Get authorization header for Bot Framework validation
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        logger.warning("Missing Authorization header in Teams webhook")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    # Create service inline (not via Depends) so the background task owns its
    # lifecycle — no race between FastAPI dependency cleanup and create_task.
    service = TeamsBotService()
    auth_result = await service.validate_auth_header(auth_header)
    if not auth_result.is_valid:
        logger.warning("Teams webhook auth failed: %s", auth_result.error)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    # Parse the incoming activity
    try:
        body = await request.json()
        logger.info("Teams webhook body: %s", body)
    except Exception as e:
        logger.error("Failed to parse Teams webhook body: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from e

    activity_type = body.get("type", "unknown")
    activity_id = body.get("id", "unknown")
    conversation_id = body.get("conversation", {}).get("id", "unknown")

    logger.info(
        "Teams webhook received: type=%s, activity_id=%s, conversation_id=%s",
        activity_type,
        activity_id,
        conversation_id,
    )

    # Basic idempotency guard (best-effort)
    if activity_id and activity_id != "unknown":
        try:
            dedupe_key = f"webhook:teams:{conversation_id}:{activity_id}"
            redis = get_redis_client()
            deduped = redis.set(
                dedupe_key,
                "1",
                nx=True,
                ex=teams_config.TEAMS_WEBHOOK_DEDUPE_TTL_SECONDS,
            )
            if not deduped:
                logger.info("Duplicate Teams webhook ignored: activity_id=%s", activity_id)
                return Response(status_code=status.HTTP_200_OK)
        except Exception:
            logger.debug("Teams webhook dedupe check failed", exc_info=True)

    # Invoke activities require a synchronous response body
    if body.get("type") == "invoke":
        try:
            result = await service.process_activity(body)
            if result:
                return JSONResponse(content=result, status_code=status.HTTP_200_OK)
            return Response(status_code=status.HTTP_200_OK)
        except Exception as e:
            logger.error("Error processing Teams invoke: %s", e, exc_info=True)
            return Response(status_code=status.HTTP_200_OK)

    # For messages and other activities, process in the background and return
    # 200 immediately to prevent Azure Bot Service retries under load.
    import asyncio

    async def _process_and_close():
        try:
            await service.process_activity(body)
        except Exception as e:
            logger.error("Error processing Teams activity: %s", e, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await service.close()

    asyncio.create_task(_process_and_close())
    return Response(status_code=status.HTTP_200_OK)
