import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from src.domain.models.master_tables import OrganisationIntegrationDetails, User, UserAppRole
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.integrations.copilot_oauth_service import copilot_oauth_service
from src.service.integrations.providers.provider_factory import ProviderFactory

logger = get_logger(__name__)


class CopilotCallbackService:
    """Process first-party OAuth callbacks and persist connection metadata."""

    _AUTH_BACKEND_CUSTOM = "custom"
    _PROVIDER_KEY_METADATA_KEY = "provider_key"
    _PROVIDER_CONFIG_KEY_METADATA_KEY = "provider_config_key"
    _TOKEN_TYPE_METADATA_KEY = "token_type"

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    @staticmethod
    def _parse_token_expiry(expires_value: int | str | None) -> datetime | None:
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
    def _parse_scopes(
        token_payload: dict[str, Any],
        fallback: list[str] | None = None,
    ) -> list[str] | None:
        scopes = token_payload.get("scope")
        if isinstance(scopes, str):
            parsed = [scope for scope in scopes.split(" ") if scope]
            return parsed or fallback
        if isinstance(scopes, list):
            parsed = [scope for scope in scopes if isinstance(scope, str) and scope]
            return parsed or fallback
        return fallback

    def _apply_custom_token_payload(
        self,
        integration: KnowledgeIntegration,
        *,
        integration_type: IntegrationType,
        provider_key: str,
        token_payload: dict[str, Any],
    ) -> None:
        credentials = dict(integration.credentials or {})
        credentials.update(token_payload)

        integration.credentials = credentials
        integration.access_token_expires_at = self._parse_token_expiry(
            credentials.get("expires_at") or credentials.get("expires_in")
        )
        integration.refresh_token_expires_at = self._parse_token_expiry(
            credentials.get("refresh_token_expires_at") or credentials.get("refresh_token_expires_in")
        )
        integration.scopes = self._parse_scopes(credentials, integration.scopes)

        metadata = dict(integration.connection_metadata or {})
        metadata["auth_backend"] = self._AUTH_BACKEND_CUSTOM
        metadata[self._PROVIDER_KEY_METADATA_KEY] = provider_key
        metadata[self._PROVIDER_CONFIG_KEY_METADATA_KEY] = f"custom-{integration_type.value}"
        token_type = credentials.get("token_type")
        if isinstance(token_type, str) and token_type:
            metadata[self._TOKEN_TYPE_METADATA_KEY] = token_type
        integration.connection_metadata = metadata

    async def handle_oauth_callback(self, code: str, state: str) -> dict[str, Any]:
        """Validate state, exchange tokens, and create or reactivate the integration."""
        try:
            state_data = copilot_oauth_service.decode_oauth_state(state)
        except Exception as e:
            logger.error("Invalid or expired OAuth state: %s", e)
            raise ValueError("Invalid or expired state parameter") from e

        try:
            user_id = UUID(str(state_data["sub"]))
            organisation_id = UUID(str(state_data["org"]))
            integration_type = IntegrationType(state_data["type"])
        except Exception as e:
            raise ValueError("State payload is missing required claims") from e

        connection_id = str(state_data.get("jti") or uuid.uuid4())
        provider = ProviderFactory.get_provider(integration_type)

        user = await self._fetch_user_from_master(user_id)
        if not user or user.organisation_id != organisation_id:
            logger.error("User mismatch in state token")
            raise ValueError("Invalid user state context")

        async for org_session in acquire_org_async_session(organisation_id):
            existing = await self._get_integration_by_connection_id(org_session, connection_id)
            if existing:
                return self._build_success_response(existing, reactivated=False)

        try:
            tokens = await provider.exchange_code(code)
            access_token = tokens.get("access_token")
            if not access_token:
                raise ValueError("Missing access_token from provider")

            user_info = await provider.get_user_info(str(access_token))
            integration_email = str(user_info.get("email") or "")
        except Exception as e:
            logger.error("Failed to exchange OAuth code for %s: %s", integration_type.value, e)
            raise

        async for org_session in acquire_org_async_session(organisation_id):
            reactivate_stmt = (
                select(KnowledgeIntegration)
                .where(
                    KnowledgeIntegration.created_by_user_id == user_id,  # type: ignore[arg-type]
                    KnowledgeIntegration.integration_type == integration_type,  # type: ignore[arg-type]
                )
                .order_by(desc(KnowledgeIntegration.updated_at))  # type: ignore[arg-type]
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
                )

        created_by_admin = user.user_role == UserAppRole.ADMIN
        integration_scope = (
            IntegrationScope.ORGANIZATION if created_by_admin else IntegrationScope.USER
        )
        return await self._create_new_integration(
            organisation_id=organisation_id,
            integration_type=integration_type,
            connection_id=connection_id,
            user_id=user_id,
            created_by_admin=created_by_admin,
            integration_scope=integration_scope,
            provider_key=provider.provider_key,
            tokens=tokens,
            integration_email=integration_email,
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
    ) -> dict[str, Any]:
        integration.connection_id = connection_id
        integration.is_active = True
        integration.last_sync_error = None
        integration.integration_email = integration_email
        self._apply_custom_token_payload(
            integration,
            integration_type=integration_type,
            provider_key=provider_key,
            token_payload=tokens,
        )

        session.add(integration)
        await session.commit()
        await session.refresh(integration)

        await self._save_connection_mapping(
            connection_id,
            integration,
            integration.connection_metadata.get("provider_config_key", f"custom-{integration_type.value}"),
        )
        return self._build_success_response(integration, reactivated=True)

    async def _create_new_integration(
        self,
        *,
        organisation_id: UUID,
        integration_type: IntegrationType,
        connection_id: str,
        user_id: UUID,
        created_by_admin: bool,
        integration_scope: IntegrationScope,
        provider_key: str,
        tokens: dict[str, Any],
        integration_email: str,
    ) -> dict[str, Any]:
        async for org_session in acquire_org_async_session(organisation_id):
            type_key = str(self._enum_value(integration_type))
            integration = KnowledgeIntegration(
                organisation_id=organisation_id,
                integration_type=integration_type,
                name=f"{type_key.replace('_', ' ').title()} Custom Integration",
                connection_id=connection_id,
                created_by_user_id=user_id,
                created_by_admin=created_by_admin,
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
            )

            org_session.add(integration)
            await org_session.commit()
            await org_session.refresh(integration)

            await self._save_connection_mapping(
                connection_id,
                integration,
                integration.connection_metadata.get("provider_config_key", f"custom-{type_key}"),
            )
            return self._build_success_response(integration, reactivated=False)

        raise RuntimeError("Failed to acquire organisation session")

    @staticmethod
    async def _get_integration_by_connection_id(
        session: Any,
        connection_id: str,
    ) -> KnowledgeIntegration | None:
        stmt = select(KnowledgeIntegration).where(
            KnowledgeIntegration.connection_id == connection_id  # type: ignore[arg-type]
        )
        result = await session.execute(stmt)
        return result.scalars().first()

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
            "created_by_admin": integration.created_by_admin,
            "integration_email": integration.integration_email,
            "reactivated": reactivated,
        }

    async def _save_connection_mapping(
        self,
        connection_id: str,
        integration: KnowledgeIntegration,
        provider_config_key: str,
    ) -> None:
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
