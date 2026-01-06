"""
Integrations Worker

This worker handles integration-related background tasks including:
- OAuth token refresh (scheduled and on-demand)
- Integration data synchronization (coming in Step 5)

The worker uses two queues:
- integrations: Normal priority tasks (scheduled refreshes, batch syncs)
- integrations_priority: High priority tasks (manual operations, immediate refreshes)

Note: The celery app and config are NOT imported at module level to avoid
circular imports when other modules import from providers subpackage.
Import directly if needed:
    from src.workers.integrations_worker.celery_app import app
    from src.workers.integrations_worker.config import IntegrationsWorkerConfig
"""

# Lazy imports to avoid circular dependencies
# The celery_app imports tasks which may import services that import providers

__all__ = [
    "app",
    "IntegrationsWorkerConfig",
]


def __getattr__(name: str):
    """Lazy import to avoid circular dependencies."""
    if name == "app":
        from .celery_app import app

        return app
    if name == "IntegrationsWorkerConfig":
        from .config import IntegrationsWorkerConfig

        return IntegrationsWorkerConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
