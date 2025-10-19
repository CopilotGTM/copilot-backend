"""
Celery tasks for integration token refresh operations.

These tasks handle automatic token refresh to maintain
continuous OAuth connectivity for integrations.
"""

from celery import Task

from src.infra.database.factory import get_sync_provider
from src.infra.utils.logger import get_logger
from src.service.integrations.token_refresh_service import refresh_all_expiring_tokens
from src.workers.integrations_worker.celery_app import app

logger = get_logger(__name__)


class TokenRefreshTask(Task):
    """
    Base task class for token refresh operations.

    Handles database session management and error handling.
    """

    def __call__(self, *args, **kwargs):
        """Execute task with proper session management."""
        return self.run(*args, **kwargs)


@app.task(
    bind=True,
    base=TokenRefreshTask,
    name="integrations.refresh_expiring_tokens",
    max_retries=3,
    default_retry_delay=300,  # 5 minutes
)
def refresh_expiring_tokens_task(self, buffer_hours: int = 2):
    """
    Scheduled task to refresh expiring OAuth tokens.

    This task runs hourly via Celery beat to proactively refresh
    tokens before they expire.

    Args:
        buffer_hours: Check for tokens expiring within this many hours (default: 2)

    Returns:
        Dict with refresh results summary
    """
    logger.info(f"Starting scheduled token refresh task (buffer: {buffer_hours}h)")

    db_provider = get_sync_provider()

    try:
        # Get master session to iterate through organizations
        with db_provider.master_session() as master_session:
            # Query all organizations
            from src.domain.models.master_tables.organisation import Organisation

            organisations = (
                master_session.query(Organisation).filter(Organisation.status == "ACTIVE").all()
            )

            logger.info(f"Processing {len(organisations)} active organizations")

            total_summary = {
                "organizations_processed": 0,
                "total_integrations_checked": 0,
                "total_successful_refreshes": 0,
                "total_failed_refreshes": 0,
                "total_deactivated": 0,
            }

            # Process each organization
            for org in organisations:
                try:
                    org_id = org.organisation_id
                    logger.info(f"Processing integrations for org {org_id}")

                    # Get tenant session for this org
                    with db_provider.tenant_session(org_id) as tenant_session:
                        # Run refresh for this org
                        import asyncio

                        summary = asyncio.run(
                            refresh_all_expiring_tokens(
                                db_session=tenant_session, buffer_hours=buffer_hours
                            )
                        )

                        # Aggregate results
                        total_summary["organizations_processed"] += 1
                        total_summary["total_integrations_checked"] += summary["total_checked"]
                        total_summary["total_successful_refreshes"] += summary[
                            "successful_refreshes"
                        ]
                        total_summary["total_failed_refreshes"] += summary["failed_refreshes"]
                        total_summary["total_deactivated"] += summary["deactivated"]

                        logger.info(
                            f"Org {org_id}: {summary['successful_refreshes']} successful, "
                            f"{summary['failed_refreshes']} failed"
                        )

                except Exception as e:
                    logger.error(
                        f"Failed to process integrations for org {org.organisation_id}: {str(e)}",
                        exc_info=True,
                    )
                    # Continue with next org
                    continue

            logger.info(
                f"Token refresh task complete: {total_summary['organizations_processed']} orgs, "
                f"{total_summary['total_integrations_checked']} integrations checked, "
                f"{total_summary['total_successful_refreshes']} refreshed, "
                f"{total_summary['total_failed_refreshes']} failed"
            )

            return total_summary

    except Exception as e:
        logger.error(f"Token refresh task failed: {str(e)}", exc_info=True)

        # Retry on failure
        try:
            raise self.retry(exc=e)
        except self.MaxRetriesExceededError:
            logger.error("Max retries exceeded for token refresh task")
            return {
                "error": str(e),
                "organizations_processed": 0,
                "total_integrations_checked": 0,
                "total_successful_refreshes": 0,
                "total_failed_refreshes": 0,
                "total_deactivated": 0,
            }


@app.task(
    bind=True,
    name="integrations.refresh_single_integration",
    max_retries=3,
    default_retry_delay=60,  # 1 minute
)
def refresh_single_integration_task(self, organisation_id: str, integration_id: str):
    """
    Manually trigger token refresh for a specific integration.

    Can be called from API endpoint or on-demand.

    Args:
        organisation_id: Organization ID
        integration_id: Integration ID to refresh

    Returns:
        Dict with refresh result
    """
    from uuid import UUID

    logger.info(
        f"Starting manual token refresh for integration {integration_id} in org {organisation_id}"
    )

    db_provider = get_sync_provider()

    try:
        org_uuid = UUID(organisation_id)
        int_uuid = UUID(integration_id)

        with db_provider.tenant_session(org_uuid) as session:
            from src.service.integrations import NangoClient, TokenRefreshService

            nango = NangoClient()
            service = TokenRefreshService(db_session=session, nango_client=nango)

            try:
                # Get the integration
                from sqlalchemy import and_, select

                from src.domain.models.organisation_tables.sync import KnowledgeIntegration

                stmt = select(KnowledgeIntegration).where(
                    and_(
                        KnowledgeIntegration.integration_id == int_uuid,
                        KnowledgeIntegration.organisation_id == org_uuid,
                    )
                )

                result = session.execute(stmt)
                integration = result.scalar_one_or_none()

                if not integration:
                    logger.warning(f"Integration {integration_id} not found")
                    return {
                        "success": False,
                        "error": "Integration not found",
                        "integration_id": integration_id,
                    }

                # Refresh the token
                import asyncio

                refresh_result = asyncio.run(service.refresh_integration_token(integration))

                logger.info(
                    f"Manual token refresh for integration {integration_id}: "
                    f"{'success' if refresh_result['success'] else 'failed'}"
                )

                return refresh_result

            finally:
                import asyncio

                asyncio.run(nango.close())

    except Exception as e:
        logger.error(
            f"Failed to manually refresh integration {integration_id}: {str(e)}",
            exc_info=True,
        )

        # Retry on failure
        try:
            raise self.retry(exc=e)
        except self.MaxRetriesExceededError:
            logger.error(f"Max retries exceeded for integration {integration_id}")
            return {
                "success": False,
                "error": str(e),
                "integration_id": integration_id,
            }
