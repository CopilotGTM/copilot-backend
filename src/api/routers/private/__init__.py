"""Private API routers for server-to-server integrations.

Expose endpoint groups that are not user-facing (for example lead ingestion) so
the app factory can mount them under protected `/private` routes.
"""

from src.api.routers.crm.leads import private_lead_ingestion_router

__all__ = ["private_lead_ingestion_router"]
