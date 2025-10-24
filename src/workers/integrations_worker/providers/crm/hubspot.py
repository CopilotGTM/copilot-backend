"""HubSpot CRM fetchers via Nango proxy."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.nango_provider_mapping import to_nango_connect_integration_key
from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient

logger = get_logger(__name__)


HUBSPOT_DEAL_PROPERTIES = [
    "dealname",
    "amount",
    "dealstage",
    "pipeline",
    "dealtype",
    "deal_currency_code",
    "hubspot_owner_id",
    "hubspot_owner_email",
    "hubspot_owner_name",
    "closedate",
    "hs_closed_won_date",
    "hs_closed_lost_date",
    "hs_closed_reason",
    "closed_lost_reason",
    "hs_priority",
    "hs_forecast_category",
    "hs_forecast_amount",
    "hs_forecast_probability",
    "hs_deal_stage_probability",
    "hs_expected_close_date",
    "hs_lastmodifieddate",
    "createdate",
    "lead_source",
    "hs_lead_source",
    "description",
    "hs_arr",
    "hs_mrr",
    "hs_annual_contract_value",
]

HUBSPOT_DEAL_ASSOCIATIONS = ["contacts", "companies"]
HUBSPOT_PROPERTIES_WITH_HISTORY = ["dealstage"]


def _provider_key(provider_config_key: str | None) -> str:
    return provider_config_key or to_nango_connect_integration_key(IntegrationType.HUBSPOT)


def _format_timestamp_ms(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return str(int(value.timestamp() * 1000))


async def fetch_pipelines(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    del is_initial, last_synced_at, data_type
    provider = _provider_key(provider_config_key)
    endpoint = "/crm/v3/pipelines/deals"
    response = await nango.proxy_request(
        provider_config_key=provider,
        connection_id=connection_id,
        endpoint=endpoint,
    )
    results = response.get("results") if isinstance(response, dict) else None
    if isinstance(results, list):
        return results
    logger.warning("HubSpot pipelines response missing results: %s", response)
    return []


async def fetch_deals(
    *,
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: datetime | None,
    data_type: str | None = None,
    provider_config_key: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch deals from HubSpot CRM via Nango proxy."""
    del data_type
    provider = _provider_key(provider_config_key)
    if not is_initial and last_synced_at:
        return await _search_deals_since(
            nango=nango,
            connection_id=connection_id,
            provider=provider,
            last_synced_at=last_synced_at,
        )
    return await _list_all_deals(
        nango=nango,
        connection_id=connection_id,
        provider=provider,
    )


async def _list_all_deals(
    *,
    nango: NangoClient,
    connection_id: str,
    provider: str,
) -> list[dict[str, Any]]:
    endpoint = "/crm/v3/objects/deals"
    params = {
        "limit": 50,
        "archived": "false",
        "properties": ",".join(HUBSPOT_DEAL_PROPERTIES),
        "associations": ",".join(HUBSPOT_DEAL_ASSOCIATIONS),
        "propertiesWithHistory": ",".join(HUBSPOT_PROPERTIES_WITH_HISTORY),
    }

    deals: list[dict[str, Any]] = []
    after: str | None = None

    while True:
        if after:
            params["after"] = after
        else:
            params.pop("after", None)

        response = await nango.proxy_request(
            provider_config_key=provider,
            connection_id=connection_id,
            endpoint=endpoint,
            params=params,
        )

        page_results = response.get("results") if isinstance(response, dict) else None
        if isinstance(page_results, list):
            deals.extend(page_results)

        after = _extract_after(response)
        if not after:
            break

    return deals


async def _search_deals_since(
    *,
    nango: NangoClient,
    connection_id: str,
    provider: str,
    last_synced_at: datetime,
) -> list[dict[str, Any]]:
    endpoint = "/crm/v3/objects/deals/search"
    timestamp_ms = _format_timestamp_ms(last_synced_at)

    deals: list[dict[str, Any]] = []
    after: str | None = None

    while True:
        body: dict[str, Any] = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "GTE",
                            "value": timestamp_ms,
                        }
                    ]
                }
            ],
            "properties": HUBSPOT_DEAL_PROPERTIES,
            "propertiesWithHistory": HUBSPOT_PROPERTIES_WITH_HISTORY,
            "associations": HUBSPOT_DEAL_ASSOCIATIONS,
            "limit": 50,  # Max 50 when requesting propertiesWithHistory
            "sorts": [
                {
                    "propertyName": "hs_lastmodifieddate",
                    "direction": "ASCENDING",
                }
            ],
        }

        if after:
            body["after"] = after

        response = await nango.proxy_request(
            provider_config_key=provider,
            connection_id=connection_id,
            endpoint=endpoint,
            method="POST",
            data=body,
        )

        page_results = response.get("results") if isinstance(response, dict) else None
        if isinstance(page_results, list):
            deals.extend(page_results)

        after = _extract_after(response)
        if not after:
            break

    return deals


def _extract_after(response: Any) -> str | None:
    if isinstance(response, dict):
        paging = response.get("paging")
        if isinstance(paging, dict):
            next_token = paging.get("next")
            if isinstance(next_token, dict):
                after = next_token.get("after")
                if isinstance(after, str):
                    return after
    return None


__all__ = ["fetch_pipelines", "fetch_deals"]
