# app_layer/utils/fastapi_app.py

import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from socketio import ASGIApp

from ..api.events.socket_app import sio
from ..infra.utils.logger import get_logger
from ..infra.messaging.redis_client import get_redis_client

from src.api.routers.healthcheck import healthcheck_router, public_health_router
from src.api.routers.chat_handler import chat_router
from src.api.routers.chat.sessions_handler import router as chat_sessions_router
from src.api.routers.chat.messages_handler import router as chat_messages_router
from src.api.routers.chat_attachments.upload_handler import upload_router
from src.api.routers.chat_attachments.access_handler import download_router as attachment_access_router
from src.api.routers.knowledge_base.upload_handler import upload_router as knowledge_upload_router
from src.api.routers.knowledge_base.access_handler import access_router as knowledge_access_router
# from src.api.routers.technical_deck.transcript_format import transcript_router
# from src.api.routers.technical_deck.get_technical_deck import technical_deck
# from src.api.routers.technical_deck.select_template import select_template
# from src.api.routers.rfp_sheet.sheet_identification import rfp__sheet_router
from src.api.routers.admin.organizations_handler import router as admin_org_router
from src.api.routers.admin.admin_auth_handler import router as admin_auth_router
from src.api.routers.admin.users_handler import router as admin_user_router
from src.api.routers.users.auth_handler import router as user_auth_router

# Import secret maintenance system
from src.infra.secrets.maintenance import maintenance_manager

# from src.api.events import chat_message_handler
# from src.api.events import connect_handler
# from src.api.events.technical_deck import transcript_summary_handler
# from src.api.events.technical_deck import slide_outline_generation
# from src.api.events.technical_deck import technical_deck_generation
# from src.api.events.rfp_sheet import rfp_answer_generation
from src.api.events import redis_subscriber

logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for FastAPI application."""
    # Startup
    logger.info("Starting application services...")
    
    # Start secret maintenance manager
    try:
        await maintenance_manager.start()
        logger.info("Secret maintenance manager started successfully")
    except Exception as e:
        logger.error(f"Failed to start secret maintenance manager: {e}")
        # Don't fail startup for this, but log the error
    
    yield
    
    # Shutdown
    logger.info("Shutting down application services...")
    
    # Stop secret maintenance manager
    try:
        await maintenance_manager.stop()
        logger.info("Secret maintenance manager stopped successfully")
    except Exception as e:
        logger.error(f"Error stopping secret maintenance manager: {e}")


def create_fastapi_app():
    """Create and configure the FastAPI application."""
    
    logger.info("Setting Up App Config")
    
    # Create FastAPI app with lifespan handler
    fastapp = FastAPI(lifespan=lifespan)

    origins = os.getenv("ALLOW_ORIGIN_URL_AUTH", "*").split(",")
    fastapp.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    # Initialize Redis client in app state
    fastapp.state.redis_client = get_redis_client()

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
    fastapp.include_router(attachment_access_router, prefix="/chats", tags=["downloads"])  # /chats/attachment/{id}/download
    # fastapp.include_router(transcript_router, prefix="/chats/transcript", tags=["transcript"])
    # fastapp.include_router(technical_deck, prefix="/chats/technical_deck", tags=["technical_deck"])
    # fastapp.include_router(select_template, prefix="/chats/technical_deck", tags=["technical_deck", "select_template"])
    # fastapp.include_router(rfp__sheet_router, prefix="/chats/rfp_sheet", tags=["rfp sheet", "sheet identification"])

    # User auth
    fastapp.include_router(user_auth_router, prefix="/users/auth", tags=["user", "authentication"])

    # Knowledge base
    fastapp.include_router(knowledge_upload_router, prefix="/users/knowledge", tags=["knowledge_upload"])
    fastapp.include_router(knowledge_access_router, prefix="/users/knowledge", tags=["knowledge_access"])

    # Chat sessions
    fastapp.include_router(chat_sessions_router, prefix="/users/chats", tags=["chats"])
    # Chat messages
    fastapp.include_router(chat_messages_router, prefix="/users/chats", tags=["chat_messages"])

    # attachment
    fastapp.include_router(upload_router, prefix="/users/chats/upload", tags=["uploads"])
    fastapp.include_router(attachment_access_router, prefix="/users/chats/upload", tags=["downloads"])

    # Admin routers
    fastapp.include_router(admin_auth_router, prefix="/admin/auth", tags=["admin", "authentication"])
    fastapp.include_router(admin_org_router, prefix="/admin/organisations", tags=["admin", "organizations"])
    fastapp.include_router(admin_user_router, prefix="/admin/users", tags=["admin", "users"])

    # Wrap in Socket.IO
    return ASGIApp(sio, other_asgi_app=fastapp)
