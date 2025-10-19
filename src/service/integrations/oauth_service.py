from __future__ import annotations

from typing import Any

from src.api.dependencies.organisation_dependencies import UserIDs
from src.api.schemas.integrations import OAuthInitiateResponse
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.integrations_metadata import (
    AvailabilityStatus,
    IntegrationMetadata,
    get_integration_metadata,
)
from src.infra.config.nango_provider_mapping import (
    to_nango_connect_integration_key,
    to_nango_provider_key,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)


class IntegrationUnavailableError(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class OAuthIntegrationService:
    """Service for OAuth initiation via Nango Connect Session."""

    _ALLOWED_AVAILABILITY_STATUSES = {AvailabilityStatus.AVAILABLE, AvailabilityStatus.BETA}

    @staticmethod
    def _get_validated_metadata(integration_type: IntegrationType) -> IntegrationMetadata:
        metadata = get_integration_metadata(integration_type)
        if not metadata:
            raise ValueError(f"Unknown integration type: {integration_type}")
        return metadata

    @classmethod
    def _ensure_integration_available(cls, metadata: IntegrationMetadata) -> None:
        if metadata.availability_status not in cls._ALLOWED_AVAILABILITY_STATUSES:
            raise IntegrationUnavailableError(metadata.availability_status)

    @staticmethod
    def _extract_session_details(session_data: dict[str, Any]) -> tuple[str, str | None]:
        raw = session_data.get("data") if isinstance(session_data, dict) else None
        payload = raw if isinstance(raw, dict) else session_data

        token = payload.get("token") or (payload.get("session") or {}).get("token")
        expires_at = payload.get("expires_at") or (payload.get("session") or {}).get("expires_at")

        if not token:
            logger.error(
                "Nango connect session created but token missing. Payload: %s", session_data
            )
            raise ValueError("Failed to retrieve session token")

        return token, expires_at

    @staticmethod
    def _extract_string_message(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _extract_message_from_dict(data: dict) -> str | None:
        return OAuthIntegrationService._extract_string_message(data.get("message"))

    @staticmethod
    def _extract_message_from_error_list(errors: list) -> str | None:
        for entry in errors:
            if isinstance(entry, dict):
                msg = OAuthIntegrationService._extract_message_from_dict(entry)
                if msg:
                    return msg
            else:
                msg = OAuthIntegrationService._extract_string_message(entry)
                if msg:
                    return msg
        return None

    @staticmethod
    def _extract_message_from_error_block(error_block: dict) -> str | None:
        msg = OAuthIntegrationService._extract_message_from_dict(error_block)
        if msg:
            return msg
        errors = error_block.get("errors")
        if isinstance(errors, list):
            return OAuthIntegrationService._extract_message_from_error_list(errors)
        return None

    @staticmethod
    def extract_nango_error_message(payload: Any) -> str | None:
        if isinstance(payload, dict):
            error_block = payload.get("error")
            if isinstance(error_block, dict):
                msg = OAuthIntegrationService._extract_message_from_error_block(error_block)
                if msg:
                    return msg
            msg = OAuthIntegrationService._extract_message_from_error_block(payload)
            if msg:
                return msg
        if isinstance(payload, list):
            return OAuthIntegrationService._extract_message_from_error_list(payload)
        return OAuthIntegrationService._extract_string_message(payload)

    async def create_connect_session(
        self,
        *,
        user_ids: UserIDs,
        user: User,
        integration_type: IntegrationType,
    ) -> OAuthInitiateResponse:
        metadata = self._get_validated_metadata(integration_type)
        self._ensure_integration_available(metadata)

        nango_provider_key = to_nango_provider_key(integration_type)
        nango_integration_key = to_nango_connect_integration_key(integration_type)

        nango = NangoClient()
        session_data = await nango.create_connect_session(
            end_user_id=str(user_ids.user_id),
            organization_id=str(user_ids.organisation_id),
            integration_id=nango_integration_key,
            end_user_email=user.email,
            end_user_display_name=f"{user.first_name} {user.last_name}",
        )

        logger.info(
            "Created Connect Session for user %s, org %s, provider %s (nango_provider=%s, nango_integration=%s)",
            user_ids.user_id,
            user_ids.organisation_id,
            integration_type.value,
            nango_provider_key,
            nango_integration_key,
        )

        token, expires_at = self._extract_session_details(session_data)
        return OAuthInitiateResponse(
            session_token=token,
            integration_type=integration_type,
            expires_at=expires_at,
        )
