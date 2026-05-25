"""Service for the Integrations > Bots management surface."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select
from src.api.dependencies.organisation_dependencies import UserIDs
from src.api.schemas.integrations.bot_integration_schemas import (
    AvailableBotIntegration,
    AvailableBotIntegrationsResponse,
    BotInstallationResponse,
    SlackBotConnectResponse,
    WhatsAppBotConnectResponse,
)
from src.domain.models.master_tables.bot_platform_installation_models import (
    BotPlatformInstallation,
    BotPlatformProvider,
)
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProvider,
)
from src.infra.config.configs import slack_config, whatsapp_config
from src.service.integrations.bot_integration_metadata import (
    SLACK_BOT_METADATA,
    WHATSAPP_BOT_METADATA,
    get_bot_integration_metadata,
)

SLACK_BOT_SCOPES = [
    "app_mentions:read",
    "chat:write",
    "im:history",
    "im:read",
    "im:write",
    "users:read",
    "users:read.email",
    "files:read",
]


class BotIntegrationService:
    """Builds bot provider cards and provider-owned connect URLs."""

    def __init__(self, session: AsyncSession, user_ids: UserIDs):
        self.session = session
        self.user_ids = user_ids

    async def list_available_bots(self) -> AvailableBotIntegrationsResponse:
        """Return bot provider cards visible to the current user."""
        bots = [await self._build_slack_card(), await self._build_whatsapp_card()]
        return AvailableBotIntegrationsResponse(
            bots=bots,
            total_available=len(get_bot_integration_metadata()),
            total_installed=sum(1 for bot in bots if bot.installed),
            total_linked=sum(1 for bot in bots if bot.identity_linked),
        )

    async def get_slack_connect_url(self) -> SlackBotConnectResponse:
        """Build the Slack OAuth v2 install URL for the workspace bot."""
        _, _, client_id, _ = await slack_config.get_credentials()
        if not client_id:
            raise RuntimeError("Slack client ID is not configured")

        redirect_uri = self._get_slack_bot_redirect_uri()
        params: dict[str, str] = {
            "client_id": client_id,
            "scope": ",".join(SLACK_BOT_SCOPES),
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri

        return SlackBotConnectResponse(
            connect_url=f"https://slack.com/oauth/v2/authorize?{urlencode(params)}",
            expires_at=None,
        )

    async def get_whatsapp_connect_url(self) -> WhatsAppBotConnectResponse:
        """Build the WhatsApp click-to-chat URL used by the QR connect modal."""
        phone_number = self._get_whatsapp_bot_phone_number_digits()
        if not phone_number:
            raise RuntimeError("WhatsApp bot phone number is not configured")

        return WhatsAppBotConnectResponse(
            connect_url=f"https://wa.me/{phone_number}?{urlencode({'text': 'hi'})}",
            expires_at=None,
        )

    async def _build_slack_card(self) -> AvailableBotIntegration:
        installation = await self._get_slack_installation()
        identity_mapping = await self._get_slack_identity_mapping()

        installed = installation is not None
        identity_linked = identity_mapping is not None
        action_required = self._get_action_required(installed, identity_linked)

        return AvailableBotIntegration(
            provider=SLACK_BOT_METADATA.provider,
            name=SLACK_BOT_METADATA.name,
            display_name=SLACK_BOT_METADATA.display_name,
            description=SLACK_BOT_METADATA.description,
            logo_url=SLACK_BOT_METADATA.logo_url,
            availability_status=SLACK_BOT_METADATA.availability_status,  # type: ignore[arg-type]
            connect_url=None,
            installed=installed,
            installation=(
                BotInstallationResponse(
                    installation_id=installation.installation_id,
                    workspace_id=installation.external_workspace_id,
                    workspace_name=installation.external_workspace_name,
                    installed_at=installation.installed_at,
                    last_used_at=installation.last_used_at,
                )
                if installation
                else None
            ),
            identity_linked=identity_linked,
            identity_mapping_id=identity_mapping.mapping_id if identity_mapping else None,
            action_required=action_required,
        )

    async def _build_whatsapp_card(self) -> AvailableBotIntegration:
        installation = await self._get_whatsapp_installation()
        identity_mapping = await self._get_whatsapp_identity_mapping()
        installed = installation is not None
        identity_linked = identity_mapping is not None

        return AvailableBotIntegration(
            provider=WHATSAPP_BOT_METADATA.provider,
            name=WHATSAPP_BOT_METADATA.name,
            display_name=WHATSAPP_BOT_METADATA.display_name,
            description=WHATSAPP_BOT_METADATA.description,
            logo_url=WHATSAPP_BOT_METADATA.logo_url,
            availability_status=WHATSAPP_BOT_METADATA.availability_status,  # type: ignore[arg-type]
            connection_mode="qr_deep_link",
            connect_url=None,
            installed=installed,
            installation=(
                BotInstallationResponse(
                    installation_id=installation.installation_id,
                    workspace_id=installation.external_workspace_id,
                    workspace_name=installation.external_workspace_name,
                    installed_at=installation.installed_at,
                    last_used_at=installation.last_used_at,
                )
                if installation
                else None
            ),
            identity_linked=identity_linked,
            identity_mapping_id=identity_mapping.mapping_id if identity_mapping else None,
            action_required=self._get_action_required(installed, identity_linked),
        )

    async def _get_slack_installation(self) -> Any | None:
        stmt = (
            select(BotPlatformInstallation)
            .where(
                col(BotPlatformInstallation.provider) == BotPlatformProvider.SLACK.value,
                col(BotPlatformInstallation.organisation_id) == self.user_ids.organisation_id,
                col(BotPlatformInstallation.is_active).is_(True),
            )
            .order_by(col(BotPlatformInstallation.installed_at).desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _get_whatsapp_installation(self) -> Any | None:
        stmt = (
            select(BotPlatformInstallation)
            .where(
                col(BotPlatformInstallation.provider) == BotPlatformProvider.WHATSAPP.value,
                col(BotPlatformInstallation.organisation_id) == self.user_ids.organisation_id,
                col(BotPlatformInstallation.is_active).is_(True),
            )
            .order_by(col(BotPlatformInstallation.installed_at).desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _get_slack_identity_mapping(self) -> Any | None:
        stmt = (
            select(ExternalIdentityMapping)
            .where(
                col(ExternalIdentityMapping.provider) == ExternalIdentityProvider.SLACK.value,
                col(ExternalIdentityMapping.organisation_id) == self.user_ids.organisation_id,
                col(ExternalIdentityMapping.user_id) == self.user_ids.user_id,
                col(ExternalIdentityMapping.is_active).is_(True),
            )
            .order_by(col(ExternalIdentityMapping.linked_at).desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _get_whatsapp_identity_mapping(self) -> Any | None:
        stmt = (
            select(ExternalIdentityMapping)
            .where(
                col(ExternalIdentityMapping.provider) == ExternalIdentityProvider.WHATSAPP.value,
                col(ExternalIdentityMapping.organisation_id) == self.user_ids.organisation_id,
                col(ExternalIdentityMapping.user_id) == self.user_ids.user_id,
                col(ExternalIdentityMapping.is_active).is_(True),
            )
            .order_by(col(ExternalIdentityMapping.linked_at).desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    @staticmethod
    def _get_action_required(installed: bool, identity_linked: bool) -> str:
        if not installed:
            return "install_required"
        if not identity_linked:
            return "identity_link_required"
        return "none"

    @staticmethod
    def _get_slack_bot_redirect_uri() -> str:
        return str(
            getattr(slack_config, "SLACK_BOT_REDIRECT_URI", None)
            or getattr(slack_config, "REDIRECT_URI", "")
            or ""
        )

    @staticmethod
    def _get_whatsapp_bot_phone_number_digits() -> str:
        configured_number = str(getattr(whatsapp_config, "WHATSAPP_BOT_PHONE_NUMBER", "") or "")
        return "".join(char for char in configured_number if char.isdigit())
