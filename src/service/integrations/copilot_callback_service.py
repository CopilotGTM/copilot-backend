from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlmodel import col
from src.domain.models.master_tables import OrganisationIntegrationDetails, User
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.integrations_metadata import INTEGRATION_METADATA
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.copilot_oauth_service import copilot_oauth_service
from src.service.integrations.initial_sync_service import InitialSyncService
from src.service.integrations.providers.provider_factory import ProviderFactory
from src.service.integrations.scope_validation import (
    get_missing_required_scopes,
    get_missing_required_token_fields,
    is_google_integration,
    is_microsoft_integration,
    parse_granted_scopes_from_token_payload,
)

logger = get_logger(__name__)


class CopilotCallbackService:
    """Process OAuth callbacks: validate state, exchange tokens, persist integration.

    Handles the second half of the OAuth flow after the provider redirects back.
    Called by the /callback route. Creates new integrations or reactivates previously
    disconnected ones for the same user + provider combination.
    """

    _AUTH_BACKEND_CUSTOM = "custom"
    _PROVIDER_KEY_METADATA_KEY = "provider_key"
    _PROVIDER_CONFIG_KEY_METADATA_KEY = "provider_config_key"
    _TOKEN_TYPE_METADATA_KEY = "token_type"
    _HUBSPOT_PORTAL_ID_METADATA_KEY = "hubspot_portal_id"
    _HUBSPOT_PORTAL_NAME_METADATA_KEY = "hubspot_portal_name"
    _HUBSPOT_USER_ID_METADATA_KEY = "hubspot_user_id"

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    @staticmethod
    def _parse_token_expiry(expires_value: int | str | None) -> datetime | None:
        """Normalize provider expiry into a datetime.

        Providers are inconsistent — some return ISO timestamps, others return
        seconds-until-expiry as int or string. This handles all three formats.
        """
        if expires_value is None:
            return None
        if isinstance(expires_value, str):
            if "T" in expires_value:
                return datetime.fromisoformat(expires_value.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
            return datetime.utcnow() + timedelta(seconds=int(expires_value))
        return datetime.utcnow() + timedelta(seconds=expires_value)

    @staticmethod
    def _get_missing_scope_message(
        integration_type: IntegrationType,
        missing_scopes: set[str],
    ) -> str:
        if integration_type == IntegrationType.GMAIL:
            return "Gmail permission was not granted. Please reconnect and allow Gmail access."

        if integration_type == IntegrationType.GOOGLE_CALENDAR:
            if "https://www.googleapis.com/auth/meetings.space.readonly" in missing_scopes:
                return (
                    "Google Meet permission was not granted. Please reconnect and allow "
                    "Google Meet access."
                )
            if "https://www.googleapis.com/auth/calendar.readonly" in missing_scopes:
                return (
                    "Google Calendar permission was not granted. Please reconnect and allow "
                    "Google Calendar access."
                )
            return (
                "Google Calendar permissions were not granted. Please reconnect and allow "
                "Google Calendar and Google Meet access."
            )

        if integration_type == IntegrationType.SLACK:
            return "Slack permissions were not granted. Please reconnect and allow Slack access."

        if integration_type == IntegrationType.HUBSPOT:
            return (
                "HubSpot permissions were not granted. Please reconnect and allow HubSpot access."
            )

        if integration_type == IntegrationType.SALESFORCE:
            return (
                "Salesforce permissions were not granted. "
                "Please reconnect and allow Salesforce access."
            )

        integration_name = INTEGRATION_METADATA[integration_type].name
        return (
            f"Required {integration_name} permissions were not granted. "
            f"Please reconnect and allow {integration_name} access."
        )

    @staticmethod
    def _get_missing_token_field_message(
        integration_type: IntegrationType,
        missing_fields: set[str],
    ) -> str:
        _ = missing_fields
        if is_microsoft_integration(integration_type):
            return (
                "Microsoft offline access was not granted. "
                "Please reconnect and allow offline access."
            )
        if is_google_integration(integration_type):
            return (
                "Google offline access was not granted. Please reconnect and allow offline access."
            )

        integration_name = INTEGRATION_METADATA[integration_type].name
        return (
            f"Required {integration_name} token access was not granted. "
            f"Please reconnect and allow {integration_name} access."
        )

    def _validate_required_scopes(
        self,
        integration_type: IntegrationType,
        token_payload: dict[str, Any],
    ) -> None:
        missing_scopes = get_missing_required_scopes(
            integration_type,
            parse_granted_scopes_from_token_payload(token_payload, []),
        )
        if missing_scopes:
            raise ValueError(self._get_missing_scope_message(integration_type, missing_scopes))

        missing_token_fields = get_missing_required_token_fields(
            integration_type,
            token_payload,
        )
        if missing_token_fields:
            raise ValueError(
                self._get_missing_token_field_message(integration_type, missing_token_fields)
            )

    def _apply_custom_token_payload(
        self,
        integration: KnowledgeIntegration,
        *,
        integration_type: IntegrationType,
        provider_key: str,
        token_payload: dict[str, Any],
        provider_user_info: dict[str, Any] | None = None,
    ) -> None:
        """Merge provider tokens into the integration record and set expiry/metadata.

        Called during both creation and reactivation to store credentials and
        track the auth backend provenance (custom vs legacy Nango).
        """
        credentials = dict(integration.credentials or {})
        credentials.update(token_payload)
        if (
            integration_type == IntegrationType.HUBSPOT
            and provider_user_info
            and "scope" not in credentials
            and "scopes" not in credentials
        ):
            credentials["scopes"] = provider_user_info.get("scopes")

        integration.credentials = credentials
        integration.access_token_expires_at = self._parse_token_expiry(
            credentials.get("expires_at") or credentials.get("expires_in")
        )
        integration.refresh_token_expires_at = self._parse_token_expiry(
            credentials.get("refresh_token_expires_at")
            or credentials.get("refresh_token_expires_in")
        )
        integration.scopes = parse_granted_scopes_from_token_payload(
            credentials, integration.scopes
        )

        metadata = dict(integration.connection_metadata or {})
        metadata["auth_backend"] = self._AUTH_BACKEND_CUSTOM
        metadata[self._PROVIDER_KEY_METADATA_KEY] = provider_key
        metadata[self._PROVIDER_CONFIG_KEY_METADATA_KEY] = f"custom-{integration_type.value}"
        token_type = credentials.get("token_type")
        if isinstance(token_type, str) and token_type:
            metadata[self._TOKEN_TYPE_METADATA_KEY] = token_type
        if integration_type == IntegrationType.HUBSPOT and provider_user_info:
            self._apply_hubspot_user_info_metadata(metadata, provider_user_info)
        integration.connection_metadata = metadata

    def _apply_hubspot_user_info_metadata(
        self,
        metadata: dict[str, Any],
        provider_user_info: dict[str, Any],
    ) -> None:
        portal_id = provider_user_info.get("hubspot_portal_id")
        if portal_id is not None:
            metadata[self._HUBSPOT_PORTAL_ID_METADATA_KEY] = str(portal_id)

        portal_name = provider_user_info.get("hubspot_portal_name")
        if portal_name is not None:
            metadata[self._HUBSPOT_PORTAL_NAME_METADATA_KEY] = str(portal_name)

        hubspot_user_id = provider_user_info.get("hubspot_user_id")
        if hubspot_user_id is not None:
            metadata[self._HUBSPOT_USER_ID_METADATA_KEY] = str(hubspot_user_id)

    async def handle_oauth_callback(self, code: str, state: str) -> dict[str, Any]:
        """Process the provider's OAuth callback and persist the integration.

        Flow:
        1. Decode JWT state → extract user/org/type and jti as connection_id
           (no authenticated session here — the signed JWT is our trust anchor)
        2. Validate user exists and belongs to the claimed org
        3. Exchange authorization code for tokens at the provider
        4. Reactivate a previously disconnected integration for the same user+type,
           or create a new one if none exists
        5. Trigger initial data sync via Celery

        Args:
            code: Authorization code from the provider.
            state: JWT state token generated during /authorize.

        Returns:
            Dict with integration_id, type, scope, and email for the redirect response.

        Raises:
            ValueError: If state is invalid/expired, user mismatch, or token exchange fails.
        """
        try:
            state_data = await copilot_oauth_service.decode_oauth_state(state)
        except Exception as e:
            logger.error("Invalid or expired OAuth state: %s", e)
            raise ValueError("Invalid or expired state parameter") from e

        try:
            user_id = UUID(str(state_data["sub"]))
            organisation_id = UUID(str(state_data["org"]))
            integration_type = IntegrationType(state_data["type"])
        except Exception as e:
            raise ValueError("State payload is missing required claims") from e

        jti = state_data.get("jti")
        if not jti:
            raise ValueError("State payload is missing jti claim")
        connection_id = str(jti)

        provider = ProviderFactory.get_provider(integration_type)

        # Cross-check user against master DB — prevents forged state tokens
        # from creating integrations for other users
        user = await self._fetch_user_from_master(user_id)
        if not user or user.organisation_id != organisation_id:
            logger.error("User mismatch in state token")
            raise ValueError("Invalid user state context")

        # Session released before external calls — token exchange and user info
        # fetch can take several seconds depending on the provider
        try:
            tokens = await provider.exchange_code(code)
            access_token = tokens.get("access_token")
            if not access_token:
                raise ValueError("Missing access_token from provider")

            user_info = await provider.get_user_info(str(access_token))
            validation_payload = dict(tokens)
            if (
                integration_type == IntegrationType.HUBSPOT
                and "scope" not in validation_payload
                and "scopes" not in validation_payload
            ):
                validation_payload["scopes"] = user_info.get("scopes")
            self._validate_required_scopes(integration_type, validation_payload)

            integration_email = str(user_info.get("email") or "").strip().lower()
            if not integration_email:
                raise ValueError("Provider did not return an email for the connected account")

            # TODO: Reintroduce business-domain validation after Google app-review access is unblocked.
        except Exception as e:
            logger.error("Failed to exchange OAuth code for %s: %s", integration_type.value, e)
            raise

        # Prevent duplicate: if another user in the org already connected this
        # email + provider type, reject — one account can't be shared across users
        async for org_session in acquire_org_async_session(organisation_id):
            duplicate_stmt = (
                select(KnowledgeIntegration)
                .where(
                    col(KnowledgeIntegration.integration_type) == integration_type,
                    col(KnowledgeIntegration.integration_email) == integration_email,
                    col(KnowledgeIntegration.is_active).is_(True),
                    col(KnowledgeIntegration.created_by_user_id) != user_id,
                )
                .limit(1)
            )
            existing = (await org_session.execute(duplicate_stmt)).scalars().first()
            if existing:
                raise ValueError(
                    f"This {integration_type.value.replace('_', ' ').title()} account "
                    f"({integration_email}) is already connected by another user."
                )

        # Reactivation: match by email to avoid reactivating the wrong account
        # when a user has multiple connections for the same provider type
        # (e.g. work@company.com and personal@gmail.com both for Outlook Calendar)
        async for org_session in acquire_org_async_session(organisation_id):
            reactivate_stmt = (
                select(KnowledgeIntegration)
                .where(
                    col(KnowledgeIntegration.created_by_user_id) == user_id,
                    col(KnowledgeIntegration.integration_type) == integration_type,
                    col(KnowledgeIntegration.integration_email) == integration_email,
                )
                .order_by(col(KnowledgeIntegration.updated_at).desc())
                .limit(1)
            )
            reactivatable = (await org_session.execute(reactivate_stmt)).scalars().first()

            if reactivatable:
                return await self._reactivate_integration(
                    session=org_session,
                    integration=reactivatable,
                    connection_id=connection_id,
                    integration_type=integration_type,
                    provider_key=provider.provider_key,
                    tokens=tokens,
                    integration_email=integration_email,
                    provider_user_info=user_info,
                )

        # Scope comes from integration metadata, not the user's role — personal
        # integrations (calendar, email) stay USER-scoped even if an admin connects
        metadata = INTEGRATION_METADATA[integration_type]
        return await self._create_new_integration(
            organisation_id=organisation_id,
            integration_type=integration_type,
            connection_id=connection_id,
            user_id=user_id,
            integration_scope=metadata.default_scope,
            provider_key=provider.provider_key,
            tokens=tokens,
            integration_email=integration_email,
            provider_user_info=user_info,
        )

    async def _reactivate_integration(
        self,
        *,
        session: Any,
        integration: KnowledgeIntegration,
        connection_id: str,
        integration_type: IntegrationType,
        provider_key: str,
        tokens: dict[str, Any],
        integration_email: str,
        provider_user_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reactivate a previously disconnected integration with fresh tokens.

        Reuses the existing DB record to preserve integration_id continuity —
        any deal associations or sync history linked to this integration remain intact.
        """
        # Update name to current metadata — old integrations may have stale names
        # from the deprecated Nango flow
        metadata = INTEGRATION_METADATA[integration_type]
        integration.name = metadata.name
        integration.connection_id = connection_id
        integration.is_active = True
        integration.requires_reauth = False
        integration.last_sync_error = None
        integration.integration_email = integration_email
        self._apply_custom_token_payload(
            integration,
            integration_type=integration_type,
            provider_key=provider_key,
            token_payload=tokens,
            provider_user_info=provider_user_info,
        )
        InitialSyncService().apply_post_connect_state(integration)

        session.add(integration)
        await session.commit()
        await session.refresh(integration)

        await self._save_connection_mapping(
            connection_id,
            integration,
            integration.connection_metadata.get(
                "provider_config_key", f"custom-{integration_type.value}"
            ),
        )

        # Trigger initial sync so data starts flowing immediately — without this,
        # users would wait up to 30 min for the next Celery Beat cycle
        self._trigger_initial_sync(integration)

        return self._build_success_response(integration, reactivated=True)

    async def _create_new_integration(
        self,
        *,
        organisation_id: UUID,
        integration_type: IntegrationType,
        connection_id: str,
        user_id: UUID,
        integration_scope: IntegrationScope,
        provider_key: str,
        tokens: dict[str, Any],
        integration_email: str,
        provider_user_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = INTEGRATION_METADATA[integration_type]
        async for org_session in acquire_org_async_session(organisation_id):
            integration = KnowledgeIntegration(
                organisation_id=organisation_id,
                integration_type=integration_type,
                name=metadata.name,
                connection_id=connection_id,
                created_by_user_id=user_id,
                integration_scope=integration_scope,
                integration_email=integration_email,
                connection_metadata={},
                credentials={},
                is_active=True,
            )
            self._apply_custom_token_payload(
                integration,
                integration_type=integration_type,
                provider_key=provider_key,
                token_payload=tokens,
                provider_user_info=provider_user_info,
            )
            InitialSyncService().apply_post_connect_state(integration)

            org_session.add(integration)
            await org_session.commit()
            await org_session.refresh(integration)

            type_key = str(self._enum_value(integration_type))
            await self._save_connection_mapping(
                connection_id,
                integration,
                integration.connection_metadata.get("provider_config_key", f"custom-{type_key}"),
            )

            # Trigger initial sync so data starts flowing immediately
            self._trigger_initial_sync(integration)

            return self._build_success_response(integration, reactivated=False)

        raise RuntimeError("Failed to acquire organisation session")

    def _trigger_initial_sync(self, integration: KnowledgeIntegration) -> None:
        """Dispatch initial sync task so data flows immediately after connect.

        Errors are caught and logged — a failed sync trigger should not
        prevent the OAuth callback from returning success to the user.
        """
        try:
            InitialSyncService().trigger_initial_sync(
                organisation_id=integration.organisation_id,
                integration=integration,
            )
            logger.info(
                "Triggered initial sync: integration=%s type=%s",
                integration.integration_id,
                integration.integration_type,
            )
        except Exception as e:
            logger.error(
                "Failed to trigger initial sync: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

    def _build_success_response(
        self,
        integration: KnowledgeIntegration,
        *,
        reactivated: bool,
    ) -> dict[str, Any]:
        return {
            "integration_id": str(integration.integration_id),
            "integration_type": self._enum_value(integration.integration_type),
            "connection_id": str(integration.connection_id),
            "integration_scope": self._enum_value(integration.integration_scope),
            "integration_email": integration.integration_email,
            "reactivated": reactivated,
        }

    async def _save_connection_mapping(
        self,
        connection_id: str,
        integration: KnowledgeIntegration,
        provider_config_key: str,
    ) -> None:
        """Persist connection→integration mapping in the master (public) schema.

        This cross-schema mapping allows the token refresh worker to look up
        which org and integration a connection belongs to without scanning
        every tenant schema.
        """
        connection_uuid = UUID(connection_id)

        async for master_session in acquire_master_async_session():
            stmt = select(OrganisationIntegrationDetails).where(
                OrganisationIntegrationDetails.connection_id == connection_uuid  # type: ignore[arg-type]
            )
            result = await master_session.execute(stmt)
            mapping = result.scalar_one_or_none()

            if mapping:
                mapping.organisation_id = integration.organisation_id
                mapping.integration_id = integration.integration_id
                mapping.provider_config_key = provider_config_key
                master_session.add(mapping)
                await master_session.commit()
                return

            master_session.add(
                OrganisationIntegrationDetails(
                    connection_id=connection_uuid,
                    organisation_id=integration.organisation_id,
                    integration_id=integration.integration_id,
                    provider_config_key=provider_config_key,
                )
            )
            await master_session.commit()

    @staticmethod
    async def _fetch_user_from_master(user_uuid: UUID) -> User | None:
        async for master_session in acquire_master_async_session():
            statement = select(User).where(User.user_id == user_uuid)  # type: ignore[arg-type]
            result = await master_session.execute(statement)
            return result.scalar_one_or_none()
        return None


copilot_callback_service = CopilotCallbackService()
