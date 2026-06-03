import datetime
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

import jwt
from src.domain.models.master_tables import Organisation, User
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.auth.user_context import UserIDs
from src.infra.config.configs import security_secrets_config
from src.infra.config.integrations_metadata import (
    AvailabilityStatus,
    IntegrationMetadata,
    get_integration_metadata,
)
from src.infra.config.microsoft_integration_scopes import (
    get_microsoft_admin_consent_scope_version,
    get_microsoft_admin_consent_scopes,
    parse_microsoft_scope_string,
)
from src.infra.database.sessions import acquire_master_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.microsoft_provider import MicrosoftProvider
from src.service.integrations.providers.provider_factory import ProviderFactory
from src.service.integrations.providers.salesforce_provider import (
    normalize_salesforce_oauth_base_url,
)
from src.service.integrations.scope_validation import is_microsoft_integration
from src.service.orgnisation.organisation_preferences import (
    has_google_gmail_read_scope_enabled,
)

logger = get_logger(__name__)
_MICROSOFT_ADMIN_CONSENT_PURPOSE = "microsoft_admin_consent"


async def _get_oauth_state_secret() -> str:
    return await security_secrets_config.get_oauth_state_secret()


class CopilotOAuthIntegrationUnavailableError(Exception):
    """Raised when a custom OAuth integration is not available for connection."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class OAuthInitiationResult:
    auth_url: str
    attempt_id: str


class CopilotOAuthService:
    """Generate OAuth authorization URLs and manage state tokens for integration connections.

    Called by copilot_oauth_routes.py to initiate the OAuth flow. The callback side
    is handled by CopilotCallbackService.
    """

    _ALLOWED_AVAILABILITY_STATUSES = {AvailabilityStatus.AVAILABLE, AvailabilityStatus.BETA}

    @staticmethod
    def _get_validated_metadata(integration_type: IntegrationType) -> IntegrationMetadata:
        metadata = get_integration_metadata(integration_type)
        if not metadata:
            raise ValueError(f"Unknown integration type: {integration_type}")
        return metadata

    @classmethod
    def _ensure_integration_available(cls, integration_type: IntegrationType) -> None:
        metadata = cls._get_validated_metadata(integration_type)
        if metadata.availability_status not in cls._ALLOWED_AVAILABILITY_STATUSES:
            raise CopilotOAuthIntegrationUnavailableError(metadata.availability_status)

    @staticmethod
    async def generate_oauth_state(
        user_ids: UserIDs,
        integration_type: IntegrationType,
        *,
        jti: str | None = None,
        oauth_base_url: str | None = None,
        purpose: str | None = None,
    ) -> str:
        """Generate a signed JWT embedding user/org context into the OAuth state parameter.

        The provider returns this token unchanged in the callback, allowing the callback
        service to identify the user and org without requiring an authenticated session.
        The jti also serves as an idempotency key for the callback.

        Args:
            user_ids: Authenticated user context.
            integration_type: The provider being connected.

        Returns:
            HS256-signed JWT string (expires in 15 minutes).
        """
        payload = {
            "sub": str(user_ids.user_id),
            "org": str(user_ids.organisation_id),
            "type": integration_type.value,
            # jti doubles as attempt id and callback idempotency key.
            "jti": jti or str(uuid4()),
            "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=15),
            "iat": datetime.datetime.now(datetime.UTC),
        }
        if integration_type == IntegrationType.SALESFORCE and oauth_base_url:
            payload["oauth_base_url"] = oauth_base_url
        if purpose:
            payload["purpose"] = purpose
        encoded_jwt = jwt.encode(payload, await _get_oauth_state_secret(), algorithm="HS256")
        return encoded_jwt

    @staticmethod
    async def decode_oauth_state(state_token: str) -> dict[str, Any]:
        """Decode and verify the OAuth state JWT from the provider callback.

        Raises:
            jwt.ExpiredSignatureError: If the 15-minute window has elapsed.
            jwt.InvalidTokenError: If the token is malformed or signature fails.
        """
        decoded = jwt.decode(state_token, await _get_oauth_state_secret(), algorithms=["HS256"])
        if isinstance(decoded, dict):
            return dict(decoded)
        return {}

    @staticmethod
    async def decode_oauth_state_allow_expired(state_token: str) -> dict[str, Any]:
        """Decode a signed OAuth state while ignoring only expiration.

        This is used after callback failures so the popup can still be tied back
        to the initiating browser attempt. Forged or malformed states remain
        rejected because signature verification is still enabled.
        """
        decoded = jwt.decode(
            state_token,
            await _get_oauth_state_secret(),
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        if isinstance(decoded, dict):
            return dict(decoded)
        return {}

    async def _should_request_google_gmail_read_scope(
        self,
        organisation_id: UUID,
    ) -> bool:
        async for master_session in acquire_master_async_session():
            organisation = await master_session.get(Organisation, organisation_id)
            return has_google_gmail_read_scope_enabled(
                organisation.preferences if organisation else None
            )
        return False

    async def initiate_oauth_flow(
        self,
        *,
        user_ids: UserIDs,
        integration_type: IntegrationType,
        oauth_base_url: str | None = None,
    ) -> OAuthInitiationResult:
        """Build the provider's OAuth authorization URL with an embedded state JWT.

        Called by the /authorize endpoint. The returned URL is opened in a popup
        by the frontend. After user consent, the provider redirects back to
        /callback with the state token and an authorization code.

        Args:
            user_ids: Authenticated user context (user_id + organisation_id).
            integration_type: The provider to connect (e.g. OUTLOOK_CALENDAR).

        Returns:
            Full OAuth authorization URL to redirect the user to.

        Raises:
            CopilotOAuthIntegrationUnavailableError: If integration is not AVAILABLE/BETA.
            ValueError: If integration_type has no metadata or provider.
        """
        self._ensure_integration_available(integration_type)
        provider = ProviderFactory.get_provider(integration_type)
        attempt_id = str(uuid4())
        validated_oauth_base_url = None
        if integration_type == IntegrationType.SALESFORCE:
            validated_oauth_base_url = normalize_salesforce_oauth_base_url(oauth_base_url)
        state_token = await self.generate_oauth_state(
            user_ids,
            integration_type,
            jti=attempt_id,
            oauth_base_url=validated_oauth_base_url,
        )

        if integration_type == IntegrationType.SALESFORCE:
            auth_url = await cast(Any, provider).get_authorization_url(
                state_token,
                oauth_base_url=validated_oauth_base_url,
            )
        elif integration_type == IntegrationType.GMAIL:
            include_read_scope = await self._should_request_google_gmail_read_scope(
                user_ids.organisation_id
            )
            auth_url = await cast(Any, provider).get_authorization_url(
                state_token,
                include_gmail_read_scope=include_read_scope,
            )
        else:
            auth_url = await provider.get_authorization_url(state_token)

        logger.info(
            "Initiated Custom OAuth flow for user %s, org %s, provider %s",
            user_ids.user_id,
            user_ids.organisation_id,
            integration_type.value,
        )
        return OAuthInitiationResult(auth_url=auth_url, attempt_id=attempt_id)

    async def initiate_microsoft_admin_consent_flow(
        self,
        *,
        user_ids: UserIDs,
        integration_type: IntegrationType,
    ) -> OAuthInitiationResult:
        if not is_microsoft_integration(integration_type):
            raise ValueError("Admin consent is only supported for Microsoft integrations")

        self._ensure_integration_available(integration_type)
        attempt_id = str(uuid4())
        state_token = await self.generate_oauth_state(
            user_ids,
            integration_type,
            jti=attempt_id,
            purpose=_MICROSOFT_ADMIN_CONSENT_PURPOSE,
        )
        auth_url = await MicrosoftProvider(integration_type).get_admin_consent_url(state_token)

        logger.info(
            "Initiated Microsoft admin consent flow for user %s, org %s, provider %s",
            user_ids.user_id,
            user_ids.organisation_id,
            integration_type.value,
        )
        return OAuthInitiationResult(auth_url=auth_url, attempt_id=attempt_id)

    async def handle_microsoft_admin_consent_callback(
        self,
        *,
        state: str,
        admin_consent: str,
        tenant: str | None,
        scope: str | None,
    ) -> dict[str, str]:
        if admin_consent != "True":
            raise ValueError("Microsoft admin consent was not granted")

        state_data = await self.decode_oauth_state(state)
        if state_data.get("purpose") != _MICROSOFT_ADMIN_CONSENT_PURPOSE:
            raise ValueError("Invalid Microsoft admin consent state")

        try:
            user_id = str(UUID(str(state_data["sub"])))
            organisation_id = UUID(str(state_data["org"]))
            integration_type = IntegrationType(state_data["type"])
        except Exception as e:
            raise ValueError("State payload is missing required claims") from e

        if not is_microsoft_integration(integration_type):
            raise ValueError("Admin consent is only supported for Microsoft integrations")

        attempt_id = str(state_data.get("jti") or "")
        if not attempt_id:
            raise ValueError("State payload is missing jti claim")

        async for master_session in acquire_master_async_session():
            user = await master_session.get(User, UUID(user_id))
            if not user or user.organisation_id != organisation_id:
                raise ValueError("Invalid user state context")

            organisation = await master_session.get(Organisation, organisation_id)
            if not organisation:
                raise ValueError("Organisation not found for Microsoft admin consent")

            preferences = dict(organisation.preferences or {})
            integrations = dict(preferences.get("integrations") or {})
            microsoft = dict(integrations.get("microsoft") or {})
            granted_scopes = parse_microsoft_scope_string(scope)
            microsoft["admin_consent"] = {
                "granted": True,
                "tenant": tenant,
                "granted_scopes": granted_scopes,
                "required_scopes": list(get_microsoft_admin_consent_scopes()),
                "scope_version": get_microsoft_admin_consent_scope_version(),
                "attempt_id": attempt_id,
                "initiated_by_user_id": user_id,
                "trigger_integration_type": integration_type.value,
                "granted_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            integrations["microsoft"] = microsoft
            preferences["integrations"] = integrations
            organisation.preferences = preferences

            master_session.add(organisation)
            await master_session.commit()
            return {
                "integration_type": integration_type.value,
                "tenant": tenant or "",
                "attempt_id": attempt_id,
            }

        raise RuntimeError("Failed to acquire master session")


copilot_oauth_service = CopilotOAuthService()
