"""Service to trigger initial sync after integration authentication.

This module handles dispatching the first sync for newly connected integrations.
Called by nango_webhook_service after successful auth.

See: docs/Technical Documentation/integration/sync_tasks/initial-sync-on-auth.md
"""

from __future__ import annotations

from uuid import UUID

from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class InitialSyncService:
    """Service for triggering initial sync on newly authenticated integrations.

    This service dispatches the appropriate Celery tasks when a user connects
    a new integration via OAuth. It handles all integration types:
    - CRM (HubSpot, Salesforce) - Uses chord pattern for parallel sync
    - Calendar (Google, Outlook) - Direct task dispatch
    - Email (Gmail, Outlook) - Direct task dispatch
    - Messaging (Teams) - Direct task dispatch
    """

    # CRM integration types that use chord pattern for parallel sync
    CRM_INTEGRATION_TYPES = {IntegrationType.HUBSPOT, IntegrationType.SALESFORCE}

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
        For CRM, uses Celery chord for parallel entity sync.

        Note: This method is synchronous because all task dispatches use .delay()
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
            # CRM - uses chord pattern
            if integration_type in self.CRM_INTEGRATION_TYPES:
                self._trigger_crm_sync(organisation_id, integration)
                return

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
        from src.workers.integrations_worker.tasks.calendar import (
            sync_calendar_integration_task,
        )

        sync_calendar_integration_task.delay(  # type: ignore[attr-defined]
            organisation_id=org_id_str,
            integration_id=int_id_str,
        )
        logger.info("Triggered initial calendar sync: integration=%s", int_id_str)

    def _trigger_email_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger email initial sync."""
        from src.workers.integrations_worker.tasks.email import (
            sync_email_integration_task,
        )

        sync_email_integration_task.delay(  # type: ignore[attr-defined]
            organisation_id=org_id_str,
            integration_id=int_id_str,
        )
        logger.info("Triggered initial email sync: integration=%s", int_id_str)

    def _trigger_messaging_sync(self, org_id_str: str, int_id_str: str) -> None:
        """Trigger messaging initial sync."""
        from src.workers.integrations_worker.tasks.messaging import (
            sync_messaging_channels_integration_task,
        )

        sync_messaging_channels_integration_task.delay(  # type: ignore[attr-defined]
            organisation_id=org_id_str,
            integration_id=int_id_str,
        )
        logger.info("Triggered initial messaging sync: integration=%s", int_id_str)

    def _trigger_crm_sync(self, organisation_id: UUID, integration: KnowledgeIntegration) -> None:
        """Trigger CRM initial sync using Celery chord for parallel execution.

        Dispatches all 6 CRM entity sync tasks in parallel, with enrichment
        running as a callback after all syncs complete.

        Args:
            organisation_id: The organization UUID
            integration: The KnowledgeIntegration record
        """
        integration_type = integration.integration_type
        if isinstance(integration_type, str):
            integration_type = IntegrationType(integration_type)

        logger.info(
            "Triggering CRM initial sync: integration=%s org=%s type=%s",
            integration.integration_id,
            organisation_id,
            integration_type.value,
        )

        from celery import chord, group
        from src.workers.integrations_worker.tasks.crm import (
            enrich_crm_after_initial_sync_task,
            sync_crm_activities_org_task,
            sync_crm_companies_org_task,
            sync_crm_contacts_org_task,
            sync_crm_deals_org_task,
            sync_crm_pipelines_org_task,
            sync_crm_users_org_task,
        )

        org_id_str = str(organisation_id)
        int_id_str = str(integration.integration_id)

        try:
            # Run all sync tasks in parallel, then enrichment
            sync_group = group(
                sync_crm_companies_org_task.s(org_id_str, skip_enrichment=True),  # type: ignore[attr-defined]
                sync_crm_contacts_org_task.s(org_id_str, skip_enrichment=True),  # type: ignore[attr-defined]
                sync_crm_deals_org_task.s(org_id_str, skip_enrichment=True),  # type: ignore[attr-defined]
                sync_crm_users_org_task.s(org_id_str, skip_enrichment=True),  # type: ignore[attr-defined]
                sync_crm_pipelines_org_task.s(org_id_str),  # type: ignore[attr-defined]
                sync_crm_activities_org_task.s(org_id_str, skip_enrichment=True),  # type: ignore[attr-defined]
            )

            chord(sync_group)(
                enrich_crm_after_initial_sync_task.si(org_id_str, int_id_str)  # type: ignore[attr-defined]
            )

            logger.info(
                "Dispatched CRM sync chord: integration=%s (6 tasks + enrichment)",
                integration.integration_id,
            )
        except Exception as e:
            logger.error("Failed to dispatch CRM sync chord: %s", str(e), exc_info=True)


# Convenience singleton for easy import
initial_sync_service = InitialSyncService()
