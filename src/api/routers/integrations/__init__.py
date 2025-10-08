"""
Integration API routers.

This package contains all API endpoints for integration management:
- OAuth flow (authorization and callbacks)
- Integration CRUD operations
- Sync operations (will be added in next step)
"""

from fastapi import APIRouter
from .oauth_routes import router as oauth_router
from .integration_routes import router as integration_router

# Main router that combines all integration routes
integrations_router = APIRouter(tags=["integrations"])

# Include sub-routers
integrations_router.include_router(oauth_router)
integrations_router.include_router(integration_router)

__all__ = ["integrations_router", "oauth_router", "integration_router"]
