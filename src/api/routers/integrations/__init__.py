"""
Integration API routers.

This package contains all API endpoints for integration management:
- Custom OAuth flow (authorization, callbacks, disconnect)
- Integration CRUD operations
- Deal integration management
"""

from fastapi import APIRouter

from .copilot_oauth_routes import router as copilot_oauth_router
from .deal_integration_routes import router as deal_integration_router
from .integration_routes import router as integration_router

# Main router that combines all integration routes
integrations_router = APIRouter(tags=["integrations"])

# Include sub-routers
integrations_router.include_router(copilot_oauth_router)
integrations_router.include_router(integration_router)
integrations_router.include_router(deal_integration_router)

__all__ = ["integrations_router", "copilot_oauth_router", "integration_router"]
