"""FastAPI application factory and global app wiring.

This module builds the shared API app object, wires lifecycle startup/shutdown,
and mounts all routers for public, private, user, and admin entrypoints.
"""

import gc
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from src.infra.observability.otel import async_init_otel, instrument_fastapi_app
from src.infra.observability.sentry import init_sentry
from src.infra.utils.logger import configure_logging

# Tune Python GC to reduce long pauses during request bursts.
# Stable p95 latency depends on avoiding full old-generation pauses during load spikes.
gc.set_threshold(50000, 500, 100)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
if os.getenv("ENV", "local") == "local":
    load_dotenv(BASE_DIR / ".env", override=False)

configure_logging(component="api")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.api.routers.admin.admin_auth_handler import router as admin_auth_router
from src.api.routers.admin.organizations_handler import router as admin_org_router
from src.api.routers.admin.users_handler import router as admin_user_router
from src.api.routers.bots import bots_router
from src.api.routers.chat.messages_handler import router as chat_messages_router
from src.api.routers.chat.sessions_handler import router as chat_sessions_router
from src.api.routers.chat.voice_realtime_handler import router as voice_realtime_router
from src.api.routers.chat_attachments.access_handler import (
    download_router as attachment_access_router,
)
from src.api.routers.chat_attachments.upload_handler import upload_router
from src.api.routers.crm import (
    accounts_router,
    contacts_router,
    crm_users_router,
    custom_fields_router,
    deal_assets_router,
    deal_import_router,
    deal_memory_router,
    deal_notes_router,
    deal_participants_router,
    deal_timeline_router,
    deals_router,
    lead_forms_router,
    lead_ingestion_keys_router,
    leads_router,
    meetings_router,
    pipelines_router,
    public_lead_forms_router,
    risks_blockers_router,
    settings_router,
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
from src.api.routers.meeting_bot import meeting_bot_router
from src.api.routers.private import private_lead_ingestion_router
from src.api.routers.reminders import reminders_router
from src.api.routers.users.auth_handler import router as user_auth_router
from src.api.routers.users.realtime_handler import router as user_realtime_router
from src.infra.config.configs import app_config, security_secrets_config
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
EXTERNAL_API_VERSION = "v1"

# Suppress uvicorn's plain-text access log — replaced by our structured middleware
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _voice_realtime_api_enabled() -> bool:
    """Read the deployment-controlled flag for exposing voice realtime routes."""
    return app_config.voice_realtime_api_enabled


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

    await security_secrets_config.preload_required(
        include_voice_realtime=_voice_realtime_api_enabled()
    )
    logger.info("Security signing secrets initialized")

    # Load Supabase credentials from Key Vault (or env vars for local dev)
    from src.infra.auth.supabase_admin_client import supabase_admin_client
    from src.infra.auth.supabase_user_client import supabase_user_client
    from src.infra.security.token_revocation import token_revocation_service

    await supabase_admin_client.initialize()
    await supabase_user_client.initialize()
    await token_revocation_service.initialize()
    logger.info("Supabase clients initialized")

    # Supabase auth uses an HTTP/2 httpx transport; create it before observability
    # integrations instrument httpx, otherwise auth requests can fail at framing time.
    init_sentry()
    logger.info("Sentry initialized")

    # Load OTel auth headers from Key Vault and initialize tracing
    await async_init_otel(service_name_suffix="-api")
    logger.info("OpenTelemetry initialized")

    yield

    # Shutdown
    logger.info("Shutting down application services...")

    # Close Redis connection pools
    close_redis_pool()
    await close_async_redis_pool()
    logger.info("Redis connection pools closed")


def create_fastapi_app():
    """Create and configure the FastAPI application."""
    from src.infra.utils.app_state import fastapp

    logger.info("Setting Up App Config")

    # Configure the shared FastAPI app instance with lifespan handler
    # We use the shared instance from app_state so dependency_overrides work in tests
    fastapp.router.lifespan_context = lifespan

    APP_ENV = os.getenv("ENV", "dev")
    configured_origins = os.getenv("ALLOW_ORIGIN_URL_AUTH", "")
    origins = [origin.strip() for origin in configured_origins.split(",") if origin.strip()]
    if not origins:
        if APP_ENV == "local":
            origins = list(app_config.BACKEND_CORS_ORIGINS)
        else:
            raise ValueError("ALLOW_ORIGIN_URL_AUTH must be set to explicit origins outside local")
    if "*" in origins:
        raise ValueError("Wildcard CORS origins are not allowed when credentials are enabled")

    fastapp.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    fastapp.add_middleware(RequestContextMiddleware)

    logger.info(f"App Environment: {APP_ENV}")

    # Add SessionMiddleware for session handling
    session_secret = security_secrets_config.get_session_secret_sync()
    fastapp.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
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
    fastapp.include_router(
        public_lead_forms_router,
        prefix=f"/public/{EXTERNAL_API_VERSION}",
        tags=["public-lead-forms"],
    )
    fastapp.include_router(
        private_lead_ingestion_router,
        prefix=f"/private/{EXTERNAL_API_VERSION}",
        tags=["private-lead-ingestion"],
    )

    # Internal health endpoints (restricted access)
    fastapp.include_router(healthcheck_router, tags=["internal", "healthcheck"])
    # fastapp.include_router(chat_router, prefix="/chats", tags=["chats"])
    fastapp.include_router(upload_router, prefix="/chats/upload", tags=["uploads"])
    fastapp.include_router(attachment_access_router, prefix="/chats", tags=["downloads"])
    # User auth
    fastapp.include_router(user_auth_router, prefix="/users/auth", tags=["user", "authentication"])
    voice_realtime_api_enabled = _voice_realtime_api_enabled()
    logger.info(
        "Voice realtime API routes %s",
        "enabled" if voice_realtime_api_enabled else "disabled",
        extra={
            "event": "voice_realtime_api_routes_configured",
            "voice_realtime_api_enabled": voice_realtime_api_enabled,
        },
    )
    if voice_realtime_api_enabled:
        fastapp.include_router(
            user_realtime_router, prefix="/users/realtime", tags=["user", "realtime"]
        )

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
    # Voice realtime chat persistence
    if voice_realtime_api_enabled:
        fastapp.include_router(
            voice_realtime_router,
            prefix="/users/chats",
            tags=["voice_realtime"],
        )

    # attachment
    fastapp.include_router(upload_router, prefix="/users/chats/upload", tags=["uploads"])
    fastapp.include_router(
        attachment_access_router, prefix="/users/chats/upload", tags=["downloads"]
    )

    # Reminders
    fastapp.include_router(reminders_router, prefix="/users/reminders", tags=["reminders"])

    # Integration routers
    fastapp.include_router(integrations_router, prefix="/users/integrations", tags=["integrations"])

    # CRM routers
    fastapp.include_router(crm_users_router, prefix="/users/crm", tags=["crm-users"])
    fastapp.include_router(settings_router, prefix="/users/crm", tags=["crm-settings"])
    fastapp.include_router(custom_fields_router, prefix="/users/crm", tags=["crm-custom-fields"])
    fastapp.include_router(pipelines_router, prefix="/users/crm", tags=["pipelines"])
    fastapp.include_router(deals_router, prefix="/users/crm", tags=["deals"])
    fastapp.include_router(lead_forms_router, prefix="/users/crm", tags=["lead-forms"])
    fastapp.include_router(
        lead_ingestion_keys_router,
        prefix="/users/crm",
        tags=["lead-ingestion-keys"],
    )
    fastapp.include_router(leads_router, prefix="/users/crm", tags=["crm-leads"])
    fastapp.include_router(deal_participants_router, prefix="/users/crm", tags=["participants"])
    fastapp.include_router(deal_import_router, prefix="/users/crm/deals", tags=["deals"])
    fastapp.include_router(stakeholders_router, prefix="/users/crm/deals", tags=["stakeholders"])
    fastapp.include_router(meetings_router, prefix="/users/crm/deals", tags=["meetings"])
    fastapp.include_router(tasks_router, prefix="/users/crm", tags=["tasks"])
    fastapp.include_router(deal_timeline_router, prefix="/users/crm/deals", tags=["timeline"])
    fastapp.include_router(deal_memory_router, prefix="/users/crm/deals", tags=["deal-memory"])
    fastapp.include_router(deal_notes_router, prefix="/users/crm/deals", tags=["deal-notes"])
    fastapp.include_router(deal_assets_router, prefix="/users/crm/deals", tags=["deal-assets"])
    fastapp.include_router(
        risks_blockers_router, prefix="/users/crm/deals", tags=["risks-blockers"]
    )
    fastapp.include_router(
        stakeholder_insights_router, prefix="/users/crm/deals", tags=["stakeholder-insights"]
    )
    fastapp.include_router(contacts_router, prefix="/users/crm", tags=["crm-contacts"])
    fastapp.include_router(accounts_router, prefix="/users/crm", tags=["crm-accounts"])

    # Admin routers
    fastapp.include_router(
        admin_auth_router, prefix="/admin/auth", tags=["admin", "authentication"]
    )
    fastapp.include_router(
        admin_org_router, prefix="/admin/organisations", tags=["admin", "organizations"]
    )
    fastapp.include_router(admin_user_router, prefix="/admin/users", tags=["admin", "users"])

    # Bot integrations (Teams, Slack, Zoom - no auth, uses provider-specific validation)
    # Endpoints:
    #   - POST /users/bots/auth/callback - Complete identity linking
    #   - POST /users/bots/teams/messages - Teams webhook
    #   - GET  /users/bots/teams/health - Teams health check
    fastapp.include_router(bots_router, prefix="/users/bots", tags=["bots"])
    # Meeting bot webhooks stay outside `/users/bots` because these calls come
    # from recording providers, not from the user-facing identity-linking flows.
    #   - POST /users/meeting-bot/recall/webhook - Recall meeting webhook
    fastapp.include_router(
        meeting_bot_router,
        prefix="/users/meeting-bot",
        tags=["meeting-bot"],
    )

    instrument_fastapi_app(fastapp)

    return fastapp
