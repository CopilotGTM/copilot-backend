"""Service to handle post-connect sync setup after integration authentication.

This module owns the post-connect handoff after OAuth succeeds:
- dispatch the first sync for providers with active sync support
- record explicit placeholder state for providers whose sync is deferred

See: docs/Technical Documentation/integration/sync_tasks/initial-sync-on-auth.md

IMPORTANT: This service runs in the API context, which has different dependencies
than the workers. It uses send_task() with task names instead of importing task
modules directly to avoid pulling in worker-specific dependencies (like bs4).
"""

from __future__ import annotations

from uuid import UUID

from celery import chain
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.orchestration.hubspot_initial_sync_chain import (
    build_hubspot_initial_sync_chain,
)
from src.service.integrations.crm.orchestration.salesforce_initial_sync_chain import (
    build_salesforce_initial_sync_chain,
)
from src.workers.integrations_worker.client import (
    get_integrations_celery_client,
    send_integrations_task,
)

logger = get_logger(__name__)

MICROSOFT_WEBHOOK_INTEGRATION_TYPES = {
    IntegrationType.OUTLOOK_CALENDAR,
    IntegrationType.OUTLOOK_EMAIL,
}

GOOGLE_WEBHOOK_INTEGRATION_TYPES = {
    IntegrationType.GOOGLE_CALENDAR,
    IntegrationType.GMAIL,
}


class InitialSyncService:
    """Service for post-connect sync handoff on newly authenticated integrations.

    This service handles the integration lifecycle immediately after OAuth succeeds.
    For providers with active sync support, it dispatches the appropriate Celery
    tasks. For providers that are connect-only today, it records explicit state in
    connection metadata so the integration truthfully reflects its current status.

    It currently handles:
    - Calendar (Google, Outlook) - Direct task dispatch
    - Email (Gmail, Outlook) - Direct task dispatch
    - Messaging (Teams, Slack) - Direct task dispatch
    - HubSpot and Salesforce - Custom auth plus CRM schema initial sync
    """

    INITIAL_SYNC_STATUS_METADATA_KEY = "initial_sync_status"
    INITIAL_SYNC_PLACEHOLDER_METADATA_KEY = "initial_sync_placeholder"

    # Calendar integration types
    CALENDAR_INTEGRATION_TYPES = {IntegrationType.GOOGLE_CALENDAR, IntegrationType.OUTLOOK_CALENDAR}

    # Email integration types
    EMAIL_INTEGRATION_TYPES = {IntegrationType.GMAIL, IntegrationType.OUTLOOK_EMAIL}

    # Messaging integration types
    MESSAGING_INTEGRATION_TYPES = {IntegrationType.TEAMS, IntegrationType.SLACK}

    CRM_INITIAL_SYNC_INTEGRATION_TYPES = {
        IntegrationType.HUBSPOT,
        IntegrationType.SALESFORCE,
    }

    def apply_post_connect_state(self, integration: KnowledgeIntegration) -> None:
        """Apply provider-specific post-connect state before the integration is committed.

        HubSpot and Salesforce both queue their provider-specific schema sync.
        """
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)

        if integration_type not in self.CRM_INITIAL_SYNC_INTEGRATION_TYPES:
            return

        metadata = dict(integration.connection_metadata or {})

        if integration_type == IntegrationType.HUBSPOT:
            metadata[self.INITIAL_SYNC_STATUS_METADATA_KEY] = "queued"
            metadata.pop(self.INITIAL_SYNC_PLACEHOLDER_METADATA_KEY, None)
            schema_sync = dict(metadata.get("schema_sync") or {})
            schema_sync["deals"] = {"status": "queued"}
            metadata["schema_sync"] = schema_sync
            record_sync = dict(metadata.get("record_sync") or {})
            record_sync["pipelines"] = {"status": "queued"}
            record_sync["users"] = {"status": "queued"}
            record_sync["companies"] = {"status": "queued"}
            record_sync["contacts"] = {"status": "queued"}
            record_sync["deals"] = {"status": "queued"}
            record_sync["tasks"] = {"status": "queued"}
            record_sync["notes"] = {"status": "queued"}
            metadata["record_sync"] = record_sync
            integration.connection_metadata = metadata
            return

        if integration_type == IntegrationType.SALESFORCE:
            metadata[self.INITIAL_SYNC_STATUS_METADATA_KEY] = "queued"
            metadata.pop(self.INITIAL_SYNC_PLACEHOLDER_METADATA_KEY, None)
            schema_sync = dict(metadata.get("schema_sync") or {})
            schema_sync["opportunities"] = {"status": "queued"}
            metadata["schema_sync"] = schema_sync
            record_sync = dict(metadata.get("record_sync") or {})
            record_sync["pipelines"] = {"status": "queued"}
            record_sync["users"] = {"status": "queued"}
            record_sync["companies"] = {"status": "queued"}
            record_sync["contacts"] = {"status": "queued"}
            record_sync["deals"] = {"status": "queued"}
            record_sync["tasks"] = {"status": "queued"}
            record_sync["notes"] = {"status": "queued"}
            metadata["record_sync"] = record_sync
            integration.connection_metadata = metadata
            return

        integration.connection_metadata = metadata

    def trigger_initial_sync(
        self,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
        enabled_capabilities: list[IntegrationType] | None = None,
    ) -> None:
        """Trigger initial sync for a newly connected/reactivated integration.

        Dispatches the appropriate integration-level (Tier 3) task based on type.

        Note: This method is synchronous because all task dispatches use send_task()
        which returns immediately without blocking.

        Args:
            organisation_id: The organization UUID
            integration: The KnowledgeIntegration record
        """
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)

        org_id_str = str(organisation_id)
        int_id_str = str(integration.integration_id)
        capabilities = enabled_capabilities or []

        try:
            if integration_type in {IntegrationType.GOOGLE, IntegrationType.MICROSOFT}:
                self._trigger_provider_capability_syncs(
                    org_id_str,
                    int_id_str,
                    str(integration.connection_id or integration.integration_id),
                    capabilities,
                )
                return

            # Calendar
            if integration_type in self.CALENDAR_INTEGRATION_TYPES:
                self._trigger_calendar_sync(
                    org_id_str,
                    int_id_str,
                    str(integration.connection_id or integration.integration_id),
                )
                if integration_type in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
                    # Initial sync establishes the first delta cursor; the
                    # subscription ensures future changes wake that same sync.
                    self._trigger_microsoft_graph_subscription_ensure(
                        org_id_str,
                        int_id_str,
                    )
                if integration_type in GOOGLE_WEBHOOK_INTEGRATION_TYPES:
                    # Calendar subscriptions should be created only after the
                    # initial sync has had a chance to establish the syncToken.
                    # If the webhook fires first, the sync task still has a
                    # full-sync fallback, but this ordering keeps the hot path
                    # incremental.
                    self._trigger_google_subscription_ensure(
                        org_id_str,
                        int_id_str,
                    )
                return

            # Email
            if integration_type in self.EMAIL_INTEGRATION_TYPES:
                self._trigger_email_sync(org_id_str, int_id_str)
                if integration_type in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
                    # Email follows the same contract as calendar: first sync
                    # stores folder delta links, then webhooks trigger refreshes.
                    self._trigger_microsoft_graph_subscription_ensure(
                        org_id_str,
                        int_id_str,
                    )
                if integration_type in GOOGLE_WEBHOOK_INTEGRATION_TYPES:
                    # Gmail watches publish only a wake-up history id; the
                    # existing email sync owns cursor persistence and recovery
                    # from expired history windows.
                    self._trigger_google_subscription_ensure(
                        org_id_str,
                        int_id_str,
                    )
                return

            # Messaging (Teams)
            if integration_type in self.MESSAGING_INTEGRATION_TYPES:
                self._trigger_messaging_sync(org_id_str, int_id_str)
                return

            if integration_type == IntegrationType.HUBSPOT:
                self._trigger_hubspot_initial_sync(org_id_str, int_id_str)
                return

            if integration_type == IntegrationType.SALESFORCE:
                self._trigger_salesforce_initial_sync(org_id_str, int_id_str)
                return

            # Unknown type
            logger.warning(
                "No initial sync configured for integration type: %s (integration=%s)",
                integration_type.value,
                int_id_str,
            )

        except Exception as e:
            # Log but don't fail - sync will happen on next schedule
            logger.error(
                "Failed to trigger initial sync for integration %s: %s",
                int_id_str,
                str(e),
                exc_info=True,
            )

    def _trigger_calendar_sync(
        self,
        org_id_str: str,
        int_id_str: str,
        connection_id: str,
    ) -> None:
        """Trigger calendar initial sync."""
        client = get_integrations_celery_client()
        sync_signature = client.signature(
            "integrations.sync_calendar_integration",
            kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
            immutable=True,
        )
        discovery_signature = client.signature(
            "deal.discover_calendar_contacts_accounts",
            kwargs={
                "organisation_id": org_id_str,
                "source_integration_id": int_id_str,
                "trigger": "calendar_connection",
                "trigger_id": connection_id,
            },
            queue="deal",
            routing_key="deal",
        )
        chain(sync_signature, discovery_signature).apply_async()
        logger.info("Triggered initial calendar sync: integration=%s", int_id_str)

    def _trigger_provider_capability_syncs(
        self,
        org_id_str: str,
        int_id_str: str,
        connection_id: str,
        enabled_capabilities: list[IntegrationType],
    ) -> None:
        for capability in enabled_capabilities:
            if capability in self.CALENDAR_INTEGRATION_TYPES:
                self._trigger_calendar_sync(org_id_str, int_id_str, connection_id)
                if capability in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
                    self._trigger_microsoft_graph_subscription_ensure(org_id_str, int_id_str)
                if capability in GOOGLE_WEBHOOK_INTEGRATION_TYPES:
                    self._trigger_google_subscription_ensure(org_id_str, int_id_str)
                continue
            if capability in self.EMAIL_INTEGRATION_TYPES:
                self._trigger_email_sync(org_id_str, int_id_str)
                if capability in MICROSOFT_WEBHOOK_INTEGRATION_TYPES:
                    self._trigger_microsoft_graph_subscription_ensure(org_id_str, int_id_str)
                if capability in GOOGLE_WEBHOOK_INTEGRATION_TYPES:
                    self._trigger_google_subscription_ensure(org_id_str, int_id_str)
                continue
            if capability in self.MESSAGING_INTEGRATION_TYPES:
                self._trigger_messaging_sync(org_id_str, int_id_str)

    def _trigger_email_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger email initial sync."""
        send_integrations_task(
            task_name="integrations.sync_email_integration",
            kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
        )
        logger.info("Triggered initial email sync: integration=%s", int_id_str)

    def _trigger_messaging_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger messaging initial sync."""
        send_integrations_task(
            task_name="integrations.sync_messaging_channels_integration",
            kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
        )
        logger.info("Triggered initial messaging sync: integration=%s", int_id_str)

    def _trigger_microsoft_graph_subscription_ensure(
        self,
        org_id_str: str,
        int_id_str: str,
    ) -> None:
        """Create the Graph webhook subscription after Outlook post-connect sync dispatch."""
        send_integrations_task(
            task_name="integrations.ensure_microsoft_graph_subscription",
            kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
            queue="integrations_priority",
            routing_key="integrations_priority",
        )
        logger.info("Triggered Microsoft Graph subscription ensure: integration=%s", int_id_str)

    def _trigger_google_subscription_ensure(
        self,
        org_id_str: str,
        int_id_str: str,
    ) -> None:
        """Create the Google webhook subscription after Google post-connect sync dispatch."""
        send_integrations_task(
            task_name="integrations.ensure_google_subscription",
            kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
            queue="integrations_priority",
            routing_key="integrations_priority",
        )
        logger.info("Triggered Google subscription ensure: integration=%s", int_id_str)

    def _trigger_hubspot_initial_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Dispatch the HubSpot post-connect Celery chain.

        The chain runs: schema sync → pipelines record sync → … → finalize.
        Each link is a Tier 3 task; on any failure the chain aborts via
        link_error and `connection_metadata.initial_sync_status` is set to
        "failed" by the failure terminator. On full success the terminal
        finalize link sets it to "success".

        See `src/service/integrations/crm/orchestration/hubspot_initial_sync_chain.py`.
        """
        build_hubspot_initial_sync_chain(
            organisation_id=UUID(org_id_str),
            integration_id=UUID(int_id_str),
        ).apply_async()
        logger.info(
            "Dispatched HubSpot initial sync chain: integration=%s",
            int_id_str,
        )

    def _trigger_salesforce_initial_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Dispatch the Salesforce post-connect Celery chain."""
        build_salesforce_initial_sync_chain(
            organisation_id=UUID(org_id_str),
            integration_id=UUID(int_id_str),
        ).apply_async()
        logger.info(
            "Dispatched Salesforce initial sync chain: integration=%s",
            int_id_str,
        )


# Convenience singleton for easy import
initial_sync_service = InitialSyncService()
