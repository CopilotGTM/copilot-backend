from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration

OPPORTUNITY_CDC_CHANNEL = "/data/OpportunityChangeEvent"
ACCOUNT_CDC_CHANNEL = "/data/AccountChangeEvent"
CONTACT_CDC_CHANNEL = "/data/ContactChangeEvent"
USER_CDC_CHANNEL = "/data/UserChangeEvent"
TASK_CDC_CHANNEL = "/data/TaskChangeEvent"
CONTENT_NOTE_CDC_CHANNEL = "/data/ContentNoteChangeEvent"
CONTENT_DOCUMENT_LINK_CDC_CHANNEL = "/data/ContentDocumentLinkChangeEvent"
_DEFAULT_PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:443"

SALESFORCE_CDC_OBJECTS: dict[str, dict[str, str]] = {
    "opportunity": {
        "channel": OPPORTUNITY_CDC_CHANNEL,
        "object_name": "Opportunity",
        "id_prefix": "006",
    },
    "account": {"channel": ACCOUNT_CDC_CHANNEL, "object_name": "Account", "id_prefix": "001"},
    "contact": {"channel": CONTACT_CDC_CHANNEL, "object_name": "Contact", "id_prefix": "003"},
    "user": {"channel": USER_CDC_CHANNEL, "object_name": "User", "id_prefix": "005"},
    "task": {"channel": TASK_CDC_CHANNEL, "object_name": "Task", "id_prefix": "00T"},
    "content_note": {
        "channel": CONTENT_NOTE_CDC_CHANNEL,
        "object_name": "ContentNote",
        "id_prefix": "069",
    },
    "content_document_link": {
        "channel": CONTENT_DOCUMENT_LINK_CDC_CHANNEL,
        "object_name": "ContentDocumentLink",
        "id_prefix": "06A",
    },
}


class SalesforceCdcMetadataError(ValueError):
    """Salesforce integration metadata does not support CDC subscription."""


@dataclass(slots=True, frozen=True)
class SalesforceOpportunityCdcMetadata:
    object_key: str
    object_name: str
    id_prefix: str
    salesforce_organization_id: str
    instance_url: str
    channel: str
    pubsub_endpoint: str


def get_salesforce_opportunity_cdc_metadata(
    integration: KnowledgeIntegration,
) -> SalesforceOpportunityCdcMetadata:
    return get_salesforce_cdc_metadata(integration, object_key="opportunity")


def get_enabled_salesforce_cdc_metadata(
    integration: KnowledgeIntegration,
) -> list[SalesforceOpportunityCdcMetadata]:
    return [
        get_salesforce_cdc_metadata(integration, object_key=object_key)
        for object_key in SALESFORCE_CDC_OBJECTS
        if _object_metadata(integration, object_key).get("enabled") is True
    ]


def get_salesforce_cdc_metadata(
    integration: KnowledgeIntegration,
    *,
    object_key: str,
) -> SalesforceOpportunityCdcMetadata:
    integration_type = integration.integration_type
    if isinstance(integration_type, str):
        integration_type = IntegrationType(integration_type)
    if integration_type != IntegrationType.SALESFORCE:
        raise SalesforceCdcMetadataError("Integration is not Salesforce")
    config = SALESFORCE_CDC_OBJECTS.get(object_key)
    if config is None:
        raise SalesforceCdcMetadataError(f"Unsupported Salesforce CDC object: {object_key}")

    metadata = dict(integration.connection_metadata or {})
    object_metadata = _object_metadata(integration, object_key)
    if object_metadata.get("enabled") is not True:
        raise SalesforceCdcMetadataError(f"{config['object_name']} CDC is not enabled")

    salesforce_organization_id = str(metadata.get("salesforce_organization_id") or "").strip()
    if not salesforce_organization_id:
        raise SalesforceCdcMetadataError("Salesforce organization ID is missing")

    credentials = dict(integration.credentials or {})
    instance_url = str(
        credentials.get("instance_url") or metadata.get("salesforce_instance_url") or ""
    ).rstrip("/")
    instance_url = _validate_instance_url(instance_url)

    channel = str(object_metadata.get("channel") or config["channel"])
    if channel != config["channel"]:
        raise SalesforceCdcMetadataError(f"Unsupported Salesforce CDC channel: {channel}")

    pubsub_endpoint = str(
        object_metadata.get("pubsub_endpoint")
        or metadata.get("salesforce_pubsub_endpoint")
        or _DEFAULT_PUBSUB_ENDPOINT
    ).strip()
    pubsub_endpoint = _validate_pubsub_endpoint(pubsub_endpoint)

    return SalesforceOpportunityCdcMetadata(
        object_key=object_key,
        object_name=config["object_name"],
        id_prefix=config["id_prefix"],
        salesforce_organization_id=salesforce_organization_id,
        instance_url=instance_url,
        channel=channel,
        pubsub_endpoint=pubsub_endpoint,
    )


def get_salesforce_cdc_metadata_for_channel(
    integration: KnowledgeIntegration,
    *,
    channel: str,
) -> SalesforceOpportunityCdcMetadata:
    for object_key, config in SALESFORCE_CDC_OBJECTS.items():
        if channel == config["channel"]:
            return get_salesforce_cdc_metadata(integration, object_key=object_key)
    raise SalesforceCdcMetadataError(f"Unsupported Salesforce CDC channel: {channel}")


def channel_metadata_key(channel: str) -> str:
    for object_key, config in SALESFORCE_CDC_OBJECTS.items():
        if channel == config["channel"]:
            return object_key
    raise ValueError(f"Unsupported Salesforce CDC channel: {channel}")


def _object_metadata(
    integration: KnowledgeIntegration,
    object_key: str,
) -> dict[str, object]:
    metadata = dict(integration.connection_metadata or {})
    cdc_metadata = dict(metadata.get("salesforce_cdc") or {})
    return dict(cdc_metadata.get(object_key) or {})


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
