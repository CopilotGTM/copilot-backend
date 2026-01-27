"""Service to process Nango auth webhooks.

NOTE: Sync webhooks have been deprecated and removed.
Sync is now handled by Celery Beat scheduled tasks.
See: docs/Technical Documentation/integration/sync_tasks/scheduled-sync-architecture.md
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
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
from src.infra.database.sessions import (
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.logger import get_logger
from src.service.integrations import NangoClient
from src.service.integrations.initial_sync_service import initial_sync_service

logger = get_logger(__name__)


class NangoWebhookService:
    """Encapsulates Nango webhook processing (auth only - sync webhooks deprecated)."""

    @staticmethod
    def _is_valid_signature(signature: str | None, payload: dict[str, Any]) -> bool:
        """Validate webhook signature using Nango client utility."""
        if signature and NangoClient.verify_webhook_signature(signature, payload):
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
                OrganisationIntegrationDetails.connection_id == connection_uuid  # type: ignore[arg-type]
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
                OrganisationIntegrationDetails.connection_id == connection_uuid  # type: ignore[arg-type]
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

    def _determine_integration_type(
        self, connection_id: str, provider_config_key: str
    ) -> IntegrationType:
        """
        Determine the correct IntegrationType based on provider_config_key.

        Handles the two separate Outlook integrations:
        - "outlook" → OUTLOOK_EMAIL (emails and folders syncs)
        - "outlook-bflm" → OUTLOOK_CALENDAR (events and calendars syncs)
        """
        from src.infra.config.nango_provider_mapping import from_nango_provider_key

        provider_key_lower = provider_config_key.lower()

        # Direct mapping for Outlook integrations
        if provider_key_lower == "outlook":
            logger.info(
                "Detected 'outlook' provider, mapping to OUTLOOK_EMAIL. Connection ID: %s",
                connection_id,
            )
            return IntegrationType.OUTLOOK_EMAIL

        if provider_key_lower == "outlook-bflm":
            logger.info(
                "Detected 'outlook-bflm' provider, mapping to OUTLOOK_CALENDAR. Connection ID: %s",
                connection_id,
            )
            return IntegrationType.OUTLOOK_CALENDAR

        # For all other providers, use the standard mapping
        return from_nango_provider_key(provider_config_key)

    async def _handle_authorization_webhook(self, payload: dict[str, Any]) -> JSONResponse:
        """
        Handle authorization webhook - reactivate existing or create new integration.

        When a user reconnects after disconnecting, we reuse the existing integration
        record to preserve historical data (calendar events, deal associations, etc.)
        instead of creating a new integration_id which would cause duplicate records.
        """
        logger.info("Processing authorization webhook: %s", payload)
        connection_id = payload.get("connectionId")
        provider_config_key = payload.get("providerConfigKey")

        if not connection_id or not provider_config_key:
            logger.error("Missing required fields in webhook: %s", payload)
            return Response.bad_request_error_response(
                message="Missing required fields: connectionId or providerConfigKey"
            )

        end_user = payload.get("endUser", {})
        end_user_id = end_user.get("endUserId") if isinstance(end_user, dict) else None
        user_id = UUID(end_user_id) if end_user_id else None
        tags: dict[str, Any] = end_user.get("tags", {}) if isinstance(end_user, dict) else {}
        tag_org_id = tags.get("organizationId") if isinstance(tags, dict) else None

        if not user_id:
            logger.error("Missing user identifiers in webhook: %s", payload)
            return Response.bad_request_error_response(
                message="Missing user identifiers in endUser"
            )

        user = await self._fetch_user_from_master(user_id)
        if not user:
            logger.error("User %s not found in master database", user_id)
            return Response.bad_request_error_response(message="User not found")

        organisation_id = user.organisation_id
        if tag_org_id:
            try:
                tag_org_uuid = UUID(tag_org_id)
                if tag_org_uuid != organisation_id:
                    logger.warning(
                        "Webhook org tag mismatch for user %s: tag=%s, master=%s",
                        user_id,
                        tag_org_uuid,
                        organisation_id,
                    )
            except ValueError:
                logger.warning(
                    "Webhook org tag is not a valid UUID for user %s: %s",
                    user_id,
                    tag_org_id,
                )

        # Determine integration type for reactivation lookup
        integration_type = self._determine_integration_type(connection_id, provider_config_key)

        async for org_session in acquire_org_async_session(organisation_id):
            # Step 1: Check if this exact connection_id already exists (idempotency)
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.connection_id == connection_id
            )
            existing_result = await org_session.execute(stmt)
            existing = existing_result.scalars().first()

            if existing:
                logger.info(
                    "Connection %s already associated with integration %s",
                    connection_id,
                    existing.integration_id,
                )
                return Response.success_response(
                    message="Connection already associated with an integration",
                    data={"integrationId": existing.integration_id},
                )

            # Step 2: Look for an existing integration to reactivate (same user + type)
            # This handles reconnection after disconnect - reuses integration_id to avoid duplicates
            reactivate_stmt = (
                select(KnowledgeIntegration)
                .where(
                    KnowledgeIntegration.created_by_user_id == user_id,  # type: ignore[arg-type]
                    KnowledgeIntegration.integration_type == integration_type,  # type: ignore[arg-type]
                )
                .order_by(desc(KnowledgeIntegration.updated_at))  # type: ignore[arg-type]
                .limit(1)
            )
            reactivatable_result = await org_session.execute(reactivate_stmt)
            reactivatable = reactivatable_result.scalars().first()

            if reactivatable:
                return await self._reactivate_integration(
                    org_session,
                    reactivatable,
                    connection_id,
                    provider_config_key,
                )

        # Step 3: No existing integration found - create new one
        return await self._handle_new_integration_auth(
            connection_id,
            provider_config_key,
            user_id,
            organisation_id,
        )

    async def _reactivate_integration(
        self,
        session,
        integration: KnowledgeIntegration,
        new_connection_id: str,
        provider_config_key: str,
    ) -> JSONResponse:
        """
        Reactivate an existing integration with a new OAuth connection.

        This preserves the integration_id so all historical synced data
        (calendar events, deal associations, meeting preps) remains linked.
        """
        old_connection_id = integration.connection_id
        was_active = integration.is_active

        # Fetch updated connection metadata from Nango
        nango = NangoClient()
        try:
            nango_connection = await nango.get_connection(
                provider=provider_config_key, connection_id=new_connection_id
            )
            connection_metadata = nango_connection.get("connection_metadata", {})
        except Exception as e:
            logger.warning("Failed to fetch connection metadata for reactivation: %s", str(e))
            connection_metadata = integration.connection_metadata or {}
        finally:
            await nango.close()

        # Update integration with new connection details
        integration.connection_id = new_connection_id
        integration.is_active = True
        integration.last_sync_error = None
        integration.connection_metadata = connection_metadata

        session.add(integration)
        await session.commit()
        await session.refresh(integration)

        logger.info(
            "Reactivated integration %s: connection_id %s → %s, was_active=%s",
            integration.integration_id,
            old_connection_id,
            new_connection_id,
            was_active,
        )

        # Update connection mapping in master database
        await self._save_connection_mapping(new_connection_id, integration, provider_config_key)

        # Trigger initial sync for the reactivated integration
        initial_sync_service.trigger_initial_sync(integration.organisation_id, integration)

        return Response.success_response(
            message="Integration reactivated successfully",
            data={
                "integration_id": str(integration.integration_id),
                "integration_type": self._enum_value(integration.integration_type),
                "connection_id": new_connection_id,
                "integration_scope": self._enum_value(integration.integration_scope),
                "reactivated": True,
                "previous_connection_id": old_connection_id,
            },
        )

    async def _handle_new_integration_auth(
        self,
        connection_id: str,
        provider_config_key: str,
        user_id: UUID,
        organisation_id: UUID,
    ) -> JSONResponse:
        """Handle authorization webhook for new integration creation."""
        try:
            # Special handling for outlook-bflm which is used for both calendar and email
            # We need to determine which type based on the provider config key
            integration_type = self._determine_integration_type(connection_id, provider_config_key)
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
                organisation_id,
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
                    organisation_id,
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
                organisation_id,
            )

            await self._save_connection_mapping(connection_id, integration, provider_config_key)

            # Trigger initial sync for the newly connected integration
            initial_sync_service.trigger_initial_sync(user.organisation_id, integration)

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
            statement = select(User).where(User.user_id == user_uuid)  # type: ignore[arg-type]
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
