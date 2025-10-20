"""Service to process Nango webhooks (auth + sync)."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.api.dependencies.organisation_dependencies import acquire_org_async_session
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
from src.service.integrations import IntegrationService, NangoClient

logger = get_logger(__name__)

# Constants for webhook responses
SYNC_WEBHOOK_ACCEPTED_MSG = "Sync webhook accepted"


class NangoWebhookService:
    """Encapsulates Nango webhook processing (auth + sync)."""

    async def handle_webhook(self, payload: dict[str, Any], signature: str | None) -> Response:
        try:
            logger.info("NANGO WEBHOOK RECEIVED")
            logger.info(
                "Connection ID: %s, Webhook Type: %s , Provider Config Key: %s",
                payload.get("connectionId"),
                payload.get("type"),
                payload.get("providerConfigKey"),
            )

            if not self._is_valid_signature(signature, payload):
                return Response.unauthorized_error_response(message="Invalid webhook signature")

            if not payload.get("success"):
                logger.warning("Received failed authorization webhook: %s", payload)
                return Response.success_response(
                    message="Webhook received (failed authorization)", data=None
                )

            webhook_type = payload.get("type")

            match webhook_type:
                case "sync":
                    return await self._handle_sync_webhook(payload)
                case "auth":
                    return await self._handle_authorization_webhook(payload)
                case _:
                    return Response.success_response(
                        message="Webhook received but unhandled type", data=None
                    )

        except Exception as exc:
            logger.exception(
                "Webhook processing failed (type=%s, connectionId=%s, providerConfigKey=%s)",
                payload.get("type"),
                payload.get("connectionId"),
                payload.get("providerConfigKey"),
            )
            return Response.success_response(
                message="Webhook received but processing failed",
                data={"error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Internal helpers

    @staticmethod
    def _is_valid_signature(signature: str | None, payload: dict[str, Any]) -> bool:
        if NangoClient.verify_webhook_signature(signature, payload):
            return True
        logger.error("Invalid webhook signature. Header: %s", signature)
        return False

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    async def _get_connection_mapping(
        self, connection_uuid: UUID
    ) -> OrganisationIntegrationDetails | None:
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

    async def _fetch_org_id_from_nango(
        self, connection_id: str, provider_config_key: str
    ) -> str | None:
        nango = NangoClient()
        try:
            nango_connection = await nango.get_connection(
                provider=provider_config_key, connection_id=connection_id
            )
        finally:
            await nango.close()

        if not isinstance(nango_connection, dict):
            return None

        end_user = nango_connection.get("end_user", {})
        if not isinstance(end_user, dict):
            return None

        tags = end_user.get("tags", {})
        if not isinstance(tags, dict):
            return None

        org_id_str = tags.get("organizationId") or tags.get("organisationId")
        if org_id_str:
            logger.info(
                "Derived organisation %s from Nango connection %s via tags",
                org_id_str,
                connection_id,
            )
        return org_id_str

    async def _handle_sync_webhook(self, payload: dict[str, Any]) -> Response:
        connection_id = payload.get("connectionId")
        provider_config_key = payload.get("providerConfigKey")

        if not connection_id or not provider_config_key:
            logger.error("Sync webhook missing required fields: %s", payload)
            return Response.bad_request_error_response(
                message="Missing required fields: connectionId or providerConfigKey"
            )

        try:
            org_id_str, integration = await self._resolve_integration_for_sync(
                connection_id, provider_config_key
            )
        except Exception as exc:
            logger.error(
                "Sync webhook: failed to resolve integration for connection %s: %s",
                connection_id,
                str(exc),
                exc_info=True,
            )
            return Response.success_response(message=SYNC_WEBHOOK_ACCEPTED_MSG, data=None)

        if not org_id_str:
            logger.error("Sync webhook: organisation not found for connection %s", connection_id)
            return Response.success_response(message=SYNC_WEBHOOK_ACCEPTED_MSG, data=None)

        if not integration:
            logger.warning(
                "Sync webhook: integration not found for connection %s (org=%s)",
                connection_id,
                org_id_str,
            )
            return Response.success_response(message=SYNC_WEBHOOK_ACCEPTED_MSG, data=None)

        category, provider = categorize_integration_type(integration.integration_type)
        data_types = self._determine_sync_data_types(category, payload.get("syncName"))

        if not data_types:
            logger.info(
                "Sync webhook: no data types determined for integration %s (category=%s)",
                integration.integration_id,
                category,
            )
            return Response.success_response(message=SYNC_WEBHOOK_ACCEPTED_MSG, data=None)

        from src.workers.integrations_worker.tasks.sync_tasks import sync_integration_data_task

        sync_integration_data_task.delay(
            organisation_id=org_id_str,
            integration_id=str(integration.integration_id),
            category=category,
            provider=provider,
            provider_config_key=provider_config_key,
            data_types=data_types,
            incremental=True,
            sync_name=payload.get("syncName"),
        )

        logger.info(
            "Sync webhook enqueued: org=%s int=%s cat=%s prov=%s types=%s",
            org_id_str,
            integration.integration_id,
            category,
            provider,
            data_types,
        )
        return Response.success_response(message=SYNC_WEBHOOK_ACCEPTED_MSG, data=None)

    async def _resolve_integration_for_sync(
        self, connection_id: str, provider_config_key: str
    ) -> tuple[str | None, KnowledgeIntegration | None]:
        try:
            connection_uuid = UUID(connection_id)
        except ValueError:
            logger.error("Invalid connection_id received: %s", connection_id)
            return None, None

        mapping = await self._get_connection_mapping(connection_uuid)

        org_id_str = str(mapping.organisation_id) if mapping else None
        mapped_integration_id = mapping.integration_id if mapping else None

        org_id_nango = await self._fetch_org_id_from_nango(connection_id, provider_config_key)

        if org_id_nango and org_id_str and org_id_nango != org_id_str:
            logger.warning(
                "Organisation ID mismatch for connection %s: mapping=%s, nango=%s",
                connection_id,
                org_id_str,
                org_id_nango,
            )
            return None, None

        integration: KnowledgeIntegration | None = None
        async for org_session in acquire_org_async_session(UUID(org_id_str)):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.connection_id == connection_id
            )
            if mapped_integration_id:
                stmt = stmt.where(KnowledgeIntegration.integration_id == mapped_integration_id)
            result = await org_session.execute(stmt)
            integration = result.scalars().first()
            if integration:
                break

        if integration:
            await self._upsert_connection_mapping(
                connection_uuid,
                integration.organisation_id,
                integration.integration_id,
                provider_config_key,
            )

        return org_id_str, integration

    @staticmethod
    def _determine_sync_data_types(category: str, sync_name: Any) -> list[str]:
        if category == "calendar":
            return ["events"]
        if isinstance(sync_name, str) and sync_name.strip():
            return [sync_name]
        return []

    async def _find_existing_integration(
        self, organisation_uuid: UUID, connection_id: str
    ) -> KnowledgeIntegration | None:
        """Find existing integration by connection_id."""
        async for org_session in acquire_org_async_session(organisation_uuid):
            stmt = select(KnowledgeIntegration).where(
                KnowledgeIntegration.connection_id == connection_id
            )
            res = await org_session.execute(stmt)
            return res.scalars().first()
        return None

    async def _handle_authorization_webhook(self, payload: dict[str, Any]) -> Response:
        """Handle authorization webhook - delegates to create or update flow."""
        connection_id = payload.get("connectionId")
        provider_config_key = payload.get("providerConfigKey")

        if not connection_id or not provider_config_key:
            logger.error("Missing required fields in webhook: %s", payload)
            return Response.bad_request_error_response(
                message="Missing required fields: connectionId or providerConfigKey"
            )

        end_user = payload.get("endUser", {})
        user_id_value = end_user.get("endUserId") if isinstance(end_user, dict) else None
        tags = end_user.get("tags", {}) if isinstance(end_user, dict) else {}
        organization_id = tags.get("organizationId") if isinstance(tags, dict) else None

        if not user_id_value or not organization_id:
            logger.error("Missing user identifiers in webhook: %s", payload)
            return Response.bad_request_error_response(
                message="Missing user identifiers in endUser"
            )

        organisation_uuid = UUID(organization_id)
        existing = await self._find_existing_integration(organisation_uuid, connection_id)

        if not existing:
            return await self._handle_new_integration_auth(
                connection_id,
                provider_config_key,
                user_id_value,
                organization_id,
                organisation_uuid,
            )
        else:
            return await self._handle_existing_integration_auth(
                existing,
                connection_id,
                provider_config_key,
                organization_id,
                organisation_uuid,
            )

    async def _handle_new_integration_auth(
        self,
        connection_id: str,
        provider_config_key: str,
        user_id_value: str,
        organization_id: str,
        organisation_uuid: UUID,
    ) -> Response:
        """Handle authorization webhook for new integration creation."""
        try:
            integration_type = from_nango_provider_key(provider_config_key)
        except ValueError:
            logger.error("Unknown provider config key: %s", provider_config_key)
            return Response.bad_request_error_response(
                message=f"Unknown integration type: {provider_config_key}"
            )

        user_uuid = UUID(user_id_value)
        logger.info("Querying user %s from master database...", user_id_value)
        user = await self._fetch_user_from_master(user_uuid)

        if not user:
            logger.error("User %s not found in master database", user_id_value)
            return Response.bad_request_error_response(message="User not found")

        logger.info(
            "User found: %s, role=%s, org=%s",
            user.email,
            user.user_role,
            user.organisation_id,
        )

        created_by_admin = user.user_role == UserAppRole.ADMIN
        integration_scope = (
            IntegrationScope.ORGANIZATION if created_by_admin else IntegrationScope.USER
        )

        logger.info(
            "Access control: created_by_admin=%s, scope=%s",
            created_by_admin,
            self._enum_value(integration_scope),
        )

        integration = await self._create_integration_record(
            organisation_uuid,
            integration_type,
            connection_id,
            user_uuid,
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
            user_id_value,
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

    async def _handle_existing_integration_auth(
        self,
        existing: KnowledgeIntegration,
        connection_id: str,
        provider_config_key: str,
        organization_id: str,
        organisation_uuid: UUID,
    ) -> Response:
        """Handle authorization webhook for existing integration update."""
        try:
            integration_type = from_nango_provider_key(provider_config_key)
        except ValueError:
            logger.error("Unknown provider config key: %s", provider_config_key)
            return Response.bad_request_error_response(
                message=f"Unknown integration type: {provider_config_key}"
            )

        nango = NangoClient()
        try:
            metadata, email = await self._get_connection_details(
                nango,
                integration_type,
                connection_id,
                provider_config_key,
            )
        finally:
            await nango.close()

        # Update in-place if anything changed
        async for org_session in acquire_org_async_session(organisation_uuid):
            changed = False
            if email and existing.integration_email != email:
                existing.integration_email = email
                changed = True
            if metadata and metadata != (existing.connection_metadata or {}):
                existing.connection_metadata = metadata
                changed = True
            if changed:
                org_session.add(existing)
                await org_session.commit()
                await org_session.refresh(existing)

        logger.info(
            "Authorization webhook processed for existing integration %s (org=%s)",
            existing.integration_id,
            organization_id,
        )

        await self._save_connection_mapping(connection_id, existing, provider_config_key)

        return Response.success_response(
            message="Webhook processed successfully",
            data={
                "integration_id": str(existing.integration_id),
                "integration_type": self._enum_value(integration_type),
                "connection_id": connection_id,
                "integration_scope": self._enum_value(existing.integration_scope),
                "created_by_admin": existing.created_by_admin,
                "integration_email": existing.integration_email,
            },
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
        async for org_session in acquire_org_async_session(organisation_id):
            nango = NangoClient()
            try:
                integration_service = IntegrationService(
                    db_session=org_session,
                    organisation_id=organisation_id,
                    nango_client=nango,
                )

                connection_metadata, integration_email = await self._get_connection_details(
                    nango,
                    integration_type,
                    connection_id,
                    provider_config_key,
                )

                type_key = str(self._enum_value(integration_type))
                integration = await integration_service.create_integration(
                    integration_type=integration_type,
                    name=f"{type_key.replace('_', ' ').title()} Integration",
                    connection_id=connection_id,
                    created_by_user_id=created_by_user_id,
                    created_by_admin=created_by_admin,
                    integration_scope=integration_scope,
                    integration_email=integration_email,
                    connection_metadata=connection_metadata,
                )
                return integration
            finally:
                await nango.close()

        return None

    async def _get_connection_details(
        self,
        nango: NangoClient,
        integration_type: IntegrationType,
        connection_id: str,
        provider_config_key: str,
    ) -> tuple[dict[str, Any], str | None]:
        try:
            logger.info(
                "Fetching Nango connection: provider=%s, connection_id=%s",
                provider_config_key,
                connection_id,
            )

            nango_connection = await nango.get_connection(
                provider=provider_config_key, connection_id=connection_id
            )

            logger.info("Full Nango connection response: %s", nango_connection)

            connection_metadata = (
                nango_connection.get("connection_metadata")
                or nango_connection.get("metadata")
                or {}
            )
            end_user = nango_connection.get("end_user", {})

            logger.info("Connection metadata: %s", connection_metadata)
            logger.info("End user data: %s", end_user)

            integration_email = self._extract_integration_email(
                integration_type, connection_metadata
            )

            if not integration_email and isinstance(end_user, dict):
                integration_email = end_user.get("email")
                logger.info("Email extracted from end_user: %s", integration_email)

            type_key = self._enum_value(integration_type)
            logger.info(
                "Final extracted integration_email='%s' from %s connection",
                integration_email,
                type_key,
            )

            return connection_metadata, integration_email

        except Exception as exc:
            logger.error(
                "Failed to get connection details from Nango for %s: %s. Continuing without integration_email.",
                self._enum_value(integration_type),
                str(exc),
                exc_info=True,
            )
            return {}, None

    @staticmethod
    def _extract_integration_email(
        integration_type: IntegrationType, connection_metadata: dict[str, Any]
    ) -> str | None:
        type_key = NangoWebhookService._enum_value(integration_type)
        logger.info(
            "Extracting email for %s from metadata keys: %s",
            type_key,
            list(connection_metadata.keys()),
        )

        if integration_type in [
            IntegrationType.GOOGLE_DRIVE,
            IntegrationType.GOOGLE_CALENDAR,
        ]:
            email = connection_metadata.get("email") or connection_metadata.get("user_email")
            logger.info("Google service email extraction: %s", email)
            return email

        if integration_type == IntegrationType.HUBSPOT:
            return connection_metadata.get("user") or connection_metadata.get("hub_email")

        if integration_type == IntegrationType.SLACK:
            user_info = connection_metadata.get("user", {})
            return user_info.get("email") if isinstance(user_info, dict) else None

        if integration_type == IntegrationType.SALESFORCE:
            return connection_metadata.get("email") or connection_metadata.get("username")

        if integration_type in [
            IntegrationType.SHAREPOINT,
            IntegrationType.ONEDRIVE,
            IntegrationType.OUTLOOK_CALENDAR,
        ]:
            return connection_metadata.get("email") or connection_metadata.get("userPrincipalName")

        if integration_type == IntegrationType.CALENDLY:
            user_info = connection_metadata.get("user", {})
            return (
                user_info.get("email")
                if isinstance(user_info, dict)
                else connection_metadata.get("email")
            )

        email = connection_metadata.get("email")
        logger.info("Generic fallback email extraction: %s", email)
        return email


# Convenience singleton
webhook_service = NangoWebhookService()
