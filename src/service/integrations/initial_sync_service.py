"""Service to trigger initial sync after integration authentication.

This module handles dispatching the first sync for newly connected integrations.
Called by nango_webhook_service after successful auth.

See: docs/Technical Documentation/integration/sync_tasks/initial-sync-on-auth.md

IMPORTANT: This service runs in the API context, which has different dependencies
than the workers. It uses send_task() with task names instead of importing task
modules directly to avoid pulling in worker-specific dependencies (like bs4).
"""

from __future__ import annotations

from uuid import UUID

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.client import send_integrations_task

logger = get_logger(__name__)


class InitialSyncService:
    """Service for triggering initial sync on newly authenticated integrations.

    This service dispatches the appropriate Celery tasks when a user connects
    a new integration via OAuth. It handles all integration types:
    - Calendar (Google, Outlook) - Direct task dispatch
    - Email (Gmail, Outlook) - Direct task dispatch
    - Messaging (Teams) - Direct task dispatch

    Note: CRM sync (HubSpot, Salesforce) deprecated — code moved to deprecated/workers/.
    """

    # Calendar integration types
    CALENDAR_INTEGRATION_TYPES = {IntegrationType.GOOGLE_CALENDAR, IntegrationType.OUTLOOK_CALENDAR}

    # Email integration types
    EMAIL_INTEGRATION_TYPES = {IntegrationType.GMAIL, IntegrationType.OUTLOOK_EMAIL}

    # Messaging integration types
    MESSAGING_INTEGRATION_TYPES = {IntegrationType.TEAMS}

    def trigger_initial_sync(
        self,
        organisation_id: UUID,
        integration: KnowledgeIntegration,
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

        try:
            # Calendar
            if integration_type in self.CALENDAR_INTEGRATION_TYPES:
                self._trigger_calendar_sync(org_id_str, int_id_str)
                return

            # Email
            if integration_type in self.EMAIL_INTEGRATION_TYPES:
                self._trigger_email_sync(org_id_str, int_id_str)
                return

            # Messaging (Teams)
            if integration_type in self.MESSAGING_INTEGRATION_TYPES:
                self._trigger_messaging_sync(org_id_str, int_id_str)
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

    def _trigger_calendar_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger calendar initial sync."""
        send_integrations_task(
            task_name="integrations.sync_calendar_integration",
            kwargs={"organisation_id": org_id_str, "integration_id": int_id_str},
        )
        logger.info("Triggered initial calendar sync: integration=%s", int_id_str)

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

    # _trigger_crm_sync removed — CRM sync (HubSpot, Salesforce) deprecated.
    # Code preserved in deprecated/workers/ for future one-time migration.


# Convenience singleton for easy import
initial_sync_service = InitialSyncService()
