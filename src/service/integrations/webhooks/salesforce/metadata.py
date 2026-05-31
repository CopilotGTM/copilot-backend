from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration

OPPORTUNITY_CDC_CHANNEL = "/data/OpportunityChangeEvent"
_DEFAULT_PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:443"


class SalesforceCdcMetadataError(ValueError):
    """Salesforce integration metadata does not support CDC subscription."""


@dataclass(slots=True, frozen=True)
class SalesforceOpportunityCdcMetadata:
    salesforce_organization_id: str
    instance_url: str
    channel: str
    pubsub_endpoint: str


def get_salesforce_opportunity_cdc_metadata(
    integration: KnowledgeIntegration,
) -> SalesforceOpportunityCdcMetadata:
    integration_type = integration.integration_type
    if isinstance(integration_type, str):
        integration_type = IntegrationType(integration_type)
    if integration_type != IntegrationType.SALESFORCE:
        raise SalesforceCdcMetadataError("Integration is not Salesforce")

    metadata = dict(integration.connection_metadata or {})
    cdc_metadata = dict(metadata.get("salesforce_cdc") or {})
    opportunity_metadata = dict(cdc_metadata.get("opportunity") or {})
    if opportunity_metadata.get("enabled") is not True:
        raise SalesforceCdcMetadataError("Opportunity CDC is not enabled")

    salesforce_organization_id = str(metadata.get("salesforce_organization_id") or "").strip()
    if not salesforce_organization_id:
        raise SalesforceCdcMetadataError("Salesforce organization ID is missing")

    credentials = dict(integration.credentials or {})
    instance_url = str(
        credentials.get("instance_url") or metadata.get("salesforce_instance_url") or ""
    ).rstrip("/")
    instance_url = _validate_instance_url(instance_url)

    channel = str(opportunity_metadata.get("channel") or OPPORTUNITY_CDC_CHANNEL)
    if channel != OPPORTUNITY_CDC_CHANNEL:
        raise SalesforceCdcMetadataError(f"Unsupported Salesforce CDC channel: {channel}")

    pubsub_endpoint = str(
        opportunity_metadata.get("pubsub_endpoint")
        or metadata.get("salesforce_pubsub_endpoint")
        or _DEFAULT_PUBSUB_ENDPOINT
    ).strip()
    pubsub_endpoint = _validate_pubsub_endpoint(pubsub_endpoint)

    return SalesforceOpportunityCdcMetadata(
        salesforce_organization_id=salesforce_organization_id,
        instance_url=instance_url,
        channel=channel,
        pubsub_endpoint=pubsub_endpoint,
    )


def _validate_pubsub_endpoint(pubsub_endpoint: str) -> str:
    if not pubsub_endpoint:
        raise SalesforceCdcMetadataError("Salesforce Pub/Sub endpoint is missing")

    parsed = urlparse(f"//{pubsub_endpoint}")
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise SalesforceCdcMetadataError("Unsupported Salesforce Pub/Sub endpoint") from exc
    if parsed.scheme or parsed.username or parsed.password or parsed.path not in ("", "/"):
        raise SalesforceCdcMetadataError("Unsupported Salesforce Pub/Sub endpoint")
    if host != "api.pubsub.salesforce.com" or port not in (443, 7443):
        raise SalesforceCdcMetadataError("Unsupported Salesforce Pub/Sub endpoint")
    return f"{host}:{port or 443}"


def _validate_instance_url(instance_url: str) -> str:
    if not instance_url:
        raise SalesforceCdcMetadataError("Salesforce instance URL is missing")

    parsed = urlparse(instance_url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise SalesforceCdcMetadataError("Unsupported Salesforce instance URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise SalesforceCdcMetadataError("Unsupported Salesforce instance URL")
    if not host.endswith(".salesforce.com") or port not in (None, 443):
        raise SalesforceCdcMetadataError("Unsupported Salesforce instance URL")
    return f"https://{host}" if port is None else f"https://{host}:443"
