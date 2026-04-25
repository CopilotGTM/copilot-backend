"""Policy for deciding which HubSpot deal fields should be exposed in our product.

This policy is intentionally stricter than raw schema sync:
- canonical fields are always kept
- explicitly ignored fields are dropped
- hidden or read-only provider fields are not shown
- provider-noise fields are suppressed even if editable
- only a tiny allowlist of provider-backed custom fields is exposed
"""

from __future__ import annotations

from dataclasses import dataclass

from src.service.integrations.crm.schema.hubspot_deal_mapping import (
    canonical_column_for,
    is_hubspot_deal_ignored,
)

_SUPPRESSED_FIELDS: frozenset[str] = frozenset(
    {
        "hs_all_assigned_business_unit_ids",
        "hs_all_collaborator_owner_ids",
        "hs_attributed_team_ids",
        "hs_campaign",
        "hs_cross_account_note",
        "hs_cross_sell_opportunity",
        "hs_deal_amount_calculation_preference",
        "hs_exchange_rate",
        "hs_forecast_probability",
        "hs_is_active_shared_deal",
        "hs_manual_forecast_category",
        "hs_net_pipeline_impact",
        "hs_owning_teams",
        "hs_partner_tech_win",
        "hs_pinned_engagement_id",
        "hs_shared_team_ids",
        "hs_shared_user_ids",
    }
)

_SUPPRESSED_PREFIXES: tuple[str, ...] = (
    "hs_analytics_",
    "hs_line_item_",
    "hs_v2_",
    "hs_object_source",
)

_SUPPRESSED_GROUPS: frozenset[str] = frozenset(
    {
        "analyticsinformation",
        "predictive_deal_score_feature_properties",
        "dealscripted",
        "hubspotmetrics",
        "multiaccountmanagement",
        "dealstages",
    }
)

_EXPOSED_CUSTOM_FIELDS: dict[str, str] = {
    "hs_next_step": "next_step",
}


@dataclass(slots=True, frozen=True)
class HubSpotDealFieldMetadata:
    """Metadata captured from HubSpot field payload for policy decisioning."""

    provider_field_key: str
    hidden: bool
    read_only_value: bool
    group_name: str | None = None
    label: str | None = None
    current_internal_key: str | None = None
    is_provider_custom: bool | None = None


@dataclass(slots=True, frozen=True)
class HubSpotDealFieldDecision:
    """Outcome used by UI/deal exposure policy."""

    category: str
    internal_name: str | None = None
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class HubSpotDealValueStorageFilterOptions:
    """Policy knobs for how aggressively to filter fields before custom storage."""

    exclude_canonical: bool = True
    exclude_ignored: bool = True
    exclude_hidden: bool = True
    exclude_read_only: bool = True
    exclude_provider_noise: bool = True


@dataclass(slots=True, frozen=True)
class HubSpotDealValueStorageDecision:
    """Outcome used by deal-level custom property storage policy."""

    keep: bool
    category: str
    internal_name: str | None = None
    reason: str | None = None


def _is_provider_noise(field: HubSpotDealFieldMetadata) -> bool:
    """Detect provider-specific fields that should never surface as product-level data."""

    return (
        field.provider_field_key in _SUPPRESSED_FIELDS
        or any(field.provider_field_key.startswith(prefix) for prefix in _SUPPRESSED_PREFIXES)
        or (field.group_name in _SUPPRESSED_GROUPS if field.group_name else False)
    )


def _suggest_internal_name(field: HubSpotDealFieldMetadata) -> str:
    """Reuse a stable internal key if already known, otherwise normalize the provider key."""

    allowlisted = _EXPOSED_CUSTOM_FIELDS.get(field.provider_field_key)
    if allowlisted is not None:
        return allowlisted

    current_key = (field.current_internal_key or "").strip()
    if current_key:
        return current_key

    provider_key = field.provider_field_key
    if provider_key.startswith("hs_"):
        return provider_key.removeprefix("hs_")
    return provider_key


def classify_hubspot_deal_field(
    field: HubSpotDealFieldMetadata,
) -> HubSpotDealFieldDecision:
    """Classify a HubSpot deal field for UI exposure decisions."""

    canonical_column = canonical_column_for(field.provider_field_key)
    if canonical_column is not None:
        return HubSpotDealFieldDecision(
            category="canonical",
            internal_name=canonical_column,
            reason="Mapped to canonical CRMDeal column.",
        )

    if is_hubspot_deal_ignored(field.provider_field_key):
        return HubSpotDealFieldDecision(
            category="ignored",
            reason="Explicitly ignored from canonical and custom exposure.",
        )

    if field.hidden:
        return HubSpotDealFieldDecision(
            category="suppressed_hidden",
            reason="Hidden in HubSpot metadata; keep out of product UI.",
        )

    if field.read_only_value:
        return HubSpotDealFieldDecision(
            category="suppressed_read_only",
            reason="HubSpot marks the value as read-only.",
        )

    if _is_provider_noise(field):
        return HubSpotDealFieldDecision(
            category="suppressed_provider_noise",
            reason="Provider-specific operational/analytics field not meant for deal UI.",
        )

    internal_name = _EXPOSED_CUSTOM_FIELDS.get(field.provider_field_key)
    if internal_name is not None:
        return HubSpotDealFieldDecision(
            category="exposed_custom",
            internal_name=internal_name,
            reason="Explicitly allowlisted for deal UI exposure.",
        )

    return HubSpotDealFieldDecision(
        category="suppressed_unapproved",
        reason="Editable and visible in provider, but not approved for product exposure.",
    )


def classify_hubspot_deal_field_for_value_storage(
    field: HubSpotDealFieldMetadata,
    options: HubSpotDealValueStorageFilterOptions | None = None,
) -> HubSpotDealValueStorageDecision:
    """Return whether a provider field should survive into deal custom-value storage.

    This is intentionally more mechanical than the product exposure policy:
    stage 1 strips out fields we definitely do not want to store as editable
    deal-level custom values, then stage 2 distinguishes globally-approved
    survivors from org-specific review candidates.
    """

    options = options or HubSpotDealValueStorageFilterOptions()

    canonical_column = canonical_column_for(field.provider_field_key)
    if canonical_column is not None and options.exclude_canonical:
        return HubSpotDealValueStorageDecision(
            keep=False,
            category="filtered_canonical",
            internal_name=canonical_column,
            reason="Already stored on CRMDeal as a canonical column.",
        )

    if is_hubspot_deal_ignored(field.provider_field_key) and options.exclude_ignored:
        return HubSpotDealValueStorageDecision(
            keep=False,
            category="filtered_ignored",
            reason="Explicitly ignored from deal storage.",
        )

    if field.is_provider_custom is True:
        return HubSpotDealValueStorageDecision(
            keep=True,
            category="provider_custom",
            internal_name=_suggest_internal_name(field),
            reason="HubSpot custom field should be stored in deal custom properties.",
        )

    if field.hidden and options.exclude_hidden:
        return HubSpotDealValueStorageDecision(
            keep=False,
            category="filtered_hidden",
            reason="Hidden in HubSpot metadata.",
        )

    if field.read_only_value and options.exclude_read_only:
        return HubSpotDealValueStorageDecision(
            keep=False,
            category="filtered_read_only",
            reason="Read-only in HubSpot; not suitable for editable custom storage.",
        )

    if _is_provider_noise(field) and options.exclude_provider_noise:
        return HubSpotDealValueStorageDecision(
            keep=False,
            category="filtered_provider_noise",
            reason="Provider-specific operational/analytics field.",
        )

    internal_name = _suggest_internal_name(field)
    if field.provider_field_key in _EXPOSED_CUSTOM_FIELDS:
        return HubSpotDealValueStorageDecision(
            keep=True,
            category="global_default_custom",
            internal_name=internal_name,
            reason="Global HubSpot allowlist candidate for deal custom storage.",
        )

    return HubSpotDealValueStorageDecision(
        keep=True,
        category="review_custom",
        internal_name=internal_name,
        reason="Survives stage-1 checks and needs product review before global rollout.",
    )
