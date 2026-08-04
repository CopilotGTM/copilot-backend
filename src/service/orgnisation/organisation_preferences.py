"""Organisation preference helpers shared by admin and user-facing contracts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

CRM_PROVIDERS = {"copilotgtm", "hubspot", "salesforce"}
LEGACY_CRM_PROVIDER_ALIASES = {"copilot_gtm": "copilotgtm"}
INTERNAL_CRM_PROVIDER = "copilotgtm"
CHANNEL_SOURCES = {"slack", "whatsapp", "teams", "google_chat"}
DEFAULT_CHANNEL_SOURCES = ["slack", "whatsapp", "teams", "google_chat"]
BOT_JOIN_POLICY_ALL = "all"
BOT_JOIN_POLICY_EXTERNAL_ONLY = "external_only"
BOT_JOIN_POLICY_NEVER = "never"
BOT_JOIN_POLICIES = {
    BOT_JOIN_POLICY_ALL,
    BOT_JOIN_POLICY_EXTERNAL_ONLY,
    BOT_JOIN_POLICY_NEVER,
}
DEFAULT_BOT_JOIN_POLICY = BOT_JOIN_POLICY_EXTERNAL_ONLY
BOT_JOIN_POLICY_OPTIONS = [
    {"value": BOT_JOIN_POLICY_EXTERNAL_ONLY, "label": "External meetings only"},
    {"value": BOT_JOIN_POLICY_ALL, "label": "All meetings"},
    {"value": BOT_JOIN_POLICY_NEVER, "label": "Never join"},
]
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
    "meetings:create",
    "meetings:update",
    "pipelines:create",
    "pipelines:update",
    "pipelines:delete",
    "tasks:create",
    "tasks:update",
    "tasks:delete",
}
DEFAULT_INTERNAL_CRM_WRITE_SCOPES = sorted(CRM_WRITE_SCOPES)
GOOGLE_GMAIL_READ_ENABLED_KEY = "gmail_read_enabled"
DOMAIN_JOIN_POLICY_OPEN = "open"
DOMAIN_JOIN_POLICY_INVITE_ONLY = "invite_only"
DOMAIN_JOIN_POLICIES = {DOMAIN_JOIN_POLICY_OPEN, DOMAIN_JOIN_POLICY_INVITE_ONLY}
DEFAULT_DOMAIN_JOIN_POLICY = DOMAIN_JOIN_POLICY_OPEN


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


def _clean_bot_join_policy(value: Any) -> str:
    return (
        value if isinstance(value, str) and value in BOT_JOIN_POLICIES else DEFAULT_BOT_JOIN_POLICY
    )


def _clean_domain_join_policy(value: Any) -> str:
    return value if value in DOMAIN_JOIN_POLICIES else DEFAULT_DOMAIN_JOIN_POLICY


def normalize_organisation_preferences(preferences: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize app-owned preference namespaces while preserving unrelated keys."""
    root = dict(preferences or {})
    raw_crm = root.get("crm")
    raw_channels = root.get("channels")
    raw_integrations = root.get("integrations")
    raw_meeting_recording = root.get("meeting_recording")
    raw_membership = root.get("membership")
    crm: dict[str, Any] = raw_crm if isinstance(raw_crm, dict) else {}
    channels: dict[str, Any] = raw_channels if isinstance(raw_channels, dict) else {}
    integrations: dict[str, Any] = raw_integrations if isinstance(raw_integrations, dict) else {}
    meeting_recording: dict[str, Any] = (
        raw_meeting_recording if isinstance(raw_meeting_recording, dict) else {}
    )
    membership: dict[str, Any] = raw_membership if isinstance(raw_membership, dict) else {}
    provider = crm.get("provider") or crm.get("source")
    raw_write_policy = crm.get("write_policy")
    write_policy: dict[str, Any] = raw_write_policy if isinstance(raw_write_policy, dict) else {}
    raw_allowed_scopes = write_policy.get("allowed_scopes")
    enabled_channels = channels.get("enabled")
    if enabled_channels is None:
        enabled_channels = integrations.get("enabled_sources")

    root["crm"] = derive_crm_settings(
        str(provider) if provider else None,
        raw_allowed_scopes if isinstance(raw_allowed_scopes, list) else None,
    )
    root["channels"] = {"enabled": _clean_enabled_channels(enabled_channels)}
    root["meeting_recording"] = {
        "bot_join_policy": _clean_bot_join_policy(meeting_recording.get("bot_join_policy")),
    }
    root["membership"] = {
        "domain_join_policy": _clean_domain_join_policy(membership.get("domain_join_policy")),
    }
    root["capabilities"] = derive_capabilities(root["crm"])
    if isinstance(integrations, dict) and set(integrations) <= {"enabled_sources"}:
        root.pop("integrations", None)
    return root


def organisation_preferences_for_settings(
    preferences: dict[str, Any] | None,
) -> dict[str, Any]:
    root = normalize_organisation_preferences(preferences)
    meeting_recording = dict(root["meeting_recording"])
    meeting_recording["bot_join_policy_options"] = [
        dict(option) for option in BOT_JOIN_POLICY_OPTIONS
    ]
    root["meeting_recording"] = meeting_recording
    return root


def has_google_gmail_read_scope_enabled(preferences: dict[str, Any] | None) -> bool:
    """Return whether this organisation explicitly opts into the Gmail read OAuth scope."""
    root = preferences if isinstance(preferences, dict) else {}
    raw_integrations = root.get("integrations")
    integrations: dict[str, Any] = raw_integrations if isinstance(raw_integrations, dict) else {}
    raw_google = integrations.get("google")
    google: dict[str, Any] = raw_google if isinstance(raw_google, dict) else {}
    return google.get(GOOGLE_GMAIL_READ_ENABLED_KEY) is True


def merge_organisation_app_settings(
    preferences: dict[str, Any] | None,
    *,
    crm_provider: str | None = None,
    enabled_channel_sources: Iterable[Any] | None = None,
    google_gmail_read_enabled: bool | None = None,
    meeting_recording_bot_join_policy: str | None = None,
    membership_domain_join_policy: str | None = None,
) -> dict[str, Any]:
    """Merge admin-controlled app settings without replacing unrelated preferences."""
    root = normalize_organisation_preferences(preferences)
    if crm_provider is not None:
        root["crm"] = derive_crm_settings(crm_provider)
        root["capabilities"] = derive_capabilities(root["crm"])
    if enabled_channel_sources is not None:
        root["channels"] = {"enabled": _clean_enabled_channels(enabled_channel_sources)}
    if google_gmail_read_enabled is not None:
        integrations = dict(root.get("integrations") or {})
        google = dict(integrations.get("google") or {})
        google[GOOGLE_GMAIL_READ_ENABLED_KEY] = google_gmail_read_enabled
        integrations["google"] = google
        root["integrations"] = integrations
    if meeting_recording_bot_join_policy is not None:
        root["meeting_recording"] = {
            "bot_join_policy": _clean_bot_join_policy(meeting_recording_bot_join_policy),
        }
    if membership_domain_join_policy is not None:
        root["membership"] = {
            "domain_join_policy": _clean_domain_join_policy(membership_domain_join_policy),
        }
    return root


def has_crm_write_scope(
    preferences: dict[str, Any] | None,
    scope: str,
) -> bool:
    """Return whether an organisation preference payload allows a write scope."""
    normalized = normalize_organisation_preferences(preferences)
    return scope in _crm_write_scopes(normalized["crm"])
