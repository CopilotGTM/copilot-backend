"""Slack Bot webhook handler.

Endpoints:
- POST /bots/slack/events        — Slack Events API webhook
- POST /bots/slack/interactions  — Slack interactive component submissions
- GET  /bots/slack/oauth/callback — OAuth v2 install callback (multi-tenant)

Authentication for events is validated using the Slack Signing Secret (HMAC-SHA256).
"""

import json
from typing import Any
from urllib.parse import parse_qs

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.domain.models.master_tables.bot_platform_installation_models import BotPlatformProvider
from src.infra.config.configs import slack_config
from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.service.bots.bot_installation_service import BotInstallationService
from src.service.bots.slack_bot_service import SlackBotService

logger = get_logger(__name__)

router = APIRouter(tags=["slack-bot"])


def _parse_slack_webhook_payload(body: bytes) -> tuple[dict[str, Any], str]:
    """Parse Slack webhook body as JSON event payload or form-encoded interaction payload.

    Returns:
        Tuple of (payload, payload_kind) where payload_kind is "event" or "interaction".
    """
    body_text = body.decode("utf-8", errors="replace")

    # Primary path: Events API sends application/json.
    try:
        parsed = json.loads(body_text)
        if isinstance(parsed, dict):
            return parsed, "event"
    except json.JSONDecodeError:
        pass

    # Compatibility path: interactive payload accidentally posted to /events.
    form_payload = parse_qs(body_text, keep_blank_values=True).get("payload", [])
    if form_payload:
        parsed = json.loads(form_payload[0] or "{}")
        if isinstance(parsed, dict):
            return parsed, "interaction"

    raise ValueError("Invalid Slack webhook payload")


@router.post("/events")
async def handle_slack_event(request: Request) -> Response:
    """
    Webhook endpoint for Slack Events API.

    This endpoint:
    1. Handles url_verification challenges (Slack sends this on app setup)
    2. Validates the request signature using the Signing Secret
    3. Processes the event (app_mention, message.im)

    Note: This endpoint does NOT use standard auth (no get_current_user).
    Authentication is validated using the Slack Signing Secret.
    """
    if not slack_config.SLACK_ENABLED:
        logger.info("Slack webhook called but SLACK_ENABLED is false")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Slack integration is not enabled",
        )

    # Read raw body for signature verification
    body = await request.body()

    # Parse JSON events payload, with compatibility support for form-encoded
    # interaction payloads sent to /events.
    try:
        payload, payload_kind = _parse_slack_webhook_payload(body)
    except Exception as e:
        logger.error("Failed to parse Slack webhook body: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request body",
        ) from e

    # Handle Slack URL verification challenge (no auth needed for this)
    # Slack sends this when you first set up the Events API endpoint
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        logger.info("Slack URL verification challenge received")
        return JSONResponse(
            content={"challenge": challenge},
            status_code=status.HTTP_200_OK,
        )

    # Validate request signature for all other events
    service = SlackBotService()
    try:
        headers = {
            "X-Slack-Request-Timestamp": request.headers.get("X-Slack-Request-Timestamp", ""),
            "X-Slack-Signature": request.headers.get("X-Slack-Signature", ""),
        }
        auth_result = await service.validate_request(headers, body)
        if not auth_result.is_valid:
            logger.warning("Slack webhook auth failed: %s", auth_result.error)
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    except Exception as e:
        logger.error("Slack auth validation error: %s", e, exc_info=True)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    if payload_kind == "interaction":
        interaction_type = payload.get("type", "")
        team_id = payload.get("team", {}).get("id", "unknown")
        user_id = payload.get("user", {}).get("id", "unknown")
        logger.warning(
            "Slack interaction payload received on /events (type=%s team=%s user=%s). "
            "Process it and return 200 for compatibility.",
            interaction_type,
            team_id,
            user_id,
        )
        try:
            await service.process_interaction(payload)
            return Response(status_code=status.HTTP_200_OK)
        except Exception as e:
            logger.error("Error processing Slack interaction on /events: %s", e, exc_info=True)
            return Response(status_code=status.HTTP_200_OK)
        finally:
            await service.close()

    # Idempotency guard
    event = payload.get("event", {})
    event_id = payload.get("event_id", "")
    team_id = payload.get("team_id", "unknown")

    logger.info(
        "Slack webhook received: type=%s, event_type=%s, event_id=%s, team=%s",
        payload.get("type"),
        event.get("type", "unknown"),
        event_id,
        team_id,
    )

    if event_id:
        try:
            dedupe_key = f"webhook:slack:{team_id}:{event_id}"
            redis = get_redis_client()
            deduped = redis.set(
                dedupe_key,
                "1",
                nx=True,
                ex=slack_config.SLACK_WEBHOOK_DEDUPE_TTL_SECONDS,
            )
            if not deduped:
                logger.info("Duplicate Slack webhook ignored: event_id=%s", event_id)
                return Response(status_code=status.HTTP_200_OK)
        except Exception:
            logger.debug("Slack webhook dedupe check failed", exc_info=True)

    # Process the event in the background — return 200 immediately so Slack
    # doesn't retry (Slack retries if it doesn't get 200 within 3 seconds).
    import asyncio

    async def _process_and_close():
        try:
            await service.process_event(payload)
        except Exception as e:
            logger.error("Error processing Slack event: %s", e, exc_info=True)
        finally:
            await service.close()

    asyncio.create_task(_process_and_close())
    return Response(status_code=status.HTTP_200_OK)


@router.post("/interactions")
async def handle_slack_interaction(request: Request) -> Response:
    """Handle Slack interactive component submissions (button clicks, form submits).

    Slack sends interactive payloads as application/x-www-form-urlencoded with
    a `payload` field containing JSON. This is separate from the Events API.

    Requires the Interactivity URL to be configured in Slack app settings
    pointing to: {base_url}/users/bots/slack/interactions
    """
    if not slack_config.SLACK_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Slack integration is not enabled",
        )

    # Read raw body for signature verification
    body = await request.body()

    # Validate signature
    service = SlackBotService()
    try:
        headers = {
            "X-Slack-Request-Timestamp": request.headers.get("X-Slack-Request-Timestamp", ""),
            "X-Slack-Signature": request.headers.get("X-Slack-Signature", ""),
        }
        auth_result = await service.validate_request(headers, body)
        if not auth_result.is_valid:
            logger.warning("Slack interaction auth failed: %s", auth_result.error)
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    except Exception as e:
        logger.error("Slack interaction auth error: %s", e, exc_info=True)
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    # Parse the payload (form-encoded with a `payload` JSON field)
    try:
        form_data = await request.form()
        payload_str = form_data.get("payload", "")
        payload = json.loads(payload_str) if payload_str else {}
    except Exception as e:
        logger.error("Failed to parse Slack interaction payload: %s", e)
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    interaction_type = payload.get("type", "")
    team_id = payload.get("team", {}).get("id", "unknown")
    user_id = payload.get("user", {}).get("id", "unknown")

    logger.info(
        "Slack interaction received: type=%s, team=%s, user=%s",
        interaction_type,
        team_id,
        user_id,
    )

    try:
        await service.process_interaction(payload)
        # Slack expects 200 OK for acknowledgement
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        logger.error("Error processing Slack interaction: %s", e, exc_info=True)
        return Response(status_code=status.HTTP_200_OK)
    finally:
        await service.close()


@router.get("/oauth/callback")
async def slack_oauth_callback(
    code: str = Query(None, description="Authorization code from Slack"),
    error: str = Query(None, description="Error from Slack OAuth flow"),
    _state: str = Query(None, description="CSRF state parameter"),
    session: AsyncSession = Depends(get_master_async_session),
) -> Response:
    """Handle Slack OAuth v2 install callback.

    When a workspace admin clicks "Add to Slack", Slack redirects here with
    an authorization code. We exchange it for a bot token and store the
    installation in bot_platform_installations.

    Flow:
    1. Slack redirects to this URL with ?code=xxx
    2. We exchange code for access token via oauth.v2.access
    3. Store bot_token + workspace info in bot_platform_installations
    4. Redirect user to a success page
    """
    if error:
        logger.warning("Slack OAuth error: %s", error)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": error, "message": "Slack installation was cancelled or failed."},
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing authorization code.",
        )

    # Load app credentials (client_id, client_secret)
    signing_secret, app_id, client_id, client_secret = await slack_config.get_credentials()

    if not client_id or not client_secret:
        logger.error("Slack OAuth: client_id or client_secret not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Slack integration is not properly configured.",
        )

    token_request_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }
    redirect_uri = (
        getattr(slack_config, "SLACK_BOT_REDIRECT_URI", None)
        or getattr(slack_config, "REDIRECT_URI", "")
        or ""
    )
    if redirect_uri:
        token_request_data["redirect_uri"] = redirect_uri

    # Exchange authorization code for bot token
    token_url = "https://slack.com/api/oauth.v2.access"
    async with (
        aiohttp.ClientSession() as http_session,
        http_session.post(
            token_url,
            data=token_request_data,
        ) as resp,
    ):
        token_data = await resp.json()

    if not token_data.get("ok"):
        slack_error = token_data.get("error", "unknown")
        logger.error("Slack OAuth token exchange failed: %s", slack_error)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to complete Slack installation: {slack_error}",
        )

    # Extract installation data
    team_info = token_data.get("team", {})
    team_id = team_info.get("id", "")
    team_name = team_info.get("name", "")
    bot_token = token_data.get("access_token", "")
    bot_user_id = token_data.get("bot_user_id", "")
    authed_user = token_data.get("authed_user", {})
    installing_user_id = authed_user.get("id", "")
    scope = token_data.get("scope", "")

    if not team_id or not bot_token:
        logger.error("Slack OAuth response missing team_id or access_token")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid response from Slack.",
        )

    # Store installation
    installation_service = BotInstallationService()
    await installation_service.upsert_installation(
        session=session,
        provider=BotPlatformProvider.SLACK,
        external_workspace_id=team_id,
        bot_token=bot_token,
        bot_user_id=bot_user_id,
        external_workspace_name=team_name,
        installed_by_external_user_id=installing_user_id,
        extra_data={
            "scope": scope,
            "app_id": token_data.get("app_id", ""),
            "token_type": token_data.get("token_type", ""),
        },
    )

    logger.info(
        "Slack bot installed: team=%s (%s) by user=%s",
        team_id,
        team_name,
        installing_user_id,
    )

    # Send a welcome DM to the user who installed the app
    if installing_user_id and bot_token:
        try:
            from src.service.bots.slack_bot_service import SlackBotService

            slack_service = SlackBotService()
            dm_channel = await slack_service._open_dm(team_id, installing_user_id)
            if dm_channel:
                welcome_text = (
                    ":wave: *Hi! I'm Kai, your CopilotGTM assistant.*\n\n"
                    f"I've been installed to *{team_name}*. "
                    "To get started, just send me a message and I'll ask you to "
                    "sign in to your CopilotGTM account.\n\n"
                    "Once linked, you can ask me questions and I'll help you find "
                    "answers from your organization's knowledge base!"
                )
                await slack_service._send_message(team_id, dm_channel, welcome_text)
        except Exception:
            logger.warning("Failed to send Slack install welcome DM", exc_info=True)

    # Redirect to success page (or return JSON for API clients)
    app_url = (
        slack_config.SLACK_AUTH_LOGIN_URL.rsplit("/bots/auth", 1)[0]
        if slack_config.SLACK_AUTH_LOGIN_URL
        else ""
    )
    if app_url:
        return RedirectResponse(
            url=f"{app_url}/bots/slack/install-success",
            status_code=status.HTTP_302_FOUND,
        )

    return JSONResponse(
        content={
            "success": True,
            "team_id": team_id,
            "team_name": team_name,
            "message": f"Kai has been installed to {team_name}!",
        }
    )
