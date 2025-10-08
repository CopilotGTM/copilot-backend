"""
Integrations Worker

This worker handles integration-related background tasks including:
- OAuth token refresh (scheduled and on-demand)
- Integration data synchronization (coming in Step 5)

The worker uses two queues:
- integrations: Normal priority tasks (scheduled refreshes, batch syncs)
- integrations_priority: High priority tasks (manual operations, immediate refreshes)
"""

from .celery_app import app
from .config import IntegrationsWorkerConfig

__all__ = [
    "app",
    "IntegrationsWorkerConfig",
]
