"""Google Chat interaction webhook handler.

Endpoint:
- POST /bots/google-chat/events

Notes:
- Endpoint relies on Google-issued bearer/JWT validation (`Authorization` header) in
  place of app-level auth.
- A dedupe key is applied before processing to avoid duplicate replies on provider retries.
- Processing stays synchronous only for auth/parse/dedupe; response generation is
  delegated to the service layer and then returned as immediate 200 payload.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from src.infra.config.configs import google_chat_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.bots.auth_validators.google_chat_auth import GoogleChatAuthValidator
from src.service.bots.google_chat_bot_service import GoogleChatBotService

logger = get_logger(__name__)

router = APIRouter(tags=["google-chat-bot"])


@router.post("/events")
async def handle_google_chat_event(request: Request) -> Response:
    """Validate signature/audience, dedupe event, and hand off to bot service."""
    if not google_chat_config.GOOGLE_CHAT_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Chat integration is not enabled",
        )

    body = await request.body()
    auth_result = await GoogleChatAuthValidator(
        audience=google_chat_config.GOOGLE_CHAT_AUTH_AUDIENCE
    ).validate(request.headers, body)
    if not auth_result.is_valid:
        logger.warning("Google Chat webhook auth failed: %s", auth_result.error)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        # Parse JSON only after passing auth to avoid work on unauthorized payloads.
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        )

    dedupe_id = _event_dedupe_id(payload)
    if dedupe_id and _is_duplicate(dedupe_id):
        logger.info("Duplicate Google Chat event ignored id=%s", dedupe_id)
        return JSONResponse(content={}, status_code=status.HTTP_200_OK)

    # Delegate actual event logic to the service so webhook path stays small and testable.
    response_payload = await GoogleChatBotService().process_event(payload)
    return JSONResponse(content=response_payload or {}, status_code=status.HTTP_200_OK)


def _event_dedupe_id(payload: dict[str, Any]) -> str | None:
    """Build a best-effort idempotency key from event metadata."""
    event_type = str(payload.get("type") or payload.get("eventType") or "").upper()
    if event_type not in {"MESSAGE", "CARD_CLICKED", "SUBMIT_FORM", "APP_COMMAND"}:
        return None

    message = payload.get("message") or {}
    user = payload.get("user") or {}
    event_id = str(payload.get("eventId") or payload.get("event_id") or "")
    message_name = str(message.get("name") or "")
    if event_id:
        return event_id
    if message_name:
        return message_name

    user_name = str(user.get("name") or "")
    create_time = str(message.get("createTime") or "")
    if user_name and create_time:
        return f"{event_type}:{user_name}:{create_time}"
    return None


def _is_duplicate(dedupe_id: str) -> bool:
    """Return whether this dedupe id has already been observed."""
    try:
        redis = get_redis_client()
        was_new = redis.set(
            f"webhook:google_chat:{dedupe_id}",
            "1",
            nx=True,
            ex=google_chat_config.GOOGLE_CHAT_WEBHOOK_DEDUPE_TTL_SECONDS,
        )
        return not bool(was_new)
    except Exception:
        # On cache failure, avoid false negatives that could block legitimate traffic.
        logger.debug("Google Chat webhook dedupe check failed", exc_info=True)
        return False
