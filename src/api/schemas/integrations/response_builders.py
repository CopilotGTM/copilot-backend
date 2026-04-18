"""Helpers for projecting integration models into API response shapes."""

from __future__ import annotations

from typing import Any

from src.api.schemas.integrations.integration_schemas import IntegrationResponse
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.scope_validation import (
    get_missing_required_scopes,
    get_missing_scope_labels,
)

_ACTION_NONE = "none"
_ACTION_REAUTH_EXPIRED = "reauth_expired"
_ACTION_ADDITIONAL_PERMISSIONS = "additional_permissions"


def _normalize_integration_type(integration_type: IntegrationType | str) -> IntegrationType:
    return (
        integration_type
        if isinstance(integration_type, IntegrationType)
        else IntegrationType(integration_type)
    )


def build_integration_response(integration: Any) -> IntegrationResponse:
    """Build an API response with derived reconnect/scope-drift state."""
    integration_type = _normalize_integration_type(integration.integration_type)
    requires_reauth = bool(getattr(integration, "requires_reauth", False))
    missing_scopes = sorted(
        get_missing_required_scopes(integration_type, getattr(integration, "scopes", None))
    )
    missing_scope_labels = get_missing_scope_labels(set(missing_scopes))

    action_required = _ACTION_NONE
    action_message: str | None = None

    if requires_reauth:
        action_required = _ACTION_REAUTH_EXPIRED
        action_message = "Integration expired. Please reconnect."
    elif missing_scopes:
        action_required = _ACTION_ADDITIONAL_PERMISSIONS
        action_message = "Additional permissions are required. Please reconnect."

    return IntegrationResponse(
        integration_id=integration.integration_id,
        organisation_id=integration.organisation_id,
        created_by_user_id=integration.created_by_user_id,
        integration_email=getattr(integration, "integration_email", None),
        integration_scope=integration.integration_scope,
        integration_type=integration_type,
        name=integration.name,
        sync_frequency=integration.sync_frequency,
        auto_sync_enabled=integration.auto_sync_enabled,
        is_active=integration.is_active,
        last_synced_at=getattr(integration, "last_synced_at", None),
        last_sync_status=getattr(integration, "last_sync_status", None),
        last_sync_error=getattr(integration, "last_sync_error", None),
        access_token_expires_at=getattr(integration, "access_token_expires_at", None),
        connection_metadata=getattr(integration, "connection_metadata", {}) or {},
        created_at=integration.created_at,
        updated_at=integration.updated_at,
        requires_reauth=requires_reauth,
        action_required=action_required,
        action_message=action_message,
        missing_scopes=missing_scopes,
        missing_scope_labels=missing_scope_labels,
    )
