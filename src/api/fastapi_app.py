"""FastAPI application factory and configuration.

This module creates and configures the FastAPI application with:
- CORS middleware
- Request context middleware
- Redis connection pool management
- Database pool monitoring
- GC tuning for reduced pause times
"""

import gc
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Configure Python garbage collector to reduce pause times
# This helps prevent health check timeouts caused by GC pauses
# - threshold0: Collect young generation less frequently (default 700)
# - threshold1: Collect middle generation less frequently (default 10)
# - threshold2: Collect old generation less frequently (default 10)
gc.set_threshold(50000, 500, 100)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scripts.monitoring.db_pool_monitor import start_pool_monitor, stop_pool_monitor
from src.api.routers.admin.admin_auth_handler import router as admin_auth_router
from src.api.routers.admin.organizations_handler import router as admin_org_router
from src.api.routers.admin.users_handler import router as admin_user_router
from src.api.routers.chat.messages_handler import router as chat_messages_router
from src.api.routers.chat.sessions_handler import router as chat_sessions_router
from src.api.routers.chat_attachments.access_handler import (
    download_router as attachment_access_router,
)
from src.api.routers.chat_attachments.upload_handler import upload_router
from src.api.routers.crm import (
    deal_notes_router,
    deal_timeline_router,
    deals_router,
    meetings_router,
    risks_blockers_router,
    stakeholder_insights_router,
    stakeholders_router,
    tasks_router,
)
from src.api.routers.healthcheck import healthcheck_router, public_health_router
from src.api.routers.integrations import integrations_router
from src.api.routers.knowledge_base.access_handler import (
    access_router as knowledge_access_router,
)
from src.api.routers.knowledge_base.upload_handler import (
    upload_router as knowledge_upload_router,
)
from src.api.routers.users.auth_handler import router as user_auth_router
from starlette.middleware.sessions import SessionMiddleware

from ..api.middleware.request_context import RequestContextMiddleware
from ..infra.messaging.redis_client import close_redis_pool, initialize_redis_pool
from ..infra.messaging.redis_streams import (
    close_async_redis_pool,
    initialize_async_redis_pool,
)
from ..infra.utils.logger import get_logger

# Secret maintenance removed; no background task required

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for FastAPI application."""
    # Startup
    logger.info("Starting application services...")

    # Initialize Redis connection pools at startup
    logger.info("Initializing Redis connection pools...")
    initialize_redis_pool()  # Sync Redis pool
    await initialize_async_redis_pool()  # Async Redis pool
    logger.info("Redis connection pools initialized successfully")

    # Start DB pool monitor if available
    if start_pool_monitor is not None:
        await start_pool_monitor()

    yield

    # Shutdown
    logger.info("Shutting down application services...")

    # Close Redis connection pools
    close_redis_pool()
    await close_async_redis_pool()
    logger.info("Redis connection pools closed")

    # Stop DB pool monitor if running
    if stop_pool_monitor is not None:
        try:
            await stop_pool_monitor()
            logger.info("DB pool monitor stopped")
        except Exception as e:
            logger.warning("Failed to stop DB pool monitor: %s", e)


def create_fastapi_app():
    """Create and configure the FastAPI application."""
    from src.infra.utils.app_state import fastapp

    logger.info("Setting Up App Config")

    # Configure the shared FastAPI app instance with lifespan handler
    # We use the shared instance from app_state so dependency_overrides work in tests
    fastapp.router.lifespan_context = lifespan

    origins = os.getenv("ALLOW_ORIGIN_URL_AUTH", "*").split(",")
    fastapp.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    fastapp.add_middleware(RequestContextMiddleware)

    APP_ENV = os.getenv("ENV", "dev")
    logger.info(f"App Environment: {APP_ENV}")

    # Add SessionMiddleware for session handling
    fastapp.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("SESSION_SECRET", "supersecretkey"),
        session_cookie="my_custom_session_cookie",
        max_age=3600,
        same_site="lax",
        https_only=APP_ENV != "local",
    )

    # App Level Variables
    fastapp.state.CLOUD_PROVIDER = os.getenv("CLOUD_PROVIDER")
    fastapp.state.APPLICATION_STORAGE_NAME = os.getenv("APPLICATION_STORAGE_NAME")
    fastapp.state.APPLICATION_REGION = os.getenv("APPLICATION_REGION")

    # Include routers here
    # Public health endpoint (safe for external access)
    fastapp.include_router(public_health_router, tags=["health"])

    # Internal health endpoints (restricted access)
    fastapp.include_router(healthcheck_router, tags=["internal", "healthcheck"])
    # fastapp.include_router(chat_router, prefix="/chats", tags=["chats"])
    fastapp.include_router(upload_router, prefix="/chats/upload", tags=["uploads"])
    fastapp.include_router(attachment_access_router, prefix="/chats", tags=["downloads"])
    # User auth
    fastapp.include_router(user_auth_router, prefix="/users/auth", tags=["user", "authentication"])

    # Knowledge base
    fastapp.include_router(
        knowledge_upload_router, prefix="/users/knowledge", tags=["knowledge_upload"]
    )
    fastapp.include_router(
        knowledge_access_router, prefix="/users/knowledge", tags=["knowledge_access"]
    )

    # Chat sessions
    fastapp.include_router(chat_sessions_router, prefix="/users/chats", tags=["chats"])
    # Chat messages
    fastapp.include_router(chat_messages_router, prefix="/users/chats", tags=["chat_messages"])

    # attachment
    fastapp.include_router(upload_router, prefix="/users/chats/upload", tags=["uploads"])
    fastapp.include_router(
        attachment_access_router, prefix="/users/chats/upload", tags=["downloads"]
    )

    # Integration routers
    fastapp.include_router(integrations_router, prefix="/users/integrations", tags=["integrations"])

    # CRM routers
    fastapp.include_router(deals_router, prefix="/users/crm", tags=["deals"])
    fastapp.include_router(stakeholders_router, prefix="/users/crm/deals", tags=["stakeholders"])
    fastapp.include_router(meetings_router, prefix="/users/crm/deals", tags=["meetings"])
    fastapp.include_router(tasks_router, prefix="/users/crm", tags=["tasks"])
    fastapp.include_router(deal_timeline_router, prefix="/users/crm/deals", tags=["timeline"])
    fastapp.include_router(deal_notes_router, prefix="/users/crm/deals", tags=["deal-notes"])
    fastapp.include_router(
        risks_blockers_router, prefix="/users/crm/deals", tags=["risks-blockers"]
    )
    fastapp.include_router(
        stakeholder_insights_router, prefix="/users/crm/deals", tags=["stakeholder-insights"]
    )

    # Admin routers
    fastapp.include_router(
        admin_auth_router, prefix="/admin/auth", tags=["admin", "authentication"]
    )
    fastapp.include_router(
        admin_org_router, prefix="/admin/organisations", tags=["admin", "organizations"]
    )
    fastapp.include_router(admin_user_router, prefix="/admin/users", tags=["admin", "users"])

    return fastapp
