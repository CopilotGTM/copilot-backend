"""Organisation preference helpers shared by admin and user-facing contracts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

CRM_PROVIDERS = {"copilotgtm", "hubspot", "salesforce"}
LEGACY_CRM_PROVIDER_ALIASES = {"copilot_gtm": "copilotgtm"}
INTERNAL_CRM_PROVIDER = "copilotgtm"
CHANNEL_SOURCES = {"slack", "whatsapp", "teams", "google_chat"}
DEFAULT_CHANNEL_SOURCES = ["slack", "whatsapp", "teams", "google_chat"]
CRM_WRITE_SCOPES = {
    "accounts:create",
    "accounts:update",
    "contacts:create",
    "contacts:update",
    "contacts:delete",
    "custom_fields:create",
    "custom_fields:update",
    "custom_fields:delete",
    "deal_participants:create",
    "deal_participants:update",
    "deal_participants:delete",
    "deals:create",
    "deals:update",
    "deals:delete",
    "leads:create",
    "leads:update",
    "leads:convert",
    "lead_forms:create",
    "lead_forms:update",
    "lead_forms:ingest",
    "lead_ingestion_keys:create",
    "lead_ingestion_keys:update",
    "lead_ingestion_keys:delete",
    "pipelines:create",
    "pipelines:update",
    "pipelines:delete",
    "tasks:create",
    "tasks:update",
    "tasks:delete",
}
DEFAULT_INTERNAL_CRM_WRITE_SCOPES = sorted(CRM_WRITE_SCOPES)


def normalize_crm_provider(provider: str | None) -> str:
    crm_provider = LEGACY_CRM_PROVIDER_ALIASES.get(provider or "", provider)
    return crm_provider if crm_provider in CRM_PROVIDERS else INTERNAL_CRM_PROVIDER


def derive_crm_settings(
    provider: str | None,
    allowed_scopes: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Return canonical CRM governance settings for a configured CRM provider."""
    crm_provider = normalize_crm_provider(provider)
    is_internal_provider = crm_provider == INTERNAL_CRM_PROVIDER
    if not is_internal_provider:
        normalized_scopes: list[str] = []
    elif allowed_scopes is None:
        normalized_scopes = list(DEFAULT_INTERNAL_CRM_WRITE_SCOPES)
    else:
        normalized_scopes = sorted(
            {
                scope
                for scope in allowed_scopes
                if isinstance(scope, str) and scope in CRM_WRITE_SCOPES
            }
        )
    return {
        "mode": "internal" if is_internal_provider else "external",
        "provider": crm_provider,
        "write_policy": {
            "allowed_scopes": normalized_scopes,
        },
    }


def _crm_write_scopes(crm_settings: dict[str, Any]) -> set[str]:
    write_policy = crm_settings.get("write_policy")
    provider = normalize_crm_provider(
        crm_settings.get("provider") if isinstance(crm_settings.get("provider"), str) else None
    )
    if not isinstance(write_policy, dict):
        return (
            set(DEFAULT_INTERNAL_CRM_WRITE_SCOPES) if provider == INTERNAL_CRM_PROVIDER else set()
        )
    raw_scopes = write_policy.get("allowed_scopes")
    if not isinstance(raw_scopes, list):
        return (
            set(DEFAULT_INTERNAL_CRM_WRITE_SCOPES) if provider == INTERNAL_CRM_PROVIDER else set()
        )
    return {scope for scope in raw_scopes if isinstance(scope, str) and scope in CRM_WRITE_SCOPES}


def derive_capabilities(crm_settings: dict[str, Any]) -> dict[str, bool]:
    crm_write_scopes = _crm_write_scopes(crm_settings)
    return {
        "can_create_deals": "deals:create" in crm_write_scopes,
        "can_edit_deals": "deals:update" in crm_write_scopes,
        "can_create_leads": "leads:create" in crm_write_scopes,
        "can_edit_leads": "leads:update" in crm_write_scopes,
        "can_manage_lead_forms": bool(
            {"lead_forms:create", "lead_forms:update"} & crm_write_scopes
        ),
    }


def _clean_enabled_channels(sources: Iterable[Any] | None) -> list[str]:
    if sources is None:
        return list(DEFAULT_CHANNEL_SOURCES)

    cleaned: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, str):
            continue
        if source not in CHANNEL_SOURCES or source in seen:
            continue
        cleaned.append(source)
        seen.add(source)
    return cleaned


def normalize_organisation_preferences(preferences: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize app-owned preference namespaces while preserving unrelated keys."""
    root = dict(preferences or {})
    crm = root.get("crm") if isinstance(root.get("crm"), dict) else {}
    channels = root.get("channels") if isinstance(root.get("channels"), dict) else {}
    integrations = root.get("integrations") if isinstance(root.get("integrations"), dict) else {}
    provider = crm.get("provider") or crm.get("source")
    write_policy = crm.get("write_policy") if isinstance(crm.get("write_policy"), dict) else {}
    raw_allowed_scopes = write_policy.get("allowed_scopes")
    enabled_channels = channels.get("enabled")
    if enabled_channels is None:
        enabled_channels = integrations.get("enabled_sources")

    root["crm"] = derive_crm_settings(
        str(provider) if provider else None,
        raw_allowed_scopes if isinstance(raw_allowed_scopes, list) else None,
    )
    root["channels"] = {"enabled": _clean_enabled_channels(enabled_channels)}
    root["capabilities"] = derive_capabilities(root["crm"])
    if isinstance(integrations, dict) and set(integrations) <= {"enabled_sources"}:
        root.pop("integrations", None)
    return root


def merge_organisation_app_settings(
    preferences: dict[str, Any] | None,
    *,
    crm_provider: str | None = None,
    enabled_channel_sources: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Merge admin-controlled app settings without replacing unrelated preferences."""
    root = normalize_organisation_preferences(preferences)
    if crm_provider is not None:
        root["crm"] = derive_crm_settings(crm_provider)
        root["capabilities"] = derive_capabilities(root["crm"])
    if enabled_channel_sources is not None:
        root["channels"] = {"enabled": _clean_enabled_channels(enabled_channel_sources)}
    return root


def has_crm_write_scope(
    preferences: dict[str, Any] | None,
    scope: str,
) -> bool:
    """Return whether an organisation preference payload allows a write scope."""
    normalized = normalize_organisation_preferences(preferences)
    return scope in _crm_write_scopes(normalized["crm"])
