"""Service to process Nango webhooks (auth + sync)."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.api.dependencies.organisation_dependencies import (
    acquire_org_async_session,
)
from src.api.utils.http_response import Response
from src.domain.models.master_tables import (
    OrganisationIntegrationDetails,
    User,
    UserAppRole,
)
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
    IntegrationType,
    KnowledgeIntegration,
)
from src.infra.config.nango_provider_mapping import from_nango_provider_key
from src.infra.config.provider_categories import categorize_integration_type
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient

logger = get_logger(__name__)

# Constants for webhook responses
SYNC_WEBHOOK_ACCEPTED_MSG = "Sync webhook accepted"


class NangoWebhookService:
    """Encapsulates Nango webhook processing (auth + sync)."""

    @staticmethod
    def _is_valid_signature(signature: str | None, payload: dict[str, Any]) -> bool:
        """Validate webhook signature using Nango client utility."""
        if NangoClient.verify_webhook_signature(signature, payload):
            return True
        logger.error("Invalid webhook signature. Header: %s", signature)
        return False

    @staticmethod
    def _enum_value(value: Any) -> Any:
        """Get the enum value or return the original value."""
        return value.value if isinstance(value, Enum) else value

    async def _get_connection_mapping(
        self, connection_uuid: UUID
    ) -> OrganisationIntegrationDetails | None:
        """Get OrganisationIntegrationDetails mapping for a given connection UUID."""
        async for master_session in acquire_master_async_session():
            stmt = select(OrganisationIntegrationDetails).where(
                OrganisationIntegrationDetails.connection_id == connection_uuid
            )
            result = await master_session.execute(stmt)
            mapping = result.scalar_one_or_none()
            if mapping:
                return mapping
        return None

    async def _upsert_connection_mapping(
        self,
        connection_uuid: UUID,
        organisation_id: UUID,
        integration_id: UUID,
        provider_config_key: str,
    ) -> None:
        async for master_session in acquire_master_async_session():
            stmt = select(OrganisationIntegrationDetails).where(
                OrganisationIntegrationDetails.connection_id == connection_uuid
            )
            result = await master_session.execute(stmt)
            mapping = result.scalar_one_or_none()

            if mapping:
                updated = False
                if mapping.organisation_id != organisation_id:
                    mapping.organisation_id = organisation_id
                    updated = True
                if mapping.integration_id != integration_id:
                    mapping.integration_id = integration_id
                    updated = True
                if mapping.provider_config_key != provider_config_key:
                    mapping.provider_config_key = provider_config_key
                    updated = True
                if updated:
                    master_session.add(mapping)
                    await master_session.commit()
                return

            master_session.add(
                OrganisationIntegrationDetails(
                    connection_id=connection_uuid,
                    organisation_id=organisation_id,
                    integration_id=integration_id,
                    provider_config_key=provider_config_key,
                )
            )
            await master_session.commit()
            return

    async def _handle_sync_webhook(
        self, payload: dict[str, Any], organisation_id: UUID, integration_id: UUID
    ) -> Response:
        """
        Handle sync webhook - triggers data sync for the given connection.
        Args:
            payload: The webhook payload containing connection details.
        Returns:
            Response indicating acceptance of the sync webhook.
        """

        # Extract necessary fields from payload
        connection_id = payload.get("connectionId")
        provider_config_key = payload.get("providerConfigKey")
        logger.info("Processing sync webhook: %s", payload)

        # Validate required fields
        integration: KnowledgeIntegration | None = None
        async for org_session in acquire_org_async_session(organisation_id):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.connection_id == connection_id
            )
            result = await org_session.execute(stmt)
            integration = result.scalars().first()
            if not integration:
                logger.error(
                    "No integration found for sync webhook: connection %s, integration %s",
                    connection_id,
                    integration_id,
                )
                return Response.bad_request_error_response(
                    message="No matching integration found for sync webhook"
                )

        # Resolve Category and provider of the sync request
        category, provider = categorize_integration_type(integration.integration_type)
        logger.info(
            "Sync webhook: resolved category=%s, provider=%s for integration %s",
            category,
            provider,
            integration.integration_id,
        )

        # Resolve organisation ID and validate mapping
        sync_name = payload.get("syncName")
        sync_type = payload.get("syncType")
        sync_variant = payload.get("syncVariant")
        logger.info(
            "Sync webhook: determined data types %s for integration %s (category=%s)",
            [sync_name, sync_type, sync_variant],
            integration.integration_id,
            category,
        )

        # Import here to avoid circular import
        from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task

        sync_integration_data_task.delay(
            organisation_id=str(organisation_id),
            integration_id=str(integration.integration_id),
            category=category,
            provider=provider,
            provider_config_key=provider_config_key,
            sync_name=sync_name,
            sync_type=sync_type,
            sync_variant=sync_variant,
            incremental=True,
        )
        return Response.success_response(message=SYNC_WEBHOOK_ACCEPTED_MSG, data=None)

    async def _handle_authorization_webhook(self, payload: dict[str, Any]) -> Response:
        """Handle authorization webhook - delegates to create or update flow."""
        logger.info("Processing authorization webhook: %s", payload)
        connection_id: str = payload.get("connectionId")
        provider_config_key: str = payload.get("providerConfigKey")

        if not connection_id or not provider_config_key:
            logger.error("Missing required fields in webhook: %s", payload)
            return Response.bad_request_error_response(
                message="Missing required fields: connectionId or providerConfigKey"
            )

        end_user = payload.get("endUser", {})
        user_id: UUID = UUID(end_user.get("endUserId"))
        tags: dict[str, Any] = end_user.get("tags", {}) if isinstance(end_user, dict) else {}
        organization_id: UUID = tags.get("organizationId") if isinstance(tags, dict) else None

        if not user_id or not organization_id:
            logger.error("Missing user identifiers in webhook: %s", payload)
            return Response.bad_request_error_response(
                message="Missing user identifiers in endUser"
            )

        async for org_session in acquire_org_async_session(organization_id):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.connection_id == connection_id
            )
            existing = await org_session.execute(stmt)
            existing = existing.scalars().first()

            logger.info("Found existing integration: %s", existing)
            if existing:
                logger.info("Existing integration ID: %s", existing.integration_id)
                return Response.success_response(
                    message="Connection already associated with an integration",
                    data={"integrationId": existing.integration_id},
                )

        return await self._handle_new_integration_auth(
            connection_id,
            provider_config_key,
            user_id,
            organization_id,
        )

    async def _handle_new_integration_auth(
        self,
        connection_id: str,
        provider_config_key: str,
        user_id: UUID,
        organization_id: UUID,
    ) -> Response:
        """Handle authorization webhook for new integration creation."""
        try:
            integration_type = from_nango_provider_key(provider_config_key)
            user = await self._fetch_user_from_master(user_id)

            if not user:
                logger.error("User %s not found in master database", user_id)
                return Response.bad_request_error_response(message="User not found")

            logger.info("User found: %s, org=%s", user.email, user.organisation_id)

            # Verify organization ID matches user's organization
            created_by_admin = user.user_role == UserAppRole.ADMIN
            integration_scope = (
                IntegrationScope.ORGANIZATION if created_by_admin else IntegrationScope.USER
            )

            integration = await self._create_integration_record(
                user.organisation_id,
                integration_type,
                connection_id,
                user_id,
                created_by_admin,
                integration_scope,
                provider_config_key,
            )

            if not integration:
                logger.error(
                    "Failed to create integration for connection %s (org=%s)",
                    connection_id,
                    organization_id,
                )
                return Response.success_response(
                    message="Webhook received but processing failed",
                    data={"error": "Integration creation failed"},
                )

            logger.info(
                "Created integration %s for user %s (role=%s, admin=%s, scope=%s) in org %s via webhook",
                integration.integration_id,
                user_id,
                self._enum_value(user.user_role),
                created_by_admin,
                self._enum_value(integration_scope),
                organization_id,
            )

            await self._save_connection_mapping(connection_id, integration, provider_config_key)
            return Response.success_response(
                message="Webhook processed successfully",
                data={
                    "integration_id": str(integration.integration_id),
                    "integration_type": self._enum_value(integration.integration_type),
                    "connection_id": connection_id,
                    "integration_scope": self._enum_value(integration.integration_scope),
                    "created_by_admin": integration.created_by_admin,
                    "integration_email": integration.integration_email,
                },
            )
        except ValueError:
            logger.error("Unknown provider config key: %s", provider_config_key)
            return Response.bad_request_error_response(
                message=f"Unknown integration type: {provider_config_key}"
            )

    async def _save_connection_mapping(
        self,
        connection_id: str,
        integration: KnowledgeIntegration,
        provider_config_key: str,
    ) -> None:
        """Save connection mapping to OrganisationIntegrationDetails if connection_id is valid UUID."""
        try:
            connection_uuid = UUID(connection_id)
        except ValueError:
            return

        await self._upsert_connection_mapping(
            connection_uuid,
            integration.organisation_id,
            integration.integration_id,
            provider_config_key,
        )

    @staticmethod
    async def _fetch_user_from_master(user_uuid: UUID) -> User | None:
        """Fetch User from master database by UUID."""
        async for master_session in acquire_master_async_session():
            statement = select(User).where(User.user_id == user_uuid)
            result = await master_session.execute(statement)
            return result.scalar_one_or_none()
        return None

    async def _create_integration_record(
        self,
        organisation_id: UUID,
        integration_type: IntegrationType,
        connection_id: str,
        created_by_user_id: UUID,
        created_by_admin: bool,
        integration_scope: IntegrationScope,
        provider_config_key: str,
    ) -> KnowledgeIntegration | None:
        """
        Create a new KnowledgeIntegration record in the organisation database.
        Args:
            organisation_id: UUID of the organisation
            integration_type: Type of the integration
            connection_id: Nango connection ID
            created_by_user_id: UUID of the user who created the integration
            created_by_admin: Whether the creator is an admin
            integration_scope: Scope of the integration (ORGANIZATION or USER)
            provider_config_key: Key of the provider configuration
        Returns:
            KnowledgeIntegration instance if created successfully, None otherwise
        """
        nango = NangoClient()
        try:
            # Fetch connection details from Nango
            nango_connection = await nango.get_connection(
                provider=provider_config_key, connection_id=connection_id
            )
            connection_metadata = nango_connection.get("connection_metadata", {})

            # Create integration record directly in the org database
            async for org_session in acquire_org_async_session(organisation_id):
                type_key = str(self._enum_value(integration_type))
                integration = KnowledgeIntegration(
                    organisation_id=organisation_id,
                    integration_type=integration_type,
                    name=f"{type_key.replace('_', ' ').title()} Integration",
                    connection_id=connection_id,
                    created_by_user_id=created_by_user_id,
                    created_by_admin=created_by_admin,
                    integration_scope=integration_scope,
                    integration_email="",
                    connection_metadata=connection_metadata,
                    credentials={},  # Credentials managed by Nango
                    is_active=True,
                )

                org_session.add(integration)
                await org_session.commit()
                await org_session.refresh(integration)

                logger.info(
                    "Created integration record %s for org %s (type=%s, connection_id=%s)",
                    integration.integration_id,
                    organisation_id,
                    integration_type,
                    connection_id,
                )
                return integration

        finally:
            await nango.close()

        return None


# Convenience singleton
webhook_service = NangoWebhookService()
