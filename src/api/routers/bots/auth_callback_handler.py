"""Bot Authentication API endpoints.

Generic endpoints for linking external identities (Teams, Slack, Zoom, etc.)
to CopilotGTM users.

Flow:
1. User sends message to bot
2. Bot creates state token, sends login URL
3. User completes Supabase login on web frontend
4. Frontend calls POST /bots/auth/callback with access_token + state
5. Backend validates state, verifies token, creates identity mapping
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.schemas.bot_auth_models import (
    BotAuthCallbackRequest,
    BotAuthCallbackResponse,
    BotAuthErrorResponse,
    BotAuthValidateStateResponse,
)
from src.domain.models.master_tables import AdminUserStatus, SupabaseUserStatus, User
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
    ExternalIdentityProviderType,
)
from src.infra.auth.supabase_user_client import SupabaseAuthError, supabase_user_client
from src.infra.utils.logger import get_logger
from src.service.bots.auth_state_service import get_bot_auth_state_service
from src.service.bots.identity_service import ExternalIdentityService
from src.service.channels import OutboundChannelService, OutboundDeliveryTarget

logger = get_logger(__name__)

router = APIRouter(tags=["bot-auth"])


# Dependencies
def get_identity_service() -> ExternalIdentityService:
    """Dependency for ExternalIdentityService."""
    return ExternalIdentityService()


# Provider type mapping
PROVIDER_TYPE_MAP = {
    ExternalIdentityProvider.TEAMS: ExternalIdentityProviderType.CHAT_BOT,
    ExternalIdentityProvider.SLACK: ExternalIdentityProviderType.CHAT_BOT,
    ExternalIdentityProvider.WHATSAPP: ExternalIdentityProviderType.CHAT_BOT,
    ExternalIdentityProvider.GOOGLE_CHAT: ExternalIdentityProviderType.CHAT_BOT,
    # Future providers:
    # ExternalIdentityProvider.AZURE_AD: ExternalIdentityProviderType.SSO,
    # ExternalIdentityProvider.GOOGLE: ExternalIdentityProviderType.SSO,
    # ExternalIdentityProvider.OKTA: ExternalIdentityProviderType.SSO,
}


@router.get(
    "/validate-state",
    response_model=BotAuthValidateStateResponse,
)
async def validate_state(
    state: str = Query(..., description="State token from login URL"),
) -> BotAuthValidateStateResponse:
    """Check if a state token is still valid (read-only, does not consume).

    Used by the frontend to verify the token before showing the login form,
    so users don't log in only to find the link has expired.

    Args:
        state: State token from the login URL query parameter

    Returns:
        Validation result with provider and remaining TTL
    """
    state_service = get_bot_auth_state_service()
    auth_state = state_service.peek_state(state)

    if not auth_state:
        return BotAuthValidateStateResponse(
            valid=False,
            provider=None,
            expires_in_seconds=0,
        )

    ttl = state_service.get_state_ttl(state)

    return BotAuthValidateStateResponse(
        valid=True,
        provider=auth_state.provider.value,
        expires_in_seconds=ttl,
    )


@router.post(
    "/callback",
    response_model=BotAuthCallbackResponse,
    responses={
        400: {"model": BotAuthErrorResponse, "description": "Invalid request"},
        401: {"model": BotAuthErrorResponse, "description": "Authentication failed"},
        404: {"model": BotAuthErrorResponse, "description": "User not found"},
    },
)
async def bot_auth_callback(
    request: BotAuthCallbackRequest,
    session: AsyncSession = Depends(get_master_async_session),
    identity_service: ExternalIdentityService = Depends(get_identity_service),
) -> BotAuthCallbackResponse:
    """Complete identity linking after Supabase login.

    This endpoint is called by the frontend after the user completes Supabase login.
    It works for any provider (Teams, Slack, Zoom, etc.) - the provider is encoded
    in the state token.

    Steps:
    1. Validates and consumes the state token (single-use)
    2. Verifies the Supabase access token
    3. Looks up the user by supabase_user_id
    4. Creates an external_identity_mappings record

    Args:
        request: Contains access_token and state

    Returns:
        Success response with user email and provider

    Raises:
        HTTPException 400: Invalid or expired state token
        HTTPException 401: Invalid access token
        HTTPException 404: User not found in CopilotGTM
    """
    # Step 1: Validate state token without consuming it. The token is only
    # consumed after the app access token and Copilot user checks succeed, so a
    # failed first web login does not burn the bot link.
    state_service = get_bot_auth_state_service()
    auth_state = state_service.peek_state(request.state)

    if not auth_state:
        logger.warning("Invalid or expired state token")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state token. Please try logging in again from the bot.",
        )

    provider = auth_state.provider
    logger.info(
        "Processing %s auth callback for external_user=%s",
        provider.value,
        auth_state.external_user_id[:20],
    )

    # Step 2: Verify Supabase access token
    try:
        user_data = supabase_user_client.verify_token(request.access_token)
    except SupabaseAuthError as e:
        logger.warning("Supabase token verification failed: %s", e.message)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token. Please try logging in again.",
        ) from e

    supabase_user_id = user_data.get("user", {}).get("id")
    if not supabase_user_id:
        logger.error("No user ID in Supabase token response")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token structure.",
        )

    # Step 3: Look up user in CopilotGTM by supabase_user_id
    stmt = select(User).where(User.supabase_user_id == supabase_user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        logger.warning(
            "User not found in CopilotGTM for supabase_user_id=%s",
            supabase_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please ensure you have a CopilotGTM account.",
        )

    # Check user is active
    if user.status != SupabaseUserStatus.ACTIVE or user.admin_status != AdminUserStatus.APPROVED:
        logger.warning("User account is not active: user_id=%s", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is not active. Please contact your administrator.",
        )

    # Verify the sign-in email matches the CopilotGTM account email
    app_email = user_data.get("user", {}).get("email", "")
    if app_email and user.email and app_email.lower() != user.email.lower():
        logger.warning(
            "Email mismatch during %s identity linking: signed_in_email=%s, copilot_email=%s",
            provider.value,
            app_email,
            user.email,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"You signed in with {app_email}, but your CopilotGTM account "
                f"is registered with {user.email}. "
                "Please sign in with the same email address as your CopilotGTM account."
            ),
        )

    # Step 4: Create or reactivate identity mapping
    provider_type = PROVIDER_TYPE_MAP.get(provider, ExternalIdentityProviderType.CHAT_BOT)
    user_email = user.email

    auth_state = state_service.validate_and_consume_state(request.state)
    if not auth_state:
        logger.warning("State token was consumed or expired before linking")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state token. Please try logging in again from the bot.",
        )

    try:
        _, created = await identity_service.create_or_reactivate_identity_mapping(
            session=session,
            user_id=user.user_id,
            organisation_id=user.organisation_id,
            provider=provider,
            provider_type=provider_type,
            external_user_id=auth_state.external_user_id,
            external_tenant_id=auth_state.external_tenant_id,
            external_user_email=user_email,
            external_user_name=auth_state.external_user_name,
            conversation_context=auth_state.context,
        )
    except Exception as e:
        logger.error("Failed to create/reactivate identity mapping: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to link account. Please try again.",
        ) from e

    message = "Account linked successfully" if created else "Account re-linked successfully"
    logger.info(
        "Successfully linked %s identity: external_user=%s -> copilot_user=%s (created=%s)",
        provider.value,
        auth_state.external_user_id[:20],
        user_email,
        created,
    )

    # Step 5: Link the bot installation to this user's organisation
    # The installation was created during OAuth with organisation_id=NULL;
    # now that we know which org this workspace belongs to, link them.
    if auth_state.external_tenant_id and user.organisation_id:
        try:
            from src.domain.models.master_tables.bot_platform_installation_models import (
                BotPlatformProvider,
            )
            from src.service.bots.bot_installation_service import BotInstallationService

            provider_to_platform = {
                ExternalIdentityProvider.TEAMS: BotPlatformProvider.TEAMS,
                ExternalIdentityProvider.SLACK: BotPlatformProvider.SLACK,
            }
            platform_provider = provider_to_platform.get(provider)
            if platform_provider:
                install_service = BotInstallationService()
                linked = await install_service.link_organisation(
                    session=session,
                    provider=platform_provider,
                    external_workspace_id=auth_state.external_tenant_id,
                    organisation_id=user.organisation_id,
                )
                if linked:
                    logger.info(
                        "Linked %s installation workspace=%s to org=%s",
                        provider.value,
                        auth_state.external_tenant_id,
                        user.organisation_id,
                    )
        except Exception:
            logger.warning("Failed to link installation to organisation", exc_info=True)

    # Send a welcome message back to the bot conversation
    context = auth_state.context or {}

    if provider == ExternalIdentityProvider.TEAMS:
        service_url = context.get("service_url")
        conversation_id = context.get("conversation_id")
        if service_url and conversation_id:
            welcome_text = (
                f"You're now signed in as **{user_email}**.\n\n"
                "You can now ask me questions and I'll help you find answers "
                "from your organization's knowledge base!\n\n"
                "Type **help** to see available commands."
            )
            try:
                await OutboundChannelService().send_text(
                    OutboundDeliveryTarget(
                        channel="teams",
                        mapping_id=None,
                        delivery_context={
                            "service_url": service_url,
                            "conversation_id": conversation_id,
                        },
                    ),
                    welcome_text,
                )
            except Exception:
                logger.warning("Failed to send Teams welcome message", exc_info=True)

    elif provider == ExternalIdentityProvider.SLACK:
        channel_id = context.get("channel_id")
        slack_team_id = auth_state.external_tenant_id
        if channel_id and slack_team_id:
            welcome_text = (
                f"You're now signed in as *{user_email}*.\n\n"
                "You can now ask me questions and I'll help you find answers "
                "from your organization's knowledge base!\n\n"
                "Type *help* to see available commands."
            )
            try:
                await OutboundChannelService().send_text(
                    OutboundDeliveryTarget(
                        channel="slack",
                        mapping_id=None,
                        delivery_context={"team_id": slack_team_id, "channel_id": channel_id},
                    ),
                    welcome_text,
                )
            except Exception:
                logger.warning("Failed to send Slack welcome message", exc_info=True)

    elif provider == ExternalIdentityProvider.WHATSAPP:
        phone_number_id = context.get("phone_number_id")
        recipient_wa_id = context.get("recipient_wa_id")
        if phone_number_id and recipient_wa_id:
            welcome_text = (
                f"You're now signed in as {user_email}.\n\n"
                "You can now ask me questions and I'll help you find answers "
                "from your organization's knowledge base.\n\n"
                "Type help to see available commands."
            )
            try:
                await OutboundChannelService().send_text(
                    OutboundDeliveryTarget(
                        channel="whatsapp",
                        mapping_id=None,
                        delivery_context={
                            "phone_number_id": phone_number_id,
                            "recipient_wa_id": recipient_wa_id,
                        },
                    ),
                    welcome_text,
                )
            except Exception:
                logger.warning("Failed to send WhatsApp welcome message", exc_info=True)

    elif provider == ExternalIdentityProvider.GOOGLE_CHAT:
        space_name = context.get("space_name")
        thread_name = context.get("thread_name")
        if space_name:
            welcome_text = (
                f"You're now signed in as {user_email}.\n\n"
                "You can now ask me questions and I'll help you find answers "
                "from your organization's knowledge base.\n\n"
                "Type help to see available commands."
            )
            try:
                from src.service.bots.google_chat_bot_service import GoogleChatBotService

                google_chat_service = GoogleChatBotService()
                await google_chat_service.send_message(
                    space_name=space_name,
                    thread_name=thread_name,
                    text=welcome_text,
                )
            except Exception:
                logger.warning("Failed to send Google Chat welcome message", exc_info=True)

    return BotAuthCallbackResponse(
        success=True,
        message=message,
        user_email=user_email,
        provider=provider.value,
    )
