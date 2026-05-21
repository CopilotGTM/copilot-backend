"""HubSpot webhook subscription table.

This file is the provider-owned map from HubSpot subscription names to handler
methods. Adding a new HubSpot webhook type should happen here first, then the
route and dispatcher automatically pick it up through the shared registry.
"""

from __future__ import annotations

from src.service.integrations.webhooks.common.registry import WebhookSubscription
from src.service.integrations.webhooks.hubspot.companies.handler import (
    HubSpotCompanyWebhookHandler,
)
from src.service.integrations.webhooks.hubspot.companies.subscriptions import (
    COMPANY_CREATION,
    COMPANY_DELETION,
    COMPANY_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.contacts.handler import (
    HubSpotContactWebhookHandler,
)
from src.service.integrations.webhooks.hubspot.contacts.privacy_deletion import (
    HubSpotContactPrivacyDeletionHandler,
)
from src.service.integrations.webhooks.hubspot.contacts.subscriptions import (
    CONTACT_CREATION,
    CONTACT_DELETION,
    CONTACT_PRIVACY_DELETION,
    CONTACT_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.deals.handler import HubSpotDealWebhookHandler
from src.service.integrations.webhooks.hubspot.deals.subscriptions import (
    DEAL_CREATION,
    DEAL_DELETION,
    DEAL_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.notes.handler import HubSpotNoteWebhookHandler
from src.service.integrations.webhooks.hubspot.notes.subscriptions import (
    NOTE_ASSOCIATION_CHANGE,
    NOTE_CREATION,
    NOTE_DELETION,
    NOTE_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.tasks.handler import HubSpotTaskWebhookHandler
from src.service.integrations.webhooks.hubspot.tasks.subscriptions import (
    TASK_ASSOCIATION_CHANGE,
    TASK_CREATION,
    TASK_DELETION,
    TASK_PROPERTY_CHANGE,
)
from src.service.integrations.webhooks.hubspot.types import HubSpotWebhookEvent

HUBSPOT_WEBHOOK_SUBSCRIPTIONS: tuple[WebhookSubscription[HubSpotWebhookEvent], ...] = (
    # Contact privacy deletion is the legacy HubSpot webhook path; deal events
    # use the same dispatch table so the route stays subscription-agnostic.
    WebhookSubscription(
        subscription_type=CONTACT_PRIVACY_DELETION,
        handler=HubSpotContactPrivacyDeletionHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=CONTACT_CREATION,
        handler=HubSpotContactWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=CONTACT_PROPERTY_CHANGE,
        handler=HubSpotContactWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=CONTACT_DELETION,
        handler=HubSpotContactWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=DEAL_CREATION,
        handler=HubSpotDealWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=DEAL_PROPERTY_CHANGE,
        handler=HubSpotDealWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=DEAL_DELETION,
        handler=HubSpotDealWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=COMPANY_CREATION,
        handler=HubSpotCompanyWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=COMPANY_PROPERTY_CHANGE,
        handler=HubSpotCompanyWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=COMPANY_DELETION,
        handler=HubSpotCompanyWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=NOTE_CREATION,
        handler=HubSpotNoteWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=NOTE_PROPERTY_CHANGE,
        handler=HubSpotNoteWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=NOTE_ASSOCIATION_CHANGE,
        handler=HubSpotNoteWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=NOTE_DELETION,
        handler=HubSpotNoteWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=TASK_CREATION,
        handler=HubSpotTaskWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=TASK_PROPERTY_CHANGE,
        handler=HubSpotTaskWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=TASK_ASSOCIATION_CHANGE,
        handler=HubSpotTaskWebhookHandler.handle,
    ),
    WebhookSubscription(
        subscription_type=TASK_DELETION,
        handler=HubSpotTaskWebhookHandler.handle,
    ),
)
